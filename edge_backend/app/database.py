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
    echo=settings.DEBUG,
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
    from app.models.db_models import Base
    from app.services.backup_service import backup_service
    from sqlalchemy import select, func

    logger.info("Initializing database schema...")
    def _run_migrations_sync(sync_conn):
        from sqlalchemy import text
        # 1. Ensure ai_decisions columns
        try:
            res = sync_conn.execute(text("PRAGMA table_info(ai_decisions);")).fetchall()
            cols = {row[1] for row in res}
            needed_cols = {
                "date": "VARCHAR(32)",
                "severity": "VARCHAR(32) DEFAULT 'MEDIUM'",
                "zone": "VARCHAR(128)",
                "finding": "VARCHAR(1024)",
                "root_cause": "VARCHAR(1024)",
                "action_item": "VARCHAR(1024)",
                "title": "VARCHAR(256)",
                "description": "VARCHAR(1024)",
                "impact": "VARCHAR(32) DEFAULT 'MEDIUM'",
                "confidence": "FLOAT DEFAULT 0.85",
                "action_type": "VARCHAR(64) DEFAULT 'OPEN_REGISTER'",
                "target_zone": "VARCHAR(128)",
                "payload_json": "JSON",
                "updated_at": "DATETIME",
                "applied_at": "DATETIME"
            }
            for col, col_type in needed_cols.items():
                if cols and col not in cols:
                    try:
                        sync_conn.execute(text(f"ALTER TABLE ai_decisions ADD COLUMN {col} {col_type};"))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Table ai_decisions migration check skipped: {e}")

        # 2. Ensure pos_transactions columns
        try:
            res_pos = sync_conn.execute(text("PRAGMA table_info(pos_transactions);")).fetchall()
            pos_cols = {row[1] for row in res_pos}
            if pos_cols:
                if "amount" not in pos_cols:
                    try:
                        sync_conn.execute(text("ALTER TABLE pos_transactions ADD COLUMN amount FLOAT DEFAULT 0.0;"))
                    except Exception:
                        pass
                if "total_amount" not in pos_cols:
                    try:
                        sync_conn.execute(text("ALTER TABLE pos_transactions ADD COLUMN total_amount FLOAT DEFAULT 0.0;"))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Table pos_transactions migration check skipped: {e}")

        # 3. Ensure cameras columns
        try:
            res_cam = sync_conn.execute(text("PRAGMA table_info(cameras);")).fetchall()
            cam_cols = {row[1] for row in res_cam}
            needed_cam_cols = {
                "channel_number": "INTEGER DEFAULT 1",
                "department": "VARCHAR(64) DEFAULT 'GENERAL'",
                "floor_x": "FLOAT DEFAULT 100.0",
                "floor_y": "FLOAT DEFAULT 100.0",
                "floor_z": "FLOAT DEFAULT 3.2",
                "azimuth_deg": "FLOAT DEFAULT 0.0",
                "fov_deg": "FLOAT DEFAULT 85.0",
                "homography_matrix": "JSON",
                "calibration_points": "JSON",
                "features": "JSON"
            }
            for col, col_type in needed_cam_cols.items():
                if cam_cols and col not in cam_cols:
                    try:
                        sync_conn.execute(text(f"ALTER TABLE cameras ADD COLUMN {col} {col_type};"))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Table cameras migration check skipped: {e}")

        # 4. Ensure theft_incidents columns
        try:
            res_theft = sync_conn.execute(text("PRAGMA table_info(theft_incidents);")).fetchall()
            theft_cols = {row[1] for row in res_theft}
            needed_theft_cols = {
                "camera_name": "VARCHAR(128) DEFAULT 'Camera'",
                "shelf_zone_id": "VARCHAR(64)",
                "evidence_summary": "VARCHAR(1024) DEFAULT ''",
                "snapshot_path": "VARCHAR(512)",
                "clip_path": "VARCHAR(512)",
                "officer_notes": "VARCHAR(1024)",
                "updated_at": "DATETIME"
            }
            for col, col_type in needed_theft_cols.items():
                if theft_cols and col not in theft_cols:
                    try:
                        sync_conn.execute(text(f"ALTER TABLE theft_incidents ADD COLUMN {col} {col_type};"))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Table theft_incidents migration check skipped: {e}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_migrations_sync)

    # NOTE: No camera / recommendation / theft-incident seeding happens here.
    # Cameras are created only by network discovery or explicit operator action,
    # and analytics records are written only by the live pipeline. An empty
    # database must stay empty rather than materialise fabricated demo rows.
    await _ensure_default_store_layout()


    # Trigger startup backup and prune old backups
    try:
        backup_res = backup_service.create_backup("startup")
        logger.info(f"Startup backup created: {backup_res.get('filename')}")
        pruned_count = backup_service.prune_backups(keep_days=7, min_keep=3)
        if pruned_count > 0:
            logger.info(f"Pruned {pruned_count} old backups during startup.")
    except Exception as e:
        logger.error(f"Startup backup or prune error: {e}")

