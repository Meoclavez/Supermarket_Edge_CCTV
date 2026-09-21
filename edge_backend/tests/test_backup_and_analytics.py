"""Unit tests for BackupService, database initialization and the analytics endpoints.

``init_db`` used to seed 32 fabricated supermarket cameras, a handful of "AI
decisions" and two theft incidents, and the tests below used to require them.
Seeding was removed: a fresh install now starts genuinely empty. These tests
pin that.
"""

import os
import pytest
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.config import settings
from app.database import engine, async_session_factory, init_db
from app.models.db_models import (
    Base,
    CameraModel,
    AIDecisionRecommendationModel,
    CustomerTrackModel,
    POSTransactionModel,
    ShelfInteractionModel
)
from app.services.backup_service import backup_service
from app.services.auth_service import auth_service
from app.models.db_models import TheftIncidentModel

# The camera route used to keep a module-level DEFAULT_CAMERAS list that it
# appended to and fell back on. The name no longer exists at all.
import app.routes.cameras as _cameras_module

assert not hasattr(_cameras_module, "DEFAULT_CAMERAS")
assert not hasattr(_cameras_module, "CURRENT_ACTIVE_SOURCE")


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_backup_service_lifecycle(tmp_path):
    """Test safe SQLite online backup creation, listing, pruning, and safe restoration."""
    test_db = tmp_path / "test_source.db"
    with sqlite3.connect(str(test_db)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, value TEXT);")
        conn.execute("INSERT INTO test_data (value) VALUES ('hello_backup');")
        conn.commit()

    test_backups_dir = tmp_path / "backups"
    test_backups_dir.mkdir(parents=True, exist_ok=True)

    from app.services.backup_service import BackupService
    service = BackupService(backups_dir=test_backups_dir, db_path=test_db)

    # 1. Create backup
    res = service.create_backup(tag="test_tag")
    assert res["status"] == "success"
    assert "test_tag" in res["filename"]
    assert res["size_bytes"] > 0
    assert os.path.exists(res["filepath"])

    # 2. List backups
    backups = service.list_backups()
    assert len(backups) == 1
    assert backups[0]["filename"] == res["filename"]
    assert backups[0]["size_bytes"] == res["size_bytes"]

    # 3. Create second and third backups
    service.create_backup(tag="tag2")
    service.create_backup(tag="tag3")
    backups = service.list_backups()
    assert len(backups) == 3

    # 4. Prune backups (keep min 2)
    pruned = service.prune_backups(keep_days=0, min_keep=2)
    assert pruned == 1
    assert len(service.list_backups()) == 2

    # 5. Restore backup
    target_restore_db = tmp_path / "restored.db"
    restore_service = BackupService(backups_dir=test_backups_dir, db_path=target_restore_db)
    valid_filename = service.list_backups()[0]["filename"]
    success = restore_service.restore_backup(valid_filename)
    assert success is True

    # Verify restored data
    with sqlite3.connect(str(target_restore_db)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM test_data WHERE id=1;")
        row = cursor.fetchone()
        assert row[0] == "hello_backup"

    # Path traversal protection test
    with pytest.raises(ValueError):
        service.restore_backup("../../etc/passwd")


import asyncio

def test_init_db_seeds_no_cameras():
    """init_db creates the schema and seeds nothing.

    Formerly this asserted ``cam_count >= 32`` and walked a DEFAULT_CAMERAS
    literal of 32 invented supermarket feeds. Those cameras never existed; the
    list is now empty and init_db inserts no rows at all. The test counts
    before and after so it is independent of what earlier tests created.
    """
    async def _run():
        # Create the schema without going through init_db, so the "before"
        # counts are what a genuinely fresh install holds.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with async_session_factory() as session:
            async def count(model):
                return (await session.execute(select(func.count()).select_from(model))).scalar()

            before = {
                "cameras": await count(CameraModel),
                "decisions": await count(AIDecisionRecommendationModel),
                "thefts": await count(TheftIncidentModel),
            }

        await init_db()

        async with async_session_factory() as session:
            async def count(model):
                return (await session.execute(select(func.count()).select_from(model))).scalar()

            assert await count(CameraModel) == before["cameras"]
            assert await count(AIDecisionRecommendationModel) == before["decisions"]
            assert await count(TheftIncidentModel) == before["thefts"]

            # The specific fabrications are gone and cannot come back.
            for fabricated in ("cam_entrance_main", "cam_checkout_01", "cam_produce_01"):
                assert (await session.execute(
                    select(CameraModel).where(CameraModel.id == fabricated)
                )).scalar_one_or_none() is None

            seeded_decisions = (await session.execute(
                select(AIDecisionRecommendationModel).where(
                    AIDecisionRecommendationModel.id.in_(
                        ["dec_01", "dec_02", "dec_03", "dec_04", "dec_05"]
                    )
                )
            )).scalars().all()
            assert seeded_decisions == []

    asyncio.run(_run())


def test_system_backup_api_endpoints(client, auth_headers):
    """Verify /api/v1/system/backups, /api/v1/system/backup, and /api/v1/system/restore/{filename}."""
    # 1. Trigger manual backup
    create_res = client.post("/api/v1/system/backup", json={"tag": "api_test"}, headers=auth_headers)
    assert create_res.status_code == 200
    create_data = create_res.json()
    assert create_data["status"] == "success"
    filename = create_data["filename"]
    assert "api_test" in filename

    # 2. List backups
    list_res = client.get("/api/v1/system/backups", headers=auth_headers)
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] >= 1
    assert any(b["filename"] == filename for b in list_data["backups"])

    # 3. Restore snapshot
    restore_res = client.post(f"/api/v1/system/restore/{filename}", headers=auth_headers)
    assert restore_res.status_code == 200
    assert restore_res.json()["status"] == "success"

    # 4. Restore nonexistent snapshot fails with 404
    bad_res = client.post("/api/v1/system/restore/edge_cctv_99999999_999999_none.db", headers=auth_headers)
    assert bad_res.status_code == 404


