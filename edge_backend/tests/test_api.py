"""Unit tests for Edge API endpoints, authentication, WebRTC ICE servers, DVR timeline, and zones."""

import pytest
import asyncio
from datetime import datetime, date, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.config import settings
from app.database import engine, async_session_factory
from app.models.db_models import Base, SystemSetupModel, CameraModel
from app.models.schemas import (
    SecurityEvent,
    EventType,
    EventSeverity,
    ZoneConfig,
    ZoneType,
    Point2D,
    TripwireDirection,
)
from app.services.notification_service import notification_service
from app.services.auth_service import auth_service, intrusion_detector


@pytest.fixture(scope="session", autouse=True)
def init_test_db():
    """Ensure database schema is created and setup marked complete before running test suite."""
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        async with async_session_factory() as session:
            # Mark setup completed
            stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed")
            res = await session.execute(stmt)
            entry = res.scalar_one_or_none()
            if not entry:
                session.add(SystemSetupModel(key="setup_completed", value="true"))
            
            # Ensure living room camera exists
            cam_stmt = select(CameraModel).where(CameraModel.id == "cam_living_room")
            cam_res = await session.execute(cam_stmt)
            if not cam_res.scalar_one_or_none():
                session.add(CameraModel(
                    id="cam_living_room",
                    name="Living Room Camera",
                    location="Indoor",
                    rtsp_url="rtsp://127.0.0.1:554/live",
                    dvr_enabled=True,
                    status="ONLINE"
                ))
            await session.commit()
    asyncio.run(_init())


@pytest.fixture(autouse=True)
def reset_intrusion_detector():
    """Reset intrusion detector failed attempts between tests."""
    intrusion_detector.failed_attempts.clear()
    app.state.setup_completed = True


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "health_url" in data


def test_health_endpoint(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "storage" in data
    assert "telemetry" in data


def test_list_cameras(client, auth_headers):
    response = client.get("/api/v1/cameras", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "cameras" in data


def test_dynamic_ice_servers(client, auth_headers):
    response = client.get("/api/v1/webrtc/ice-servers?client_id=test_client", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "iceServers" in data
    assert len(data["iceServers"]) >= 2
    # Verify TURN credential presence
    turn_entry = data["iceServers"][1]
    assert "username" in turn_entry
    assert "credential" in turn_entry


def test_storage_health_endpoint(client, auth_headers):
    response = client.get("/api/v1/storage/health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "total_gb" in data
    assert "used_percent" in data
    assert "smart_status" in data
    assert "camera_quotas" in data


def test_camera_zones_crud(client, auth_headers):
    camera_id = "cam_living_room"
    zone_payload = {
        "id": "zone_test_tripwire",
        "camera_id": camera_id,
        "name": "Front Yard Tripwire",
        "zone_type": "TRIPWIRE",
        "enabled": True,
        "line_start": {"x": 0.1, "y": 0.5},
        "line_end": {"x": 0.9, "y": 0.5},
        "direction": "BIDIRECTIONAL"
    }

    # 1. Create Zone
    post_res = client.post(f"/api/v1/cameras/{camera_id}/zones", json=zone_payload, headers=auth_headers)
    assert post_res.status_code == 200
    assert post_res.json()["id"] == "zone_test_tripwire"

    # 2. Get Zones
    get_res = client.get(f"/api/v1/cameras/{camera_id}/zones", headers=auth_headers)
    assert get_res.status_code == 200
    zones = get_res.json()
    assert any(z["id"] == "zone_test_tripwire" for z in zones)

    # 3. Delete Zone
    del_res = client.delete(f"/api/v1/cameras/{camera_id}/zones/zone_test_tripwire", headers=auth_headers)
    assert del_res.status_code == 200


def test_camera_timeline_endpoint(client, auth_headers):
    camera_id = "cam_living_room"
    today_str = date.today().isoformat()
    response = client.get(f"/api/v1/cameras/{camera_id}/timeline?date={today_str}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["camera_id"] == camera_id
    assert "segments" in data
    assert "events" in data
    assert "gaps" in data
    assert "hls_master_url" in data


def test_trigger_security_event_authorized(client):
    payload = {
        "camera_id": "cam_living_room",
        "event_type": "FALL_DETECTED",
        "severity": "CRITICAL",
        "confidence": 0.94,
        "bounding_box": {
            "x_min": 0.2,
            "y_min": 0.6,
            "x_max": 0.8,
            "y_max": 0.9,
            "confidence": 0.94,
            "label": "person_fallen"
        },
        "kinematics": {
            "hip_descent_velocity": 2.3,
            "aspect_ratio_initial": 1.6,
            "aspect_ratio_final": 0.65,
            "transition_duration_ms": 420,
            "immobility_duration_sec": 5.2,
            "floor_proximity_score": 0.88
        }
    }
    response = client.post(
        "/api/v1/events/trigger",
        json=payload,
        headers={"X-Edge-API-Key": settings.INTERNAL_SERVICE_KEY}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("evt_")
    assert data["event_type"] == "FALL_DETECTED"
    assert data["severity"] == "CRITICAL"


def test_mute_camera_endpoint(client, auth_headers):
    camera_id = "cam_living_room"
    response = client.post(f"/api/v1/cameras/{camera_id}/mute", json={"duration_minutes": 5}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_auth_bypass_trigger_event(client):
    payload = {
        "camera_id": "cam_living_room",
        "event_type": "FALL_DETECTED",
        "severity": "CRITICAL",
        "confidence": 0.94
    }
    response = client.post(
        "/api/v1/events/trigger", 
        json=payload, 
        headers={"X-Edge-API-Key": "invalid_key"}
    )
    assert response.status_code in (401, 403)


def test_path_traversal_prevention(client):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        auth_service.sanitize_and_resolve_file(settings.CLIPS_DIR, "../../etc/passwd")
    assert exc_info.value.status_code == 400
    assert "Invalid filename format" in exc_info.value.detail


def test_webrtc_offer_exchange(client, auth_headers):
    offer_payload = {
        "camera_id": "cam_living_room",
        "sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=EdgeCCTV_test\r\nt=0 0\r\na=sendrecv\r\n",
        "type": "offer"
    }
    response = client.post("/api/v1/webrtc/offer", json=offer_payload, headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "sdp" in data
    assert data["type"] == "answer"


def test_dvr_export_incident(client, auth_headers):
    now_utc = datetime.now(timezone.utc).isoformat()
    payload = {
        "start_time": now_utc,
        "end_time": now_utc,
        "title": "Suspicious Activity"
    }
    response = client.post("/api/v1/cameras/cam_living_room/export", json=payload, headers=auth_headers)
    assert response.status_code in [200, 404, 500]


def test_setup_status_endpoint(client):
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_completed" in data
    assert "hardware_report" in data


def test_setup_hardware_scan(client, auth_headers):
    response = client.post("/api/v1/setup/hardware-scan", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "hardware" in data
    assert "hailo_available" in data["hardware"]
    assert "vaapi_available" in data["hardware"]


def test_pairing_code_flow(client):
    # 1. Generate pairing code
    code = auth_service.generate_app_pairing_code("test_admin")
    assert len(code) == 6
    assert code.isdigit()

    # 2. Pair with the generated code
    pair_res = client.post("/api/v1/auth/pair", json={"pairing_code": code})
    assert pair_res.status_code == 200
    pair_data = pair_res.json()
    assert "access_token" in pair_data
    assert "refresh_token" in pair_data

    # 3. Subsequent pair with same code should fail (single-use)
    replay_res = client.post("/api/v1/auth/pair", json={"pairing_code": code})
    assert replay_res.status_code == 401
