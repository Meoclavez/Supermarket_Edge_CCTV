"""Remote access (online dashboard): settings, tunnel process manager,
reachability verification and public-exposure hardening.

No real tunnel is ever created: cloudflared is replaced by
tests/fixtures/fake_cloudflared.sh and HTTPS verification by a fake client.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.requests import Request

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import public_exposure, secret_store
from app.services import remote_access_service as ra_mod
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.remote_access_service import RemoteAccessService, remote_access_service

FAKE_CF = Path(__file__).resolve().parent / "fixtures" / "fake_cloudflared.sh"
TOKEN = base64.b64encode(json.dumps(
    {"a": "acct0000000000000000000000000000", "t": "11111111-2222-3333-4444-555555555555",
     "s": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA=="}).encode()).decode()
HOST = "cctv.example-store.com.au"


def _wait(pred, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _clear_persisted():
    with sqlite3.connect(str(settings.DATABASE_PATH), timeout=10) as conn:
        conn.execute("DELETE FROM system_setup WHERE key = ?", (ra_mod.SETTINGS_KEY,))
    secret_store.delete_named_secret(settings.STORAGE_DIR, ra_mod.TOKEN_SECRET_NAME)


@pytest.fixture(autouse=True)
def _schema_and_cleanup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    _clear_persisted()
    remote_access_service.reload_settings()
    monkeypatch.setenv("FAKE_CF_DIR", str(tmp_path / "cf"))
    monkeypatch.setattr(remote_access_service, "binary_finder", lambda: str(FAKE_CF))
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    yield
    remote_access_service._stop_supervisor()
    _clear_persisted()
    remote_access_service.reload_settings()


@pytest.fixture
def operator(monkeypatch):
    """Real authentication with an operator session."""
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    tokens = auth_service.issue_session_tokens("op-test", "owner")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Settings round trip
# --------------------------------------------------------------------------- #

def test_settings_round_trip_never_returns_token(client, operator, tmp_path):
    r = client.get("/api/v1/remote-access", headers=operator)
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["token_configured"] is False

    pasted = f"sudo cloudflared service install {TOKEN}"  # whole install command
    r = client.put("/api/v1/remote-access", headers=operator,
                   json={"hostname": f"https://{HOST.upper()}/", "token": pasted})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hostname"] == HOST
    assert body["token_configured"] is True
    assert TOKEN not in r.text and "token\":" not in r.text.replace("token_configured", "")

    again = client.get("/api/v1/remote-access", headers=operator)
    assert again.json()["hostname"] == HOST and again.json()["token_configured"] is True
    assert TOKEN not in again.text

    # Persisted: settings row without the token; token encrypted at rest.
    with sqlite3.connect(str(settings.DATABASE_PATH)) as conn:
        row = conn.execute("SELECT value FROM system_setup WHERE key='remote_access'").fetchone()
    assert json.loads(row[0])["hostname"] == HOST and TOKEN not in row[0]
    enc = (Path(settings.STORAGE_DIR) / "secrets" / "named" / "cloudflare_tunnel_token.enc").read_bytes()
    assert TOKEN.encode() not in enc
    assert secret_store.get_named_secret(settings.STORAGE_DIR, "cloudflare_tunnel_token",
                                         settings.NVR_CREDENTIAL_KEY).decode() == TOKEN

    # Remove the token: reported not configured, and it cannot be enabled.
    r = client.put("/api/v1/remote-access", headers=operator, json={"clear_token": True})
    assert r.json()["token_configured"] is False
    r = client.put("/api/v1/remote-access", headers=operator, json={"enabled": True})
    assert r.status_code == 400 and "token" in r.json()["detail"].lower()


def test_rejects_bad_hostname_and_token(client, operator):
    for bad in ("localhost", "192.168.1.10", "cctv.example.com:8443", "shop.local", "a b.com"):
        r = client.put("/api/v1/remote-access", headers=operator, json={"hostname": bad})
        assert r.status_code == 400, bad
    r = client.put("/api/v1/remote-access", headers=operator, json={"token": "not-a-token"})
    assert r.status_code == 400 and "eyJ" in r.json()["detail"]


def test_stream_token_cannot_reconfigure(client, operator):
    stream = auth_service.generate_stream_token("cam1")
    r = client.put("/api/v1/remote-access", headers={"Authorization": f"Bearer {stream}"},
                   json={"hostname": HOST})
    assert r.status_code == 403


def test_refuses_to_enable_when_auth_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    r = client.put("/api/v1/remote-access", json={"hostname": HOST, "token": TOKEN, "enabled": True})
    assert r.status_code == 400
    assert "AUTH_DISABLED" in r.json()["detail"]
    assert client.get("/api/v1/remote-access").json()["enabled"] is False
    # And a stored "enabled" never starts the tunnel while auth is off.
    run, reason = RemoteAccessService.should_run(
        types.SimpleNamespace(settings={"enabled": True, "provider": "cloudflare_tunnel", "hostname": HOST}))
    assert run is False and "AUTH_DISABLED" in reason


def test_enable_starts_and_disable_stops_tunnel(client, operator, tmp_path):
    r = client.put("/api/v1/remote-access", headers=operator,
                   json={"hostname": HOST, "token": TOKEN, "enabled": True})
    assert r.status_code == 200, r.text
    assert _wait(lambda: client.get("/api/v1/remote-access", headers=operator).json()["process"] == "connected")
    proc = remote_access_service._proc
    assert proc is not None and proc.poll() is None
    r = client.put("/api/v1/remote-access", headers=operator, json={"enabled": False})
    assert r.json()["process"] == "stopped"
    assert proc.poll() is not None


# --------------------------------------------------------------------------- #
# Process manager
# --------------------------------------------------------------------------- #

def _service(tmp_path) -> RemoteAccessService:
    secret_store.set_named_secret(settings.STORAGE_DIR, "cloudflare_tunnel_token", TOKEN, settings.NVR_CREDENTIAL_KEY)
    svc = RemoteAccessService()
    svc.binary_finder = lambda: str(FAKE_CF)
    svc.backoff_base = 0.2
    svc.backoff_max = 1.0
    svc._settings.update(enabled=True, provider="cloudflare_tunnel", hostname=HOST)
    return svc


def test_process_start_crash_backoff_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setenv("FAKE_CF_CRASHES", "2")
    cf_dir = tmp_path / "cf"
    svc = _service(tmp_path)
    t0 = time.monotonic()
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error" and svc.restarts >= 1, timeout=5)
        assert "exited with code 1" in svc.last_error and "not valid" in svc.last_error
        assert _wait(lambda: svc.state == "connected", timeout=10)
        # Two crashes, backoff 0.2 s then 0.4 s before the third start.
        assert svc.restarts == 2
        assert time.monotonic() - t0 >= 0.6
        proc = svc._proc
        assert proc is not None and proc.poll() is None
    finally:
        svc._stop_supervisor()
    assert svc.state == "stopped"
    assert proc.poll() is not None

    args = (cf_dir / "args.log").read_text()
    keys = (cf_dir / "envkeys.log").read_text()
    assert TOKEN not in args, "token must never be on argv"
    assert "tunnel --no-autoupdate run" in args
    assert "TUNNEL_TOKEN" in keys
    assert (cf_dir / "count").read_text().strip() == "3"


def test_missing_binary_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    svc.binary_finder = lambda: None
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error", timeout=3)
        assert "cloudflared is not installed" in svc.last_error
    finally:
        svc._stop_supervisor()


def test_token_never_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    caplog.set_level("DEBUG", logger="edge.remote_access")
    svc._secret_redactions = (TOKEN,)
    svc._handle_log_line(f"2026-09-23T00:00:00Z ERR bad token {TOKEN}")
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "connected")
    finally:
        svc._stop_supervisor()
    assert TOKEN not in caplog.text
    assert "[REDACTED]" in caplog.text


# --------------------------------------------------------------------------- #
# Verification compares device_id
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _fake_client(status, body, seen):
    class C:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            seen.append(url)
            if isinstance(body, Exception):
                raise body
            return _FakeResp(status, body)

    return C


def test_verify_compares_device_id(tmp_path, monkeypatch):
    # device_identity.py is built by another work stream; stub it here only.
    stub = types.ModuleType("app.services.device_identity")
    stub.get_identity = lambda: {"device_id": "dev-this-one", "device_name": "Test", "app_version": "x", "api_version": 1}
    monkeypatch.setitem(sys.modules, "app.services.device_identity", stub)
    svc = RemoteAccessService()
    svc._settings.update(enabled=True, hostname=HOST)
    seen: list[str] = []

    ok = svc.verify(client_factory=_fake_client(200, {"device_id": "dev-this-one"}, seen))
    assert seen == [f"https://{HOST}/api/v1/device/identity"]
    assert ok["verified"] is True and ok["verified_at"]
    assert svc.status()["verified"] is True

    other = svc.verify(client_factory=_fake_client(200, {"device_id": "dev-another-store"}, seen))
    assert other["verified"] is False and "different device" in other["verify_error"]
    assert svc.status()["verified"] is False

    down = svc.verify(client_factory=_fake_client(0, ConnectionError("dns"), seen))
    assert down["verified"] is False and "not reachable" in down["verify_error"]

    err = svc.verify(client_factory=_fake_client(530, {}, seen))
    assert err["verified"] is False and "530" in err["verify_error"]


def test_verify_endpoint(client, operator, monkeypatch):
    client.put("/api/v1/remote-access", headers=operator, json={"hostname": HOST})
    monkeypatch.setattr(ra_mod, "this_device_id", lambda: "dev-A")
    monkeypatch.setattr("httpx.Client", _fake_client(200, {"device_id": "dev-A"}, []))
    r = client.post("/api/v1/remote-access/verify", headers=operator)
    assert r.status_code == 200 and r.json()["verified"] is True
    # A hostname change clears the verification.
    r = client.put("/api/v1/remote-access", headers=operator, json={"hostname": "other." + HOST})
    assert r.json()["verified"] is False


# --------------------------------------------------------------------------- #
# Trusted-proxy client IP
# --------------------------------------------------------------------------- #

def _req(peer, headers=None, host="edge.lan:8000", scheme="http"):
    hdrs = [(b"host", host.encode())] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": hdrs,
                    "client": (peer, 5555), "scheme": scheme, "query_string": b"", "server": ("x", 80)})


def test_client_ip_trusts_proxy_headers_only_from_loopback():
    cip = public_exposure.client_ip
    assert cip(_req("127.0.0.1", {"CF-Connecting-IP": "203.0.113.7"})) == "203.0.113.7"
    assert cip(_req("::1", {"CF-Connecting-IP": "2001:db8::1"})) == "2001:db8::1"
    assert cip(_req("127.0.0.1", {"X-Forwarded-For": "10.9.9.9, 198.51.100.4"})) == "198.51.100.4"
    assert cip(_req("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"})) == "127.0.0.1"
    # A LAN client cannot spoof its address to dodge the lockout.
    assert cip(_req("192.168.1.50", {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})) == "192.168.1.50"


def test_lockout_uses_real_remote_ip(monkeypatch):
    """Two internet users behind cloudflared (both peer 127.0.0.1) are counted separately."""
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    for _ in range(intrusion_detector.max_attempts):
        with pytest.raises(Exception):
            auth_service.verify_api_access(
                _req("127.0.0.1", {"CF-Connecting-IP": "203.0.113.66", "Authorization": "Bearer bad"}),
                api_key=None, bearer=None, token=None)
    assert "203.0.113.66" in intrusion_detector.failed_attempts
    assert "127.0.0.1" not in intrusion_detector.failed_attempts


def test_is_remote_request(monkeypatch):
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    assert public_exposure.is_remote_request(_req("127.0.0.1", host=HOST))
    assert public_exposure.is_remote_request(_req("127.0.0.1", {"CF-Ray": "abc"}))
    assert not public_exposure.is_remote_request(_req("127.0.0.1"))
    assert not public_exposure.is_remote_request(_req("192.168.1.9", {"CF-Ray": "abc"}))


# --------------------------------------------------------------------------- #
# Setup refused remotely; headers; CORS
# --------------------------------------------------------------------------- #

def test_setup_endpoints_refused_via_remote_host(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    remote = TestClient(app, base_url=f"https://{HOST}")
    for path in ("/api/v1/setup/admin", "/api/v1/setup/camera-scan", "/api/v1/setup/complete"):
        r = remote.post(path, json={"username": "x", "password": "y" * 10, "setup_code": "AAAA-BBBB"})
        assert r.status_code == 403, path
        assert "store network" in r.json()["detail"]
    assert remote.get("/docs").status_code == 403
    assert remote.get("/api/v1/setup/status").status_code == 200  # read-only, used by the app
    # The same endpoint on the LAN address is not refused by the remote guard.
    local = TestClient(app)
    r = local.post("/api/v1/setup/admin", json={"username": "x", "password": "y" * 10, "setup_code": "AAAA-BBBB"})
    assert "store network" not in r.text


def test_remote_requests_refused_while_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    r = TestClient(app, base_url=f"https://{HOST}").get("/api/v1/health")
    assert r.status_code == 403 and "AUTH_DISABLED" in r.json()["detail"]


def test_security_headers(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    local = TestClient(app).get("/dashboard")
    assert local.headers["x-content-type-options"] == "nosniff"
    assert local.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert local.headers["x-frame-options"] == "SAMEORIGIN"
    assert local.headers["referrer-policy"] == "same-origin"
    assert "strict-transport-security" not in local.headers  # plain-HTTP LAN: never HSTS

    remote = TestClient(app, base_url=f"https://{HOST}").get("/dashboard")
    assert remote.status_code == 200
    assert remote.headers["strict-transport-security"].startswith("max-age=")
    assert remote.headers["x-content-type-options"] == "nosniff"


def test_cors_only_own_origins(monkeypatch):
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    c = TestClient(app)
    pre = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    ok = c.options("/api/v1/health", headers={"Origin": f"https://{HOST}", **pre})
    assert ok.headers.get("access-control-allow-origin") == f"https://{HOST}"
    bad = c.options("/api/v1/health", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers
    simple = c.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_mjpeg_token_in_query_works_through_proxy(monkeypatch):
    """<img src="/stream?...&token="> must authenticate via the tunnel, and the
    remote user's address (not 127.0.0.1) is what gets recorded."""
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    tok = auth_service.issue_session_tokens("op-test", "owner")["access_token"]
    req = Request({"type": "http", "method": "GET", "path": "/stream", "scheme": "http", "server": ("x", 80),
                   "headers": [(b"host", HOST.encode()), (b"cf-connecting-ip", b"203.0.113.9"),
                               (b"x-forwarded-proto", b"https")],
                   "client": ("127.0.0.1", 40000), "query_string": f"camera_id=c1&token={tok}".encode()})
    assert public_exposure.is_remote_request(req)
    assert auth_service.verify_api_access(req, api_key=None, bearer=None, token=None) is True
    stream_tok = auth_service.generate_stream_token("c1")
    assert auth_service.verify_stream_access("c1", req, token=stream_tok, bearer=None)["camera_id"] == "c1"
    # A bad token is charged to the remote user's address, not to the tunnel.
    bad = Request({**req.scope, "query_string": b"camera_id=c1&token=bad"})
    with pytest.raises(Exception):
        auth_service.verify_api_access(bad, api_key=None, bearer=None, token=None)
    assert "203.0.113.9" in intrusion_detector.failed_attempts
    assert "127.0.0.1" not in intrusion_detector.failed_attempts


