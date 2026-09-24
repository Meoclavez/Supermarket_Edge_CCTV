"""Lockout accounting and server-side sign-out.

* A stale session (signed with this device's key but expired, from an old
  auth epoch, or signed out) is refused with 401 and never counted as a
  brute-force attempt; only unverifiable credentials count, and they lock the
  address's *token* bucket, never the password sign-in.
* ``POST /api/v1/auth/logout`` revokes the session token (and its sign-in
  session, including the refresh token) until expiry; migration m0011 creates
  ``revoked_tokens``.

Uses the real-auth ``client`` fixture from test_auth_setup against the suite's
throwaway database.
"""

from __future__ import annotations

import shutil
import sqlite3
import time

import jwt
import pytest

from app.config import settings
from app.migrations import discover, run_migrations
from app.routes import setup as setup_routes
from app.services.auth_service import auth_service, intrusion_detector
from app.services.token_revocation import token_revocations

from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: F401  (fixture)

PASSWORD = "correct horse 1"
IP = "testclient"          # starlette TestClient's peer address


@pytest.fixture(autouse=True)
def _clean_state():
    token_revocations.clear_cache()
    setup_routes._last_locked_login.clear()
    yield
    token_revocations.clear_cache()
    setup_routes._last_locked_login.clear()
    intrusion_detector.failed_attempts.clear()


def _admin(client):
    body = _create_admin(client, _issued_code(client), password=PASSWORD).json()
    assert body.get("access_token"), body
    return body


def _login(client, password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"username": "manager", "password": password})


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _signed(**claims):
    now = int(time.time())
    payload = {"sub": "u1", "type": "user_session", "role": "owner", "ep": 0, "iat": now - 100, "exp": now + 3600}
    payload.update(claims)
    return jwt.encode(payload, auth_service.secret, algorithm=auth_service.algorithm)


# --------------------------------------------------------------------------- lockout

def test_signed_but_stale_tokens_polled_many_times_do_not_lock_out(client):
    _admin(client)
    now = int(time.time())
    stale = {
        "expired": _signed(iat=now - 90000, exp=now - 60),
        "old_epoch": _signed(ep=99),
        "wrong_type": _signed(type="refresh"),
    }
    for name, tok in stale.items():
        for _ in range(20):
            r = client.get("/api/v1/cameras", headers=_bearer(tok))
            assert r.status_code == 401, (name, r.status_code, r.text)
    assert intrusion_detector.recent_failures(IP, "token") == 0
    assert intrusion_detector.recent_failures(IP, "login") == 0
    # The dashboard's own status probe with the stale token is not an attempt either.
    assert client.get("/api/v1/auth/status", headers=_bearer(stale["expired"])).json()["authenticated"] is False
    assert _login(client).status_code == 200


def test_signed_out_token_polling_is_not_counted(client):
    tok = _admin(client)["access_token"]
    assert client.post("/api/v1/auth/logout", headers=_bearer(tok)).status_code == 200
    for _ in range(15):
        assert client.get("/api/v1/layout", headers=_bearer(tok)).status_code == 401
    assert intrusion_detector.recent_failures(IP, "token") == 0


def test_garbage_tokens_lock_the_token_bucket_but_never_the_password_sign_in(client):
    good = _admin(client)["access_token"]
    other_key = jwt.encode({"sub": "x", "type": "user_session", "exp": int(time.time()) + 60},
                           "not-this-device", algorithm="HS256")
    statuses = []
    for i in range(11):
        tok = other_key if i % 2 else f"garbage-{i}"
        statuses.append(client.get("/api/v1/cameras", headers=_bearer(tok)).status_code)
    assert statuses[: intrusion_detector.max_attempts - 1] == [401] * (intrusion_detector.max_attempts - 1)
    assert statuses[-1] == 403                      # token bucket locked
    assert intrusion_detector.is_locked_out(IP, "token")
    assert not intrusion_detector.is_locked_out(IP, "login")
    # A wrong API key counts too.
    assert client.get("/api/v1/cameras", headers={"X-Edge-API-Key": "wrong"}).status_code == 403
    # A valid session still works, and the password sign-in is untouched.
    assert client.get("/api/v1/cameras", headers=_bearer(good)).status_code == 200
    r = _login(client)
    assert r.status_code == 200 and r.json()["access_token"]


def test_wrong_passwords_lock_out_but_the_right_password_still_signs_in(client, monkeypatch):
    _admin(client)
    codes = [_login(client, "wrong password!").status_code for _ in range(intrusion_detector.max_attempts)]
    assert codes[:-1] == [401] * (intrusion_detector.max_attempts - 1) and codes[-1] == 429
    assert intrusion_detector.is_locked_out(IP, "login")
    # Locked out: attempts are throttled (one per LOGIN_LOCKED_RETRY_SEC) ...
    assert _login(client, "wrong password!").status_code == 429
    assert _login(client).status_code == 429          # within the throttle interval
    # ... but not refused: after the interval the right password signs in.
    monkeypatch.setattr(setup_routes, "LOGIN_LOCKED_RETRY_SEC", 0.0)
    r = _login(client)
    assert r.status_code == 200 and r.json()["access_token"]
    assert not intrusion_detector.is_locked_out(IP, "login")


# --------------------------------------------------------------------------- logout

