"""Unit tests for Camera CRUD endpoints, position patching, department listing, and theft alert seeding."""

import pytest
import asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.config import settings
from app.database import async_session_factory, init_db
from app.models.db_models import CameraModel, TheftIncidentModel
from app.services.auth_service import auth_service


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_camera_crud_lifecycle(client, auth_headers):
    """Test creating a camera, updating attributes, patching position, listing departments, and deleting."""
    test_cam_id = "cam_test_bot_99"

    # 0. Clean up if leftover
    client.delete(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)

    # 1. Create camera
    new_cam_payload = {
        "id": test_cam_id,
        "name": "CAM-99: Robot Logistics & Aisle Rover",
        "location": "Aisle 15 - Rover Bay",
        "channel_number": 99,
        "department": "LOGISTICS",
        "rtsp_url": "rtsp://192.168.1.199:554/live",
        "webrtc_url": "http://localhost:8000/api/v1/webrtc/offer?camera_id=cam_test_bot_99",
        "status": "ONLINE",
        "fps": 30,
        "resolution": "1920x1080",
        "is_ai_enabled": True,
        "ai_models": ["yolov5n", "rover_tracker"],
        "floor_x": 420.5,
        "floor_y": 380.0,
        "floor_z": 2.8,
        "azimuth_deg": 180.0,
        "fov_deg": 90.0,
        "dvr_enabled": True,
        "dvr_retention_days": 7,
        "dvr_quota_gb": 50.0
    }
    create_res = client.post("/api/v1/cameras", json=new_cam_payload, headers=auth_headers)
    assert create_res.status_code == 200, create_res.text
    created_data = create_res.json()
    assert created_data["id"] == test_cam_id
    assert created_data["department"] == "LOGISTICS"
    assert created_data["floor_x"] == 420.5

    # 2. Get camera by ID
    get_res = client.get(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "CAM-99: Robot Logistics & Aisle Rover"

    # 3. List departments
    dept_res = client.get("/api/v1/cameras/departments/list", headers=auth_headers)
    assert dept_res.status_code == 200
    dept_data = dept_res.json()
    assert dept_data["status"] == "success"
    departments = {d["department"]: d["camera_count"] for d in dept_data["departments"]}
    assert "LOGISTICS" in departments
    assert departments["LOGISTICS"] >= 1

    # 4. Update camera attributes
    updated_payload = dict(new_cam_payload)
    updated_payload["name"] = "CAM-99: Upgraded Aisle Rover v2"
    updated_payload["fps"] = 60
    put_res = client.put(f"/api/v1/cameras/{test_cam_id}", json=updated_payload, headers=auth_headers)
    assert put_res.status_code == 200
    assert put_res.json()["name"] == "CAM-99: Upgraded Aisle Rover v2"
    assert put_res.json()["fps"] == 60

    # 5. Patch camera position
    pos_payload = {
        "floor_x": 512.0,
        "floor_y": 620.0,
        "floor_z": 3.0,
        "azimuth_deg": 225.0,
        "fov_deg": 80.0
    }
    patch_res = client.patch(f"/api/v1/cameras/{test_cam_id}/position", json=pos_payload, headers=auth_headers)
    assert patch_res.status_code == 200
    patch_data = patch_res.json()
    assert patch_data["status"] == "success"
    assert patch_data["floor_x"] == 512.0
    assert patch_data["azimuth_deg"] == 225.0

    # Verify patched position in GET
    get_patched = client.get(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert get_patched.status_code == 200
    assert get_patched.json()["floor_x"] == 512.0

    # 6. Verify floorplan dynamic reflection
    floorplan_res = client.get("/api/v1/analytics/floorplan", headers=auth_headers)
    assert floorplan_res.status_code == 200
    floor_data = floorplan_res.json()
    assert "cameras" in floor_data
    matched = next((c for c in floor_data["cameras"] if c["camera_id"] == test_cam_id), None)
    assert matched is not None
    assert matched["position_2d"]["x"] == 512.0
    assert "fov_polygon" in matched
    assert len(matched["fov_polygon"]) == 3

    # 7. Delete camera
    del_res = client.delete(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"

    # Verify 404 after delete
    get_deleted = client.get(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert get_deleted.status_code == 404


def test_theft_incident_seeding():
    """Verify TheftIncidentModel is seeded with active incidents during init_db."""
    async def _check():
        await init_db()
        async with async_session_factory() as session:
            theft_count = (await session.execute(select(func.count(TheftIncidentModel.id)))).scalar()
            assert theft_count >= 2

            # Check Liquor alert
            stmt_liq = select(TheftIncidentModel).where(TheftIncidentModel.department == "LIQUOR")
            liq_inc = (await session.execute(stmt_liq)).scalars().first()
            assert liq_inc is not None
            assert liq_inc.theft_type == "SHELF_SWEEPING"
            assert liq_inc.status == "ACTIVE"

            # Check Pharmacy alert
            stmt_pharm = select(TheftIncidentModel).where(TheftIncidentModel.theft_type == "CONCEALMENT")
            pharm_inc = (await session.execute(stmt_pharm)).scalars().first()
            assert pharm_inc is not None
            assert pharm_inc.confidence >= 0.85

    asyncio.run(_check())
