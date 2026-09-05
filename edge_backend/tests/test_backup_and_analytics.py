"""Unit tests for BackupService, database initialization, 32 supermarket cameras, and analytics endpoints."""

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
from app.routes.cameras import DEFAULT_CAMERAS
from app.services.auth_service import auth_service


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

def test_init_db_and_32_cameras():
    """Verify init_db creates all tables, seeds 32 supermarket cameras, and initializes recommendations."""
    async def _run():
        await init_db()

        async with async_session_factory() as session:
            # Check cameras count
            cam_count = (await session.execute(select(func.count(CameraModel.id)))).scalar()
            assert cam_count >= 32

            # Check default cameras presence
            for c in DEFAULT_CAMERAS:
                c_id = c.id if hasattr(c, "id") else c["id"]
                stmt = select(CameraModel).where(CameraModel.id == c_id)
                cam = (await session.execute(stmt)).scalar_one_or_none()
                assert cam is not None
                assert cam.location is not None

            # Check AI decision recommendations presence
            dec_count = (await session.execute(select(func.count(AIDecisionRecommendationModel.id)))).scalar()
            assert dec_count >= 1

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
    """Verify /api/v1/analytics/overview returns real database aggregated metrics."""
    res = client.get("/api/v1/analytics/overview", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "total_footfall" in data
    assert "conversion_rate" in data or "conversion_rate_percent" in data
    assert "daily_revenue" in data
    assert "active_shoppers" in data or "active_shoppers_now" in data


def test_analytics_decisions_crud_live_db(client, auth_headers):
    """Verify /api/v1/analytics/decisions filtering and action status updating."""
    # 1. Fetch decisions
    res = client.get("/api/v1/analytics/decisions", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    decisions = data.get("decisions", data) if isinstance(data, dict) else data
    assert len(decisions) >= 1
    target_id = decisions[0]["id"]

    # 2. Update decision status to APPLIED
    action_res = client.post(
        f"/api/v1/analytics/decisions/{target_id}/action",
        json={"status": "APPLIED", "notes": "Manager deployed team to checkout"},
        headers=auth_headers
    )
    assert action_res.status_code == 200
    action_data = action_res.json()
    updated = action_data.get("decision", action_data)
    assert updated["status"] == "APPLIED"


def test_cameras_route_32_cameras(client, auth_headers):
    """Verify /api/v1/cameras returns at least 32 configured supermarket feeds."""
    res = client.get("/api/v1/cameras", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data["cameras"]) >= 32
    # Verify key zones present
    locations = [c["location"] for c in data["cameras"]]
    assert any("Entrance" in loc for loc in locations)
    assert any("Checkout" in loc for loc in locations)
    assert any("Produce" in loc for loc in locations)
    assert any("Bakery" in loc for loc in locations)
    assert any("Dairy" in loc for loc in locations)
