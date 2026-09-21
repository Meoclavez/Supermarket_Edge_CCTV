"""Unit tests for Camera CRUD, position patching, department listing, and theft incidents.

The floor plan is now a metric blueprint: a camera is placed at ``floor_x`` /
``floor_y`` metres and drawn from its own azimuth and FOV. The old
``position_2d`` pixel pair and the pre-computed ``fov_polygon`` triangle are
gone. Theft incidents are no longer seeded either.
"""

import pytest
import asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.main import app
from app.config import settings
from app.database import async_session_factory, engine, init_db
from app.models.db_models import Base, CameraModel, TheftIncidentModel
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

    # 6. Verify floorplan dynamic reflection (metres, not a pixel pair)
    floorplan_res = client.get("/api/v1/analytics/floorplan", headers=auth_headers)
    assert floorplan_res.status_code == 200
    floor_data = floorplan_res.json()
    assert floor_data["units"] == "metres"
    matched = next((c for c in floor_data["cameras"] if c["camera_id"] == test_cam_id), None)
    assert matched is not None

    assert matched["floor_x"] == 512.0
    assert matched["floor_y"] == 620.0
    assert matched["floor_z"] == 3.0
    assert matched["azimuth_deg"] == 225.0
    assert matched["fov_deg"] == 80.0
    assert matched["name"] == "CAM-99: Upgraded Aisle Rover v2"
    assert matched["department"] == "LOGISTICS"

    # The client draws the cone from azimuth + FOV; the server no longer ships
    # a pre-baked triangle, and there is no pixel-space position any more.
    assert "position_2d" not in matched
    assert "fov_polygon" not in matched

    # Without a homography this camera cannot place anyone on the floor, and
    # the payload says so rather than implying coverage it does not have.
    assert matched["has_homography"] is False

    # 7. Delete camera
    del_res = client.delete(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "success"

    # Verify 404 after delete
    get_deleted = client.get(f"/api/v1/cameras/{test_cam_id}", headers=auth_headers)
    assert get_deleted.status_code == 404


def test_theft_incidents_not_seeded():
    """The theft log starts empty and only fills from real detections.

    This used to require two seeded incidents -- a LIQUOR "shelf sweeping" and
    a PHARMACY "concealment" -- that no camera had ever observed. init_db seeds
    nothing now; an incident has to be produced by the detector. The
    /simulate endpoint that invented one on demand is gone.
    """
    async def _check():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with async_session_factory() as session:
            before = (await session.execute(
                select(func.count()).select_from(TheftIncidentModel)
            )).scalar()

        await init_db()

        async with async_session_factory() as session:
            after = (await session.execute(
                select(func.count()).select_from(TheftIncidentModel)
            )).scalar()
            assert after == before

            # The two fabricated alerts are gone.
            fabricated = (await session.execute(
                select(TheftIncidentModel).where(
                    TheftIncidentModel.theft_type.in_(["SHELF_SWEEPING", "CONCEALMENT"]),
                    TheftIncidentModel.department.in_(["LIQUOR", "PHARMACY"]),
                    TheftIncidentModel.person_track_id.is_(None),
                )
            )).scalars().all()
            assert fabricated == []

    asyncio.run(_check())


def test_theft_simulate_endpoint_removed(client, auth_headers):
    """No route may invent an incident. Only a detector write creates one."""
    async def _count():
        async with async_session_factory() as session:
            return (await session.execute(
                select(func.count()).select_from(TheftIncidentModel)
            )).scalar()

    before = asyncio.run(_count())
    res = client.post("/api/v1/theft/simulate", headers=auth_headers)
    assert res.status_code in (404, 405), res.text
    assert asyncio.run(_count()) == before
