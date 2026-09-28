"""Requests arriving through the VPS tunnel: who the client is, whether the
request is remote, HTTPS/HSTS, refusals, CORS and the public address changing.

Header sets below are what the chain really delivers (measured with frp 0.71
and Caddy 2.11): the VPS proxy appends the visitor to X-Forwarded-For (a
spoofed value the visitor sent stays on the left, or is dropped), frps
appends the VPS proxy's own address and overwrites X-Forwarded-Proto with
"http", X-Real-IP is passed through unchanged (so it is never believed), and
frpc connects to uvicorn from 127.0.0.1 with Host = the public address.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.main import app
from app.services import public_exposure
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.remote_access_service import remote_access_service

PUBLIC = "cctv.test-owner.com.au"
CUSTOMER = "cctv.customer-store.com.au"
VISITOR = "203.0.113.50"
VPS_PROXY = "172.18.0.4"  # the reverse proxy container on the VPS's docker network

# nginx / Nginx Proxy Manager ($proxy_add_x_forwarded_for keeps what the visitor sent)
TUNNEL_NGINX = {"X-Forwarded-For": f"6.6.6.6, {VISITOR}, {VPS_PROXY}", "X-Forwarded-Proto": "http",
                "X-Forwarded-Host": PUBLIC, "X-Real-IP": "7.7.7.7"}
# Traefik / Caddy (untrusted incoming X-Forwarded-For replaced)
TUNNEL_TRAEFIK = {"X-Forwarded-For": f"{VISITOR}, {VPS_PROXY}", "X-Forwarded-Proto": "http",
                  "X-Forwarded-Host": PUBLIC}


def _req(peer, headers=None, host="edge.lan:8000", scheme="http", path="/"):
    hdrs = [(b"host", host.encode())] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": path, "headers": hdrs, "client": (peer, 5555),
                    "scheme": scheme, "query_string": b"", "server": ("x", 80)})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", PUBLIC)
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    yield
    intrusion_detector.failed_attempts.clear()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("headers", [TUNNEL_NGINX, TUNNEL_TRAEFIK])
def test_tunnel_request_is_remote_https_with_visitor_ip(headers):
    for host in (PUBLIC, f"{PUBLIC}:443", PUBLIC.upper(), f"{PUBLIC}."):
        r = _req("127.0.0.1", headers, host=host)
        assert public_exposure.proxy_kind(r) == "tunnel", host
        assert public_exposure.client_ip(r) == VISITOR, host
        assert public_exposure.is_remote_request(r), host
        # frps says X-Forwarded-Proto: http, but the browser's connection was HTTPS.
        assert public_exposure.came_via_https_proxy(r), host


def test_tunnel_client_ip_edge_cases():
    cip = public_exposure.client_ip
    # VPS proxy added nothing: frps's entry (the proxy) is all we know; never a visitor-chosen value.
    assert cip(_req("127.0.0.1", {"X-Forwarded-For": VPS_PROXY}, host=PUBLIC)) == VPS_PROXY
    assert cip(_req("::1", {"X-Forwarded-For": f"2001:db8::7, {VPS_PROXY}"}, host=PUBLIC)) == "2001:db8::7"
    # Garbage in the visitor's position falls back to the proxy, not further left.
    assert cip(_req("127.0.0.1", {"X-Forwarded-For": f"1.2.3.4, not-an-ip, {VPS_PROXY}"}, host=PUBLIC)) == VPS_PROXY
    assert cip(_req("127.0.0.1", {}, host=PUBLIC)) == "127.0.0.1"
    # X-Real-IP is passed through frps unchanged: never believed.
    assert cip(_req("127.0.0.1", {"X-Real-IP": "7.7.7.7", "X-Forwarded-For": f"{VISITOR}, {VPS_PROXY}"},
                    host=PUBLIC)) == VISITOR


@pytest.mark.parametrize("xff,expected", [
    # nginx / NPM not trusting the CDN: CDN appended the visitor, nginx the CDN edge.
    (f"6.6.6.6, {VISITOR}, 198.51.100.200, {VPS_PROXY}", VISITOR),
    # nginx with real_ip from the CDN ($remote_addr = visitor) appends the visitor again.
    (f"6.6.6.6, {VISITOR}, {VISITOR}, {VPS_PROXY}", VISITOR),
    # Traefik/Caddy not trusting the CDN replace the header: only the CDN edge is known, never a forged value.
    (f"198.51.100.200, {VPS_PROXY}", "198.51.100.200"),
])
def test_cdn_in_front_of_the_vps(monkeypatch, xff, expected):
    monkeypatch.setitem(remote_access_service._settings, "extra_proxies", 1)
    assert public_exposure.client_ip(_req("127.0.0.1", {"X-Forwarded-For": xff}, host=PUBLIC)) == expected


def test_extra_proxies_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setitem(remote_access_service._settings, "extra_proxies", 7)
    assert remote_access_service.forwarding_hops() == 1
    assert public_exposure.client_ip(_req("127.0.0.1", TUNNEL_NGINX, host=PUBLIC)) == VISITOR


def test_spoofed_forwarding_headers_from_the_network_are_ignored():
    """A LAN/tailnet client connecting directly cannot choose its address or claim HTTPS."""
    for host in ("192.168.1.20:8000", PUBLIC):
        lan = _req("192.168.1.40", {"X-Forwarded-For": f"8.8.8.8, {VISITOR}", "X-Forwarded-Proto": "https",
                                    "X-Real-IP": "9.9.9.9"}, host=host)
        assert public_exposure.client_ip(lan) == "192.168.1.40"
        assert public_exposure.proxy_kind(lan) is None
        assert not public_exposure.came_via_https_proxy(lan)
    # Naming the public address from the LAN only adds restrictions.
    assert public_exposure.is_remote_request(_req("192.168.1.40", host=PUBLIC))
    assert not public_exposure.is_remote_request(_req("192.168.1.40", host="192.168.1.20:8000"))


def test_private_tailscale_ip_and_lan_stay_private_without_hsts():
    """The owner's access today: plain http://<tailscale ip>:8000."""
    tailnet = _req("100.101.102.103", host="100.78.122.93:8000")
    assert not public_exposure.is_remote_request(tailnet)
    assert not public_exposure.came_via_https_proxy(tailnet)
    assert public_exposure.client_ip(tailnet) == "100.101.102.103"
    lan = TestClient(app, client=("192.168.1.40", 40000), base_url="http://192.168.1.20:8000")
    r = lan.get("/dashboard", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "strict-transport-security" not in r.headers


def test_another_local_proxy_is_handled_generically():
    local = _req("127.0.0.1", {"X-Forwarded-For": "10.9.9.9, 192.168.1.77", "X-Forwarded-Proto": "https"},
                 host="edge.lan")
    assert public_exposure.proxy_kind(local) == "proxy"
    assert public_exposure.client_ip(local) == "192.168.1.77"
    assert public_exposure.came_via_https_proxy(local)
    assert not public_exposure.is_remote_request(local)


def test_funnel_header_is_treated_as_public():
    assert public_exposure.is_remote_request(_req("127.0.0.1", {"Tailscale-Funnel-Request": "?1"},
                                                  host="box.tail1234.ts.net"))


def test_rewritten_peer_is_safe_and_warned_once(caplog, monkeypatch):
    """uvicorn with proxy headers on hands over the VPS proxy as peer: still remote, never the forged value."""
    monkeypatch.setattr(public_exposure, "_warned_rewritten_peer", False)
    caplog.set_level("WARNING", logger="edge.security")
    r = _req(VPS_PROXY, TUNNEL_NGINX, host=PUBLIC)
    assert public_exposure.client_ip(r) == VPS_PROXY
    assert public_exposure.is_remote_request(r)
    public_exposure.client_ip(r)
    assert caplog.text.count("--no-proxy-headers") == 1


# --------------------------------------------------------------------------- #
# Through the app
# --------------------------------------------------------------------------- #

def _via_tunnel(host=PUBLIC):
    return TestClient(app, client=("127.0.0.1", 40000), base_url=f"http://{host}", raise_server_exceptions=False)


def test_first_run_setup_and_docs_refused_through_the_tunnel():
    body = {"username": "x", "password": "y" * 12, "setup_code": "AAAA-BBBB"}
    tunnel = _via_tunnel()
    for path in ("/api/v1/setup/admin", "/api/v1/setup/camera-scan", "/api/v1/setup/complete"):
        r = tunnel.post(path, json=body, headers=TUNNEL_NGINX)
        assert r.status_code == 403 and "store network" in r.json()["detail"], path
    assert tunnel.get("/docs", headers=TUNNEL_NGINX).status_code == 403
    assert tunnel.get("/openapi.json", headers=TUNNEL_NGINX).status_code == 403
    assert tunnel.get("/api/v1/setup/status", headers=TUNNEL_NGINX).status_code == 200
    # The same request on the LAN address is not refused by the remote guard.
    lan = TestClient(app, client=("192.168.1.40", 40000), raise_server_exceptions=False)
    assert "store network" not in lan.post("/api/v1/setup/admin", json=body).text


def test_remote_requests_refused_while_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    r = _via_tunnel().get("/api/v1/health", headers=TUNNEL_TRAEFIK)
    assert r.status_code == 403 and "AUTH_DISABLED" in r.json()["detail"]


def test_security_headers_and_hsts_through_the_tunnel():
    r = _via_tunnel().get("/dashboard", headers=TUNNEL_TRAEFIK)
    assert r.status_code == 200
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "includesubdomains" not in r.headers["strict-transport-security"].lower()
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_lockout_is_keyed_on_the_visitor_not_the_tunnel():
    """Two visitors behind the tunnel (both peer 127.0.0.1) are counted separately."""
    for _ in range(intrusion_detector.max_attempts):
        with pytest.raises(Exception):
            auth_service.verify_api_access(
                _req("127.0.0.1", {**TUNNEL_NGINX, "Authorization": "Bearer bad"}, host=PUBLIC),
                api_key=None, bearer=None, token=None)
    assert intrusion_detector.key(VISITOR, "token") in intrusion_detector.failed_attempts
    for forged in ("6.6.6.6", "7.7.7.7", "127.0.0.1", VPS_PROXY):
        assert not any(k.startswith(forged) for k in intrusion_detector.failed_attempts), forged


def test_sign_in_through_the_tunnel_charges_the_visitor():
    tunnel = _via_tunnel()
    r = tunnel.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password-123"},
                    headers=TUNNEL_TRAEFIK)
    assert r.status_code in (400, 401, 403, 429)
    keys = list(intrusion_detector.failed_attempts)
    assert keys and all(k.startswith(VISITOR) for k in keys), keys


