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

@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

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
    """FastAPI dependency for obtaining async database sessions with automatic retry."""
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        async with async_session_factory() as session:
            try:
                yield session
                break
            except OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"Database locked, retrying ({attempt + 1}/{max_retries})...")
                    await session.rollback()
                    await asyncio.sleep(retry_delay)
                else:
                    await session.rollback()
                    raise
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()


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


    # Startup snapshot (skipped when the content equals the newest backup),
    # then retention: last N startup + one per day for 14 days, pre-migration
    # and operator backups kept, older snapshots gzip-compressed.
    try:
        backup_res = await asyncio.to_thread(backup_service.create_backup, "startup")
        if backup_res.get("status") == "skipped":
            logger.info(f"Startup backup skipped ({backup_res.get('reason')}): "
                        f"{backup_res.get('duplicate_of') or 'no source database'}")
        else:
            logger.info(f"Startup backup created: {backup_res.get('filename')}")
        retention = await asyncio.to_thread(backup_service.apply_retention)
        if retention["deleted"] or retention["compressed"]:
            logger.info(f"Backup retention: deleted {len(retention['deleted'])}, "
                        f"compressed {len(retention['compressed'])}")
    except Exception as e:
        logger.error(f"Startup backup or prune error: {e}")

