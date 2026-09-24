"""SQLite database backup, retention and restore for the Edge CCTV backend.

Policy
------
* Every snapshot is taken with the SQLite online backup API, so it is
  consistent even while the server is writing in WAL mode.
* Automatic snapshots (tag ``startup`` or ``auto``) are **deduplicated**: the
  new snapshot's logical content hash (schema + every row) is compared with the
  newest existing backup and, when equal, the new file is discarded. Restarting
  the server ten times without any change produces one backup, not ten.
* Retention (:meth:`BackupService.apply_retention`) only ever deletes automatic
  snapshots. It keeps the newest ``keep_startup`` of them, plus the newest one
  of each UTC day for the last ``keep_daily_days`` days. Pre-migration
  snapshots (``pre-v0005`` ...) and operator snapshots (``manual``, custom
  tags) are never deleted automatically.
* Older automatic snapshots are gzip-compressed (``.db.gz``); the newest backup
  stays a plain ``.db`` so it restores and deduplicates without decompression.
  Pre-migration snapshots stay uncompressed so the documented manual rollback
  (``cp storage/backups/*pre-vNNNN.db ...``) keeps working.
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


class BackupService:
    """Transactional backups, deduplication, retention and safe restore of the SQLite DB."""

    def __init__(
        self,
        backups_dir: Optional[Path] = None,
        db_path: Optional[Path] = None,
        keep_startup: Optional[int] = None,
        keep_daily_days: Optional[int] = None,
        compress: Optional[bool] = None,
    ):
        self.backups_dir = Path(backups_dir or settings.BACKUPS_DIR)
        self.db_path = Path(db_path or settings.DATABASE_PATH)
        self.keep_startup = int(keep_startup if keep_startup is not None
                                else getattr(settings, "BACKUP_KEEP_STARTUP", 7))
        self.keep_daily_days = int(keep_daily_days if keep_daily_days is not None
                                   else getattr(settings, "BACKUP_KEEP_DAILY_DAYS", 14))
        self.compress = bool(compress if compress is not None
                             else getattr(settings, "BACKUP_COMPRESS", True))
        self.backups_dir.mkdir(parents=True, exist_ok=True)

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

    def _hash_backup(self, path: Path) -> Optional[str]:
        """Content hash of an existing backup, decompressing a .gz to a temp file."""
        try:
            if path.name.endswith(".gz"):
                tmp = path.with_name(f".hash_{os.getpid()}_{path.name[:-3]}")
                try:
                    with gzip.open(path, "rb") as fin, open(tmp, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    return content_hash(tmp)
                finally:
                    tmp.unlink(missing_ok=True)
            return content_hash(path)
        except Exception as e:
            logger.warning(f"Could not hash backup {path.name}: {e}")
            return None

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

        dest_path = self._unique_dest(timestamp_str, safe_tag)
        # Write under a hidden temp name so a crash mid-backup never leaves a
        # truncated file that looks like a valid backup.
        tmp_path = dest_path.with_name(f".tmp_{dest_path.name}")
        logger.info(f"Initiating online SQLite backup from {self.db_path} to {dest_path}...")
        try:
            self._snapshot(tmp_path)
            digest = content_hash(tmp_path)
            if dedupe:
                # Foreign files (salvage exports, hand copies) are listed but
                # never used as a dedup reference nor touched by retention.
                existing = [b for b in self._entries() if BACKUP_FILENAME_PATTERN.match(b["filename"])]
                if existing:
                    newest = existing[0]
                    if self._hash_backup(Path(newest["filepath"])) == digest:
                        tmp_path.unlink(missing_ok=True)
                        logger.info(f"Database unchanged since {newest['filename']}; {safe_tag} backup skipped.")
                        return {"status": "skipped", "reason": "unchanged", "duplicate_of": newest["filename"],
                                "filename": newest["filename"], "filepath": newest["filepath"],
                                "size_bytes": newest["size_bytes"], "size_mb": newest["size_mb"],
                                "timestamp": now_utc.isoformat(), "tag": safe_tag, "content_sha256": digest}
            os.replace(tmp_path, dest_path)
        finally:
            tmp_path.unlink(missing_ok=True)
            self._remove_sidecars(tmp_path)

        file_size = os.path.getsize(dest_path)
        size_mb = round(file_size / (1024 * 1024), 2)
        logger.info(f"Backup created: {dest_path.name} ({size_mb} MB)")
        return {"status": "success", "filename": dest_path.name, "filepath": str(dest_path.resolve()),
                "size_bytes": file_size, "size_mb": size_mb, "timestamp": now_utc.isoformat(),
                "tag": safe_tag, "content_sha256": digest}

    def list_backups(self) -> List[Dict[str, Any]]:
        """All snapshots (.db and .db.gz), newest first."""
        entries = self._entries()
        for b in entries:
            b.pop("_ts", None)
            b.pop("_mtime", None)
        return entries

    def apply_retention(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Prune and compress automatic snapshots according to the policy above.

        Returns ``{"deleted": [...], "compressed": [...], "kept": int}``.
        """
        now = now or datetime.now(timezone.utc)
        entries = self._entries()
        auto = [b for b in entries if BACKUP_FILENAME_PATTERN.match(b["filename"]) and is_auto_tag(b["tag"])]

        keep: set[str] = {b["filename"] for b in auto[: max(self.keep_startup, 0)]}
        cutoff = now - timedelta(days=max(self.keep_daily_days, 0))
        seen_days: set = set()
        for b in auto:  # newest first -> first seen per day is that day's newest
            day = b["_ts"].date()
            if b["_ts"] >= cutoff and day not in seen_days:
                seen_days.add(day)
                keep.add(b["filename"])

        deleted: List[str] = []
        for b in auto:
            if b["filename"] in keep:
                continue
            p = Path(b["filepath"])
            try:
                p.unlink(missing_ok=True)
                self._remove_sidecars(p)
                deleted.append(b["filename"])
            except Exception as e:
                logger.error(f"Failed to prune backup {b['filename']}: {e}")

        compressed: List[str] = []
        if self.compress:
            ours = [b for b in entries if BACKUP_FILENAME_PATTERN.match(b["filename"])]
            newest = ours[0]["filename"] if ours else None
            for b in entries:
                if (
                    b["filename"] in deleted
                    or b["compressed"]
                    or b["filename"] == newest
                    or not BACKUP_FILENAME_PATTERN.match(b["filename"])
                    or is_pre_migration_tag(b["tag"])
                ):
                    continue
                try:
                    compressed.append(self.compress_backup(b["filename"]))
                except Exception as e:
                    logger.error(f"Failed to compress backup {b['filename']}: {e}")

        if deleted or compressed:
            logger.info(f"Backup retention: deleted {len(deleted)}, compressed {len(compressed)}")
        return {"deleted": deleted, "compressed": compressed, "kept": len(entries) - len(deleted)}

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


# Global singleton instance
backup_service = BackupService()
