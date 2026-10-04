"""Accounts (routes/users.py), their guard rails, and role enforcement on writes.

Uses the real-auth ``client`` fixture from test_auth_setup (AUTH_DISABLED off)
against the suite's throwaway database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest

from app.database import async_session_factory
from app.models.db_models import TheftIncidentModel
from app.routes import setup as setup_routes
from app.services import auth_service as auth_mod
from app.services.auth_service import (
    SETUP_FORBIDDEN_DETAIL,
    auth_service,
    intrusion_detector,
    write_allowed,
)
from app.services.token_revocation import token_revocations

from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: F401  (fixture)

OWNER_PW = "correct horse 1"
PW = "staff password 1"


@pytest.fixture(autouse=True)
def _clean_state():
    token_revocations.clear_cache()
    auth_mod.invalidate_account_cache()
    setup_routes._last_locked_login.clear()
    intrusion_detector.failed_attempts.clear()
    yield
    token_revocations.clear_cache()
    auth_mod.invalidate_account_cache()
    setup_routes._last_locked_login.clear()
    intrusion_detector.failed_attempts.clear()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _owner(client):
    body = _create_admin(client, _issued_code(client), password=OWNER_PW).json()
    assert body.get("access_token"), body
    return body["access_token"]


def _login(client, username, password=PW):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _add(client, tok, username, role="operator", password=PW):
    return client.post("/api/v1/users", headers=_h(tok),
                       json={"username": username, "display_name": username.title(), "role": role,
                             "password": password})


def _accounts(client, tok):
    r = client.get("/api/v1/users", headers=_h(tok))
    assert r.status_code == 200, r.text
    return {a["username"]: a for a in r.json()["accounts"]}


def _seed_incident() -> str:
    iid = f"theft_test_{uuid.uuid4().hex[:8]}"

    async def _write():
        async with async_session_factory() as s:
            s.add(TheftIncidentModel(
                id=iid, theft_type="SHELF_SWEEPING", severity="HIGH", status="ACTIVE",
                department="Test", camera_id="cam_test", timestamp=datetime.utcnow(),
                confidence=0.5, items_involved=[], notes="inserted by test"))
            await s.commit()

    asyncio.run(_write())
    return iid


# --------------------------------------------------------------------------- who am I

def test_status_and_me_report_username_display_name_and_role(client):
    tok = _owner(client)
    s = client.get("/api/v1/auth/status", headers=_h(tok)).json()
    assert s["authenticated"] is True
    assert s["user"]["username"] == "manager" and s["user"]["role"] == "owner"
    assert s["user"]["display_name"] == "Manager" and s["user"]["can_change_setup"] is True
    me = client.get("/api/v1/auth/me", headers=_h(tok)).json()
    assert me["username"] == "manager" and me["role"] == "owner"
    assert client.get("/api/v1/auth/status").json()["user"] is None


# --------------------------------------------------------------------------- accounts CRUD

def test_owner_creates_lists_changes_role_and_removes_an_account(client):
    tok = _owner(client)
    r = _add(client, tok, "sam")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "operator" and "password_hash" not in body and body["is_you"] is False
    accts = _accounts(client, tok)
    assert set(accts) == {"manager", "sam"} and accts["manager"]["is_you"] is True

    sam_tok = _login(client, "sam")
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).json()["role"] == "operator"

    r = client.patch(f"/api/v1/users/{body['id']}", headers=_h(tok), json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    auth_mod.invalidate_account_cache()
    # Role is read from the database: the same token is an administrator now.
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).json()["role"] == "admin"

    r = client.delete(f"/api/v1/users/{body['id']}", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert set(_accounts(client, tok)) == {"manager"}
    # Removing an account signs it out at once.
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "sam", "password": PW}).status_code == 401


def test_username_and_password_rules_are_the_shared_ones(client):
    tok = _owner(client)
    assert _add(client, tok, "x").status_code == 400                       # too short
    assert _add(client, tok, "sam", password="short").status_code == 400   # too short
    assert _add(client, tok, "samsamsam", password="samsamsam").status_code == 400  # same as username
    assert _add(client, tok, "sam").status_code == 201
    assert _add(client, tok, "SAM").status_code == 409                     # case-insensitive clash
    assert _add(client, tok, "pat", role="superuser").status_code == 422


# --------------------------------------------------------------------------- guard rails

def test_last_owner_cannot_be_removed_demoted_or_disabled_and_not_yourself(client):
    tok = _owner(client)
    me_id = client.get("/api/v1/auth/me", headers=_h(tok)).json()["id"]
    assert client.delete(f"/api/v1/users/{me_id}", headers=_h(tok)).status_code == 403
    assert client.patch(f"/api/v1/users/{me_id}", headers=_h(tok), json={"role": "admin"}).status_code == 403
    assert client.patch(f"/api/v1/users/{me_id}", headers=_h(tok), json={"is_active": False}).status_code == 403
    assert client.post(f"/api/v1/users/{me_id}/reset-password", headers=_h(tok),
                       json={"new_password": "another pass 1"}).status_code == 403

    # An administrator (not an owner) cannot touch the only owner either.
    _add(client, tok, "ada", role="admin")
    ada = _login(client, "ada")
    r = client.delete(f"/api/v1/users/{me_id}", headers=_h(ada))
    assert r.status_code == 403
    r = client.patch(f"/api/v1/users/{me_id}", headers=_h(ada), json={"role": "operator"})
    assert r.status_code == 403
    assert "owner" in r.json()["detail"].lower()


def test_last_active_owner_guard_counts_owners(client):
    tok = _owner(client)
    second = _add(client, tok, "olive", role="owner").json()
    olive = _login(client, "olive")
    me_id = client.get("/api/v1/auth/me", headers=_h(tok)).json()["id"]
    # With two owners, one may demote the other ...
    assert client.patch(f"/api/v1/users/{me_id}", headers=_h(olive), json={"role": "admin"}).status_code == 200
    auth_mod.invalidate_account_cache()
    # ... but then the remaining owner is the last one.
    r = client.delete(f"/api/v1/users/{second['id']}", headers=_h(tok))
    assert r.status_code == 403  # an admin cannot remove an owner
    assert client.patch(f"/api/v1/users/{second['id']}", headers=_h(olive), json={"role": "admin"}).status_code == 403


def test_only_an_owner_creates_or_promotes_owners(client):
    tok = _owner(client)
    _add(client, tok, "ada", role="admin")
    op = _add(client, tok, "sam").json()
    ada = _login(client, "ada")
    assert _add(client, ada, "pat", role="owner").status_code == 403
    assert _add(client, ada, "pat", role="operator").status_code == 201
    assert client.patch(f"/api/v1/users/{op['id']}", headers=_h(ada), json={"role": "owner"}).status_code == 403
    assert client.patch(f"/api/v1/users/{op['id']}", headers=_h(ada), json={"role": "admin"}).status_code == 200


def test_reset_password_signs_that_account_out(client):
    tok = _owner(client)
    sam = _add(client, tok, "sam").json()
    sam_tok = _login(client, "sam")
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).status_code == 200
    r = client.post(f"/api/v1/users/{sam['id']}/reset-password", headers=_h(tok),
                    json={"new_password": "brand new pass"})
    assert r.status_code == 200 and r.json()["signed_out"] is True
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).status_code == 401
    assert client.get("/api/v1/auth/status", headers=_h(sam_tok)).json()["authenticated"] is False
    assert client.post("/api/v1/auth/login", json={"username": "sam", "password": PW}).status_code == 401
    fresh = _login(client, "sam", "brand new pass")
    assert client.get("/api/v1/auth/me", headers=_h(fresh)).status_code == 200
    # The owner's own session is untouched.
    assert client.get("/api/v1/auth/me", headers=_h(tok)).status_code == 200


def test_account_sign_out_survives_a_cache_reload(client):
    tok = _owner(client)
    sam = _add(client, tok, "sam").json()
    sam_tok = _login(client, "sam")
    client.post(f"/api/v1/users/{sam['id']}/reset-password", headers=_h(tok), json={"new_password": "brand new pass"})
    token_revocations.clear_cache()   # as another worker / after a restart: read from revoked_tokens
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).status_code == 401


# --------------------------------------------------------------------------- own password

def test_change_password_signs_out_other_sessions_and_returns_a_new_one(client):
    tok = _owner(client)
    other = _login(client, "manager", OWNER_PW)
    r = client.post("/api/v1/auth/change-password", headers=_h(tok),
                    json={"old_password": "wrong one 123", "new_password": "fresh pass 99"})
    assert r.status_code == 403 and r.json()["detail"] == "Current password is incorrect."
    r = client.post("/api/v1/auth/change-password", headers=_h(tok),
                    json={"old_password": OWNER_PW, "new_password": "fresh pass 99"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["other_sessions_signed_out"] is True and body["access_token"]
    assert client.get("/api/v1/auth/me", headers=_h(other)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_h(tok)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_h(body["access_token"])).status_code == 200
    # The refresh token of the old session is dead too.
    assert _login(client, "manager", "fresh pass 99")


def test_operator_can_change_own_password(client):
    tok = _owner(client)
    _add(client, tok, "sam")
    sam_tok = _login(client, "sam")
    r = client.post("/api/v1/auth/change-password", headers=_h(sam_tok),
                    json={"old_password": PW, "new_password": "sams own pass"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- enforcement

def test_operator_gets_a_clear_403_on_setup_writes(client):
    tok = _owner(client)
    _add(client, tok, "sam")
    sam = _login(client, "sam")
    for method, path, body in (
        ("PUT", "/api/v1/cameras/cam_x", {"name": "x"}),
        ("POST", "/api/v1/cameras", {"name": "x", "rtsp_url": "rtsp://10.0.0.1/x"}),
        ("DELETE", "/api/v1/cameras/cam_x", None),
        ("PUT", "/api/v1/layout", {}),
        ("PUT", "/api/v1/remote-access", {}),
        ("POST", "/api/v1/pairing/sessions", {}),
        ("POST", "/api/v1/system/backup", None),
        ("POST", "/api/v1/analytics/products/zones", {}),
        ("PUT", "/api/v1/cameras/cam_x/night-watch", {}),
    ):
        r = client.request(method, path, headers=_h(sam), json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["detail"] == SETUP_FORBIDDEN_DETAIL
        assert r.headers.get("X-Edge-Role-Denied") == "1"
    # Accounts are admin-only, reads included.
    assert client.get("/api/v1/users", headers=_h(sam)).status_code == 403
    assert _add(client, sam, "pat").status_code == 403
    # Reads stay open to operators.
    assert client.get("/api/v1/cameras", headers=_h(sam)).status_code == 200


def test_operator_handles_theft_incidents(client):
    tok = _owner(client)
    _add(client, tok, "sam")
    sam = _login(client, "sam")
    iid = _seed_incident()
    r = client.post(f"/api/v1/theft/incidents/{iid}/acknowledge", headers=_h(sam))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACKNOWLEDGED"


def test_admin_and_owner_writes_are_not_refused_by_the_role_layer(client):
    tok = _owner(client)
    r = client.put("/api/v1/cameras/cam_does_not_exist", headers=_h(tok), json={"name": "x"})
    assert r.status_code != 403, r.text


def test_disabled_account_is_signed_out_and_cannot_sign_in(client):
    tok = _owner(client)
    sam = _add(client, tok, "sam").json()
    sam_tok = _login(client, "sam")
    r = client.patch(f"/api/v1/users/{sam['id']}", headers=_h(tok), json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False and r.json()["signed_out"] is True
    assert client.get("/api/v1/auth/me", headers=_h(sam_tok)).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "sam", "password": PW}).status_code == 401


def test_write_allowlist_table():
    op = {"type": "user_session", "sub": "nobody", "role": "operator"}
    assert write_allowed("POST", "/api/v1/theft/incidents/abc/resolve", "operator", op)
    assert write_allowed("POST", "/api/v1/analytics/decisions/7/action", "operator", op)
    assert write_allowed("POST", "/api/v1/events/42/acknowledge", "operator", op)
    assert write_allowed("POST", "/api/v1/analytics/heatmaps/record-now", "operator", op)
    assert write_allowed("POST", "/api/v1/webrtc/sessions/s1/heartbeat", "operator", op)
    assert write_allowed("GET", "/api/v1/anything", "operator", op)
    assert not write_allowed("PUT", "/api/v1/cameras/cam_1", "operator", op)
    assert not write_allowed("POST", "/api/v1/users", "operator", op)
    assert not write_allowed("POST", "/api/v1/theft/incidents/abc/acknowledge/extra", "operator", op)
    # A phone may change its own paired-device row, not another phone's.
    phone = dict(op, pd="pd1", dev="d")
    assert write_allowed("PATCH", "/api/v1/pairing/devices/pd1", "operator", phone)
    assert not write_allowed("DELETE", "/api/v1/pairing/devices/pd2", "operator", phone)
    # Stream/clip tokens (viewer) may only drive live-video signalling.
    assert write_allowed("POST", "/api/v1/webrtc/offer", "viewer", None)
    assert not write_allowed("POST", "/api/v1/theft/incidents/a/acknowledge", "viewer", None)
    assert write_allowed("DELETE", "/api/v1/cameras/cam_1", "admin", None)


def test_stream_token_cannot_change_setup(client):
    _owner(client)
    st = auth_service.generate_stream_token("cam_x")
    r = client.put("/api/v1/cameras/cam_x", headers=_h(st), json={"name": "x"})
    assert r.status_code == 403


def test_tokens_without_an_account_keep_their_claimed_role(client):
    _owner(client)
    admin = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    op = auth_service.create_access_token({"sub": "test_op", "role": "operator", "type": "user_session"})
    assert client.put("/api/v1/cameras/cam_x", headers=_h(admin), json={"name": "x"}).status_code != 403
    assert client.put("/api/v1/cameras/cam_x", headers=_h(op), json={"name": "x"}).status_code == 403
