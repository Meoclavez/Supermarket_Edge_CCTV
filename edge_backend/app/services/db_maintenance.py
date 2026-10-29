"""Nightly database upkeep (audit 2026-10-29, L10 / M20).

Runs at 04:00 store time, after the 03:00 backup, in a worker thread with its
own short ``sqlite3`` transactions (never one long write lock):

1. **Track retention.** ``customer_tracks.trajectory_points`` (one point every
   0.5 s, up to 3600 per track) was the main reason the database grew about
   75 MB a day without limit. Hourly heatmap snapshots
   (``heatmap_snapshots``) keep the history, so the raw points are emptied
   (set to ``[]``) once a track is older than ``TRACK_POINTS_RETENTION_DAYS``
   (21 days, and never inside the heatmap backfill window). Whole rows go
   after ``TRACK_ROWS_RETENTION_DAYS`` (13 months: year-on-year comparisons
   still work). Freed pages are reused by new data; the file does not shrink
   without a VACUUM, which needs as much free space as the database and is a
   manual, quiet-hours job.
2. ``PRAGMA optimize`` (refresh the query planner's statistics) and
   ``PRAGMA wal_checkpoint(TRUNCATE)`` (shrink the -wal file).

Each pass stops after ``MAX_PASS_SECONDS`` and resumes the next night, so a
first run over months of history never holds the box busy for long.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("edge.db_maintenance")

BATCH_ROWS = 300
BATCH_PAUSE_S = 0.05
MAX_PASS_SECONDS = 20 * 60
RUN_HOUR = 4


def _points_retention_days() -> int:
    days = int(getattr(settings, "TRACK_POINTS_RETENTION_DAYS", 21) or 0)
    if days <= 0:
        return 0
    return max(days, int(getattr(settings, "HEATMAP_BACKFILL_DAYS", 14)) + 2)


def _rows_retention_days() -> int:
    return int(getattr(settings, "TRACK_ROWS_RETENTION_DAYS", 396) or 0)


def _naive_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=15.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


def run_once(db_path: Optional[Path] = None, *, now: Optional[datetime] = None,
             max_seconds: float = MAX_PASS_SECONDS, stop=None) -> dict:
    """One maintenance pass (blocking; call from a thread). Returns counts."""
    db = Path(db_path or settings.DATABASE_PATH)
    now = now or datetime.now(timezone.utc)
    out = {"points_cleared": 0, "rows_deleted": 0, "optimized": False, "checkpoint": None,
           "complete": True}
    if not db.exists():
        return out
    deadline = time.monotonic() + max_seconds
    conn = _connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "customer_tracks" in tables:
            def batches(sql: str, args: tuple, key: str) -> None:
                while True:
                    if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                        out["complete"] = False
                        return
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        n = conn.execute(sql, args).rowcount
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                    out[key] += max(n, 0)
                    if n < BATCH_ROWS:
                        return
                    time.sleep(BATCH_PAUSE_S)  # let the pipeline flush in between

            rows_days = _rows_retention_days()
            if rows_days > 0:
                cut = _naive_utc(now - timedelta(days=rows_days))
                batches("DELETE FROM customer_tracks WHERE rowid IN (SELECT rowid FROM customer_tracks "
                        f"WHERE start_time < ? ORDER BY start_time LIMIT {BATCH_ROWS})", (cut,), "rows_deleted")
            pts_days = _points_retention_days()
            if pts_days > 0:
                cut = _naive_utc(now - timedelta(days=pts_days))
                batches("UPDATE customer_tracks SET trajectory_points = '[]' WHERE rowid IN ("
                        "SELECT rowid FROM customer_tracks WHERE start_time < ? "
                        "AND trajectory_points IS NOT NULL AND trajectory_points != '[]' "
                        f"ORDER BY start_time LIMIT {BATCH_ROWS})", (cut,), "points_cleared")
        conn.execute("PRAGMA optimize")
        out["optimized"] = True
        out["checkpoint"] = list(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() or ())
    finally:
        conn.close()
    return out


def seconds_until(now_local: datetime, hour: int = RUN_HOUR) -> float:
    from app.services.backup_service import seconds_until_daily_backup
    return seconds_until_daily_backup(now_local, hour=hour)


class DatabaseMaintenance:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = None
        self.last: Optional[dict] = None

    async def start(self) -> None:
        import threading
        if self._task is None or self._task.done():
            self._stop = threading.Event()
            self._task = asyncio.create_task(self._run(), name="db-maintenance")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        from app.services.timeutil import local_now
        while True:
            await asyncio.sleep(seconds_until(local_now()))
            try:
                res = await asyncio.to_thread(run_once, stop=self._stop)
                self.last = {**res, "at": datetime.now(timezone.utc).isoformat()}
                logger.info(f"Database upkeep: cleared the raw points of {res['points_cleared']} old track(s), "
                            f"deleted {res['rows_deleted']} track(s) past retention"
                            + ("" if res["complete"] else "; continues tomorrow night"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed night must not stop later nights
                logger.error(f"Database upkeep failed: {exc}")


db_maintenance = DatabaseMaintenance()