def test_mjpeg_token_in_query_works_through_the_tunnel():
    tok = auth_service.issue_session_tokens("op-test", "owner")["access_token"]
    req = Request({"type": "http", "method": "GET", "path": "/stream", "scheme": "http", "server": ("x", 80),
                   "headers": [(b"host", PUBLIC.encode()), (b"x-forwarded-for", f"{VISITOR}, {VPS_PROXY}".encode()),
                               (b"x-forwarded-proto", b"http")],
                   "client": ("127.0.0.1", 40000), "query_string": f"camera_id=c1&token={tok}".encode()})
    assert public_exposure.is_remote_request(req)
    assert auth_service.verify_api_access(req, api_key=None, bearer=None, token=None) is True
    stream_tok = auth_service.generate_stream_token("c1")
    assert auth_service.verify_stream_access("c1", req, token=stream_tok, bearer=None)["camera_id"] == "c1"


def test_pairing_lockout_uses_the_real_phone_address():
    from app.routes.pairing import _ip

    assert _ip(_req("127.0.0.1", TUNNEL_NGINX, host=PUBLIC)) == VISITOR
    assert _ip(_req("192.168.1.9", {"X-Forwarded-For": "1.1.1.1"})) == "192.168.1.9"


def test_cors_only_own_origins():
    c = TestClient(app)
    pre = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    ok = c.options("/api/v1/health", headers={"Origin": f"https://{PUBLIC}", **pre})
    assert ok.headers.get("access-control-allow-origin") == f"https://{PUBLIC}"
    bad = c.options("/api/v1/health", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


def test_public_address_change_moves_everything(monkeypatch):
    monkeypatch.setitem(remote_access_service._settings, "hostname", CUSTOMER)
    # The old address is no longer this device's public name ...
    assert not public_exposure.is_remote_request(_req("192.168.1.9", host=PUBLIC))
    assert public_exposure.is_remote_request(_req("192.168.1.9", host=CUSTOMER))
    assert f"https://{PUBLIC}" not in public_exposure.configured_origins()
    assert f"https://{CUSTOMER}" in public_exposure.configured_origins()
    # ... and the tunnel rules follow the new one.
    r = _via_tunnel(CUSTOMER).post("/api/v1/setup/admin", json={}, headers=TUNNEL_TRAEFIK)
    assert r.status_code == 403
    assert public_exposure.client_ip(_req("127.0.0.1", TUNNEL_NGINX, host=CUSTOMER)) == VISITOR
