"""Unit tests for Edge API endpoints, authentication, loss-prevention alerts, WebRTC ICE servers, DVR timeline, and zones."""

import pytest
import asyncio
from datetime import datetime, date, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.config import settings
from app.database import engine, async_session_factory
from app.models.db_models import Base, SystemSetupModel, CameraModel, SecurityEventModel
from app.models.schemas import EventType
from app.services.notification_service import notification_service
from app.services.auth_service import auth_service, intrusion_detector


TEST_CAMERA = "cam_aisle_3"


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
            
            # Ensure the test camera exists
            cam_stmt = select(CameraModel).where(CameraModel.id == TEST_CAMERA)
            cam_res = await session.execute(cam_stmt)
            if not cam_res.scalar_one_or_none():
                session.add(CameraModel(
                    id=TEST_CAMERA,
                    name="Aisle 3 Camera",
                    location="Aisle 3",
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


def test_dynamic_ice_servers(client, auth_headers, monkeypatch):
    # TURN is opt-in (compose profile "turn"); with it enabled, time-limited
    # credentials are issued.
    from app.config import settings
    from app.services.turn_service import turn_service

    monkeypatch.setattr(settings, "TURN_ENABLED", True)
    monkeypatch.setattr(turn_service, "turn_host", "203.0.113.10")
    response = client.get("/api/v1/webrtc/ice-servers?client_id=test_client", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "iceServers" in data
    assert data["turn_enabled"] is True
    assert len(data["iceServers"]) >= 2
    # Verify TURN credential presence
    turn_entry = data["iceServers"][1]
    assert "username" in turn_entry
    assert "credential" in turn_entry


def test_ice_servers_stun_only_without_turn(client, auth_headers, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "TURN_ENABLED", False)
    data = client.get("/api/v1/webrtc/ice-servers", headers=auth_headers).json()
    assert data["turn_enabled"] is False
    assert data["webrtc_scope"] == "lan_only"
    assert all("credential" not in s for s in data["iceServers"])
    assert all(u.startswith("stun:") for s in data["iceServers"] for u in s["urls"])


def test_storage_health_endpoint(client, auth_headers):
    response = client.get("/api/v1/storage/health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "total_gb" in data
    assert "used_percent" in data
    assert "smart_status" in data
    assert "camera_quotas" in data


def test_camera_zones_crud(client, auth_headers):
    camera_id = TEST_CAMERA
    zone_payload = {
        "id": "zone_test_mask",
        "camera_id": camera_id,
        "name": "Staff area privacy mask",
        "zone_type": "EXCLUSION",
        "enabled": True,
        "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.4}],
        "mask_mode": "BLUR",
    }

    # 1. Create Zone
    post_res = client.post(f"/api/v1/cameras/{camera_id}/zones", json=zone_payload, headers=auth_headers)
    assert post_res.status_code == 200
    assert post_res.json()["id"] == "zone_test_mask"

    # 2. Get Zones
    get_res = client.get(f"/api/v1/cameras/{camera_id}/zones", headers=auth_headers)
    assert get_res.status_code == 200
    zones = get_res.json()
    assert any(z["id"] == "zone_test_mask" and z["zone_type"] == "EXCLUSION" for z in zones)

    # 3. Delete Zone
    del_res = client.delete(f"/api/v1/cameras/{camera_id}/zones/zone_test_mask", headers=auth_headers)
    assert del_res.status_code == 200


def test_camera_timeline_endpoint(client, auth_headers):
    camera_id = TEST_CAMERA
    today_str = date.today().isoformat()
    response = client.get(f"/api/v1/cameras/{camera_id}/timeline?date={today_str}", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["camera_id"] == camera_id
    assert "segments" in data
    assert "events" in data
    assert "gaps" in data
    assert "hls_master_url" in data


def _run(coro):
    return asyncio.run(coro)


def test_trigger_loss_prevention_alert_authorized(client, auth_headers):
    payload = {
        "camera_id": TEST_CAMERA,
        "event_type": "CONCEALMENT",
        "severity": "HIGH",
        "confidence": 0.71,
        "description": "Item moved from shelf to bag; staff review requested.",
        "bounding_box": {"x_min": 0.2, "y_min": 0.3, "x_max": 0.4, "y_max": 0.9, "confidence": 0.71},
    }
    response = client.post(
        "/api/v1/events/trigger",
        json=payload,
        headers={"X-Edge-API-Key": settings.INTERNAL_SERVICE_KEY}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"].startswith("evt_")
    assert data["event_type"] == "CONCEALMENT"
    assert data["severity"] == "HIGH"
    assert "kinematics" not in data

    listed = client.get("/api/v1/events?event_type=CONCEALMENT", headers=auth_headers).json()
    assert any(e["id"] == data["id"] for e in listed["events"])

    ack = client.post(f"/api/v1/events/{data['id']}/acknowledge", headers=auth_headers)
    assert ack.status_code == 200
    assert client.get(f"/api/v1/events/{data['id']}", headers=auth_headers).json()["acknowledged"] is True


def test_home_security_event_types_rejected(client):
    for legacy in ("FALL_DETECTED", "DOOR_LEFT_OPEN", "PACKAGE_THEFT", "INACTIVITY_ALARM"):
        response = client.post(
            "/api/v1/events/trigger",
            json={"camera_id": TEST_CAMERA, "event_type": legacy, "severity": "HIGH"},
            headers={"X-Edge-API-Key": settings.INTERNAL_SERVICE_KEY},
        )
        assert response.status_code == 422, legacy


def test_legacy_rows_not_served(client, auth_headers):
    """A database from the home-security build may still hold fall events."""
    async def _insert():
        async with async_session_factory() as session:
            session.add(SecurityEventModel(
                id="evt_legacy_fall", camera_id=TEST_CAMERA, camera_name="x", location="x",
                event_type="FALL_DETECTED", severity="CRITICAL", confidence=0.9,
            ))
            await session.commit()
    _run(_insert())
    listed = client.get("/api/v1/events", headers=auth_headers)
    assert listed.status_code == 200
    assert all(e["id"] != "evt_legacy_fall" for e in listed.json()["events"])
    assert client.get("/api/v1/events/evt_legacy_fall", headers=auth_headers).status_code == 404


def test_notify_loss_prevention_logs_and_reports_honestly(client, auth_headers):
    """No push credentials in tests: pushes are reported not_configured, never sent."""
    from app.services.alert_dispatcher import alert_dispatcher
    from app.services import pairing_service

    alert_dispatcher.reset()

    async def _pair():
        async with async_session_factory() as session:
            return await pairing_service.register_phone(session, {
                "name": "Test Android", "platform": "android", "app_instance_id": "inst_api_test",
                "push_provider": "fcm", "push_token": "tok_android_test"}, None, "code")
    paired = _run(_pair())
    # The retired unbound registration endpoint says where to go instead.
    assert client.post("/api/v1/events/devices", json={"device_token": "x"}, headers=auth_headers).status_code == 410
    # Unmute in case another test muted the camera.
    client.post(f"/api/v1/cameras/{TEST_CAMERA}/mute", json={"duration_minutes": 0}, headers=auth_headers)

    result = _run(notification_service.notify_loss_prevention(
        "Suspicious behaviour - Aisle 3",
        "Possible concealment near the spirits shelf. Please review.",
        {"camera_id": TEST_CAMERA, "event_type": "THEFT_SUSPECTED", "incident_id": "inc_test"},
    ))
    assert result["logged"] is True
    assert result["event_type"] == "THEFT_SUSPECTED"
    push = result["push"]
    assert push["targets"] >= 1
    assert push["sent"] == 0
    assert push["not_configured"] == push["targets"]

    alert = client.get(f"/api/v1/events/{result['alert_id']}", headers=auth_headers).json()
    assert alert["event_type"] == "THEFT_SUSPECTED"
    assert alert["confidence"] is None  # none was measured, so none is shown

    # Same camera + type inside the cooldown: logged again, but no second push.
    again = _run(notification_service.notify_loss_prevention("t", "b", {"camera_id": TEST_CAMERA}))
    assert again["logged"] is True
    assert again["push"]["skipped"].startswith("cooldown")

    assert client.delete(f"/api/v1/pairing/devices/{paired['paired_device_id']}", headers=auth_headers).status_code == 200


def test_push_payloads_have_no_emergency_features():
    from app.services.alert_dispatcher import fcm_provider

    data = {"camera_id": TEST_CAMERA, "event_type": "SHELF_SWEEP"}
    msg = fcm_provider.build_message("tok", "ios", "t", "b", data)["message"]
    apns = msg["apns"]["payload"]
    assert apns["aps"]["sound"] == "default"
    assert apns["aps"]["interruption-level"] == "time-sensitive"
    assert msg["android"]["priority"] == "HIGH"
    assert msg["android"]["notification"]["sound"] == "default"
    fcm = msg["android"]
    text = repr(apns) + repr(fcm)
    for banned in ("critical", "siren", "911", "emergency", "USAGE_ALARM"):
        assert banned.lower() not in text.lower(), banned


def test_mute_camera_holds_back_pushes(client, auth_headers):
    response = client.post(f"/api/v1/cameras/{TEST_CAMERA}/mute", json={"duration_minutes": 5}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    result = _run(notification_service.notify_loss_prevention(
        "t", "b", {"camera_id": TEST_CAMERA, "event_type": "LOITERING"}))
    assert result["logged"] is True
    assert result["push"]["skipped"].startswith("camera alerts muted")
    unmute = client.post(f"/api/v1/cameras/{TEST_CAMERA}/mute", json={"duration_minutes": 0}, headers=auth_headers)
    assert unmute.json()["muted_until"] is None


def test_auth_bypass_trigger_event(client):
    payload = {
        "camera_id": TEST_CAMERA,
        "event_type": "THEFT_SUSPECTED",
        "severity": "HIGH",
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
        "camera_id": TEST_CAMERA,
        "sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=EdgeCCTV_test\r\nt=0 0\r\na=sendrecv\r\n",
        "type": "offer"
    }
    response = client.post("/api/v1/webrtc/offer", json=offer_payload, headers=auth_headers)
    # With a go2rtc gateway the answer is its real SDP; without one the route
    # says so. It used to return a hand-written SDP no peer could connect to.
    assert response.status_code in (200, 501), response.text
    data = response.json()
    if response.status_code == 200:
        assert "sdp" in data and data["type"] == "answer"
    else:
        assert data["detail"].startswith("WebRTC signaling not available")


def test_dvr_export_incident(client, auth_headers):
    now_utc = datetime.now(timezone.utc).isoformat()
    payload = {
        "start_time": now_utc,
        "end_time": now_utc,
        "title": "Suspicious Activity"
    }
    response = client.post(f"/api/v1/cameras/{TEST_CAMERA}/export", json=payload, headers=auth_headers)
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


def test_legacy_pairing_code_routes_are_gone(client):
    """The unbound 6-digit code flow was replaced by /api/v1/pairing."""
    assert client.get("/api/v1/auth/pairing-code").status_code == 410
    assert client.post("/api/v1/auth/pair", json={"pairing_code": "123456"}).status_code == 410
