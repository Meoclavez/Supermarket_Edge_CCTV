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


async def init_db():
    """Initialize database schema, seed cameras & initial AI recommendations, and trigger startup backup."""
    from app.models.db_models import Base, CameraModel, AIDecisionRecommendationModel
    from app.routes.cameras import DEFAULT_CAMERAS
    from app.services.backup_service import backup_service
    from sqlalchemy import select

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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_migrations_sync)

    async with async_session_factory() as session:
        # Check and sync all 32 cameras from DEFAULT_CAMERAS
        try:
            res = await session.execute(select(CameraModel))
            existing_cams = res.scalars().all()
            existing_ids = {c.id for c in existing_cams}
            missing_cams = [c for c in DEFAULT_CAMERAS if (c.id if hasattr(c, "id") else c["id"]) not in existing_ids]
            if missing_cams:
                logger.info(f"Syncing {len(missing_cams)} supermarket cameras into SQLite DB...")
                valid_cols = set(CameraModel.__table__.columns.keys())
                for c in missing_cams:
                    c_data = c.model_dump() if hasattr(c, "model_dump") else (dict(c) if isinstance(c, dict) else c.__dict__)
                    filtered = {k: (v.value if hasattr(v, "value") else v) for k, v in c_data.items() if k in valid_cols}
                    session.add(CameraModel(**filtered))
                await session.commit()
                logger.info(f"All {len(DEFAULT_CAMERAS)} supermarket cameras synchronized in database.")
        except Exception as e:
            logger.error(f"Failed to seed cameras: {e}")
            await session.rollback()

        # Check AIDecisionRecommendationModel
        try:
            dec_res = await session.execute(select(AIDecisionRecommendationModel))
            existing_decisions = dec_res.scalars().all()
            if not existing_decisions:
                logger.info("Initializing fresh initial supermarket store recommendations...")
                now_date = datetime.utcnow().strftime("%Y-%m-%d")
                initial_decisions = [
                    AIDecisionRecommendationModel(
                        id="rec_queue_pos_01",
                        date=now_date,
                        category="STAFFING",
                        severity="CRITICAL",
                        zone="zone_pos",
                        finding="Checkout Queue SLA Threshold Exceeded (avg wait 4.8 mins).",
                        root_cause="Peak footfall wave from Bakery & Produce causing self-checkout backup.",
                        action_item="Activate Express Register 5 and dispatch floor associate to assist self-scan.",
                        title="Open Checkout Lanes 3 & 4 (Rush Predicted)",
                        description="Shopper count in Aisles 1-3 increased by 45%. Projected checkout wait time exceeds 4 minutes within 10 minutes.",
                        impact="HIGH",
                        confidence=0.92,
                        action_type="OPEN_REGISTER",
                        status="PENDING",
                        target_zone="Checkout",
                        payload_json={"lanes": ["cam_pos_lane_3", "cam_pos_lane_4"], "trigger": "queue_prediction"}
                    ),
                    AIDecisionRecommendationModel(
                        id="rec_stockout_dairy_02",
                        date=now_date,
                        category="INVENTORY",
                        severity="HIGH",
                        zone="zone_aisle_12",
                        finding="Shelf fill rate below 15% for Organic Whole Milk.",
                        root_cause="High morning traffic exhausted front display shelves.",
                        action_item="Dispatch stockroom associate to replenish dairy shelf.",
                        title="Restock Organic Whole Milk (Shelf A3-2)",
                        description="Shelf face stock below 15% with 18 customer picks in the last hour. Immediate restock recommended to prevent lost sales.",
                        impact="HIGH",
                        confidence=0.89,
                        action_type="DISPATCH_RESTOCK",
                        status="PENDING",
                        target_zone="Aisle 3 - Dairy",
                        payload_json={"shelf_id": "shelf_dairy_02", "sku": "DAIRY-ORG-MILK-1GAL"}
                    ),
                    AIDecisionRecommendationModel(
                        id="rec_promo_bakery_03",
                        date=now_date,
                        category="MERCHANDISING",
                        severity="MEDIUM",
                        zone="zone_bakery",
                        finding="High shopper dwell (>28s) detected with low pick rate (14%).",
                        root_cause="Price tags obscured by promotional banner.",
                        action_item="Reposition eye-level shelf price tags & bundle artisanal baguettes with fine cheese.",
                        title="Endcap Feature: Bundle Artisanal Baguettes with Fine Cheese",
                        description="Dwell time in Bakery increased to 58s with 22% conversion. Cross-merchandising with Specialty Cheese will increase basket size.",
                        impact="MEDIUM",
                        confidence=0.84,
                        action_type="UPDATE_PROMOTION",
                        status="PENDING",
                        target_zone="Aisle 2 - Bakery",
                        payload_json={"target_shelf": "shelf_bakery_endcap", "recommended_discount": 0.15}
                    ),
                    AIDecisionRecommendationModel(
                        id="rec_security_liquor_04",
                        date=now_date,
                        category="LOSS_PREVENTION",
                        severity="HIGH",
                        zone="zone_liquor",
                        finding="Shopper Dwell: 3m 45s near premium spirits cabinet with zero staff presence.",
                        root_cause="Unattended lockable spirits cabinet creates loitering blindspot.",
                        action_item="Trigger customer greeting prompt via staff earpiece / visual deterrence display.",
                        title="Audit Security Tagging on Top-Shelf Bourbon",
                        description="Dwell-without-pick pattern flagged by AI vision in Fine Spirits section. EAS sensor verification advised.",
                        impact="MEDIUM",
                        confidence=0.81,
                        action_type="AUDIT_SECURITY",
                        status="PENDING",
                        target_zone="Aisle 9 - Liquor",
                        payload_json={"section": "Rare Spirits Shelf 1"}
                    ),
                    AIDecisionRecommendationModel(
                        id="rec_energy_warehouse_05",
                        date=now_date,
                        category="ENERGY",
                        severity="LOW",
                        zone="zone_warehouse",
                        finding="Zero worker presence in Logistics Staging for 45+ minutes.",
                        root_cause="Shift changeover window completed.",
                        action_item="Switch high-bay luminaires to eco-dimming mode.",
                        title="Dim Lighting in High-Bay Warehouse Staging",
                        description="No personnel detected in Logistics Staging for 45+ minutes. Switching to eco-lighting saves 1.8 kWh/hour.",
                        impact="LOW",
                        confidence=0.95,
                        action_type="ECO_LIGHTING",
                        status="PENDING",
                        target_zone="Warehouse",
                        payload_json={"zone": "High-Bay Staging", "dim_level": 30}
                    ),
                ]
                for dec in initial_decisions:
                    session.add(dec)
                await session.commit()
                logger.info("AI store recommendations successfully initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize store recommendations: {e}")
            await session.rollback()

    # Trigger startup backup and prune old backups
    try:
        backup_res = backup_service.create_backup("startup")
        logger.info(f"Startup backup created: {backup_res.get('filename')}")
        pruned_count = backup_service.prune_backups(keep_days=7, min_keep=3)
        if pruned_count > 0:
            logger.info(f"Pruned {pruned_count} old backups during startup.")
    except Exception as e:
        logger.error(f"Startup backup or prune error: {e}")