def test_caddy_block_for_direct_provider(client, operator):
    r = client.put("/api/v1/remote-access", headers=operator, json={"provider": "direct", "hostname": HOST})
    block = r.json()["caddy_site_block"]
    assert block.startswith(f"{HOST} {{") and f"reverse_proxy 127.0.0.1:{settings.PORT}" in block
    assert r.json()["process"] == "stopped"


def test_bootstrap_checksum_parsing():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bootstrap

    release = {"body": "```\ncloudflared-linux-amd64: " + "a" * 64 + "\ncloudflared-linux-arm64: " + "b" * 64 + "\n```",
               "assets": [{"name": "cloudflared-linux-amd64", "digest": "sha256:" + "a" * 64}]}
    assert bootstrap.published_sha256(release, "cloudflared-linux-amd64") == ("a" * 64, "a" * 64)
    assert bootstrap.published_sha256(release, "cloudflared-linux-arm64") == ("b" * 64, None)
    assert bootstrap.published_sha256(release, "cloudflared-linux-arm") == (None, None)


def test_stop_during_start_never_leaves_orphan(tmp_path, monkeypatch):
    """Stop requested before the supervisor reaches Popen: the child must not survive."""
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    real_read = ra_mod._read_token

    def slow_read():
        time.sleep(0.5)  # stop() lands while the supervisor is still starting
        return real_read()

    monkeypatch.setattr(ra_mod, "_read_token", slow_read)
    svc.apply()
    time.sleep(0.1)
    svc._stop_supervisor(timeout=10)
    assert not svc._thread
    proc = svc._proc
    assert proc is None or proc.poll() is not None
    time.sleep(0.3)
    import subprocess as sp
    alive = sp.run(["pgrep", "-f", str(FAKE_CF)], capture_output=True, text=True).stdout.split()
    assert not alive, alive
