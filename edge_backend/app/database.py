"""Asynchronous SQLAlchemy SQLite database session manager."""

import logging
import time
import os
import sqlite3
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import event, text
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
