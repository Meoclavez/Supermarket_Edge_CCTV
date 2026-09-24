"""Device identity and phone pairing: sessions, claim, revocation, token binding.

Runs with real authentication (AUTH_DISABLED=false) against the suite's
throwaway database.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import auth_service as auth_mod
from app.services import device_identity, pairing_service
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _reset_limits():
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    pairing_service._global_failures.clear()


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with _db() as conn:
        conn.execute("DELETE FROM paired_devices")
        conn.execute("DELETE FROM pairing_sessions")
        conn.execute("DELETE FROM system_setup WHERE key = 'remote_access'")
    pairing_service.registry.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)
    _reset_limits()
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    yield TestClient(app)
    _reset_limits()


@pytest.fixture
def operator():
    tokens = auth_service.issue_session_tokens("op_test_user", "owner")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _phone(inst="inst_1", token="fcm_tok_1", name="Staff Pixel"):
    return {"name": name, "platform": "android", "app_instance_id": inst,
            "push_provider": "fcm" if token else None, "push_token": token}


def _new_session(client, operator):
    r = client.post("/api/v1/pairing/sessions", headers=operator)
    assert r.status_code == 200, r.text
    return r.json()


def _claim(client, code, device_id=None, phone=None):
    return client.post("/api/v1/pairing/claim", json={
        "device_id": device_id or device_identity.current_device_id(),
        "code": code, "phone": phone or _phone(),
    })


# --------------------------------------------------------------------------- identity

def test_identity_is_public_stable_and_renamable(client, operator):
    first = client.get("/api/v1/device/identity")
    assert first.status_code == 200
    ident = first.json()
    assert len(ident["device_id"]) == 36 and ident["api_version"] == 1
    assert ident["pairing"] is True and ident["remote_url"] is None
    assert client.get("/api/v1/device/identity").json()["device_id"] == ident["device_id"]

    assert client.put("/api/v1/device/identity", json={"device_name": "Nope"}).status_code == 401
    renamed = client.put("/api/v1/device/identity", json={"device_name": "  Store 12   Aisle  "}, headers=operator)
    assert renamed.status_code == 200 and renamed.json()["device_name"] == "Store 12 Aisle"
    assert renamed.json()["device_id"] == ident["device_id"]
    assert client.put("/api/v1/device/identity", json={"device_name": "  "}, headers=operator).status_code == 422

    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_setup (key, value, updated_at) VALUES ('remote_access', ?, ?)",
                     (json.dumps({"enabled": True, "provider": "cloudflare_tunnel",
                                  "hostname": "store12.example.com"}), datetime.utcnow().isoformat()))
    assert client.get("/api/v1/device/identity").json()["remote_url"] == "https://store12.example.com"


# --------------------------------------------------------------------------- sessions & claim

def test_session_requires_operator_and_shape(client, operator):
    assert client.post("/api/v1/pairing/sessions").status_code == 401
    s = _new_session(client, operator)
    assert len(s["code"]) == 9 and s["code"][4] == "-"
    dev = device_identity.current_device_id()
    assert s["qr_payload"].startswith(f"edgecctv://pair?v=1&d={dev}&n=")
    assert f"&c={s['code']}" in s["qr_payload"]
    assert s["qr_svg"].lstrip().startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in s["qr_svg"]
    # Only an HMAC of the code is stored.
    with _db() as conn:
        rows = conn.execute("SELECT code_hash FROM pairing_sessions").fetchall()
    assert rows and all(s["code"] not in r[0] and s["code"].replace("-", "") not in r[0] for r in rows)


def test_claim_happy_path(client, operator):
    s = _new_session(client, operator)
    r = _claim(client, s["code"].lower().replace("-", ""))  # case / dash insensitive
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("paired_device_id", "device_id", "device_name", "access_token", "refresh_token", "urls"):
        assert key in body, key
    assert body["token_type"] == "bearer"
    assert body["device_id"] == device_identity.current_device_id()

    claims = jwt.decode(body["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    assert claims["dev"] == body["device_id"] and claims["pd"] == body["paired_device_id"]

    phone = {"Authorization": f"Bearer {body['access_token']}"}
    listed = client.get("/api/v1/pairing/devices", headers=phone)
    assert listed.status_code == 200
    dev = next(d for d in listed.json()["devices"] if d["id"] == body["paired_device_id"])
    assert dev["name"] == "Staff Pixel" and dev["platform"] == "android"
    assert dev["push"]["token_registered"] is True and dev["paired_via"] == "code"
    assert dev["last_seen_at"] is not None
    # Refresh token only stored hashed.
    with _db() as conn:
        stored = conn.execute("SELECT refresh_token_hash FROM paired_devices WHERE id = ?",
                              (body["paired_device_id"],)).fetchone()[0]
    assert stored and stored != body["refresh_token"] and body["refresh_token"] not in stored


def test_wrong_device_id_is_409(client, operator):
    s = _new_session(client, operator)
    r = _claim(client, s["code"], device_id="00000000-0000-4000-8000-000000000000")
    assert r.status_code == 409 and r.json() == {"detail": "device_mismatch"}
    # The code was not spent by the mismatch.
    assert _claim(client, s["code"]).status_code == 200


def test_reused_expired_and_wrong_codes_are_403(client, operator):
    s = _new_session(client, operator)
    assert _claim(client, s["code"]).status_code == 200
    reused = _claim(client, s["code"], phone=_phone("inst_2"))
    assert reused.status_code == 403

    s2 = _new_session(client, operator)
    with _db() as conn:
        conn.execute("UPDATE pairing_sessions SET expires_at = ? WHERE id = ?",
                     ((datetime.utcnow() - timedelta(seconds=1)).isoformat(sep=" "), s2["session_id"]))
    assert _claim(client, s2["code"], phone=_phone("inst_3")).status_code == 403

    assert _claim(client, "ZZZZ-ZZZZ", phone=_phone("inst_4")).status_code == 403


def test_regenerating_closes_the_previous_code(client, operator):
    old = _new_session(client, operator)
    new = _new_session(client, operator)
    assert _claim(client, old["code"]).status_code == 403
    assert _claim(client, new["code"]).status_code == 200


def test_lockout_applies_after_repeated_wrong_codes(client, operator):
    s = _new_session(client, operator)
    for _ in range(intrusion_detector.max_attempts):
        assert _claim(client, "ZZZZ-ZZZZ").status_code == 403
    locked = _claim(client, s["code"])  # right code, but this address is locked out
    assert locked.status_code == 403 and "locked" in locked.json()["detail"].lower()
    _reset_limits()
    assert _claim(client, s["code"]).status_code == 200


# --------------------------------------------------------------------------- token binding

def test_token_for_another_device_is_rejected(client, operator):
    s = _new_session(client, operator)
    body = _claim(client, s["code"]).json()
    claims = jwt.decode(body["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    claims["dev"] = "11111111-1111-4111-8111-111111111111"
    forged = jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    r = client.get("/api/v1/pairing/devices", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    # Same for refresh.
    rclaims = jwt.decode(body["refresh_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    rclaims["dev"] = claims["dev"]
    rforged = jwt.encode(rclaims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": rforged}).status_code == 401


def test_revoked_device_is_rejected_including_refresh(client, operator):
    s = _new_session(client, operator)
    body = _claim(client, s["code"]).json()
    phone = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/v1/cameras", headers=phone).status_code == 200

    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200
    new_claims = jwt.decode(refreshed.json()["access_token"], settings.JWT_SECRET,
                            algorithms=[settings.JWT_ALGORITHM])
    assert new_claims["pd"] == body["paired_device_id"] and new_claims["dev"] == body["device_id"]

    assert client.delete(f"/api/v1/pairing/devices/{body['paired_device_id']}", headers=operator).status_code == 200
    assert client.get("/api/v1/cameras", headers=phone).status_code == 401
    assert client.get("/api/v1/cameras",
                      headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}).status_code == 401
    # Revocation is visible to a fresh process too (cache cleared -> DB read).
    pairing_service.registry.clear()
    assert client.get("/api/v1/cameras", headers=phone).status_code == 401
    assert all(d["id"] != body["paired_device_id"]
               for d in client.get("/api/v1/pairing/devices", headers=operator).json()["devices"])


def test_repairing_rotates_the_refresh_token(client, operator):
    first = _claim(client, _new_session(client, operator)["code"]).json()
    second = _claim(client, _new_session(client, operator)["code"]).json()
    assert first["paired_device_id"] == second["paired_device_id"]  # same app instance, one row
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 200


# --------------------------------------------------------------------------- password login

def test_login_with_phone_registers_a_device(client, operator):
    from app.database import async_session_factory

    async def _mk():
        async with async_session_factory() as session:
            return (await auth_service.create_admin_user(session, "pairtest_mgr", "correct horse 9", "Mgr")).id

    uid = _run(_mk())
    try:
        plain = client.post("/api/v1/auth/login", json={"username": "pairtest_mgr", "password": "correct horse 9"})
        assert plain.status_code == 200 and "paired_device_id" not in plain.json()

        r = client.post("/api/v1/auth/login", json={
            "username": "pairtest_mgr", "password": "correct horse 9",
            "phone": {"name": "Manager iPhone", "platform": "ios", "app_instance_id": "inst_login",
                      "push_provider": "fcm", "push_token": "apns_via_fcm_tok"},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["device_id"] == device_identity.current_device_id()
        assert body["paired_device_id"].startswith("pd_") and body["urls"] is not None
        claims = jwt.decode(body["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        assert claims["sub"] == uid and claims["pd"] == body["paired_device_id"]
        devs = client.get("/api/v1/pairing/devices", headers=operator).json()["devices"]
        d = next(d for d in devs if d["id"] == body["paired_device_id"])
        assert d["paired_via"] == "password" and d["platform"] == "ios"

        bad = client.post("/api/v1/auth/login", json={
            "username": "pairtest_mgr", "password": "wrong", "phone": _phone("inst_x")})
        assert bad.status_code == 401
    finally:
        with _db() as conn:
            conn.execute("DELETE FROM admin_users WHERE id = ?", (uid,))


# --------------------------------------------------------------------------- device management

def test_phone_updates_own_push_token_and_prefs_validation(client, operator):
    body = _claim(client, _new_session(client, operator)["code"], phone=_phone(token=None)).json()
    phone = {"Authorization": f"Bearer {body['access_token']}"}
    pd = body["paired_device_id"]

    assert client.put("/api/v1/pairing/devices/me/push", json={"push_provider": "fcm", "push_token": "t"},
                      headers=operator).status_code == 403
    r = client.put("/api/v1/pairing/devices/me/push", json={"push_provider": "fcm", "push_token": "new_tok"},
                   headers=phone)
    assert r.status_code == 200 and r.json()["push"]["token_registered"] is True

    bad = client.patch(f"/api/v1/pairing/devices/{pd}", json={"alert_prefs": {"min_severity": "LOUD"}},
                       headers=operator)
    assert bad.status_code == 422
    bad_qh = client.patch(f"/api/v1/pairing/devices/{pd}",
                          json={"alert_prefs": {"quiet_hours": {"start": "25:00", "end": "07:00"}}}, headers=operator)
    assert bad_qh.status_code == 422
    prefs = {"event_types": ["CONCEALMENT", "THEFT_SUSPECTED"], "min_severity": "WARNING",
             "camera_ids": ["cam_a"], "quiet_hours": {"start": "22:00", "end": "07:00"}}
    ok = client.patch(f"/api/v1/pairing/devices/{pd}", json={"name": "Front desk", "alert_prefs": prefs},
                      headers=operator)
    assert ok.status_code == 200, ok.text
    again = next(d for d in client.get("/api/v1/pairing/devices", headers=operator).json()["devices"] if d["id"] == pd)
    assert again["name"] == "Front desk"
    assert again["alert_prefs"] == {**prefs, "event_types": sorted(prefs["event_types"])}


def test_legacy_unbound_pairing_is_gone(client):
    assert client.post("/api/v1/auth/pair", json={"pairing_code": "123456"}).status_code == 410
