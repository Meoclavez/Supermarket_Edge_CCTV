"""Scoped stream/clip tokens, sign-out of live sockets, public health, mutes, plan gates.

Audit 2026-10-29 (L1, M11, M12, M15, M16, L8): an alert link used to carry a
24-hour token that worked as a read-only session for the whole API and could
be swapped at /webrtc/token for live video of any camera. Now:

* a clip token opens exactly one evidence file, a stream token exactly one
  camera's video; neither opens anything else (401 at the door);
* both are minted for a signed-in session when a link is served, expire in
  15 minutes and die with that session (sign-out, deactivation, password);
* stored links carry no token; tokens from older builds are refused.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import AdminUserModel, Base, CameraModel
from app.services import auth_service as auth_mod
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector

OWNER = "usr_st_owner"
OPER = "usr_st_oper"
CAM_A = "cam_st_a"
CAM_B = "cam_st_b"


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    with _db() as conn:
        for t, ids in (("admin_users", (OWNER, OPER)), ("cameras", (CAM_A, CAM_B))):
            conn.execute(f"DELETE FROM {t} WHERE id IN (?, ?)", ids)
    with Session(eng) as s:
        s.add(AdminUserModel(id=OWNER, username="st_owner", password_hash="x", display_name="Olive Owner",
                             role="owner", is_active=True))
        s.add(AdminUserModel(id=OPER, username="st_oper", password_hash="x", display_name="Omar Operator",
                             role="operator", is_active=True))
        for cid in (CAM_A, CAM_B):
            s.add(CameraModel(id=cid, name=cid, location="x", rtsp_url="rtsp://127.0.0.1:9/x", status="OFFLINE"))
        s.commit()
    eng.dispose()
    monkeypatch.setattr(settings, "SNAPSHOTS_DIR", tmp_path)
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xff\xd9")
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)
    auth_mod._accounts_cache.update(at=0.0, db=None, rows={})
    app.state.setup_completed = True
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    owner = auth_service.issue_session_tokens(OWNER, "owner")["access_token"]
    oper = auth_service.issue_session_tokens(OPER, "operator")["access_token"]
    yield TestClient(app), owner, oper
    with _db() as conn:
        conn.execute("DELETE FROM admin_users WHERE id IN (?, ?)", (OWNER, OPER))
        conn.execute("DELETE FROM cameras WHERE id IN (?, ?)", (CAM_A, CAM_B))
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()


def _owner_payload(tok: str) -> dict:
    return auth_service.verify_token(tok)


# ------------------------------------------------------------------ clip tokens

def test_clip_token_opens_exactly_its_own_file(env):
    client, owner, _ = env
    clip = auth_service.generate_clip_token("a.jpg", owner=_owner_payload(owner))
    assert client.get(f"/api/v1/events/snapshots/a.jpg?token={clip}").status_code == 200
    assert client.get(f"/api/v1/events/snapshots/b.jpg?token={clip}").status_code in (401, 403)
    assert client.get(f"/api/v1/events/clips/b.mp4?token={clip}").status_code in (401, 403)
    # Not a session: no alert list, layout, analytics or settings.
    for path in ("/api/v1/events", "/api/v1/layout", "/api/v1/analytics/overview", "/api/v1/cameras"):
        assert client.get(f"{path}?token={clip}").status_code == 401, path
        assert client.get(path, headers=_h(clip)).status_code == 401, path


def test_clip_token_cannot_become_live_video(env):
    client, owner, _ = env
    clip = auth_service.generate_clip_token("a.jpg", owner=_owner_payload(owner))
    assert client.get(f"/api/v1/webrtc/token?camera_id={CAM_A}", headers=_h(clip)).status_code == 401
    assert client.get(f"/stream?camera_id={CAM_A}&token={clip}").status_code == 401
    assert client.get(f"/api/v1/cameras/{CAM_A}/snapshot?token={clip}").status_code == 401


def test_stream_token_is_bound_to_its_camera_and_cannot_mint_another(env):
    client, owner, _ = env
    r = client.get(f"/api/v1/webrtc/token?camera_id={CAM_A}", headers=_h(owner))
    assert r.status_code == 200 and r.json()["expires_in"] <= 900
    stream = r.json()["token"]
    claims = auth_service.verify_token(stream)
    assert claims["camera_id"] == CAM_A and claims["sub"] == OWNER and claims["sid"]
    # Its own camera passes the door (no picture in tests: anything but 401/403).
    assert client.get(f"/api/v1/cameras/{CAM_A}/snapshot?token={stream}").status_code not in (401, 403)
    # Another camera, a new token, or the API: refused.
    assert client.get(f"/api/v1/cameras/{CAM_B}/snapshot?token={stream}").status_code == 401
    assert client.get(f"/stream?camera_id={CAM_B}&token={stream}").status_code == 401
    assert client.get(f"/api/v1/webrtc/token?camera_id={CAM_B}", headers=_h(stream)).status_code == 401
    assert client.get("/api/v1/events", headers=_h(stream)).status_code == 401


def test_scoped_tokens_die_with_their_session(env):
    client, owner, _ = env
    payload = _owner_payload(owner)
    clip = auth_service.generate_clip_token("a.jpg", owner=payload)
    assert client.get(f"/api/v1/events/snapshots/a.jpg?token={clip}").status_code == 200
    # Sign-out of the session that minted it (sid) ...
    assert client.post("/api/v1/auth/logout", headers=_h(owner)).status_code == 200
    assert client.get(f"/api/v1/events/snapshots/a.jpg?token={clip}").status_code in (401, 403)


def test_account_sign_out_revokes_scoped_tokens(env):
    client, _, oper = env
    payload = _owner_payload(oper)
    stream = auth_service.generate_stream_token(CAM_A, owner=payload)
    time.sleep(0.002)
    auth_service.sign_out_account(OPER, True)      # deactivated / deleted / password reset
    assert client.get(f"/api/v1/cameras/{CAM_A}/snapshot?token={stream}").status_code == 401


def test_old_unscoped_tokens_are_refused(env):
    client, _, _ = env
    now = int(time.time())
    legacy_clip = jwt.encode({"event_id": "a", "type": "clip_access", "iat": now, "exp": now + 86400},
                             settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    legacy_stream = jwt.encode({"sub": "mobile_app", "camera_id": CAM_A, "type": "stream_access",
                                "iat": now, "exp": now + 86400}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    assert client.get(f"/api/v1/events/snapshots/a.jpg?token={legacy_clip}").status_code in (401, 403)
    assert client.get("/api/v1/events", headers=_h(legacy_clip)).status_code == 401
    assert client.get(f"/api/v1/cameras/{CAM_A}/snapshot?token={legacy_stream}").status_code == 401


def test_dashboard_cookie_opens_evidence_without_a_token(env):
    client, owner, _ = env
    client.cookies.set("edge_cctv_token", owner)
    r = client.get("/api/v1/events/snapshots/a.jpg")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=3600"
    client.cookies.clear()
    assert client.get("/api/v1/events/snapshots/a.jpg").status_code == 401


def test_signed_path_mints_a_short_token_for_the_file_and_owner():
    from urllib.parse import parse_qs, urlsplit

    from app.services.evidence_urls import signed_path

    owner = {"sub": "u1", "sid": "s1", "type": "user_session"}
    url = signed_path("http://localhost:8000/api/v1/events/clips/x.mp4?token=old", owner)
    u = urlsplit(url)
    assert u.path == "/api/v1/events/clips/x.mp4" and not u.netloc
    claims = auth_service.verify_token(parse_qs(u.query)["token"][0])
    assert claims["file"] == "x.mp4" and claims["sub"] == "u1" and claims["sid"] == "s1"
    assert claims["exp"] - claims["iat"] <= 900
    # Not an evidence file: no token added.
    assert signed_path("/api/v1/theft/incidents/1/evidence", owner) == "/api/v1/theft/incidents/1/evidence"


# ------------------------------------------------------------------ websockets (M11)

class _FakeSocket:
    def __init__(self):
        self.sent, self.closed = [], None

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed = code


def test_alert_sockets_close_on_sign_out_and_get_per_session_links(env):
    from app.services.notification_service import AlertHub

    _, owner, oper = env
    hub = AlertHub()
    a, b = _FakeSocket(), _FakeSocket()
    asyncio.run(hub.connect(a, owner, _owner_payload(owner)))
    asyncio.run(hub.connect(b, oper, _owner_payload(oper)))
    asyncio.run(hub.broadcast_event({"id": "e1", "snapshot_url": "/api/v1/events/snapshots/a.jpg?token=stored"}))
    assert len(a.sent) == len(b.sent) == 1
    ta = auth_service.verify_token(a.sent[0]["snapshot_url"].split("token=", 1)[1])
    tb = auth_service.verify_token(b.sent[0]["snapshot_url"].split("token=", 1)[1])
    assert ta["sub"] == OWNER and tb["sub"] == OPER and ta["file"] == "a.jpg"
    # The operator is signed out (deactivated): their socket closes, the owner's stays.
    time.sleep(0.002)
    auth_service.sign_out_account(OPER, True)
    assert asyncio.run(hub.prune_revoked()) == 1
    assert b.closed == 1008 and a.closed is None
    asyncio.run(hub.broadcast_event({"id": "e2"}))
    assert len(a.sent) == 2 and len(b.sent) == 1


def test_revocation_check_fails_closed(env, monkeypatch):
    client, owner, _ = env
    from app.services import token_revocation

    def boom(*_a, **_k):
        raise RuntimeError("store broken")

    monkeypatch.setattr(token_revocation.token_revocations, "is_revoked", boom)
    assert client.get("/api/v1/cameras", headers=_h(owner)).status_code == 401


# ------------------------------------------------------------------ health (M12)

def test_health_without_a_session_says_only_status_and_version(env):
    client, owner, _ = env
    r = client.get("/api/v1/health")
    assert r.status_code in (200, 503)
    assert set(r.json()) == {"status", "version"}
    full = client.get("/api/v1/health", headers=_h(owner)).json()
    assert {"services", "telemetry", "logs"} <= set(full)


# ------------------------------------------------------------------ mutes (M16)

def test_operator_mutes_are_capped_and_recorded(env):
    client, owner, oper = env
    r = client.post(f"/api/v1/cameras/{CAM_A}/mute", headers=_h(oper), json={"duration_minutes": 120})
    assert r.status_code == 403 and "60 minutes" in r.json()["detail"]
    assert client.post(f"/api/v1/cameras/{CAM_A}/mute", headers=_h(oper), json={"duration_minutes": 30}).status_code == 200
    assert client.post(f"/api/v1/cameras/{CAM_A}/mute", headers=_h(owner), json={"duration_minutes": 240}).status_code == 200
    with _db() as conn:
        rows = conn.execute("SELECT action, actor_id, actor_name FROM restricted_area_audit "
                            "WHERE action = 'camera_mute' AND detail LIKE ? ORDER BY ts", (f'%{CAM_A}%',)).fetchall()
        conn.execute("DELETE FROM restricted_area_audit WHERE detail LIKE ?", (f'%{CAM_A}%',))
    assert [(r[1], r[2]) for r in rows][-2:] == [(OPER, "Omar Operator"), (OWNER, "Olive Owner")]


# ------------------------------------------------------------------ fixed iKorex key (L8)

def test_fixed_control_key_blocks_reset_and_server_change(env, monkeypatch):
    import base64

    client, owner, _ = env
    from app.services import fleet_control

    monkeypatch.setattr(settings, "FLEET_CONTROL_PUBLIC_KEY", base64.b64encode(b"k" * 32).decode())
    assert fleet_control.env_public_key()
    r = client.post("/api/v1/fleet/reset-key", headers=_h(owner))
    assert r.status_code == 409
    r = client.put("/api/v1/remote-access", headers=_h(owner), json={"server_url": "wss://evil.example"})
    assert r.status_code == 403 and "fixed" in r.json()["detail"]


# ------------------------------------------------------------------ admin-only model probe

def test_model_status_probe_is_for_administrators(env):
    client, _, oper = env
    assert client.get("/api/v1/analytics/business/model-status", headers=_h(oper)).status_code == 403


# ------------------------------------------------------------------ plan gate on camera zones

def test_restricted_area_drawing_respects_the_plan(env, monkeypatch):
    client, owner, _ = env
    from app.services import entitlements

    real = entitlements.feature_enabled
    monkeypatch.setattr(entitlements, "feature_enabled",
                        lambda key: False if key in ("zone_alerts", "restricted_areas") else real(key))
    body = {"zone_type": "INTRUSION", "name": "x",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.5}]}
    r = client.post(f"/api/v1/cameras/{CAM_A}/zones", headers=_h(owner), json=body)
    assert r.status_code == 403 and r.json().get("code") == "feature_disabled"


# ------------------------------------------------------------------ privacy and tunnel

def test_privacy_masks_fail_closed(monkeypatch):
    from app.services import ai_zone_service as azs, privacy_mask

    def boom(_cam):
        raise OSError("zones file unreadable")

    monkeypatch.setattr(azs.ai_zone_service, "get_all_zones", boom)
    masks = privacy_mask.camera_masks("cam_any")
    assert masks and masks[0]["mask_mode"] == "BLACKOUT" and len(masks[0]["points"]) == 4


def test_shadow_trial_samples_never_leave_through_the_tunnel():
    from app.services.public_exposure import is_live_video_path

    assert is_live_video_path("/api/v1/system/shadow-trial/samples/x.jpg")


def test_fcm_data_keys_are_ids_only():
    from app.services.alert_dispatcher import FCM_DATA_KEYS, fcm_provider

    msg = fcm_provider.build_message("tok", "android", "t", "b", {
        "camera_id": "c1", "camera_name": "Till 2", "event_type": "THEFT_SUSPECTED", "incident_id": "i1",
        "snapshot_url": "/api/v1/events/snapshots/a.jpg", "announced_by": "Sam", "note": "x",
        "location": "Back"})
    data = msg["message"]["data"]
    assert {"camera_name", "snapshot_url", "announced_by", "note", "location"}.isdisjoint(data)
    assert data["camera_id"] == "c1" and data["incident_id"] == "i1"
    assert set(data) - {"title", "body", "type"} <= FCM_DATA_KEYS


def test_unique_ids_do_not_collide():
    # Two tokens minted in the same millisecond are still distinct (jti).
    a = auth_service.generate_clip_token("a.jpg", owner={"sub": "u"})
    b = auth_service.generate_clip_token("a.jpg", owner={"sub": "u"})
    assert auth_service.verify_token(a)["jti"] != auth_service.verify_token(b)["jti"]
    assert uuid.UUID(hex=auth_service.verify_token(a)["jti"])


def test_rate_limit_answers_429_with_retry_after():
    """The dashboard backs off on 429 (static/js/poll.js) for exactly as long as the box asks."""
    from fastapi import Depends, FastAPI

    from app.services.auth_service import RateLimiter

    lim = RateLimiter(requests=3, window=60)
    mini = FastAPI()

    @mini.get("/api/v1/x", dependencies=[Depends(lim)])
    def x():
        return {"ok": True}

    c = TestClient(mini)
    assert [c.get("/api/v1/x").status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/api/v1/x")
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 60
