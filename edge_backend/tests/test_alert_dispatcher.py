"""Alert dispatcher: prefs, quiet hours, cooldown, FCM HTTP v1 wire format, dead tokens."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine

from app.config import settings
from app.database import async_session_factory
from app.models.db_models import Base, CameraModel
from app.services import alert_dispatcher as ad
from app.services import device_identity, pairing_service
from app.services.alert_dispatcher import alert_dispatcher, fcm_provider
from app.services.notification_service import notification_service
from app.services.secret_store import delete_named_secret, set_named_secret

CAM = "cam_dispatch_test"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _fake_service_account(project="edge-test-proj") -> tuple[dict, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {
        "type": "service_account", "project_id": project, "private_key_id": "kid123",
        "private_key": pem, "client_email": f"push@{project}.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }, key


@pytest.fixture(autouse=True)
def clean_state():
    engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with _db() as conn:
        conn.execute("DELETE FROM paired_devices")
        conn.execute("INSERT OR IGNORE INTO cameras (id, name, location, rtsp_url, status, fps, resolution, "
                     "is_ai_enabled, ai_models, dvr_enabled, dvr_retention_days, dvr_quota_gb, created_at, "
                     "channel_number, department, floor_x, floor_y, floor_z, azimuth_deg, fov_deg) VALUES "
                     "(?, 'Dispatch Cam', 'Aisle 9', 'rtsp://x', 'OFFLINE', 0, 'unknown', 1, '[]', 0, 7, 1.0, "
                     "?, 1, 'GENERAL', 0, 0, 3, 0, 90)", (CAM, datetime.utcnow().isoformat(sep=" ")))
        conn.execute("UPDATE cameras SET muted_until = NULL WHERE id = ?", (CAM,))
    delete_named_secret(settings.STORAGE_DIR, ad.FCM_SECRET_NAME)
    fcm_provider.invalidate()
    alert_dispatcher.reset()
    ad.http_transport = None
    yield
    ad.http_transport = None
    delete_named_secret(settings.STORAGE_DIR, ad.FCM_SECRET_NAME)
    fcm_provider.invalidate()


def _pair(inst, token="tok_" + "a" * 20, prefs=None, platform="android", name=None):
    async def go():
        async with async_session_factory() as session:
            res = await pairing_service.register_phone(session, {
                "name": name or inst, "platform": platform, "app_instance_id": inst,
                "push_provider": "fcm" if token else None, "push_token": token}, None, "code")
            if prefs is not None:
                await pairing_service.update_device(session, res["paired_device_id"],
                                                    alert_prefs=prefs, prefs_given=True)
            return res["paired_device_id"]
    return _run(go())


def _configure_fcm():
    sa, key = _fake_service_account()
    set_named_secret(settings.STORAGE_DIR, ad.FCM_SECRET_NAME, json.dumps(sa), settings.NVR_CREDENTIAL_KEY)
    fcm_provider.invalidate()
    return sa, key


# --------------------------------------------------------------------------- prefs

def test_prefs_filter():
    p = pairing_service.validate_prefs({"event_types": ["CONCEALMENT"], "min_severity": "WARNING",
                                        "camera_ids": ["cam_a"], "quiet_hours": None})
    noon = datetime(2026, 9, 23, 12, 0)
    assert pairing_service.prefs_filter_reason(p, "CONCEALMENT", "HIGH", "cam_a", noon) is None
    assert "event type" in pairing_service.prefs_filter_reason(p, "LOITERING", "HIGH", "cam_a", noon)
    assert "severity" in pairing_service.prefs_filter_reason(p, "CONCEALMENT", "INFO", "cam_a", noon)
    assert "camera" in pairing_service.prefs_filter_reason(p, "CONCEALMENT", "HIGH", "cam_b", noon)
    assert pairing_service.prefs_filter_reason(None, "LOITERING", "INFO", "cam_z", noon) is None
    with pytest.raises(ValueError):
        pairing_service.validate_prefs({"event_types": ["FALL_DETECTED"]})


def test_quiet_hours_wrap_midnight():
    q = {"start": "22:00", "end": "07:00"}
    assert pairing_service.in_quiet_hours(q, datetime(2026, 1, 1, 23, 30))
    assert pairing_service.in_quiet_hours(q, datetime(2026, 1, 1, 6, 59))
    assert not pairing_service.in_quiet_hours(q, datetime(2026, 1, 1, 7, 0))
    assert not pairing_service.in_quiet_hours(q, datetime(2026, 1, 1, 12, 0))
    day = {"start": "12:00", "end": "13:00"}
    assert pairing_service.in_quiet_hours(day, datetime(2026, 1, 1, 12, 30))
    assert not pairing_service.in_quiet_hours(day, datetime(2026, 1, 1, 13, 30))
    reason = pairing_service.prefs_filter_reason({"quiet_hours": q, "min_severity": "INFO"},
                                                 "CONCEALMENT", "HIGH", CAM, datetime(2026, 1, 1, 23, 0))
    assert reason.startswith("quiet hours")


def test_dispatch_applies_prefs_and_reports_not_configured():
    wanted = _pair("inst_all")
    filtered = _pair("inst_high_only", prefs={"min_severity": "HIGH"})
    no_token = _pair("inst_no_token", token=None)
    now = datetime.now()
    quiet = {"start": f"{now.hour:02d}:00", "end": f"{(now.hour + 1) % 24:02d}:00"}
    in_quiet = _pair("inst_quiet", prefs={"quiet_hours": quiet})

    report = _run(alert_dispatcher.dispatch("CONCEALMENT", "WARNING", "Review: concealment",
                                            "Possible concealment", {"camera_id": CAM, "incident_id": "inc_1"}))
    assert report["logged"] is True and report["event_type"] == "CONCEALMENT"
    push = report["push"]
    by_id = {r["paired_device_id"]: r for r in push["results"]}
    assert by_id[wanted]["status"] == "not_configured"
    assert by_id[filtered]["status"] == "filtered" and "severity" in by_id[filtered]["reason"]
    assert by_id[no_token]["status"] == "no_token"
    assert by_id[in_quiet]["status"] == "filtered" and by_id[in_quiet]["reason"].startswith("quiet hours")
    assert push["sent"] == 0 and push["not_configured"] == 1 and push["targets"] == 1
    assert alert_dispatcher.recent and alert_dispatcher.recent[0]["status"] == "not_configured"


def test_cooldown_and_mute():
    _pair("inst_cool")
    first = _run(alert_dispatcher.dispatch("LOITERING", "HIGH", "t", "b", {"camera_id": CAM}))
    assert first["push"]["skipped"] is None and first["push"]["targets"] == 1
    second = _run(alert_dispatcher.dispatch("LOITERING", "HIGH", "t", "b", {"camera_id": CAM}))
    assert second["logged"] is True
    assert second["push"]["skipped"].startswith("cooldown")
    other_type = _run(alert_dispatcher.dispatch("SHELF_SWEEP", "HIGH", "t", "b", {"camera_id": CAM}))
    assert other_type["push"]["skipped"] is None

    with _db() as conn:
        conn.execute("UPDATE cameras SET muted_until = '2999-01-01 00:00:00' WHERE id = ?", (CAM,))
    muted = _run(alert_dispatcher.dispatch("CONCEALMENT", "HIGH", "t", "b", {"camera_id": CAM}))
    assert muted["push"]["skipped"].startswith("camera alerts muted")


# --------------------------------------------------------------------------- FCM v1

def test_fcm_v1_request_shape_and_token_cache():
    sa, key = _configure_fcm()
    pd = _pair("inst_fcm", token="fcm-device-token-xyz")
    calls = {"token": 0, "send": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == sa["token_uri"]:
            calls["token"] += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            claims = jwt.decode(form["assertion"][0], key.public_key(), algorithms=["RS256"],
                                audience=sa["token_uri"])
            assert claims["iss"] == sa["client_email"]
            assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
            assert jwt.get_unverified_header(form["assertion"][0])["kid"] == "kid123"
            return httpx.Response(200, json={"access_token": "ya29.test-token", "expires_in": 3599,
                                             "token_type": "Bearer"})
        calls["send"].append(request)
        return httpx.Response(200, json={"name": f"projects/{sa['project_id']}/messages/0:1"})

    ad.http_transport = httpx.MockTransport(handler)
    report = _run(alert_dispatcher.dispatch("CONCEALMENT", "HIGH", "Review: concealment", "Check aisle 9",
                                            {"camera_id": CAM, "incident_id": "inc_42", "confidence": 0.81}))
    assert report["push"]["sent"] == 1, report
    assert calls["token"] == 1 and len(calls["send"]) == 1
    req = calls["send"][0]
    assert str(req.url) == "https://fcm.googleapis.com/v1/projects/edge-test-proj/messages:send"
    assert req.headers["Authorization"] == "Bearer ya29.test-token"
    msg = json.loads(req.content)["message"]
    assert msg["token"] == "fcm-device-token-xyz"
    assert msg["android"]["priority"] == "HIGH"
    assert msg["android"]["notification"]["channel_id"] == "loss_prevention_alerts"
    assert msg["apns"]["payload"]["aps"]["interruption-level"] == "time-sensitive"
    data = msg["data"]
    assert all(isinstance(v, str) for v in data.values())
    ident = device_identity.get_identity()
    assert data["device_id"] == ident["device_id"] and data["device_name"] == ident["device_name"]
    assert data["event_type"] == "CONCEALMENT" and data["severity"] == "HIGH"
    assert data["incident_id"] == "inc_42" and data["camera_id"] == CAM and data["alert_id"]

    # A second alert reuses the cached OAuth token.
    _run(alert_dispatcher.dispatch("SHELF_SWEEP", "HIGH", "t", "b", {"camera_id": CAM}))
    assert calls["token"] == 1 and len(calls["send"]) == 2
    with _db() as conn:
        assert conn.execute("SELECT last_push_status FROM paired_devices WHERE id = ?", (pd,)).fetchone()[0] == "sent"


def test_not_configured_without_service_account():
    assert fcm_provider.configured() is False
    assert fcm_provider.status()["configured"] is False
    pd = _pair("inst_nc")
    out = _run(alert_dispatcher.send_test(pd))
    assert out["status"] == "not_configured"


def test_unregistered_token_is_cleared():
    sa, _ = _configure_fcm()
    pd = _pair("inst_dead", token="dead-token")

    def handler(request):
        if str(request.url) == sa["token_uri"]:
            return httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3600})
        return httpx.Response(404, json={"error": {
            "code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND",
            "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                         "errorCode": "UNREGISTERED"}]}})

    ad.http_transport = httpx.MockTransport(handler)
    report = _run(alert_dispatcher.dispatch("CONCEALMENT", "HIGH", "t", "b", {"camera_id": CAM}))
    assert report["push"]["unregistered"] == 1 and report["push"]["sent"] == 0
    with _db() as conn:
        row = conn.execute("SELECT push_token, last_push_status FROM paired_devices WHERE id = ?", (pd,)).fetchone()
    assert row == (None, "unregistered")


def test_oauth_rejection_is_reported_as_failure():
    sa, _ = _configure_fcm()
    pd = _pair("inst_badcreds")
    ad.http_transport = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."}))
    out = _run(alert_dispatcher.send_test(pd))
    assert out["status"] == "failed" and "invalid_grant" in out["error"]


def test_service_account_validation_does_not_echo_key():
    sa, _ = _fake_service_account()
    assert ad.validate_service_account(json.dumps(sa))["project_id"] == "edge-test-proj"
    for broken, needle in (({**sa, "type": "authorized_user"}, "service_account"),
                           ({k: v for k, v in sa.items() if k != "client_email"}, "client_email"),
                           ({**sa, "private_key": "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"},
                            "private_key")):
        with pytest.raises(ValueError) as exc:
            ad.validate_service_account(json.dumps(broken))
        assert needle in str(exc.value)
        assert "BEGIN PRIVATE KEY" not in str(exc.value)


# --------------------------------------------------------------------------- legacy entry point

def test_notify_loss_prevention_still_serves_pose_analytics():
    from app.services.pose_analytics import PoseAnalytics

    _pair("inst_pose")
    title, body, data = PoseAnalytics._notification(
        {"rule": "CONCEALMENT", "confidence": 0.8, "camera_id": CAM, "id": "inc_pose_1",
         "evidence": ["wrist to waistband", "held 2.1 s"], "zone_name": "Spirits"},
        {"camera_name": "Dispatch Cam", "evidence_snapshot_url": "/api/v1/theft/incidents/inc_pose_1/evidence"},
    )
    report = _run(notification_service.notify_loss_prevention(title, body, data))
    assert report["logged"] is True
    assert report["event_type"] == data["event_type"] and report["severity"] == "HIGH"
    assert report["push"]["not_configured"] == 1
    with _db() as conn:
        row = conn.execute("SELECT event_type, confidence FROM security_events WHERE id = ?",
                           (report["alert_id"],)).fetchone()
    assert row[0] == data["event_type"] and abs(row[1] - 0.8) < 1e-9