def test_analytics_overview_live_db(client, auth_headers):
    """The overview aggregates the live database and admits what it cannot see."""
    res = client.get("/api/v1/analytics/overview", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()

    assert set(data) == {
        "store_name", "timestamp", "window", "today_footfall", "active_shoppers_now",
        "avg_dwell_seconds", "avg_dwell_minutes", "daily_revenue", "transactions",
        "conversion_rate_pct", "zones_total", "zones_with_data", "top_zones",
        "coverage", "has_data", "pos_connected",
    }
    # Renamed/removed because they reported numbers nothing measured.
    for gone in ("total_footfall", "conversion_rate", "conversion_rate_percent",
                 "active_shoppers", "store_id", "hot_zones", "edge_uptime_percent"):
        assert gone not in data

    # Each metric is a real measurement or an explicit "not observed".
    for key in ("today_footfall", "daily_revenue", "conversion_rate_pct",
                "avg_dwell_minutes", "transactions"):
        assert data[key] is None or isinstance(data[key], (int, float)), key

    assert data["coverage"]["cameras_total"] >= data["coverage"]["cameras_calibrated"]
    # Footfall cannot be claimed without a camera calibrated to the floor.
    if not data["coverage"]["floor_tracking_possible"]:
        assert data["today_footfall"] is None


def test_analytics_decisions_crud_live_db(client, auth_headers):
    """Status mutation works on a row that the test itself creates.

    The recommendation table is empty until the rule engine writes to it, so
    there is no seeded ``dec_01`` to pick up any more.
    """
    row_id = "dec_live_db_under_test"

    async def _delete():
        async with async_session_factory() as session:
            row = await session.get(AIDecisionRecommendationModel, row_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def _insert():
        async with async_session_factory() as session:
            session.add(AIDecisionRecommendationModel(
                id=row_id,
                date=datetime.now(timezone.utc).date().isoformat(),
                category="STAFFING",
                severity="HIGH",
                zone="zone_checkout_under_test",
                finding="Queue exceeded target wait",
                root_cause="Two lanes closed during peak",
                action_item="Open a third lane",
                status="PENDING",
            ))
            await session.commit()

    asyncio.run(_delete())
    asyncio.run(_insert())
    try:
        res = client.get("/api/v1/analytics/decisions", headers=auth_headers)
        assert res.status_code == 200
        decisions = res.json()["decisions"]
        assert any(d["id"] == row_id for d in decisions)

        action_res = client.post(
            f"/api/v1/analytics/decisions/{row_id}/action",
            json={"status": "APPLIED", "notes": "Manager deployed team to checkout"},
            headers=auth_headers,
        )
        assert action_res.status_code == 200
        assert action_res.json()["decision"]["status"] == "APPLIED"

        async def _status():
            async with async_session_factory() as session:
                return (await session.get(AIDecisionRecommendationModel, row_id)).status

        assert asyncio.run(_status()) == "APPLIED"
    finally:
        asyncio.run(_delete())


def test_cameras_route_empty_by_default(client, auth_headers):
    """/api/v1/cameras returns exactly the configured cameras -- and by default none.

    This used to require 32 feeds and to assert that Entrance, Checkout,
    Produce, Bakery and Dairy zones were all present. Not one of those cameras
    existed on any network; they were a literal in the source. The route now
    mirrors the database, so the assertion is that it adds nothing of its own.
    """

    async def _db_cameras():
        async with async_session_factory() as session:
            return {c.id: c.location for c in (await session.execute(select(CameraModel))).scalars()}

    db_cameras = asyncio.run(_db_cameras())

    res = client.get("/api/v1/cameras", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()

    assert data["total"] == len(data["cameras"]) == len(db_cameras)
    assert {c["id"] for c in data["cameras"]} == set(db_cameras)

    # Every location served is the one stored, verbatim.
    for cam in data["cameras"]:
        assert cam["location"] == db_cameras[cam["id"]]

    # None of the 32 invented supermarket feeds survive anywhere.
    for fabricated in ("cam_entrance_main", "cam_checkout_01", "cam_produce_01",
                       "cam_bakery_01", "cam_dairy_01"):
        assert fabricated not in db_cameras
