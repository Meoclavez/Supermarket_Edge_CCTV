"""Online access: trusted proxies (tailscale serve, cloudflared), HSTS, the
public hostname changing at handover, and the read-only Tailscale status.

Header sets below are what the proxies really send: tailscale serve (ipn/
ipnlocal/serve.go: X-Forwarded-Host/-Proto/-For, Tailscale-User-*, Host kept,
client copies of those removed) and cloudflared (CF-Connecting-IP, CF-Ray,
CF-Visitor, X-Forwarded-For appended, X-Forwarded-Proto). Each is tested both
raw (loopback peer, as the app sees it without uvicorn's proxy-header
handling) and as uvicorn's ProxyHeadersMiddleware rewrites it (peer and scheme
taken from X-Forwarded-For/-Proto), because production runs with the latter.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.main import app
from app.services import public_exposure, remote_access_service as ra_mod
from app.services.auth_service import general_rate_limiter, intrusion_detector
from app.services.remote_access_service import RemoteAccessService, remote_access_service

TS_HOST = "store-box.tail1234.ts.net"
PUBLIC = "cctv.test-owner.com.au"
CUSTOMER = "cctv.customer-store.com.au"
FAKE_CF = Path(__file__).resolve().parent / "fixtures" / "fake_cloudflared.sh"


def _token(secret: str) -> str:
    return base64.b64encode(json.dumps(
        {"a": "acct0000000000000000000000000000", "t": "11111111-2222-3333-4444-555555555555",
         "s": base64.b64encode(secret.encode()).decode()}).encode()).decode()


def _req(peer, headers=None, host="edge.lan:8000", scheme="http", path="/"):
    hdrs = [(b"host", host.encode())] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": path, "headers": hdrs, "client": (peer, 5555),
                    "scheme": scheme, "query_string": b"", "server": ("x", 80)})


TAILSCALE_SERVE = {"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Proto": "https",
                   "X-Forwarded-Host": TS_HOST, "Tailscale-User-Login": "owner@example.com",
                   "Tailscale-User-Name": "Owner", "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"}
CLOUDFLARE = {"CF-Connecting-IP": "203.0.113.50", "CF-Ray": "8a1b2c3d4e5f-SYD", "CF-Visitor": '{"scheme":"https"}',
              "X-Forwarded-For": "203.0.113.50", "X-Forwarded-Proto": "https"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", PUBLIC)
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    yield


# --------------------------------------------------------------------------- #
# Classification: client IP, remote vs private, HTTPS
# --------------------------------------------------------------------------- #

def test_tailscale_serve_is_private_https_with_tailnet_client_ip():
    raw = _req("127.0.0.1", TAILSCALE_SERVE, host=TS_HOST)
    assert public_exposure.proxy_kind(raw) == "tailscale"
    assert public_exposure.client_ip(raw) == "100.101.102.103"
    assert public_exposure.came_via_https_proxy(raw)
    assert not public_exposure.is_remote_request(raw)
    # Tagged devices get no Tailscale-User-* headers: the .ts.net host decides.
    tagged = _req("127.0.0.1", {"X-Forwarded-For": "100.64.0.9", "X-Forwarded-Proto": "https"}, host=TS_HOST)
    assert public_exposure.client_ip(tagged) == "100.64.0.9" and not public_exposure.is_remote_request(tagged)
    # As uvicorn hands it to the app in production (peer/scheme rewritten).
    rewritten = _req("100.101.102.103", TAILSCALE_SERVE, host=TS_HOST, scheme="https")
    assert public_exposure.client_ip(rewritten) == "100.101.102.103"
    assert public_exposure.came_via_https_proxy(rewritten)
    assert not public_exposure.is_remote_request(rewritten)


def test_tailnet_user_cannot_spoof_cloudflare_headers_through_serve():
    """tailscaled passes unknown client headers through: CF-* on a .ts.net host is ignored."""
    forged = _req("127.0.0.1", {**TAILSCALE_SERVE, "CF-Connecting-IP": "198.51.100.1"}, host=TS_HOST)
    assert public_exposure.proxy_kind(forged) == "tailscale"
    assert public_exposure.client_ip(forged) == "100.101.102.103"


def test_tailscale_funnel_is_public():
    funnel = _req("127.0.0.1", {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https",
                                "Tailscale-Funnel-Request": "?1"}, host=TS_HOST)
    assert public_exposure.proxy_kind(funnel) == "tailscale_funnel"
    assert public_exposure.is_remote_request(funnel)
    assert public_exposure.client_ip(funnel) == "198.51.100.7"
    rewritten = _req("198.51.100.7", {"Tailscale-Funnel-Request": "?1"}, host=TS_HOST, scheme="https")
    assert public_exposure.is_remote_request(rewritten)


def test_cloudflare_is_remote_https_with_visitor_ip():
    raw = _req("127.0.0.1", CLOUDFLARE, host=PUBLIC)
    assert public_exposure.proxy_kind(raw) == "cloudflare"
    assert public_exposure.client_ip(raw) == "203.0.113.50"
    assert public_exposure.came_via_https_proxy(raw) and public_exposure.is_remote_request(raw)
    # CF-Visitor alone proves HTTPS for a Cloudflare request.
    visitor_only = _req("127.0.0.1", {"CF-Connecting-IP": "203.0.113.50", "CF-Visitor": '{"scheme":"https"}'},
                        host=PUBLIC)
    assert public_exposure.came_via_https_proxy(visitor_only)
    rewritten = _req("203.0.113.50", CLOUDFLARE, host=PUBLIC, scheme="https")
    assert public_exposure.client_ip(rewritten) == "203.0.113.50"
    assert public_exposure.is_remote_request(rewritten) and public_exposure.came_via_https_proxy(rewritten)
    # A Cloudflare route for a name that is not (or no longer) configured is still remote.
    other = _req("203.0.113.50", CLOUDFLARE, host="old-name.example.com", scheme="https")
    assert public_exposure.is_remote_request(other)


def test_direct_clients_cannot_claim_https_or_another_address():
    lan = _req("192.168.1.40", {"X-Forwarded-Proto": "https", "X-Forwarded-For": "8.8.8.8",
                                "Tailscale-User-Login": "x@y"}, host="192.168.1.20:8000")
    assert public_exposure.client_ip(lan) == "192.168.1.40"
    assert not public_exposure.came_via_https_proxy(lan)
    assert not public_exposure.is_remote_request(lan)
    # Plain http over the tailnet IP (today's live setup): private, no HSTS.
    tailnet_http = _req("100.101.102.103", host="100.78.122.93:8000")
    assert not public_exposure.came_via_https_proxy(tailnet_http)
    assert not public_exposure.is_remote_request(tailnet_http)


def test_hsts_only_when_the_request_really_was_https():
    via_serve = TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{TS_HOST}")
    r = via_serve.get("/dashboard", headers=TAILSCALE_SERVE)
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "includesubdomains" not in r.headers["strict-transport-security"].lower()
    # Same proxy, plain http (no X-Forwarded-Proto): no HSTS.
    plain = via_serve.get("/dashboard", headers={"X-Forwarded-For": "100.101.102.103"})
    assert "strict-transport-security" not in plain.headers
    lan = TestClient(app, client=("192.168.1.40", 40000), base_url="http://192.168.1.20:8000")
    assert "strict-transport-security" not in lan.get("/dashboard", headers={"X-Forwarded-Proto": "https"}).headers
    via_cf = TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{PUBLIC}")
    assert via_cf.get("/dashboard", headers=CLOUDFLARE).headers["strict-transport-security"]


def test_first_run_setup_allowed_on_tailnet_refused_via_cloudflare_and_funnel():
    body = {"username": "x", "password": "y" * 12, "setup_code": "AAAA-BBBB"}
    serve = TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{TS_HOST}", raise_server_exceptions=False)
    r = serve.post("/api/v1/setup/admin", json=body, headers=TAILSCALE_SERVE)
    assert "store network" not in r.text  # not refused by the remote guard
    cf = TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{PUBLIC}")
    r = cf.post("/api/v1/setup/admin", json=body, headers=CLOUDFLARE)
    assert r.status_code == 403 and "store network" in r.json()["detail"]
    r = serve.post("/api/v1/setup/admin", json=body,
                   headers={"X-Forwarded-For": "198.51.100.7", "Tailscale-Funnel-Request": "?1"})
    assert r.status_code == 403


def test_pairing_lockout_uses_the_real_phone_address():
    from app.routes.pairing import _ip

    assert _ip(_req("127.0.0.1", CLOUDFLARE, host=PUBLIC)) == "203.0.113.50"
    assert _ip(_req("127.0.0.1", TAILSCALE_SERVE, host=TS_HOST)) == "100.101.102.103"
    assert _ip(_req("192.168.1.9", {"CF-Connecting-IP": "1.1.1.1"})) == "192.168.1.9"


# --------------------------------------------------------------------------- #
# Handover: the public hostname changes; the tunnel token changes
# --------------------------------------------------------------------------- #

@pytest.fixture
def ra(monkeypatch, tmp_path):
    """The real remote-access service on the test database, cloudflared faked."""
    import sqlite3

    from sqlalchemy import create_engine

    from app.models.db_models import Base
    from app.services import secret_store

    engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(engine)
    engine.dispose()

    def clear():
        with sqlite3.connect(str(settings.DATABASE_PATH), timeout=10) as conn:
            conn.execute("DELETE FROM system_setup WHERE key = ?", (ra_mod.SETTINGS_KEY,))
        secret_store.delete_named_secret(settings.STORAGE_DIR, ra_mod.TOKEN_SECRET_NAME)

    clear()
    remote_access_service.reload_settings()
    monkeypatch.setenv("FAKE_CF_DIR", str(tmp_path / "cf"))
    monkeypatch.setattr(remote_access_service, "binary_finder", lambda: str(FAKE_CF))
    monkeypatch.setattr(remote_access_service, "tailscale_reader", lambda: {"installed": False, "state": "not_installed"})
    monkeypatch.setattr(remote_access_service, "_tailscale_cache", None)
    from app.services.auth_service import auth_service

    tokens = auth_service.issue_session_tokens("op-test", "owner")
    yield TestClient(app), {"Authorization": f"Bearer {tokens['access_token']}"}, tmp_path / "cf"
    remote_access_service._stop_supervisor()
    clear()
    remote_access_service.reload_settings()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_hostname_change_moves_the_public_identity(ra, monkeypatch):
    client, op, _ = ra
    assert client.put("/api/v1/remote-access", headers=op, json={"hostname": PUBLIC}).status_code == 200
    monkeypatch.setattr(ra_mod, "this_device_id", lambda: "dev-A")

    class Fake:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            class R:
                status_code = 200
                def json(self_inner): return {"device_id": "dev-A"}
            return R()

    monkeypatch.setattr("httpx.Client", Fake)
    assert client.post("/api/v1/remote-access/verify", headers=op).json()["verified"] is True

    r = client.put("/api/v1/remote-access", headers=op, json={"hostname": f"https://{CUSTOMER}/"})
    s = r.json()
    assert s["hostname"] == CUSTOMER and s["verified"] is False and s["verified_at"] is None

    # The old test domain is no longer this device's public name ...
    old_direct = _req("192.168.1.9", host=PUBLIC)
    assert not public_exposure.is_remote_request(old_direct)
    assert public_exposure.is_remote_request(_req("192.168.1.9", host=CUSTOMER))
    assert f"https://{PUBLIC}" not in public_exposure.configured_origins()
    # ... CORS follows the new name ...
    pre = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    ok = client.options("/api/v1/health", headers={"Origin": f"https://{CUSTOMER}", **pre})
    assert ok.headers.get("access-control-allow-origin") == f"https://{CUSTOMER}"
    old = client.options("/api/v1/health", headers={"Origin": f"https://{PUBLIC}", **pre})
    assert "access-control-allow-origin" not in old.headers
    # ... and HSTS / refusals apply on it (served through cloudflared on loopback).
    via_cf = TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{CUSTOMER}")
    r = via_cf.get("/dashboard", headers=CLOUDFLARE)
    assert r.headers["strict-transport-security"]
    assert via_cf.post("/api/v1/setup/admin", json={}, headers=CLOUDFLARE).status_code == 403
    # Nothing about the old name survives a restart either.
    assert ra_mod.load_settings()["hostname"] == CUSTOMER


def test_new_token_restarts_cloudflared_hostname_change_does_not(ra):
    client, op, cf_dir = ra
    r = client.put("/api/v1/remote-access", headers=op,
                   json={"hostname": PUBLIC, "token": _token("owner-test-tunnel-secret"), "enabled": True})
    assert r.status_code == 200, r.text
    assert _wait(lambda: client.get("/api/v1/remote-access", headers=op).json()["process"] == "connected")
    first = remote_access_service._proc
    assert first is not None and first.poll() is None

    # Routes of a remotely managed tunnel live at Cloudflare: renaming alone keeps the connector.
    client.put("/api/v1/remote-access", headers=op, json={"hostname": CUSTOMER})
    assert remote_access_service._proc is first and first.poll() is None

    # The customer's tunnel token replaces the test one: the connector restarts with it.
    client.put("/api/v1/remote-access", headers=op, json={"token": _token("customer-tunnel-secret-xyz")})
    assert _wait(lambda: remote_access_service._proc is not None and remote_access_service._proc is not first
                 and client.get("/api/v1/remote-access", headers=op).json()["process"] == "connected")
    assert first.poll() is not None
    assert (cf_dir / "count").read_text().strip() == "2"
    from app.services import secret_store

    stored = secret_store.get_named_secret(settings.STORAGE_DIR, ra_mod.TOKEN_SECRET_NAME,
                                           settings.NVR_CREDENTIAL_KEY).decode()
    assert stored == _token("customer-tunnel-secret-xyz")
    # Token never on the command line.
    assert "eyJ" not in (cf_dir / "args.log").read_text()

    # Clearing the test token stops the tunnel.
    r = client.put("/api/v1/remote-access", headers=op, json={"clear_token": True, "enabled": False})
    assert r.json()["process"] == "stopped" and r.json()["token_configured"] is False


# --------------------------------------------------------------------------- #
# Tailscale status (read-only)
# --------------------------------------------------------------------------- #

def _runner(status=None, serve=None, fail=None):
    calls = []

    def run(binary, *args, **kw):
        calls.append(args)
        if fail:
            raise fail
        if args[:1] == ("status",):
            return subprocess.CompletedProcess(args, 0 if status is not None else 1,
                                               json.dumps(status) if status is not None else "",
                                               "" if status is not None else "failed to connect to local tailscaled")
        return subprocess.CompletedProcess(args, 0, json.dumps(serve if serve is not None else {}), "")

    run.calls = calls
    return run


RUNNING = {"BackendState": "Running", "Self": {"DNSName": f"{TS_HOST}."}, "CertDomains": [TS_HOST],
           "CurrentTailnet": {"Name": "owner@example.com", "MagicDNSEnabled": True}}


def test_tailscale_status_states():
    read = ra_mod.read_tailscale_status
    none = read(8000, finder=lambda: None)
    assert none["installed"] is False and none["state"] == "not_installed" and none["url"] is None

    down = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner(status=None))
    assert down["state"] == "unavailable" and "tailscaled" in down["message"]

    denied = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner(fail=PermissionError(13, "denied")))
    assert denied["state"] == "unavailable"
    slow = read(8000, finder=lambda: "/usr/bin/tailscale",
                runner=_runner(fail=subprocess.TimeoutExpired("tailscale", 5)))
    assert slow["state"] == "unavailable"

    no_certs = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner({**RUNNING, "CertDomains": None}))
    assert no_certs["state"] == "running" and no_certs["https_enabled"] is False
    assert "HTTPS Certificates" in no_certs["message"] and no_certs["url"] is None

    not_served = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner(RUNNING, {}))
    assert not_served["serving"] is False and "tailscale serve --bg --https=443 http://127.0.0.1:8000" in not_served["message"]

    web = {"TCP": {"443": {"HTTPS": True}},
           "Web": {f"{TS_HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}
    served = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner(RUNNING, web))
    assert served["serving"] is True and served["url"] == f"https://{TS_HOST}/dashboard"
    assert served["dns_name"] == TS_HOST and served["funnel"] is False

    other_port = read(8001, finder=lambda: "/usr/bin/tailscale", runner=_runner(RUNNING, web))
    assert other_port["serving"] is False and other_port["url"] is None

    funnel = read(8000, finder=lambda: "/usr/bin/tailscale",
                  runner=_runner(RUNNING, {**web, "AllowFunnel": {f"{TS_HOST}:443": True}}))
    assert funnel["funnel"] is True

    stopped = read(8000, finder=lambda: "/usr/bin/tailscale", runner=_runner({**RUNNING, "BackendState": "NeedsLogin"}))
    assert stopped["state"] == "stopped" and stopped["url"] is None


def test_tailscale_status_is_cached_and_in_the_api(ra, monkeypatch):
    client, op, _ = ra
    calls = []

    def reader():
        calls.append(1)
        return {"installed": True, "state": "running", "dns_name": TS_HOST, "url": f"https://{TS_HOST}/dashboard"}

    monkeypatch.setattr(remote_access_service, "tailscale_reader", reader)
    for _ in range(3):
        s = client.get("/api/v1/remote-access", headers=op).json()
    assert s["tailscale"]["url"] == f"https://{TS_HOST}/dashboard"
    assert len(calls) == 1
    assert s["local_origin"].startswith("http://127.0.0.1:")

    def broken():
        raise RuntimeError("boom")

    svc = RemoteAccessService()
    svc.tailscale_reader = broken
    assert svc.tailscale_status()["state"] == "unavailable"
