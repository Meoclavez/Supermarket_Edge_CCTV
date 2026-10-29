"""Asynchronous SQLAlchemy SQLite database session manager."""

import logging
import time
import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event, text, select, func
from sqlalchemy.pool import QueuePool
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from app.config import settings

logger = logging.getLogger("Database")

DATABASE_URL = f"sqlite+aiosqlite:///{settings.DATABASE_PATH.resolve()}"

# Configure engine with connection pool, timeout, and recycle
engine = create_async_engine(
    DATABASE_URL,
    echo=settings.SQL_ECHO,
    connect_args={
        "check_same_thread": False,
        "timeout": 15.0  # Busy timeout
    },
    pool_pre_ping=True,
    pool_recycle=3600,
)

def _page_cache_kib() -> int:
    """Per-connection page cache: 16 MiB on boxes with >= 12 GB RAM, else 8 MiB."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        total = 0
    return 16384 if total >= 12 * 1024 ** 3 else 8192


_CACHE_KIB = _page_cache_kib()
# The WAL file is truncated back to this size after a checkpoint, so one large
# transaction (layout fit, retention) does not leave a multi-GB -wal behind.
WAL_SIZE_LIMIT = 64 * 1024 * 1024


def apply_connection_pragmas(cursor) -> None:
    """PRAGMAs every connection to the main database should carry (also ad-hoc sqlite3 ones)."""
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
    cursor.execute(f"PRAGMA cache_size=-{_CACHE_KIB}")
    cursor.execute("PRAGMA temp_store=MEMORY")


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    apply_connection_pragmas(cursor)
    cursor.close()

async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

_startup_backup_task = None


async def _startup_backup(skip_copy: bool = False, delay_s: float = 30.0) -> None:
    """Background startup snapshot + retention (see init_db)."""
    from app.services.backup_service import backup_service

    try:
        await asyncio.sleep(delay_s)  # let cameras and the API come up first
        if skip_copy:
            logger.info("Startup backup skipped: this start already made a pre-upgrade backup.")
        else:
            backup_res = await asyncio.to_thread(backup_service.create_backup, "startup")
            if backup_res.get("status") == "skipped":
                logger.info(f"Startup backup skipped ({backup_res.get('reason')}): "
                            f"{backup_res.get('duplicate_of') or backup_res.get('detail') or 'no source database'}")
            else:
                logger.info(f"Startup backup created: {backup_res.get('filename')}")
        retention = await asyncio.to_thread(backup_service.apply_retention)
        if retention["deleted"] or retention["compressed"]:
            logger.info(f"Backup retention: deleted {len(retention['deleted'])}, "
                        f"compressed {len(retention['compressed'])}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Startup backup or prune error: {e}")


async def stop_background_tasks() -> None:
    """Cancel the background startup backup (shutdown)."""
    task = _startup_backup_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def check_db_health():
    """Periodic connection health check."""
    while True:
        try:
            async with async_session_factory() as session:
                start_time = time.time()
                await session.execute(text("SELECT 1"))
                latency = (time.time() - start_time) * 1000
                
                db_size = 0
                if settings.DATABASE_PATH.exists():
                    db_size = os.path.getsize(settings.DATABASE_PATH) / (1024 * 1024)
                
                pool_status = engine.pool.status() if hasattr(engine.pool, "status") else "Unknown"
                
                logger.info(f"DB Health: Latency={latency:.2f}ms, Size={db_size:.2f}MB, Pool={pool_status}")
        except Exception as e:
            logger.error(f"DB Health Check Failed: {e}")
        await asyncio.sleep(60)

async def get_db():
    """FastAPI dependency: one async session per request, rolled back on error.

    A dependency with ``yield`` may yield exactly once. The old version caught
    ``database is locked`` here, slept and yielded a second session: FastAPI
    then failed with "generator didn't stop after athrow()" (a 500 after a
    pointless 0.5 s wait) and nothing was retried -- the request had already
    run. Work that must survive lock contention retries itself as one whole
    transaction (e.g. ``store_layout_service._site_change``).
    """
    async with async_session_factory() as session:
        try:
            yield session
        except Exception as e:
            if isinstance(e, OperationalError) and "database is locked" in str(e):
                logger.warning(f"Request failed on a locked database: {e.orig if e.orig else e}")
            try:
                await session.rollback()
            except Exception as rb:  # noqa: BLE001 - keep the original error
                logger.debug(f"rollback after request error failed: {rb}")
            raise


async def _ensure_default_store_layout():
    """Create one empty store layout if none exists.

    This is configuration, not data: it defines the physical extent of the
    premises so the blueprint editor has a canvas to draw on. It contains no
    zones and no metrics -- the operator draws the real store outline.
    """
    from app.models.db_models import StoreLayoutModel
    from sqlalchemy import select, func

    try:
        async with async_session_factory() as session:
            count = await session.scalar(select(func.count(StoreLayoutModel.id)))
            if count:
                return
            session.add(StoreLayoutModel(
                id="layout_default",
                store_id=settings.STORE_ID,
                name="Store Floor",
                width_m=settings.DEFAULT_STORE_WIDTH_M,
                height_m=settings.DEFAULT_STORE_HEIGHT_M,
                is_active=True,
            ))
            await session.commit()
            logger.info("Created empty default store layout (no zones).")
    except Exception as e:
        logger.error(f"Failed to ensure default store layout: {e}")


async def init_db():
    """Initialize database schema and trigger startup backup.

    Deliberately seeds no cameras, recommendations or incidents.
    """
    from app.services.backup_service import backup_service
    from sqlalchemy import select, func

    logger.info("Initializing database schema...")
    # Versioned migrations (app/migrations/mNNNN_*.py) followed by the additive
    # reconcile against the models. Runs under a file lock, backs up a database
    # holding data before changing it, and raises SchemaTooNewError -- refusing
    # to start -- if the file was migrated by a newer build.
    from app.migrations import run_migrations

    report = await asyncio.to_thread(run_migrations, settings.DATABASE_PATH)
    reconcile = report.get("reconcile") or {}
    logger.info(
        f"Database schema v{report.get('to_version')} (head v{report.get('head')}); "
        f"migrations applied: {report.get('applied') or 'none'}; "
        f"reconcile added: {reconcile.get('applied') or 'none'}"
    )

    # NOTE: No camera / recommendation / theft-incident seeding happens here.
    # Cameras are created only by network discovery or explicit operator action,
    # and analytics records are written only by the live pipeline. An empty
    # database must stay empty rather than materialise fabricated demo rows.
    await _ensure_default_store_layout()


    # Startup snapshot (skipped when the database is unchanged since the newest
    # backup, or when this start already took a pre-migration copy), then
    # retention. Runs in the background after the app starts serving: on a
    # 2.8 GB database it took 1-2 minutes of every start (twice after an upgrade).
    global _startup_backup_task
    took_copy = bool(report.get("backup"))
    _startup_backup_task = asyncio.create_task(_startup_backup(skip_copy=took_copy),
                                               name="startup-backup")


