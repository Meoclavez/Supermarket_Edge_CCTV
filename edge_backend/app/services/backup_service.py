"""SQLite database backup, retention and restore for the Edge CCTV backend.

Policy
------
* Every snapshot is taken with the SQLite online backup API, so it is
  consistent even while the server is writing in WAL mode, and only after a
  free-space check (the copy is as big as the database: 2.8 GB at Pearcedale).
* Automatic snapshots (tag ``startup`` or ``auto``) are **deduplicated** with a
  cheap change key of the live database (schema; every small table's rows;
  row count and highest rowid of the big append-mostly tables), stored next
  to each backup as ``<name>.key.json``. When the key equals the newest
  backup's and that backup is under 24 h old, no copy is made at all.
  Restarting the server ten times without any change produces one backup.
* Retention (:meth:`BackupService.apply_retention`) keeps the newest
  ``keep_startup`` automatic snapshots plus the newest one of each UTC day for
  ``keep_daily_days`` days, and the newest ``keep_per_kind`` (2) of every
  other kind: before upgrade (``pre-v*``), manual, before restore, before
  reset, uploaded. Then the total is held under a byte budget
  (``BACKUP_MAX_GB``; automatic: 15 % of the disk, at least two database
  copies): oldest automatic snapshots go first, then older ones of other kinds.
  The newest backup is never deleted.
* Everything but the newest backup and the newest pre-migration snapshot is
  gzip-compressed (``.db.gz``); restore accepts both. The newest ``pre-v*``
  stays plain so the documented manual rollback
  (``cp storage/backups/*pre-vNNNN.db ...``) works for the latest upgrade;
  older ones need ``gunzip -k`` first.
* :meth:`BackupService.restore_backup` accepts ``.db`` and ``.db.gz``; the
  snapshot is integrity-checked before it replaces anything.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("BackupService")

BACKUP_FILENAME_PATTERN = re.compile(r"^edge_cctv_(\d{8}_\d{6})_([a-zA-Z0-9_\-]+)\.db(\.gz)?$")

# Tags written automatically; only these are subject to dedup and retention.
AUTO_TAGS = ("startup", "auto")
_AUTO_TAG_RE = re.compile(r"^(startup|auto)(-\d+)?$")
_PRE_MIGRATION_RE = re.compile(r"^pre-v\d+")

_GZIP_MAGIC = b"\x1f\x8b"


def is_auto_tag(tag: str) -> bool:
    return bool(_AUTO_TAG_RE.match(tag or ""))


def is_pre_migration_tag(tag: str) -> bool:
    return bool(_PRE_MIGRATION_RE.match(tag or ""))


def content_hash(db_path: Path) -> str:
    """Logical hash of an SQLite file: its schema plus every row of every table.

    Independent of page layout, freelist and header counters, so two snapshots of
    an unchanged database hash equal even when their bytes differ. The file is
    opened read-only; a self-contained snapshot (no -wal next to it) is opened
    immutable so hashing never creates -wal/-shm files, while a file that has a
    WAL is read normally so un-checkpointed rows are included.
    """
    resolved = Path(db_path).resolve()
    immutable = "" if Path(f"{resolved}-wal").exists() else "&immutable=1"
    uri = f"file:{resolved}?mode=ro{immutable}"
    h = hashlib.sha256()
    conn = sqlite3.connect(uri, uri=True)
    try:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        h.update(repr(schema).encode())
        tables = [n for (t, n, _s) in schema if t == "table" and not n.startswith("sqlite_")]
        for name in tables:
            h.update(b"\x00T" + name.encode())
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                cur = conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
                for row in cur:
                    h.update(repr(row).encode())
            except sqlite3.OperationalError:
                # WITHOUT ROWID table: order in Python instead.
                rows = conn.execute(f"SELECT * FROM {quoted}").fetchall()
                for row in sorted(rows, key=repr):
                    h.update(repr(row).encode())
    finally:
        conn.close()
    return h.hexdigest()


# Tables with more rows than this are keyed by (row count, highest rowid)
# only: they are the append-mostly history tables (customer_tracks,
# zone_visits, ...), whose changes always add rows. Smaller tables (settings,
# layout, cameras, users) are hashed row by row, which catches updates too.
_SMALL_TABLE_ROWS = 50_000


def change_key(db_path: Path) -> str:
    """Cheap fingerprint of a database for backup dedupe (seconds, not minutes, on GBs)."""
    resolved = Path(db_path).resolve()
    immutable = "" if Path(f"{resolved}-wal").exists() else "&immutable=1"
    conn = sqlite3.connect(f"file:{resolved}?mode=ro{immutable}", uri=True, timeout=15.0)
    h = hashlib.sha256()
    try:
        schema = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        h.update(repr(schema).encode())
        for (t, name, _sql) in schema:
            if t != "table" or name.startswith("sqlite_"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            h.update(b"\x00T" + name.encode())
            try:
                count, top = conn.execute(f"SELECT count(*), max(rowid) FROM {quoted}").fetchone()
            except sqlite3.OperationalError:  # WITHOUT ROWID
                count, top = conn.execute(f"SELECT count(*), NULL FROM {quoted}").fetchone()
            h.update(repr((count, top)).encode())
            if (count or 0) <= _SMALL_TABLE_ROWS:
                try:
                    rows = conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
                except sqlite3.OperationalError:
                    rows = sorted(conn.execute(f"SELECT * FROM {quoted}").fetchall(), key=repr)
                for row in rows:
                    h.update(repr(row).encode())
    finally:
        conn.close()
    return h.hexdigest()


def _key_path(backup: Path) -> Path:
    name = backup.name
    for suffix in (".db.gz", ".db"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return backup.with_name(f"{name}.key.json")


def db_bytes(db_path: Path) -> int:
    """Database plus its WAL, the size a full backup copy needs."""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(f"{db_path}{suffix}")
        except OSError:
            pass
    return total


class BackupService:
    """Transactional backups, deduplication, retention and safe restore of the SQLite DB."""

    def __init__(
        self,
        backups_dir: Optional[Path] = None,
        db_path: Optional[Path] = None,
        keep_startup: Optional[int] = None,
        keep_daily_days: Optional[int] = None,
        compress: Optional[bool] = None,
        keep_per_kind: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ):
        self.backups_dir = Path(backups_dir or settings.BACKUPS_DIR)
        self.db_path = Path(db_path or settings.DATABASE_PATH)
        self.keep_startup = int(keep_startup if keep_startup is not None
                                else getattr(settings, "BACKUP_KEEP_STARTUP", 7))
        self.keep_daily_days = int(keep_daily_days if keep_daily_days is not None
                                   else getattr(settings, "BACKUP_KEEP_DAILY_DAYS", 14))
        self.compress = bool(compress if compress is not None
                             else getattr(settings, "BACKUP_COMPRESS", True))
        self.keep_per_kind = max(1, int(keep_per_kind if keep_per_kind is not None
                                        else getattr(settings, "BACKUP_KEEP_PER_KIND", 2)))
        self.max_bytes_setting = (max_bytes if max_bytes is not None
                                  else int(float(getattr(settings, "BACKUP_MAX_GB", 0) or 0) * 1024 ** 3))
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def budget_bytes(self) -> int:
        """Byte budget for all backups: the setting, or 15 % of the backups disk (>= 2 DB copies)."""
        if self.max_bytes_setting and self.max_bytes_setting > 0:
            return int(self.max_bytes_setting)
        try:
            total = shutil.disk_usage(str(self.backups_dir)).total
        except OSError:
            total = 0
        return int(max(total * 0.15, 2.2 * db_bytes(self.db_path), 256 * 1024 ** 2))

    def space_check(self) -> tuple[bool, str]:
        """(ok, reason): room for one more full copy of the database (1.2x its size)?"""
        need = int(db_bytes(self.db_path) * 1.2)
        try:
            free = shutil.disk_usage(str(self.backups_dir)).free
        except OSError as exc:
            return False, f"cannot read free disk space: {exc}"
        if free >= need:
            return True, ""
        return False, (f"only {free / 1e9:.1f} GB free; a backup copy needs about {need / 1e9:.1f} GB")

    # ------------------------------------------------------------------ helpers

    def _sanitize_tag(self, tag: str) -> str:
        """Sanitize tag string to prevent path manipulation and illegal characters."""
        cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", tag).strip("_")
        return cleaned or "auto"

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal."""
        safe_name = os.path.basename(filename)
        if (
            safe_name != filename
            or ".." in safe_name
            or not (safe_name.endswith(".db") or safe_name.endswith(".db.gz"))
        ):
            raise ValueError(f"Invalid or unsafe backup filename: {filename}")
        return safe_name

    @staticmethod
    def _remove_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

    def _delete_backup(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self._remove_sidecars(path)
        _key_path(path).unlink(missing_ok=True)

    def _key_of_backup(self, path: Path) -> dict:
        """Change key of a backup written before keys were stored (once; then saved beside it)."""
        import json
        tmp: Optional[Path] = None
        try:
            src = path
            if path.name.endswith(".gz"):
                tmp = path.with_name(f".key_{os.getpid()}_{path.name[:-3]}")
                with gzip.open(path, "rb") as fin, open(tmp, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                src = tmp
            data = {"key": change_key(src), "tag": "computed"}
            _key_path(path).write_text(json.dumps(data))
            return data
        except Exception as e:
            logger.warning(f"Could not compute the change key of backup {path.name}: {e}")
            return {}
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
                self._remove_sidecars(tmp)

    @staticmethod
    def _read_key(path: Path) -> Optional[dict]:
        try:
            import json
            return json.loads(_key_path(path).read_text())
        except Exception:
            return None

    def _unique_dest(self, timestamp_str: str, tag: str) -> Path:
        """edge_cctv_<ts>_<tag>.db, with a -N suffix on the tag if that second is taken."""
        candidate = self.backups_dir / f"edge_cctv_{timestamp_str}_{tag}.db"
        n = 2
        while candidate.exists() or Path(f"{candidate}.gz").exists():
            candidate = self.backups_dir / f"edge_cctv_{timestamp_str}_{tag}-{n}.db"
            n += 1
        return candidate

    def _snapshot(self, dest_path: Path) -> None:
        """Online-backup self.db_path into dest_path (a plain rollback-journal DB)."""
        src_conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            try:
                src_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception as e:
                logger.warning(f"PRAGMA wal_checkpoint warning (continuing): {e}")
            dst_conn = sqlite3.connect(str(dest_path), timeout=15.0)
            try:
                src_conn.backup(dst_conn)
                # The copy inherits WAL mode from the source; switch the snapshot to
                # a self-contained single file so no -wal/-shm are left behind.
                dst_conn.execute("PRAGMA journal_mode=DELETE;")
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        self._remove_sidecars(dest_path)

    def _parse(self, p: Path) -> Dict[str, Any]:
        stat = p.stat()
        mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        ts_dt = mtime_utc
        match = BACKUP_FILENAME_PATTERN.match(p.name)
        if match:
            raw_ts, tag, _gz = match.groups()
            try:
                ts_dt = datetime.strptime(raw_ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                ts_dt = mtime_utc
        else:
            tag = "custom"
        return {
            "filename": p.name,
            "filepath": str(p.resolve()),
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "timestamp": ts_dt.isoformat(),
            "created_at": mtime_utc.isoformat(),
            "tag": tag,
            "compressed": p.name.endswith(".gz"),
            "_ts": ts_dt,
            "_mtime": stat.st_mtime,
        }

    def _entries(self) -> List[Dict[str, Any]]:
        """Backup entries with internal sort keys, newest first."""
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        out: List[Dict[str, Any]] = []
        for p in self.backups_dir.iterdir():
            if not p.is_file() or not (p.name.endswith(".db") or p.name.endswith(".db.gz")):
                continue
            try:
                out.append(self._parse(p))
            except Exception as e:
                logger.warning(f"Error inspecting backup file {p.name}: {e}")
        out.sort(key=lambda b: (b["_ts"], b["_mtime"], b["filename"]), reverse=True)
        return out

    # ------------------------------------------------------------------ public API

    def create_backup(self, tag: str = "auto", dedupe: Optional[bool] = None) -> Dict[str, Any]:
        """Snapshot the database to storage/backups/edge_cctv_{timestamp}_{tag}.db.

        ``dedupe`` defaults to True for automatic tags (startup/auto) and False
        otherwise, so pre-migration and operator backups are always written.
        When deduplicated, returns ``status: "skipped"`` and ``duplicate_of``.
        """
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        safe_tag = self._sanitize_tag(tag)
        if dedupe is None:
            dedupe = is_auto_tag(safe_tag)
        now_utc = datetime.now(timezone.utc)
        timestamp_str = now_utc.strftime("%Y%m%d_%H%M%S")

        if not self.db_path.exists():
            if dedupe:
                logger.info(f"No database at {self.db_path}; nothing to back up.")
                return {"status": "skipped", "reason": "no_source", "filename": "", "filepath": None,
                        "size_bytes": 0, "size_mb": 0.0, "timestamp": now_utc.isoformat(), "tag": safe_tag}
            logger.warning(f"Source database does not exist at {self.db_path}; creating empty snapshot.")
            dest_path = self._unique_dest(timestamp_str, safe_tag)
            with sqlite3.connect(str(dest_path)) as conn:
                conn.execute("PRAGMA user_version = 1;")
            file_size = os.path.getsize(dest_path)
            return {"status": "success", "filename": dest_path.name, "filepath": str(dest_path.resolve()),
                    "size_bytes": file_size, "size_mb": round(file_size / (1024 * 1024), 2),
                    "timestamp": now_utc.isoformat(), "tag": safe_tag}

        ok, why = self.space_check()
        if not ok:
            logger.error(f"Backup ({safe_tag}) not taken: {why}. Free disk space (old clips, backups).")
            if dedupe:
                return {"status": "skipped", "reason": "no_space", "detail": why, "filename": "",
                        "filepath": None, "size_bytes": 0, "size_mb": 0.0,
                        "timestamp": now_utc.isoformat(), "tag": safe_tag}
            raise OSError(f"Not enough disk space for a backup: {why}")

        key = None
        if dedupe:
            # Compare a cheap change key of the live database with the newest
            # backup's before copying anything (a 2.8 GB copy plus a full row
            # hash took minutes of every start). Foreign files (salvage
            # exports, hand copies) are never a dedupe reference.
            key = change_key(self.db_path)
            existing = [b for b in self._entries() if BACKUP_FILENAME_PATTERN.match(b["filename"])]
            if existing:
                newest = existing[0]
                stored = self._read_key(Path(newest["filepath"])) or self._key_of_backup(Path(newest["filepath"]))
                age_s = (now_utc - newest["_ts"]).total_seconds()
                if stored.get("key") == key and age_s < 24 * 3600:
                    logger.info(f"Database unchanged since {newest['filename']}; {safe_tag} backup skipped.")
                    return {"status": "skipped", "reason": "unchanged", "duplicate_of": newest["filename"],
                            "filename": newest["filename"], "filepath": newest["filepath"],
                            "size_bytes": newest["size_bytes"], "size_mb": newest["size_mb"],
                            "timestamp": now_utc.isoformat(), "tag": safe_tag}

        dest_path = self._unique_dest(timestamp_str, safe_tag)
        # Write under a hidden temp name so a crash mid-backup never leaves a
        # truncated file that looks like a valid backup.
        tmp_path = dest_path.with_name(f".tmp_{dest_path.name}")
        logger.info(f"Initiating online SQLite backup from {self.db_path} to {dest_path}...")
        try:
            self._snapshot(tmp_path)
            if key is None:
                key = change_key(tmp_path)
            os.replace(tmp_path, dest_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            self._remove_sidecars(tmp_path)
            raise
        finally:
            tmp_path.unlink(missing_ok=True)
            self._remove_sidecars(tmp_path)
        try:
            import json
            _key_path(dest_path).write_text(json.dumps({"key": key, "tag": safe_tag}))
        except OSError as exc:
            logger.warning(f"Could not write the change key of {dest_path.name}: {exc}")
        _clear_migration_backup_warning(self.db_path)

        file_size = os.path.getsize(dest_path)
        size_mb = round(file_size / (1024 * 1024), 2)
        logger.info(f"Backup created: {dest_path.name} ({size_mb} MB)")
        return {"status": "success", "filename": dest_path.name, "filepath": str(dest_path.resolve()),
                "size_bytes": file_size, "size_mb": size_mb, "timestamp": now_utc.isoformat(),
                "tag": safe_tag, "change_key": key}

    def list_backups(self) -> List[Dict[str, Any]]:
        """All snapshots (.db and .db.gz), newest first."""
        entries = self._entries()
        for b in entries:
            b.pop("_ts", None)
            b.pop("_mtime", None)
        return entries

    def apply_retention(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Prune and compress snapshots according to the policy above.

        Returns ``{"deleted": [...], "compressed": [...], "kept": int, "total_bytes": int,
        "budget_bytes": int}``.
        """
        now = now or datetime.now(timezone.utc)
        entries = self._entries()
        ours = [b for b in entries if BACKUP_FILENAME_PATTERN.match(b["filename"])]
        newest_name = ours[0]["filename"] if ours else None
        auto = [b for b in ours if is_auto_tag(b["tag"])]

        keep: set[str] = {b["filename"] for b in auto[: max(self.keep_startup, 0)]}
        cutoff = now - timedelta(days=max(self.keep_daily_days, 0))
        seen_days: set = set()
        for b in auto:  # newest first -> first seen per day is that day's newest
            day = b["_ts"].date()
            if b["_ts"] >= cutoff and day not in seen_days:
                seen_days.add(day)
                keep.add(b["filename"])
        # Other kinds: the newest keep_per_kind of each (pre-v* counted together).
        per_kind: Dict[str, int] = {}
        doomed_other: List[Dict[str, Any]] = []
        for b in ours:
            if is_auto_tag(b["tag"]):
                continue
            kind = "pre-v" if is_pre_migration_tag(b["tag"]) else re.sub(r"-\d+$", "", b["tag"])
            per_kind[kind] = per_kind.get(kind, 0) + 1
            if per_kind[kind] <= self.keep_per_kind:
                keep.add(b["filename"])
            else:
                doomed_other.append(b)
        if newest_name:
            keep.add(newest_name)

        deleted: List[str] = []

        def _drop(b: Dict[str, Any], why: str) -> None:
            try:
                self._delete_backup(Path(b["filepath"]))
                deleted.append(b["filename"])
                logger.info(f"Backup retention: deleted {b['filename']} ({why})")
            except Exception as e:
                logger.error(f"Failed to prune backup {b['filename']}: {e}")

        for b in auto:
            if b["filename"] not in keep:
                _drop(b, "automatic, outside the kept window")
        for b in doomed_other:
            _drop(b, f"more than {self.keep_per_kind} of its kind")

        compressed: List[str] = []
        if self.compress:
            pre = [b for b in ours if is_pre_migration_tag(b["tag"]) and b["filename"] not in deleted]
            newest_pre = pre[0]["filename"] if pre else None
            for b in ours:
                if (b["filename"] in deleted or b["compressed"]
                        or b["filename"] in (newest_name, newest_pre)):
                    continue
                try:
                    compressed.append(self.compress_backup(b["filename"]))
                except Exception as e:
                    logger.error(f"Failed to compress backup {b['filename']}: {e}")

        # Byte budget over everything this service wrote; oldest automatic
        # first, then oldest of any kind. The newest backup always stays.
        budget = self.budget_bytes()
        remaining = [b for b in self._entries() if BACKUP_FILENAME_PATTERN.match(b["filename"])]
        total = sum(b["size_bytes"] for b in remaining)
        if total > budget:
            order = sorted((b for b in remaining if b["filename"] != newest_name),
                           key=lambda b: (0 if is_auto_tag(b["tag"]) else 1, b["_ts"]))
            for b in order:
                if total <= budget:
                    break
                _drop(b, f"backups over the {budget / 1024 ** 3:.1f} GB budget")
                total -= b["size_bytes"]
            if total > budget:
                logger.warning(f"Backups use {total / 1024 ** 3:.1f} GB, over the "
                               f"{budget / 1024 ** 3:.1f} GB budget, with only the newest left.")

        if deleted or compressed:
            logger.info(f"Backup retention: deleted {len(deleted)}, compressed {len(compressed)}")
        return {"deleted": deleted, "compressed": compressed, "kept": len(entries) - len(deleted),
                "total_bytes": int(total), "budget_bytes": int(budget)}

    def usage(self) -> Dict[str, Any]:
        """Bytes used by backups (for storage accounting)."""
        entries = self._entries()
        return {"count": len(entries), "bytes": int(sum(b["size_bytes"] for b in entries)),
                "budget_bytes": self.budget_bytes()}

    def compress_backup(self, filename: str) -> str:
        """gzip a .db backup in place (atomic), verifying the archive before removing the original."""
        safe = self._sanitize_filename(filename)
        src = self.backups_dir / safe
        if safe.endswith(".gz"):
            return safe
        dest = self.backups_dir / f"{safe}.gz"
        tmp = self.backups_dir / f".tmp_{safe}.gz"
        st = src.stat()
        try:
            with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            # Verify the archive round-trips to the same bytes before deleting.
            h_src, h_gz = hashlib.sha256(), hashlib.sha256()
            with open(src, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h_src.update(chunk)
            with gzip.open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h_gz.update(chunk)
            if h_src.digest() != h_gz.digest():
                raise IOError(f"gzip verification failed for {safe}")
            os.replace(tmp, dest)
            os.utime(dest, (st.st_atime, st.st_mtime))
        finally:
            tmp.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        self._remove_sidecars(src)
        return dest.name

    def prune_backups(self, keep_days: int = 7, min_keep: int = 3) -> int:
        """Legacy age-based pruning. Never deletes pre-migration snapshots.

        Startup now uses :meth:`apply_retention`; this remains for callers and
        tests that ask for a plain "older than N days" sweep.
        """
        backups = self._entries()
        if len(backups) <= min_keep:
            return 0
        now = time.time()
        max_age_seconds = keep_days * 86400
        pruned_count = 0
        for b in backups[min_keep:]:
            if is_pre_migration_tag(b["tag"]):
                continue
            filepath = Path(b["filepath"])
            try:
                age_seconds = now - filepath.stat().st_mtime
                if age_seconds > max_age_seconds:
                    filepath.unlink(missing_ok=True)
                    self._remove_sidecars(filepath)
                    pruned_count += 1
                    logger.info(f"Pruned old backup: {filepath.name} (age: {age_seconds / 86400:.1f} days)")
            except Exception as e:
                logger.error(f"Failed to prune backup {filepath.name}: {e}")
        return pruned_count

    def restore_backup(self, filename: str) -> bool:
        """Restore a .db or .db.gz snapshot via the SQLite online backup API.

        The snapshot (decompressed to a temp file if needed) must carry a valid
        SQLite header and pass ``PRAGMA integrity_check`` before the live
        database is touched.
        """
        safe_name = self._sanitize_filename(filename)
        backup_path = self.backups_dir / safe_name
        if not backup_path.exists() or not backup_path.is_file():
            logger.error(f"Cannot restore: Backup file {backup_path} does not exist.")
            raise FileNotFoundError(f"Backup file not found: {safe_name}")

        with open(backup_path, "rb") as f:
            magic = f.read(16)
        tmp: Optional[Path] = None
        source = backup_path
        try:
            if safe_name.endswith(".gz"):
                if not magic.startswith(_GZIP_MAGIC):
                    raise ValueError(f"File {safe_name} is not a gzip archive.")
                tmp = self.backups_dir / f".restore_{os.getpid()}_{safe_name[:-3]}"
                with gzip.open(backup_path, "rb") as fin, open(tmp, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                source = tmp
                with open(source, "rb") as f:
                    magic = f.read(16)
            if not magic.startswith(b"SQLite format 3"):
                raise ValueError(f"File {safe_name} is not a valid SQLite database header.")

            check = sqlite3.connect(f"file:{source.resolve()}?mode=ro&immutable=1", uri=True)
            try:
                result = check.execute("PRAGMA integrity_check").fetchone()
            finally:
                check.close()
            if not result or result[0] != "ok":
                raise ValueError(f"Backup {safe_name} failed integrity_check: {result}")
            # A snapshot written by a newer release would stop the next start
            # (SchemaTooNewError), so it is refused before anything is replaced.
            validate_sqlite_snapshot(source, require_schema=False)

            logger.info(f"Restoring database from snapshot {backup_path} to {self.db_path}...")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            src_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True, timeout=15.0)
            try:
                dst_conn = sqlite3.connect(str(self.db_path), timeout=15.0)
                try:
                    src_conn.backup(dst_conn)
                    dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
                self._remove_sidecars(tmp)

        logger.info(f"Database successfully restored from {safe_name}")
        return True

    # ------------------------------------------------------------------ dashboard helpers

    def resolve_backup_path(self, filename: str) -> Path:
        """Path of a listed backup, or ValueError/FileNotFoundError.

        Only a plain name of a regular file directly inside the backups
        directory is accepted: no separators, no ``..``, no hidden temp files
        and no symlink that points elsewhere.
        """
        if not isinstance(filename, str) or not filename or filename.startswith("."):
            raise ValueError(f"Invalid or unsafe backup filename: {filename!r}")
        if "/" in filename or "\\" in filename or "\x00" in filename:
            raise ValueError(f"Invalid or unsafe backup filename: {filename!r}")
        safe = self._sanitize_filename(filename)
        base = self.backups_dir.resolve()
        path = self.backups_dir / safe
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"Backup file not found: {safe}")
        if path.resolve().parent != base:
            raise ValueError(f"Invalid or unsafe backup filename: {filename!r}")
        return path

    def inspect_snapshot(self, filename: str) -> Dict[str, Any]:
        """Validate a listed backup for restore without touching the live database."""
        path = self.resolve_backup_path(filename)
        if path.name.endswith(".gz"):
            tmp = self.backups_dir / f".inspect_{os.getpid()}_{path.name[:-3]}"
            try:
                with open(path, "rb") as f:
                    if not f.read(2).startswith(_GZIP_MAGIC):
                        raise ValueError(f"File {path.name} is not a gzip archive.")
                with gzip.open(path, "rb") as fin, open(tmp, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                return validate_sqlite_snapshot(tmp, require_schema=False)
            finally:
                tmp.unlink(missing_ok=True)
                self._remove_sidecars(tmp)
        return validate_sqlite_snapshot(path, require_schema=False)

    def import_upload(self, tmp_file: Path, original_name: str = "") -> Dict[str, Any]:
        """Turn an uploaded file (plain SQLite or gzip) into a listed ``uploaded`` backup.

        The upload must be a SQLite database of this application: integrity ok,
        a ``schema_migrations`` table, the core tables, and a schema version not
        newer than this release. Nothing is restored here; the caller only adds
        it to the list. ``tmp_file`` is always consumed (moved or deleted).
        """
        tmp_file = Path(tmp_file)
        plain = tmp_file
        decompressed: Optional[Path] = None
        try:
            with open(tmp_file, "rb") as f:
                magic = f.read(16)
            if magic.startswith(_GZIP_MAGIC):
                decompressed = tmp_file.with_name(tmp_file.name + ".plain")
                written = 0
                try:
                    with gzip.open(tmp_file, "rb") as fin, open(decompressed, "wb") as fout:
                        for chunk in iter(lambda: fin.read(1 << 20), b""):
                            written += len(chunk)
                            if written > max_upload_expanded_bytes():
                                raise ValueError("The uploaded archive expands to more than the allowed size.")
                            fout.write(chunk)
                except (OSError, EOFError, gzip.BadGzipFile) as e:
                    raise ValueError(f"The uploaded file is not a readable gzip archive ({e}).")
                plain = decompressed
            info = validate_sqlite_snapshot(plain, require_schema=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest = self._unique_dest(ts, "uploaded")
            os.replace(plain, dest)
            self._remove_sidecars(plain)
            entry = self._parse(dest)
            entry.pop("_ts", None)
            entry.pop("_mtime", None)
            entry["schema_version"] = info["schema_version"]
            entry["original_name"] = os.path.basename(original_name or "")[:200]
            logger.info(f"Uploaded backup accepted as {dest.name} (schema v{info['schema_version']})")
            return entry
        finally:
            tmp_file.unlink(missing_ok=True)
            if decompressed is not None:
                decompressed.unlink(missing_ok=True)
                self._remove_sidecars(decompressed)


def _clear_migration_backup_warning(db_path: Path) -> None:
    """A full backup now exists: drop the "upgraded without a backup" marker (runner.py)."""
    try:
        marker = Path(f"{Path(db_path).resolve()}.backup-status.json")
        if marker.exists():
            import json
            if json.loads(marker.read_text() or "{}").get("state") == "skipped":
                marker.unlink(missing_ok=True)
    except Exception:
        pass


# Uploaded backups: hard caps so an upload cannot fill the disk. A plain
# backup is as big as the database, so the cap follows it (2 GB minimum).
MAX_UPLOAD_EXPANDED_BYTES = 8 * 1024 ** 3


def max_upload_bytes() -> int:
    return int(max(2 * 1024 ** 3, 1.5 * db_bytes(Path(settings.DATABASE_PATH))))


def max_upload_expanded_bytes() -> int:
    return int(max(MAX_UPLOAD_EXPANDED_BYTES, 3 * db_bytes(Path(settings.DATABASE_PATH))))


MAX_UPLOAD_BYTES = 2 * 1024 ** 3  # kept for importers; the live limit is max_upload_bytes()

# Tables every database of this application has (m0001 baseline onwards).
CORE_TABLES = frozenset({"admin_users", "cameras", "store_layouts", "store_zones"})


def validate_sqlite_snapshot(path: Path, require_schema: bool = True) -> Dict[str, Any]:
    """Check that ``path`` is an intact SQLite file this release can open.

    Raises ValueError with a plain reason. ``require_schema`` (uploads) also
    demands the ``schema_migrations`` table and the core tables; a backup this
    device wrote itself may predate versioning (pre-v0001 snapshots) and is
    migrated on the next start like any old database.
    """
    path = Path(path)
    with open(path, "rb") as f:
        magic = f.read(16)
    if not magic.startswith(b"SQLite format 3"):
        raise ValueError("The file is not an SQLite database.")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"The database cannot be read ({e}).")
        if not result or result[0] != "ok":
            raise ValueError(f"The database failed its integrity check: {result}")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        versions: List[int] = []
        if "schema_migrations" in tables:
            versions = [int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations")]
    finally:
        conn.close()

    from app.migrations.runner import head_version

    head = head_version()
    newest = max(versions) if versions else 0
    if newest > head:
        raise ValueError(
            f"This backup comes from a newer release (database version {newest}); this device "
            f"runs version {head}. Update the device software first.")
    if require_schema:
        if "schema_migrations" not in tables:
            raise ValueError("The file is not a backup of this system (it has no schema version).")
        missing = sorted(CORE_TABLES - tables)
        if missing:
            raise ValueError(f"The file is not a backup of this system (missing tables: {', '.join(missing)}).")
    return {"schema_version": newest, "head_version": head, "tables": len(tables)}


_KIND_LABELS = {
    "startup": "At start",
    "auto": "Automatic",
    "manual": "Manual",
    "pre-restore": "Before restore",
    "pre-reset": "Before reset",
    "uploaded": "Uploaded",
}


def backup_kind(tag: str) -> str:
    """Plain-language kind of a backup from its tag."""
    tag = tag or ""
    if is_pre_migration_tag(tag):
        return "Before upgrade"
    base = re.sub(r"-\d+$", "", tag)
    return _KIND_LABELS.get(base, "Other")


def seconds_until_daily_backup(now_local: datetime, hour: int = 3) -> float:
    """Seconds from ``now_local`` (store time) to the next ``hour``:00 store time."""
    target = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_local:
        target = (target + timedelta(days=1)).replace(hour=hour)
    # Subtract in UTC: aware datetimes sharing a tzinfo subtract as wall clock,
    # which put the backup at 04:00 (or 02:00) on DST change nights.
    if now_local.tzinfo is not None:
        delta = target.astimezone(timezone.utc) - now_local.astimezone(timezone.utc)
    else:
        delta = target - now_local
    return max(60.0, delta.total_seconds())


class DailyBackupScheduler:
    """Takes an automatic snapshot every night at 03:00 store time.

    Uses the "auto" tag, so an unchanged database is skipped (content dedupe)
    and retention keeps one per day for BACKUP_KEEP_DAILY_DAYS. The store
    time zone is read at each wait, so a zone changed in the dashboard applies
    from the next night.
    """

    def __init__(self, service: "BackupService"):
        self._service = service
        self._task = None

    async def start(self) -> None:
        import asyncio

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="daily-backup")

    async def stop(self) -> None:
        import asyncio

        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        import asyncio
        from app.services.timeutil import local_now

        while True:
            await asyncio.sleep(seconds_until_daily_backup(local_now()))
            try:
                res = await asyncio.to_thread(self._service.create_backup, "auto")
                if res.get("status") == "skipped":
                    logger.info(f"Daily backup skipped ({res.get('reason')})")
                else:
                    logger.info(f"Daily backup created: {res.get('filename')}")
                await asyncio.to_thread(self._service.apply_retention)
            except Exception as exc:  # a failed night must not stop later nights
                logger.error(f"Daily backup failed: {exc}")


# Global singleton instance
backup_service = BackupService()
daily_backup = DailyBackupScheduler(backup_service)
