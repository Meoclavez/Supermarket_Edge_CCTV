"""Evidence storage limit: the one place loss-prevention evidence is aged out.

This device does not record continuous video (the store's NAS does). What it
keeps is evidence for an alert: a still per detection and, where enabled, a
short clip. Every kind of evidence lives in its own directory under
``STORAGE_DIR`` on this device's own disk:

====================  ======================================  =================
kind                  directory                               written by
====================  ======================================  =================
theft_evidence        THEFT_EVIDENCE_DIR or theft_evidence/   pose_analytics
zone_alerts           ZONE_ALERT_EVIDENCE_DIR or zone_alerts/ tripwire_engine
night_watch           NIGHT_WATCH_EVIDENCE_DIR or night_watch night_watch
alert_snapshots       SNAPSHOTS_DIR                           events / cameras
alert_clips           CLIPS_DIR                               clip_recorder
====================  ======================================  =================

Limits, applied oldest file first across all kinds together:

* ``STORAGE_RETENTION_DAYS``: a file older than this is deleted (0 = no age limit).
* ``NIGHT_WATCH_EVIDENCE_MAX_MB``: sub-cap for night watch alone.
* ``EVIDENCE_MAX_GB``: total for all kinds. 0 = automatic, 10 % of the disk
  holding ``STORAGE_DIR``, at least 1 GB and at most 50 GB.
* ``STORAGE_MAX_DISK_PERCENT``: evidence is also trimmed so that the disk does
  not pass this share (the database and the system need the room).

When a file goes, the database rows that pointed at it lose the link and get
``evidence_expired_at``, so the dashboard says "evidence expired" instead of
showing a broken image.

Safety: only regular ``.jpg``/``.jpeg``/``.mp4`` files directly inside a kind's
directory are ever deleted. A directory that resolves outside ``STORAGE_DIR``
is reported and left alone, symlinks are never followed or deleted, and if
``STORAGE_DIR`` sits on a network filesystem (NFS, SMB/CIFS, SSHFS, ...) the
manager refuses to run at all and says so in the health report: it must never
delete anything on the store NAS. ``dvr/``, ``archives/`` and ``recordings/``
(older continuous-recording and export directories) are measured for the
report but never deleted from automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("edge.evidence_storage")

GB = 1024 ** 3
MB = 1024 ** 2
EVIDENCE_SUFFIXES = (".jpg", ".jpeg", ".mp4")
AUTO_CAP_FRACTION = 0.10
AUTO_CAP_MIN_BYTES = 1 * GB
AUTO_CAP_MAX_BYTES = 50 * GB

# Filesystem types that mean "somewhere else on the network".
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "fuse.sshfs", "sshfs",
    "fuse.rclone", "davfs", "fuse.davfs2", "glusterfs", "fuse.glusterfs", "ceph",
    "fuse.ceph", "afs", "9p", "fuse.s3fs", "fuse.gcsfuse",
})

# Media directories that are reported but never cleaned automatically.
UNMANAGED_DIRS = ("dvr", "archives", "recordings")


# --------------------------------------------------------------------------- #
# Mount check
# --------------------------------------------------------------------------- #

def _unescape_mount(field: str) -> str:
    """/proc/mounts writes space, tab, newline and backslash as octal escapes."""
    return (field.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def mount_for(path: Path, mounts_file: str = "/proc/mounts") -> Optional[Tuple[str, str, str]]:
    """(mount point, fstype, source) of the mount holding ``path``, or None if unreadable."""
    try:
        target = Path(os.path.realpath(path))
        with open(mounts_file, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    best: Optional[Tuple[str, str, str]] = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        src, mnt, fstype = _unescape_mount(parts[0]), _unescape_mount(parts[1]), parts[2]
        mp = Path(mnt)
        if target == mp or mp in target.parents:
            # Later lines win on ties: the last mount on a point is the visible one.
            if best is None or len(mnt) >= len(best[0]):
                best = (mnt, fstype, src)
    return best


def network_mount_problem(path: Path, mounts_file: str = "/proc/mounts") -> Optional[str]:
    """A sentence saying ``path`` is on a network filesystem, or None if it is local."""
    m = mount_for(path, mounts_file)
    if m is None:
        return None
    mnt, fstype, src = m
    if fstype.lower() in NETWORK_FSTYPES or fstype.lower().startswith("nfs"):
        return (f"{path} is on a network filesystem ({fstype} {src} mounted at {mnt}); "
                "evidence must stay on this device's own disk and the NAS is never cleaned up")
    return None


# --------------------------------------------------------------------------- #
# Kinds
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EvidenceKind:
    name: str
    label: str
    directory: Callable[[], Path]
    link: str                       # "theft": theft_incidents.id; "event": security_events.id
    sub_cap_bytes: Callable[[], Optional[int]] = lambda: None


def _dir_or(value: str, default: str) -> Path:
    return Path(value) if value else Path(settings.STORAGE_DIR) / default


def _night_watch_cap() -> Optional[int]:
    mb = float(settings.NIGHT_WATCH_EVIDENCE_MAX_MB or 0)
    return int(mb * MB) if mb > 0 else None


KINDS: Tuple[EvidenceKind, ...] = (
    EvidenceKind("theft_evidence", "Theft review stills",
                 lambda: _dir_or(settings.THEFT_EVIDENCE_DIR, "theft_evidence"), "theft"),
    EvidenceKind("zone_alerts", "Restricted area / tripwire stills",
                 lambda: _dir_or(settings.ZONE_ALERT_EVIDENCE_DIR, "zone_alerts"), "event"),
    EvidenceKind("night_watch", "Night watch stills and clips",
                 lambda: _dir_or(settings.NIGHT_WATCH_EVIDENCE_DIR, "night_watch"), "event",
                 _night_watch_cap),
    EvidenceKind("alert_snapshots", "Alert and manual snapshots",
                 lambda: Path(settings.SNAPSHOTS_DIR), "event"),
    EvidenceKind("alert_clips", "Alert and manual clips",
                 lambda: Path(settings.CLIPS_DIR), "event"),
)
KINDS_BY_NAME = {k.name: k for k in KINDS}


@dataclass
class _File:
    mtime: float
    size: int
    path: Path
    kind: str


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class EvidenceStorage:
    def __init__(self, mounts_file: str = "/proc/mounts") -> None:
        self.mounts_file = mounts_file
        self._lock = threading.Lock()          # one enforcement pass at a time
        self._state_lock = threading.Lock()
        self._rerun = False
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None  # created in start()
        self.last_report: Dict[str, Any] = {}
        self.stats = {"runs": 0, "deleted_files": 0, "deleted_bytes": 0, "errors": 0,
                      "last_run_at": None, "last_cleanup_at": None, "last_deleted": 0,
                      "last_error": None, "refused": None}
        self._used_bytes: Optional[int] = None  # last scan + writes since
        self._kind_bytes: Dict[str, int] = {}
        self._effective_cap: Optional[int] = None

    # ------------------------------------------------------------ config

    @staticmethod
    def root() -> Path:
        return Path(os.path.realpath(settings.STORAGE_DIR))

    def cap(self) -> Tuple[int, str]:
        """(configured total cap in bytes, where it came from)."""
        gb = float(getattr(settings, "EVIDENCE_MAX_GB", 0) or 0)
        if gb > 0:
            return int(gb * GB), f"EVIDENCE_MAX_GB={gb:g}"
        try:
            total = shutil.disk_usage(str(self.root())).total
        except OSError:
            return AUTO_CAP_MIN_BYTES, "automatic (disk size unreadable: 1 GB)"
        auto = int(min(AUTO_CAP_MAX_BYTES, max(AUTO_CAP_MIN_BYTES, total * AUTO_CAP_FRACTION)))
        return auto, "automatic: 10% of the disk, 1-50 GB"

    def kind_dir(self, kind: EvidenceKind) -> Tuple[Path, Optional[str]]:
        """(resolved directory, reason it is not managed or None)."""
        root = self.root()
        raw = kind.directory()
        d = Path(os.path.realpath(raw))
        if d == root or root not in d.parents:
            return d, f"{raw} resolves to {d}, outside STORAGE_DIR {root}; not managed"
        problem = network_mount_problem(d, self.mounts_file)
        if problem:
            return d, f"{problem}; not managed"
        return d, None

    # ------------------------------------------------------------ scanning

    def _scan_kind(self, kind: EvidenceKind, d: Path) -> List[_File]:
        out: List[_File] = []
        try:
            it = os.scandir(d)
        except FileNotFoundError:
            return out
        except OSError as e:
            logger.warning(f"cannot list {d}: {e}")
            return out
        with it:
            for entry in it:
                name = entry.name
                if not name.lower().endswith(EVIDENCE_SUFFIXES) or ".temp." in name:
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                out.append(_File(st.st_mtime, st.st_size, Path(entry.path), kind.name))
        return out

    @staticmethod
    def _dir_usage(d: Path, limit: int = 200_000) -> Dict[str, Any]:
        """Bytes and files under an unmanaged directory (no symlinks followed)."""
        size = files = 0
        oldest: Optional[float] = None
        if not d.is_dir() or d.is_symlink():
            return {"path": str(d), "bytes": 0, "files": 0, "oldest": None}
        for base, dirs, names in os.walk(d, followlinks=False):
            for n in names:
                try:
                    st = os.lstat(os.path.join(base, n))
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                size += st.st_size
                files += 1
                oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
                if files >= limit:
                    return {"path": str(d), "bytes": size, "files": files, "oldest": _iso(oldest),
                            "truncated": True}
        return {"path": str(d), "bytes": size, "files": files, "oldest": _iso(oldest)}

    # ------------------------------------------------------------ deleting

    def _safe_unlink(self, f: _File, kind_dir: Path) -> bool:
        """Delete one regular file directly inside ``kind_dir`` (itself under STORAGE_DIR)."""
        root = self.root()
        p = f.path
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode):          # symlink, directory, device: never
            logger.error(f"refusing to delete {p}: not a regular file")
            return False
        parent = Path(os.path.realpath(p.parent))
        if parent != kind_dir or (root not in parent.parents):
            logger.error(f"refusing to delete {p}: outside {kind_dir} / STORAGE_DIR {root}")
            return False
        try:
            os.unlink(p)
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning(f"could not delete old evidence {p}: {e}")
            return False

    def _mark_expired(self, deleted: List[_File]) -> int:
        """Point every row that referenced a deleted file at 'evidence expired'."""
        if not deleted:
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")  # naive UTC, like the other columns
        theft_img, theft_clip, ev_img, ev_clip = set(), set(), set(), set()
        for f in deleted:
            stem = f.path.name.split(".", 1)[0]
            clip = f.path.suffix.lower() == ".mp4"
            if KINDS_BY_NAME[f.kind].link == "theft":
                (theft_clip if clip else theft_img).add(stem)
            else:
                (ev_clip if clip else ev_img).add(stem)
        db = Path(settings.DATABASE_PATH)
        if not db.exists():
            return 0
        changed = 0
        try:
            conn = sqlite3.connect(str(db), timeout=15.0)
        except sqlite3.Error as e:
            logger.error(f"evidence expiry not recorded (database unavailable): {e}")
            return 0
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                    for t in ("theft_incidents", "security_events")}

            def run(table: str, set_sql: str, where_col: str, keys) -> None:
                nonlocal changed
                keys = list(keys)
                if not keys or not cols.get(table):
                    return
                sets = set_sql
                if "evidence_expired_at" in cols[table]:
                    sets += ", evidence_expired_at = COALESCE(evidence_expired_at, ?)"
                    extra = [now]
                else:
                    extra = []
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    q = f"UPDATE {table} SET {sets} WHERE {where_col} IN ({','.join('?' * len(chunk))})"
                    changed += conn.execute(q, extra + chunk).rowcount

            run("theft_incidents", "evidence_snapshot_url = NULL", "id", theft_img)
            run("theft_incidents", "evidence_clip_url = NULL", "id", theft_clip)
            # The alert log rows of theft incidents link the incident's evidence route.
            run("security_events", "snapshot_url = NULL", "snapshot_url",
                [f"/api/v1/theft/incidents/{s}/evidence" for s in theft_img])
            run("security_events", "snapshot_url = NULL", "id", ev_img)
            run("security_events", "clip_url = NULL", "id", ev_clip)
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"evidence expiry not recorded in the database: {e}")
        finally:
            conn.close()
        return changed

    # ------------------------------------------------------------ enforcement

    def _refusal(self) -> Optional[str]:
        root = self.root()
        problem = network_mount_problem(root, self.mounts_file)
        if problem:
            return f"STORAGE_DIR {problem}"
        return None

    def _refuse(self, why: str) -> Dict[str, Any]:
        from app.services.resilience import ServiceHealthTracker

        if self.stats.get("refused") != why:
            logger.error(f"Evidence storage limit NOT enforced: {why}")
        self.stats["refused"] = why
        ServiceHealthTracker.report_status("evidence_storage", ServiceHealthTracker.FAILED, why)
        return {"status": "refused", "error": why, "deleted": 0}

    def enforce(self, now: Optional[float] = None) -> Dict[str, Any]:
        """One full pass: retention, sub-caps, total cap, disk limit. Returns the report."""
        with self._state_lock:
            self._rerun = False
        with self._lock:
            return self._enforce(now)

    def _enforce(self, now: Optional[float]) -> Dict[str, Any]:
        from app.services.resilience import ServiceHealthTracker

        now = time.time() if now is None else now
        self.stats["runs"] += 1
        self.stats["last_run_at"] = now
        why = self._refusal()
        if why:
            report = self._refuse(why)
            self.last_report = {**self._base_report(), **report}
            return report
        self.stats["refused"] = None

        dirs: Dict[str, Tuple[Path, Optional[str]]] = {k.name: self.kind_dir(k) for k in KINDS}
        files: List[_File] = []
        for k in KINDS:
            d, reason = dirs[k.name]
            if reason is None:
                files.extend(self._scan_kind(k, d))
        files.sort(key=lambda f: (f.mtime, str(f.path)))

        doomed: Dict[Path, Tuple[_File, str]] = {}
        # 1. Age.
        days = int(settings.STORAGE_RETENTION_DAYS or 0)
        if days > 0:
            cutoff = now - days * 86400
            for f in files:
                if f.mtime < cutoff:
                    doomed[f.path] = (f, "retention")
        # 2. Per-kind sub-caps.
        for k in KINDS:
            sub = k.sub_cap_bytes()
            if sub is None:
                continue
            mine = [f for f in files if f.kind == k.name and f.path not in doomed]
            total = sum(f.size for f in mine)
            for f in mine:
                if total <= sub:
                    break
                doomed[f.path] = (f, f"{k.name} cap")
                total -= f.size
        # 3. Total cap, bounded by the disk limit.
        cap, cap_source = self.cap()
        remaining = [f for f in files if f.path not in doomed]
        used = sum(f.size for f in remaining)
        effective, limited_by = cap, "cap"
        disk: Dict[str, Any] = {}
        try:
            du = shutil.disk_usage(str(self.root()))
            disk = {"total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
                    "used_percent": round(du.used / du.total * 100, 1) if du.total else None}
            pct = float(settings.STORAGE_MAX_DISK_PERCENT or 0)
            if 0 < pct < 100 and du.total:
                pending = sum(f.size for f, _ in doomed.values())
                headroom = du.total * pct / 100.0 - (du.used - pending)
                bound = int(max(0, used + headroom))
                if bound < effective:
                    effective, limited_by = bound, f"disk above STORAGE_MAX_DISK_PERCENT={pct:g}"
        except OSError:
            pass
        for f in remaining:
            if used <= effective:
                break
            doomed[f.path] = (f, "total cap" if limited_by == "cap" else "disk limit")
            used -= f.size

        deleted: List[_File] = []
        reasons: Dict[str, int] = {}
        for f, reason in sorted(doomed.values(), key=lambda x: (x[0].mtime, str(x[0].path))):
            if self._safe_unlink(f, dirs[f.kind][0]):
                deleted.append(f)
                reasons[reason] = reasons.get(reason, 0) + 1
        freed = sum(f.size for f in deleted)
        rows = self._mark_expired(deleted)
        if deleted:
            self.stats["deleted_files"] += len(deleted)
            self.stats["deleted_bytes"] += freed
            self.stats["last_cleanup_at"] = now
            detail = ", ".join(f"{n} for {r}" for r, n in reasons.items())
            msg = (f"Evidence storage: deleted {len(deleted)} oldest file(s), {freed / MB:.1f} MB "
                   f"({detail}); {rows} record(s) marked evidence expired")
            (logger.warning if limited_by != "cap" else logger.info)(
                msg + (f"; limited by {limited_by}" if limited_by != "cap" else ""))
        self.stats["last_deleted"] = len(deleted)

        gone = {f.path for f in deleted}
        kept = [f for f in files if f.path not in gone]
        by_kind: Dict[str, Any] = {}
        for k in KINDS:
            d, reason = dirs[k.name]
            mine = [f for f in kept if f.kind == k.name]
            by_kind[k.name] = {
                "label": k.label, "path": str(d), "managed": reason is None, "note": reason,
                "bytes": sum(f.size for f in mine), "files": len(mine),
                "oldest": _iso(min((f.mtime for f in mine), default=None)),
                "sub_cap_bytes": k.sub_cap_bytes(),
            }
        root = self.root()
        unmanaged = {n: self._dir_usage(root / n) for n in UNMANAGED_DIRS}
        total_bytes = sum(f.size for f in kept)
        with self._state_lock:
            self._used_bytes = total_bytes
            self._kind_bytes = {k: v["bytes"] for k, v in by_kind.items()}
            self._effective_cap = effective
        report = {
            **self._base_report(),
            "status": "over_limit" if total_bytes > effective else "ok",
            "error": None,
            "cap_bytes": cap, "cap_source": cap_source,
            "effective_cap_bytes": effective, "limited_by": limited_by,
            "used_bytes": total_bytes, "files": len(kept),
            "oldest": _iso(min((f.mtime for f in kept), default=None)),
            "deleted": len(deleted), "deleted_bytes": freed,
            "by_kind": by_kind,
            "disk": disk,
            # Older continuous-recording / export directories: measured, never cleaned here.
            "not_managed": unmanaged,
        }
        if any(v["files"] for n, v in unmanaged.items() if n != "archives"):
            report["note"] = ("Continuous-recording files exist under STORAGE_DIR (dvr/ or recordings/). "
                              "This device no longer records continuously; the owner decides whether to "
                              "delete them.")
        self.last_report = report
        ServiceHealthTracker.report_status("evidence_storage", ServiceHealthTracker.HEALTHY)
        return report

    def enforce_kind_cap(self, kind_name: str) -> int:
        """Only the sub-cap of one kind (e.g. night watch after a write). Returns files deleted."""
        kind = KINDS_BY_NAME[kind_name]
        sub = kind.sub_cap_bytes()
        if sub is None:
            return 0
        with self._lock:
            why = self._refusal()
            if why:
                self._refuse(why)
                return 0
            d, reason = self.kind_dir(kind)
            if reason:
                logger.error(f"{kind.name} evidence cap not enforced: {reason}")
                return 0
            files = sorted(self._scan_kind(kind, d), key=lambda f: (f.mtime, str(f.path)))
            total = sum(f.size for f in files)
            deleted: List[_File] = []
            for f in files:
                if total <= sub:
                    break
                if self._safe_unlink(f, d):
                    deleted.append(f)
                    total -= f.size
            if deleted:
                freed = sum(f.size for f in deleted)
                self._mark_expired(deleted)
                self.stats["deleted_files"] += len(deleted)
                self.stats["deleted_bytes"] += freed
                self.stats["last_cleanup_at"] = time.time()
                with self._state_lock:
                    if self._used_bytes is not None:
                        self._used_bytes = max(0, self._used_bytes - freed)
                logger.info(f"{kind.name} evidence over {sub / MB:g} MB: deleted {len(deleted)} oldest file(s)")
            return len(deleted)

    # ------------------------------------------------------------ after a write

    def note_write(self, path: Optional[os.PathLike | str]) -> None:
        """Called after evidence is written: enforce at once if the limit is now exceeded.

        Cheap: one stat and a comparison against the last pass's total. The
        pass itself runs on a background thread so the writer never waits.
        """
        if not path:
            return
        try:
            size = os.lstat(path).st_size
        except OSError:
            return
        with self._state_lock:
            if self._used_bytes is None:
                # No pass yet: the background loop's first pass (~30 s after
                # start) measures everything, this file included.
                return
            else:
                self._used_bytes += size
                cap = self._effective_cap if self._effective_cap is not None else self.cap()[0]
                over = self._used_bytes > cap
            if not over:
                return
            if self._lock.locked():
                self._rerun = True
                return
        threading.Thread(target=self._background_enforce, name="evidence-limit", daemon=True).start()

    def _background_enforce(self) -> None:
        try:
            self.enforce()
            with self._state_lock:
                again = self._rerun
            if again:
                self.enforce()
        except Exception as e:  # noqa: BLE001 - never kill the writer that noted the file
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)
            logger.error(f"evidence storage limit pass failed: {e}")

    # ------------------------------------------------------------ status / lifecycle

    def _base_report(self) -> Dict[str, Any]:
        cap, source = self.cap()
        return {
            "storage_dir": str(self.root()),
            "cap_bytes": cap, "cap_source": source,
            "retention_days": int(settings.STORAGE_RETENTION_DAYS or 0),
            "max_disk_percent": float(settings.STORAGE_MAX_DISK_PERCENT or 0),
            "interval_seconds": float(getattr(settings, "EVIDENCE_CLEANUP_INTERVAL_SEC", 900)),
        }

    def status(self) -> Dict[str, Any]:
        """The last pass's report plus running totals. Never scans."""
        s = self.stats
        rep = dict(self.last_report) if self.last_report else {
            **self._base_report(), "status": "refused" if s.get("refused") else "not_run_yet",
            "error": s.get("refused"), "used_bytes": None, "files": None, "oldest": None}
        rep.update({
            "last_run_at": _iso(s["last_run_at"]),
            "last_cleanup_at": _iso(s["last_cleanup_at"]),
            "last_deleted": s["last_deleted"],
            "deleted_files_total": s["deleted_files"],
            "deleted_bytes_total": s["deleted_bytes"],
            "errors": s["errors"],
            "last_error": s["last_error"],
        })
        if s.get("refused"):
            rep["status"], rep["error"] = "refused", s["refused"]
        return rep

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="evidence-storage")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        delay = 30.0                       # let startup settle first
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.enforce)
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)
                logger.error(f"evidence storage limit pass failed: {e}")
            delay = max(60.0, float(getattr(settings, "EVIDENCE_CLEANUP_INTERVAL_SEC", 900)))


evidence_storage = EvidenceStorage()


def note_evidence_written(path) -> None:
    """Writers call this after saving evidence; never raises."""
    try:
        evidence_storage.note_write(path)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"evidence note_write failed: {e}")
