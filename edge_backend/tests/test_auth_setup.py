"""First-run protection, DEBUG/auth decoupling and the operator recovery CLI.

These tests run with real authentication (AUTH_DISABLED=false) against the
suite's throwaway database, and restore that database's operator/setup rows
afterwards so other test modules see the state they expect.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import auth_service as auth_mod
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector, verify_password
from app.services.setup_service import setup_code_manager, setup_code_path

EDGE_BACKEND = Path(__file__).resolve().parent.parent
CLI = EDGE_BACKEND / "scripts" / "manage_operator.py"
SETUP_KEYS = ("setup_completed", "setup_step", "auth_epoch")


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _reset_limits() -> None:
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)


@pytest.fixture
def client(monkeypatch):
    """Real auth on, DEBUG on (to prove it no longer opens anything), clean first-run state."""
    sync_engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    with _db() as conn:
        saved_users = conn.execute("SELECT * FROM admin_users").fetchall()
        saved_setup = conn.execute(
            f"SELECT key, value, updated_at FROM system_setup WHERE key IN ({','.join('?' * len(SETUP_KEYS))})",
            SETUP_KEYS,
        ).fetchall()
        conn.execute("DELETE FROM admin_users")
        conn.execute(f"DELETE FROM system_setup WHERE key IN ({','.join('?' * len(SETUP_KEYS))})", SETUP_KEYS)
    setup_code_manager.invalidate()
    _reset_limits()

    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setattr(settings, "DEBUG", True)
    yield TestClient(app)

    with _db() as conn:
        conn.execute("DELETE FROM admin_users")
        conn.execute(f"DELETE FROM system_setup WHERE key IN ({','.join('?' * len(SETUP_KEYS))})", SETUP_KEYS)
        if saved_users:
            conn.executemany(
                f"INSERT INTO admin_users VALUES ({','.join('?' * len(saved_users[0]))})", saved_users
            )
        conn.executemany("INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?)", saved_setup)
    setup_code_manager.invalidate()
    _reset_limits()


def _issued_code(client) -> str:
    status = client.get("/api/v1/auth/status").json()
    assert status["admin_exists"] is False and status["setup_code_required"] is True
    code = setup_code_manager.read()
    assert code, "a setup code must exist while no operator account exists"
    return code


def _create_admin(client, code, username="manager", password="correct horse 1"):
    return client.post("/api/v1/setup/admin", json={
        "username": username, "password": password, "display_name": "Manager", "setup_code": code,
    })


# --- first run ------------------------------------------------------------------

def test_setup_code_file_is_private_and_well_formed(client):
    code = _issued_code(client)
    assert len(code) == 9 and code[4] == "-"
    mode = stat.S_IMODE(os.stat(setup_code_path()).st_mode)
    assert mode == 0o600, oct(mode)


def test_first_run_requires_the_setup_code(client):
    code = _issued_code(client)

    missing = client.post("/api/v1/setup/admin", json={"username": "intruder", "password": "whatever123"})
    assert missing.status_code == 403
    wrong = _create_admin(client, "AAAA-AAAA", username="intruder")
    assert wrong.status_code == 403
    assert "setup code" in wrong.json()["detail"].lower()
    assert client.get("/api/v1/auth/status").json()["admin_exists"] is False

    ok = _create_admin(client, code.lower().replace("-", ""))   # case/dash insensitive
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["access_token"] and body["refresh_token"]

    # The returned session is a real one ...
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/v1/auth/status", headers=headers).json()["authenticated"] is True
    assert client.get("/api/v1/cameras", headers=headers).status_code == 200
    # ... and the code is spent.
    assert setup_code_manager.read() is None
    assert not setup_code_path().exists()


def test_weak_password_is_refused_without_consuming_the_code(client):
    code = _issued_code(client)
    weak = _create_admin(client, code, password="short")
    assert weak.status_code == 400
    assert setup_code_manager.read() == code
    assert _create_admin(client, code).status_code == 200


def test_second_admin_creation_is_refused(client):
    code = _issued_code(client)
    assert _create_admin(client, code).status_code == 200
    again = _create_admin(client, code, username="second")
    assert again.status_code == 403
    assert "already exists" in again.json()["detail"]
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0] == 1


def test_setup_step_endpoints_need_code_before_and_session_after(client):
    code = _issued_code(client)
    # Before an account exists: closed without the code, open with it.
    assert client.post("/api/v1/setup/hardware-scan").status_code == 403
    assert client.post("/api/v1/setup/hardware-scan", headers={"X-Setup-Code": code}).status_code == 200

    token = _create_admin(client, code).json()["access_token"]
    # After: the old code is worthless and a session is required.
    assert client.post("/api/v1/setup/hardware-scan").status_code == 401
    assert client.post("/api/v1/setup/hardware-scan", headers={"X-Setup-Code": code}).status_code == 401
    assert client.post("/api/v1/setup/network-config").status_code == 401
    assert client.post("/api/v1/setup/complete").status_code == 401
    ok = client.post("/api/v1/setup/hardware-scan", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_stale_setup_completed_flag_without_admin_is_not_completed(client):
    with _db() as conn:
        conn.execute("INSERT INTO system_setup (key, value, updated_at) VALUES "
                     "('setup_completed', 'true', '2026-09-05 00:00:00'), "
                     "('setup_step', '2', '2026-09-05 00:00:00')")
    status = client.get("/api/v1/setup/status").json()
    assert status["is_completed"] is False
    assert status["current_step"] == 1
    # And the stale flag does not open the step endpoints either.
    assert client.post("/api/v1/setup/camera-scan").status_code == 403


def test_wrong_setup_codes_are_rate_limited(client):
    _issued_code(client)
    statuses = [_create_admin(client, "ZZZZ-ZZZZ").status_code for _ in range(intrusion_detector.max_attempts)]
    assert statuses[:-1] == [403] * (intrusion_detector.max_attempts - 1)
    assert statuses[-1] == 429
    # Even the right code is refused while locked out.
    assert _create_admin(client, setup_code_manager.read()).status_code == 429


# --- DEBUG no longer disables auth ------------------------------------------------

def test_debug_true_does_not_bypass_auth(client):
    assert settings.DEBUG is True and settings.AUTH_DISABLED is False
    assert client.get("/api/v1/cameras").status_code == 401
    assert client.get("/api/v1/auth/status").json()["debug_bypass_active"] is False


def test_live_mjpeg_stream_requires_auth(client):
    # /stream serves live camera video; it must not be readable signed out.
    assert client.get("/stream").status_code == 401
    assert client.get("/stream?camera_id=cam_x&overlay=1").status_code == 401
    assert client.get("/stream?camera_id=cam_x&token=not-a-token").status_code == 401


def test_auth_disabled_is_the_only_bypass(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    assert client.get("/api/v1/cameras").status_code == 200
    assert client.get("/api/v1/auth/status").json()["debug_bypass_active"] is True


def test_login_success_records_last_login_and_failures_are_401(client):
    code = _issued_code(client)
    _create_admin(client, code)
    assert client.post("/api/v1/auth/login", json={"username": "manager", "password": "nope-nope"}).status_code == 401
    ok = client.post("/api/v1/auth/login", json={"username": "manager", "password": "correct horse 1"})
    assert ok.status_code == 200 and ok.json()["access_token"]
    with _db() as conn:
        assert conn.execute("SELECT last_login FROM admin_users WHERE username='manager'").fetchone()[0]


# --- recovery CLI -----------------------------------------------------------------

def _cli(*args, stdin: str | None = None, db: Path | None = None, storage: Path | None = None):
    env = dict(os.environ)
    env["STORAGE_DIR"] = str(storage or Path(settings.STORAGE_DIR))
    argv = [sys.executable, str(CLI)]
    if db is not None:
        argv += ["--db", str(db)]
    else:
        env["DATABASE_PATH"] = env["SQLITE_DB_PATH"] = str(settings.DATABASE_PATH)
    return subprocess.run(argv + list(args), input=stdin, capture_output=True, text=True, env=env, timeout=120)


def test_cli_reset_password_and_reset_setup_on_temp_db(tmp_path):
    db = tmp_path / "ops.db"

    r = _cli("create", "--username", "owner1", "--password-stdin", stdin="first-pass-1\n", db=db, storage=tmp_path)
    assert r.returncode == 0, r.stderr
    r = _cli("list", db=db, storage=tmp_path)
    assert r.returncode == 0 and "owner1" in r.stdout and "never" in r.stdout
    assert "$2" not in r.stdout   # never prints bcrypt hashes

    weak = _cli("reset-password", "--username", "owner1", "--password-stdin", stdin="short\n", db=db, storage=tmp_path)
    assert weak.returncode != 0 and "at least 8" in (weak.stdout + weak.stderr)
    r = _cli("reset-password", "--username", "owner1", "--password-stdin", stdin="second-pass-2\n", db=db, storage=tmp_path)
    assert r.returncode == 0, r.stderr
    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT password_hash FROM admin_users WHERE username='owner1'").fetchone()[0]
        epoch = conn.execute("SELECT value FROM system_setup WHERE key='auth_epoch'").fetchone()[0]
    assert verify_password("second-pass-2", stored) and not verify_password("first-pass-1", stored)
    assert epoch == "1"

    refused = _cli("reset-setup", db=db, storage=tmp_path)   # no tty, no --yes
    assert refused.returncode != 0
    r = _cli("reset-setup", "--yes", db=db, storage=tmp_path)
    assert r.returncode == 0, r.stderr
    code = (tmp_path / "setup_code.txt").read_text().strip()
    assert f"SETUP CODE:  {code}" in r.stdout
    assert stat.S_IMODE(os.stat(tmp_path / "setup_code.txt").st_mode) == 0o600
    assert list((tmp_path / "backups").glob("edge_cctv_*_pre_reset_setup.db"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM system_setup WHERE key='auth_epoch'").fetchone()[0] == "2"


def test_cli_reset_setup_takes_effect_on_a_running_server(client):
    """The CLI runs out of process: the live app must drop old sessions and accept the new code."""
    token = _create_admin(client, _issued_code(client)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/cameras", headers=headers).status_code == 200

    r = _cli("reset-setup", "--yes")
    assert r.returncode == 0, r.stderr
    auth_mod._epoch_cache.update(at=0.0)   # skip the 2 s cache instead of sleeping

    assert client.get("/api/v1/cameras", headers=headers).status_code == 401
    status = client.get("/api/v1/auth/status", headers=headers).json()
    assert status["admin_exists"] is False and status["authenticated"] is False
    new_code = setup_code_manager.read()
    assert new_code and f"SETUP CODE:  {new_code}" in r.stdout
    assert _create_admin(client, new_code, username="newowner").status_code == 200


def test_alert_websocket_refuses_missing_or_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    for url in ("/api/v1/events/ws", "/api/v1/events/ws?token=not-a-jwt"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        assert exc.value.code == 1008


def test_alert_websocket_delivers_alerts_to_a_valid_session(client):
    from app.services.notification_service import alert_hub

    token = auth_service.issue_session_tokens("ws-test-user", "owner")["access_token"]
    with client.websocket_connect(f"/api/v1/events/ws?token={token}") as ws:
        ws.portal.call(alert_hub.broadcast_event, {"event_type": "THEFT_SUSPECTED", "probe": "ws-ok"})
        assert ws.receive_json()["probe"] == "ws-ok"