def test_logout_revokes_the_session_everywhere_and_leaves_other_sessions_alone(client):
    from starlette.websockets import WebSocketDisconnect

    _admin(client)
    a, b = _login(client).json(), _login(client).json()
    assert a["access_token"] != b["access_token"]
    for s in (a, b):
        assert client.get("/api/v1/cameras", headers=_bearer(s["access_token"])).status_code == 200

    r = client.post("/api/v1/auth/logout", headers=_bearer(a["access_token"]))
    assert r.status_code == 200 and r.json()["revoked"] >= 1

    # HTTP: 401 for the signed-out token (header, query and cookie forms).
    assert client.get("/api/v1/cameras", headers=_bearer(a["access_token"])).status_code == 401
    assert client.get(f"/api/v1/cameras?token={a['access_token']}").status_code == 401
    assert client.get("/api/v1/auth/status", headers=_bearer(a["access_token"])).json()["authenticated"] is False
    # Websocket: 1008.
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/events/ws?token={a['access_token']}") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    # The refresh token of the same sign-in is dead too (session revoked).
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": a["refresh_token"]}).status_code == 401

    # The other session is unaffected, including its refresh.
    assert client.get("/api/v1/cameras", headers=_bearer(b["access_token"])).status_code == 200
    with client.websocket_connect(f"/api/v1/events/ws?token={b['access_token']}"):
        pass
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": b["refresh_token"]})
    assert refreshed.status_code == 200
    assert client.get("/api/v1/cameras", headers=_bearer(refreshed.json()["access_token"])).status_code == 200

    # Idempotent, and never counted as a failed attempt.
    assert client.post("/api/v1/auth/logout", headers=_bearer(a["access_token"])).status_code == 200
    assert intrusion_detector.recent_failures(IP, "token") == 0
    assert client.post("/api/v1/auth/logout").status_code == 401   # nothing presented


def test_logout_revokes_a_presented_refresh_token_and_a_refreshed_access_token(client):
    _admin(client)
    s = _login(client).json()
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": s["refresh_token"]}).json()["access_token"]
    assert client.get("/api/v1/cameras", headers=_bearer(refreshed)).status_code == 200
    r = client.post("/api/v1/auth/logout", headers=_bearer(s["access_token"]),
                    json={"refresh_token": s["refresh_token"]})
    assert r.status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": s["refresh_token"]}).status_code == 401
    # An access token refreshed from the same sign-in shares its session.
    assert client.get("/api/v1/cameras", headers=_bearer(refreshed)).status_code == 401


def test_legacy_token_without_jti_is_still_valid_and_revocable(client):
    _admin(client)
    legacy = _signed(sub="legacy-user")             # no jti / sid: issued by an older build
    assert client.get("/api/v1/cameras", headers=_bearer(legacy)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=_bearer(legacy)).json()["revoked"] == 1
    assert client.get("/api/v1/cameras", headers=_bearer(legacy)).status_code == 401


def test_revocations_are_persisted_until_expiry_and_pruned(client):
    tok = _admin(client)["access_token"]
    client.post("/api/v1/auth/logout", headers=_bearer(tok))
    token_revocations.clear_cache()                 # as after a restart: reloaded from the table
    assert client.get("/api/v1/cameras", headers=_bearer(tok)).status_code == 401

    with sqlite3.connect(str(settings.DATABASE_PATH)) as conn:
        conn.execute("INSERT INTO revoked_tokens (jti, token_type, expires_at, revoked_at) "
                     "VALUES ('jti:long-gone', 'user_session', ?, '2026-01-01 00:00:00')", (int(time.time()) - 5,))
    # Any insert prunes rows whose tokens have expired.
    auth_service.revoke_session(auth_service.issue_session_tokens("u9", "owner")["access_token"])
    with sqlite3.connect(str(settings.DATABASE_PATH)) as conn:
        keys = {r[0] for r in conn.execute("SELECT jti FROM revoked_tokens")}
    assert "jti:long-gone" not in keys and keys


# --------------------------------------------------------------------------- migration

def test_m0011_creates_revoked_tokens_on_a_copy_of_an_m0010_database(tmp_path):
    migrations = discover()
    assert [m for m in migrations if m.version == 11 and m.name == "revoked_tokens"]
    base = tmp_path / "at_m0010.db"
    run_migrations(base, migrations=[m for m in migrations if m.version <= 10],
                   backups_dir=tmp_path / "backups", run_reconcile=False)
    with sqlite3.connect(base) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='revoked_tokens'").fetchone() is None
    copy = tmp_path / "copy.db"
    shutil.copy(base, copy)

    # Up to m0011 only: later migrations (m0012+) are tested on their own.
    report = run_migrations(copy, migrations=[m for m in migrations if m.version <= 11],
                            backups_dir=tmp_path / "backups")
    assert report["applied"] == ["0011_revoked_tokens"]
    with sqlite3.connect(copy) as conn:
        cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(revoked_tokens)")}
        idx = {r[1] for r in conn.execute("PRAGMA index_list(revoked_tokens)")}
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert cols == {"jti": "VARCHAR(64)", "token_type": "VARCHAR(16)", "expires_at": "INTEGER",
                    "revoked_at": "DATETIME"}
    assert "ix_revoked_tokens_expires_at" in idx and version == 11
    # The original is untouched.
    with sqlite3.connect(base) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
