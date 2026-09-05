"""SQLite Database Backup and Resilience Service for Edge AI CCTV Surveillance."""

import os
import re
import time
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from app.config import settings

logger = logging.getLogger("BackupService")

BACKUP_FILENAME_PATTERN = re.compile(r"^edge_cctv_(\d{8}_\d{6})_([a-zA-Z0-9_\-]+)\.db$")


class BackupService:
    """Manages transactional backups, retention pruning, and zero-downtime restoration of SQLite database."""

    def __init__(self, backups_dir: Optional[Path] = None, db_path: Optional[Path] = None):
        self.backups_dir = backups_dir or settings.BACKUPS_DIR
        self.db_path = db_path or settings.DATABASE_PATH
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_tag(self, tag: str) -> str:
        """Sanitize tag string to prevent path manipulation and illegal characters."""
        cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", tag).strip("_")
        return cleaned or "auto"

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal."""
        safe_name = os.path.basename(filename)
        if safe_name != filename or not safe_name.endswith(".db") or ".." in safe_name:
            raise ValueError(f"Invalid or unsafe backup filename: {filename}")
        return safe_name

    def create_backup(self, tag: str = "auto") -> Dict[str, Any]:
        """Performs a safe SQLite backup to storage/backups/edge_cctv_{timestamp}_{tag}.db.
        
        Flushes WAL mode before copying and uses the atomic SQLite online backup API.
        """
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        safe_tag = self._sanitize_tag(tag)
        now_utc = datetime.now(timezone.utc)
        timestamp_str = now_utc.strftime("%Y%m%d_%H%M%S")
        backup_filename = f"edge_cctv_{timestamp_str}_{safe_tag}.db"
        dest_path = self.backups_dir / backup_filename

        if not self.db_path.exists():
            logger.warning(f"Source database does not exist at {self.db_path}; creating empty snapshot.")
            # Touch or initialize empty sqlite DB
            with sqlite3.connect(str(dest_path)) as conn:
                conn.execute("PRAGMA user_version = 1;")
            file_size = os.path.getsize(dest_path)
            return {
                "status": "success",
                "filename": backup_filename,
                "filepath": str(dest_path.resolve()),
                "size_bytes": file_size,
                "size_mb": round(file_size / (1024 * 1024), 2),
                "timestamp": now_utc.isoformat(),
                "tag": safe_tag
            }

        logger.info(f"Initiating online SQLite backup from {self.db_path} to {dest_path}...")
        
        # 1. Open source connection with timeout and flush WAL mode safely
        src_conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            try:
                src_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception as e:
                logger.warning(f"PRAGMA wal_checkpoint warning (continuing): {e}")

            # 2. Use SQLite online backup API to copy page-by-page safely
            dst_conn = sqlite3.connect(str(dest_path), timeout=15.0)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        file_size = os.path.getsize(dest_path)
        size_mb = round(file_size / (1024 * 1024), 2)
        logger.info(f"Backup created: {backup_filename} ({size_mb} MB)")

        return {
            "status": "success",
            "filename": backup_filename,
            "filepath": str(dest_path.resolve()),
            "size_bytes": file_size,
            "size_mb": size_mb,
            "timestamp": now_utc.isoformat(),
            "tag": safe_tag
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        """Lists all existing database snapshots with size (MB), timestamp, and filename.
        
        Sorted descending by modification time (most recent first).
        """
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        backups: List[Dict[str, Any]] = []

        for p in self.backups_dir.iterdir():
            if not p.is_file() or not p.name.endswith(".db"):
                continue

            try:
                stat = p.stat()
                size_bytes = stat.st_size
                size_mb = round(size_bytes / (1024 * 1024), 2)
                mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

                match = BACKUP_FILENAME_PATTERN.match(p.name)
                if match:
                    raw_ts, tag = match.groups()
                    try:
                        ts_dt = datetime.strptime(raw_ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                        formatted_ts = ts_dt.isoformat()
                    except ValueError:
                        formatted_ts = mtime_utc.isoformat()
                else:
                    tag = "custom"
                    formatted_ts = mtime_utc.isoformat()

                backups.append({
                    "filename": p.name,
                    "filepath": str(p.resolve()),
                    "size_bytes": size_bytes,
                    "size_mb": size_mb,
                    "timestamp": formatted_ts,
                    "created_at": mtime_utc.isoformat(),
                    "tag": tag,
                    "mtime": stat.st_mtime
                })
            except Exception as e:
                logger.warning(f"Error inspecting backup file {p.name}: {e}")

        # Sort descending by mtime (newest first)
        backups.sort(key=lambda x: x["mtime"], reverse=True)
        # Strip internal mtime key from final response
        for b in backups:
            b.pop("mtime", None)

        return backups

    def prune_backups(self, keep_days: int = 7, min_keep: int = 3) -> int:
        """Automatically cleans up backups older than keep_days, always preserving at least min_keep."""
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        backups = self.list_backups()
        if len(backups) <= min_keep:
            logger.info(f"Total backups ({len(backups)}) <= min_keep ({min_keep}); no pruning needed.")
            return 0

        now = time.time()
        max_age_seconds = keep_days * 86400
        pruned_count = 0

        # Preserve the newest `min_keep` backups regardless of age
        eligible_for_prune = backups[min_keep:]

        for b in eligible_for_prune:
            filepath = Path(b["filepath"])
            try:
                age_seconds = now - filepath.stat().st_mtime
                if age_seconds > max_age_seconds:
                    filepath.unlink(missing_ok=True)
                    # Also unlink any auxiliary wal/shm if left over
                    Path(f"{filepath}-wal").unlink(missing_ok=True)
                    Path(f"{filepath}-shm").unlink(missing_ok=True)
                    pruned_count += 1
                    logger.info(f"Pruned old backup: {filepath.name} (age: {age_seconds / 86400:.1f} days)")
            except Exception as e:
                logger.error(f"Failed to prune backup {filepath.name}: {e}")

        return pruned_count

    def restore_backup(self, filename: str) -> bool:
        """Restores a snapshot safely using SQLite online backup API to prevent corruption in WAL mode."""
        safe_name = self._sanitize_filename(filename)
        backup_path = self.backups_dir / safe_name

        if not backup_path.exists() or not backup_path.is_file():
            logger.error(f"Cannot restore: Backup file {backup_path} does not exist.")
            raise FileNotFoundError(f"Backup file not found: {safe_name}")

        # Validate that the backup file is a legitimate SQLite database
        try:
            with open(backup_path, "rb") as f:
                header = f.read(16)
                if not header.startswith(b"SQLite format 3"):
                    raise ValueError(f"File {safe_name} is not a valid SQLite database header.")
        except Exception as e:
            logger.error(f"Database validation check failed for {safe_name}: {e}")
            raise

        logger.info(f"Restoring database from snapshot {backup_path} to {self.db_path}...")

        # Ensure target directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Restore safely using SQLite backup API
        src_conn = sqlite3.connect(str(backup_path), timeout=15.0)
        try:
            dst_conn = sqlite3.connect(str(self.db_path), timeout=15.0)
            try:
                src_conn.backup(dst_conn)
                # Checkpoint WAL safely on destination
                dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        logger.info(f"Database successfully restored from {safe_name}")
        return True


# Global singleton instance
backup_service = BackupService()
