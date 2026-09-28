"""Online access through the owner's VPS: settings, frpc configuration,
process manager, log-driven status and reachability verification.

No real tunnel is created here: frpc is replaced by tests/fixtures/fake_frpc.sh
(which prints the log lines a real frpc 0.71 prints) and HTTPS verification by
a fake client. tests/test_online_access.py covers request classification; the
real frps/frpc pair is exercised by the integration check in the docs.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import secret_store
from app.services import remote_access_service as ra_mod
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.remote_access_service import RemoteAccessError, RemoteAccessService, remote_access_service

FAKE_FRPC = Path(__file__).resolve().parent / "fixtures" / "fake_frpc.sh"
TOKEN = "k7Qm2VxP9sLr4TzW8nBc3HyJ6dFg1AeU5oKi0MpNqRs"  # 43 chars, test only
HOST = "store1-cctv.example-store.com.au"  # first-level name under the owner's domain
SERVER = "wss://tunnel.example-store.com.au"
STORE = "store1"
SERVER_KEY = "shared-frp-key-0123456789"
FAKE_ENV = ("FAKE_FRPC_DIR", "FAKE_FRPC_CRASHES", "FAKE_FRPC_PROXY_ERROR")


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
    for name in (ra_mod.TOKEN_SECRET_NAME, ra_mod.SERVER_KEY_SECRET_NAME, *ra_mod.LEGACY_SECRET_NAMES):
        secret_store.delete_named_secret(settings.STORAGE_DIR, name)


@pytest.fixture(autouse=True)
def _schema_and_cleanup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    _clear_persisted()
    remote_access_service.reload_settings()
    monkeypatch.setenv("FAKE_FRPC_DIR", str(tmp_path / "frpc"))
    monkeypatch.setattr(remote_access_service, "binary_finder", lambda: str(FAKE_FRPC))
    monkeypatch.setattr(remote_access_service, "config_file", tmp_path / "tunnel" / "frpc.toml")
    monkeypatch.setattr(remote_access_service, "env_passthrough", ("PATH", *FAKE_ENV))
    monkeypatch.setattr(settings, "HOST", "0.0.0.0")
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
# Validation
# --------------------------------------------------------------------------- #

def test_server_url_parsing():
    p = ra_mod.parse_server_url
    assert p("wss://Tunnel.Example.com/") == {"url": "wss://tunnel.example.com", "protocol": "wss",
                                              "host": "tunnel.example.com", "port": 443}
    assert p("tunnel.example.com")["url"] == "wss://tunnel.example.com"
    assert p("https://tunnel.example.com:8443")["url"] == "wss://tunnel.example.com:8443"
    assert p("tcp://vps.example.com:7000") == {"url": "tcp://vps.example.com:7000", "protocol": "tcp",
                                               "host": "vps.example.com", "port": 7000}
    assert p("tcp://vps.example.com")["port"] == 7000
    assert p("") == {}
    for bad in ("http://tunnel.example.com", "ftp://x.example.com", "wss://localhost", "wss://10.0.0.1",
                "wss://user:pw@tunnel.example.com", "wss://tunnel.example.com/path", "wss://tunnel.example.com:0",
                "wss://tunnel.example.com:99999", "wss://shop.local"):
        with pytest.raises(RemoteAccessError):
            p(bad)


def test_token_store_id_and_hostname_validation():
    assert ra_mod.validate_token(f"  {TOKEN} ") == TOKEN
    for bad in ("", "short-token", "has spaces in it " * 3, "x" * 600, "tab\tinside" + "y" * 40):
        with pytest.raises(RemoteAccessError):
            ra_mod.validate_token(bad)
    assert ra_mod.normalise_store_id(" Store_12-A ") == "store_12-a"
    assert ra_mod.normalise_store_id("") == ""
    for bad in ("store.1", "-store", "store-", "a b", "x" * 41, "st/ore"):
        with pytest.raises(RemoteAccessError):
            ra_mod.normalise_store_id(bad)
    # Generic hostnames: first-level names under the owner's domain and deeper ones alike.
    for good in ("store1-cctv.ikorex.com.au", "cctv.store1.example.com", "iga-pearcedale-cctv.example.net"):
        assert ra_mod.normalise_hostname(good) == good


# --------------------------------------------------------------------------- #
# frpc configuration
# --------------------------------------------------------------------------- #

def test_frpc_config_contents_and_permissions(tmp_path):
    server = ra_mod.parse_server_url(SERVER)
    text = ra_mod.build_frpc_config(server=server, hostname=HOST, store_id=STORE, local_port=8123,
                                    ca_file="/etc/ssl/ca.pem")
    import tomllib

    cfg = tomllib.loads(text)
    assert cfg["serverAddr"] == "tunnel.example-store.com.au" and cfg["serverPort"] == 443
    # Per-store identity for the multi-tenant server's Login check.
    assert cfg["user"] == STORE
    assert cfg["metadatas"] == {"token": "{{ .Envs.EDGE_FRP_STORE_TOKEN }}"}
    # No shared frp token unless the server sets one.
    assert cfg["auth"] == {"method": "token"}
    assert cfg["loginFailExit"] is True
    tr = cfg["transport"]
    assert tr["protocol"] == "wss" and tr["tls"]["enable"] is True
    assert tr["heartbeatInterval"] == 30 and tr["heartbeatTimeout"] == 90 and tr["dialServerTimeout"] == 10
    # A CA file is what turns frpc's certificate verification on.
    assert tr["tls"]["trustedCaFile"] == "/etc/ssl/ca.pem" and tr["tls"]["serverName"] == "tunnel.example-store.com.au"
    assert "webServer" not in cfg  # no frpc admin API
    (proxy,) = cfg["proxies"]  # exactly one proxy
    assert proxy == {"name": "store1-cctv", "type": "http", "localIP": "127.0.0.1",
                     "localPort": 8123, "customDomains": [HOST]}
    keyed = tomllib.loads(ra_mod.build_frpc_config(server=server, hostname=HOST, store_id=STORE, local_port=8000,
                                                   ca_file="/ca", server_key=True))
    assert keyed["auth"] == {"method": "token", "token": "{{ .Envs.EDGE_FRP_AUTH_TOKEN }}"}
    tcp = tomllib.loads(ra_mod.build_frpc_config(server=ra_mod.parse_server_url("tcp://vps.example.com:7443"),
                                                 hostname=HOST, store_id=STORE, local_port=8000, ca_file="/ca"))
    assert tcp["transport"]["protocol"] == "tcp" and tcp["serverPort"] == 7443
    override = tomllib.loads(ra_mod.build_frpc_config(server=server, hostname=HOST, store_id=STORE, local_port=8000,
                                                      ca_file="/ca", connect_host="127.0.0.1", connect_port=18443))
    assert override["serverAddr"] == "127.0.0.1" and override["transport"]["tls"]["serverName"] == server["host"]

    path = ra_mod.write_frpc_config(text, tmp_path / "tunnel" / "frpc.toml")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text() == text


def test_ca_bundle_found_and_overridable(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_TUNNEL_CA_FILE", raising=False)
    found = ra_mod.find_ca_bundle()
    assert found and os.path.isfile(found)
    ca = tmp_path / "private-ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("EDGE_TUNNEL_CA_FILE", str(ca))
    assert ra_mod.find_ca_bundle() == str(ca)
    monkeypatch.setenv("EDGE_TUNNEL_CA_FILE", str(tmp_path / "missing.pem"))
    assert ra_mod.find_ca_bundle() is None


# --------------------------------------------------------------------------- #
# Settings API
# --------------------------------------------------------------------------- #

def test_settings_round_trip_never_returns_token(client, operator):
    r = client.get("/api/v1/remote-access", headers=operator)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["token_configured"] is False and body["provider"] == "vps_tunnel"
    assert body["process"] == "stopped" and body["tunnel_client_found"] is True
    assert body["tunnel_client_version"] == "0.71.0"

    r = client.put("/api/v1/remote-access", headers=operator,
                   json={"hostname": f"https://{HOST.upper()}/", "server_url": "Tunnel.Example-Store.com.au",
                         "store_id": "Store1", "token": TOKEN, "server_key": SERVER_KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hostname"] == HOST and body["server_url"] == SERVER and body["token_configured"] is True
    assert body["store_id"] == STORE and body["server_key_configured"] is True and body["proxy_name"] == "store1-cctv"
    assert TOKEN not in r.text and SERVER_KEY not in r.text and '"token"' not in r.text

    again = client.get("/api/v1/remote-access", headers=operator)
    assert again.json()["server_url"] == SERVER and TOKEN not in again.text

    # Persisted: settings row without the token; token encrypted at rest.
    with sqlite3.connect(str(settings.DATABASE_PATH)) as conn:
        row = conn.execute("SELECT value FROM system_setup WHERE key='remote_access'").fetchone()
    stored = json.loads(row[0])
    assert stored["hostname"] == HOST and stored["server_url"] == SERVER and stored["store_id"] == STORE
    assert TOKEN not in row[0] and SERVER_KEY not in row[0]
    enc = (Path(settings.STORAGE_DIR) / "secrets" / "named" / f"{ra_mod.TOKEN_SECRET_NAME}.enc").read_bytes()
    assert TOKEN.encode() not in enc
    assert secret_store.get_named_secret(settings.STORAGE_DIR, ra_mod.TOKEN_SECRET_NAME,
                                         settings.NVR_CREDENTIAL_KEY).decode() == TOKEN
    key_enc = (Path(settings.STORAGE_DIR) / "secrets" / "named" / f"{ra_mod.SERVER_KEY_SECRET_NAME}.enc").read_bytes()
    assert SERVER_KEY.encode() not in key_enc
    r = client.put("/api/v1/remote-access", headers=operator, json={"clear_server_key": True})
    assert r.json()["server_key_configured"] is False and r.json()["token_configured"] is True

    # Sending no token keeps the stored one.
    r = client.put("/api/v1/remote-access", headers=operator, json={"hostname": HOST, "token": ""})
    assert r.json()["token_configured"] is True

    # Remove the token: reported not configured, and it cannot be enabled.
    r = client.put("/api/v1/remote-access", headers=operator, json={"clear_token": True})
    assert r.json()["token_configured"] is False
    r = client.put("/api/v1/remote-access", headers=operator, json={"enabled": True})
    assert r.status_code == 400 and "token" in r.json()["detail"].lower()


def test_enable_requires_address_server_store_and_token(client, operator):
    r = client.put("/api/v1/remote-access", headers=operator, json={"enabled": True, "token": TOKEN})
    assert r.status_code == 400 and "public address" in r.json()["detail"]
    # A refused change is not saved at all, so each attempt sends everything so far.
    body = {"enabled": True, "hostname": HOST}
    r = client.put("/api/v1/remote-access", headers=operator, json=body)
    assert r.status_code == 400 and "tunnel server" in r.json()["detail"]
    body["server_url"] = SERVER
    r = client.put("/api/v1/remote-access", headers=operator, json=body)
    assert r.status_code == 400 and "store ID" in r.json()["detail"]
    body["store_id"] = STORE
    r = client.put("/api/v1/remote-access", headers=operator, json=body)
    assert r.status_code == 400 and "store token" in r.json()["detail"]
    assert client.get("/api/v1/remote-access", headers=operator).json()["enabled"] is False


def test_rejects_bad_values(client, operator):
    for bad in ("localhost", "192.168.1.10", "cctv.example.com:8443", "shop.local", "a b.com"):
        r = client.put("/api/v1/remote-access", headers=operator, json={"hostname": bad})
        assert r.status_code == 400, bad
    r = client.put("/api/v1/remote-access", headers=operator, json={"server_url": "http://tunnel.example.com"})
    assert r.status_code == 400 and "wss://" in r.json()["detail"]
    r = client.put("/api/v1/remote-access", headers=operator, json={"token": "too-short"})
    assert r.status_code == 400 and "too short" in r.json()["detail"]
    r = client.put("/api/v1/remote-access", headers=operator, json={"provider": "cloudflare_tunnel"})
    assert r.status_code == 400
    r = client.put("/api/v1/remote-access", headers=operator, json={"store_id": "store.one"})
    assert r.status_code == 400 and "store ID" in r.json()["detail"]
    r = client.put("/api/v1/remote-access", headers=operator, json={"server_key": "short"})
    assert r.status_code == 400 and "server key" in r.json()["detail"]
    assert client.put("/api/v1/remote-access", headers=operator, json={"extra_proxies": 3}).status_code == 422
    r = client.put("/api/v1/remote-access", headers=operator, json={"extra_proxies": 1})
    assert r.status_code == 200 and r.json()["extra_proxies"] == 1


def test_unauthenticated_and_non_operator_tokens_refused(client, operator):
    assert client.get("/api/v1/remote-access").status_code == 401
    assert client.put("/api/v1/remote-access", json={"hostname": HOST}).status_code == 401
    stream = auth_service.generate_stream_token("cam1")
    r = client.put("/api/v1/remote-access", headers={"Authorization": f"Bearer {stream}"}, json={"hostname": HOST})
    assert r.status_code == 403
    r = client.post("/api/v1/remote-access/verify", headers={"Authorization": f"Bearer {stream}"})
    assert r.status_code == 403


def test_refuses_to_enable_when_auth_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    r = client.put("/api/v1/remote-access",
                   json={"hostname": HOST, "server_url": SERVER, "store_id": STORE, "token": TOKEN, "enabled": True})
    assert r.status_code == 400 and "AUTH_DISABLED" in r.json()["detail"]
    assert client.get("/api/v1/remote-access").json()["enabled"] is False
    # And a stored "enabled" never starts the tunnel while auth is off.
    run, reason = RemoteAccessService.should_run(
        types.SimpleNamespace(settings={"enabled": True, "hostname": HOST, "server_url": SERVER, "store_id": STORE}))
    assert run is False and "AUTH_DISABLED" in reason


def test_legacy_cloudflare_settings_are_disabled_and_secret_removed(client, operator):
    with sqlite3.connect(str(settings.DATABASE_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO system_setup (key, value, updated_at) VALUES ('remote_access', ?, '2026-01-01')",
                     (json.dumps({"enabled": True, "provider": "cloudflare_tunnel", "hostname": HOST}),))
    secret_store.set_named_secret(settings.STORAGE_DIR, "cloudflare_tunnel_token", "eyJold", settings.NVR_CREDENTIAL_KEY)
    import asyncio

    asyncio.run(remote_access_service.start())
    s = client.get("/api/v1/remote-access", headers=operator).json()
    assert s["provider"] == "vps_tunnel" and s["enabled"] is False and s["hostname"] == HOST
    assert s["process"] == "stopped"
    assert not secret_store.has_named_secret(settings.STORAGE_DIR, "cloudflare_tunnel_token")


# --------------------------------------------------------------------------- #
# Process manager
# --------------------------------------------------------------------------- #

def _service(tmp_path) -> RemoteAccessService:
    secret_store.set_named_secret(settings.STORAGE_DIR, ra_mod.TOKEN_SECRET_NAME, TOKEN, settings.NVR_CREDENTIAL_KEY)
    svc = RemoteAccessService()
    svc.binary_finder = lambda: str(FAKE_FRPC)
    svc.config_file = tmp_path / "tunnel" / "frpc.toml"
    svc.env_passthrough = ("PATH", *FAKE_ENV)
    svc.backoff_base = 0.2
    svc.backoff_max = 1.0
    svc._settings.update(enabled=True, hostname=HOST, server_url=SERVER, store_id=STORE)
    return svc


def test_enable_starts_restart_on_change_and_disable_stops(client, operator, tmp_path):
    frpc_dir = tmp_path / "frpc"
    r = client.put("/api/v1/remote-access", headers=operator,
                   json={"hostname": HOST, "server_url": SERVER, "store_id": STORE, "token": TOKEN, "enabled": True})
    assert r.status_code == 200, r.text
    assert _wait(lambda: client.get("/api/v1/remote-access", headers=operator).json()["process"] == "connected")
    s = client.get("/api/v1/remote-access", headers=operator).json()
    assert s["connected_since"] and s["last_error"] is None and s["public_url"] == f"https://{HOST}"
    first = remote_access_service._proc
    assert first is not None and first.poll() is None

    # Token never on argv, never in the config file; frpc gets it in its environment only.
    args = (frpc_dir / "args.log").read_text()
    assert TOKEN not in args and args.startswith("-c ")
    conf = (frpc_dir / "config.toml").read_text()
    assert TOKEN not in conf and "{{ .Envs.EDGE_FRP_STORE_TOKEN }}" in conf and f'"{HOST}"' in conf
    assert f'user = "{STORE}"' in conf and "auth.token" not in conf
    envkeys = (frpc_dir / "envkeys.log").read_text().split()
    assert "EDGE_FRP_STORE_TOKEN" in envkeys and "EDGE_FRP_AUTH_TOKEN" not in envkeys
    assert not any(k in envkeys for k in ("JWT_SECRET", "NVR_CREDENTIAL_KEY", "INTERNAL_SERVICE_KEY"))

    # A new public address is part of frpc's config: the process restarts.
    client.put("/api/v1/remote-access", headers=operator, json={"hostname": "shop2." + HOST})
    assert _wait(lambda: remote_access_service._proc is not None and remote_access_service._proc is not first
                 and client.get("/api/v1/remote-access", headers=operator).json()["process"] == "connected")
    assert first.poll() is not None
    second = remote_access_service._proc
    # Unchanged settings (e.g. enabling again) keep the running process.
    client.put("/api/v1/remote-access", headers=operator, json={"enabled": True, "store_id": STORE})
    assert remote_access_service._proc is second
    # A server key is added: restart, and frpc now also gets frp's shared token.
    client.put("/api/v1/remote-access", headers=operator, json={"server_key": SERVER_KEY})
    assert _wait(lambda: remote_access_service._proc is not None and remote_access_service._proc is not second
                 and client.get("/api/v1/remote-access", headers=operator).json()["process"] == "connected")
    second = remote_access_service._proc
    assert "{{ .Envs.EDGE_FRP_AUTH_TOKEN }}" in (frpc_dir / "config.toml").read_text()
    assert "EDGE_FRP_AUTH_TOKEN" in (frpc_dir / "envkeys.log").read_text().splitlines()[-1].split()
    assert SERVER_KEY not in (frpc_dir / "args.log").read_text() + (frpc_dir / "config.toml").read_text()

    r = client.put("/api/v1/remote-access", headers=operator, json={"enabled": False})
    assert r.json()["process"] == "stopped" and r.json()["connected_since"] is None
    assert second.poll() is not None


def test_login_failure_backoff_then_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setenv("FAKE_FRPC_CRASHES", "2")
    frpc_dir = tmp_path / "frpc"
    svc = _service(tmp_path)
    t0 = time.monotonic()
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error" and svc.restarts >= 1, timeout=5)
        assert "refused this store's login (invalid store token)" in svc.last_error and "Retrying in" in svc.last_error
        assert svc.status()["error_kind"] == "login_rejected"
        assert _wait(lambda: svc.state == "connected", timeout=10)
        # Two failures, backoff 0.2 s then 0.4 s before the third start.
        assert svc.restarts == 2
        assert time.monotonic() - t0 >= 0.6
        proc = svc._proc
        assert proc is not None and proc.poll() is None
    finally:
        svc._stop_supervisor()
    assert svc.state == "stopped"
    assert proc.poll() is not None
    assert (frpc_dir / "count").read_text().strip() == "3"


@pytest.mark.parametrize("reason,kind,needle", [
    ("router config conflict", "address_in_use", "already serves " + HOST),
    (f"hostname {HOST} is not allowed for store store1", "address_rejected", "did not allow " + HOST + " for store store1"),
])
def test_proxy_rejection_is_reported(tmp_path, monkeypatch, reason, kind, needle):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setenv("FAKE_FRPC_PROXY_ERROR", reason)
    svc = _service(tmp_path)
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error", timeout=5)
        assert needle in svc.last_error
        assert svc.status()["error_kind"] == kind
    finally:
        svc._stop_supervisor()


@pytest.mark.parametrize("line,state,kind,needle", [
    ("2026-09-28 15:46:50.805 [I] [client/service.go:312] try to connect to server...", "starting", None, None),
    ("2026-09-28 15:46:50.808 [I] [client/service.go:332] [e54b32da31363efa] login to server success, get run id [e54b32da31363efa]", "starting", None, None),
    ("2026-09-28 15:46:50.809 [I] [client/control.go:174] [e54b32da31363efa] [store1-cctv] start proxy success", "connected", None, None),
    # The store check (server plugin) refused the login / the address.
    ("2026-09-28 15:47:09.275 [W] [client/service.go:323] connect to server error: invalid store token", "error", "login_rejected", "Check the Store ID and Store token"),
    ("2026-09-28 15:47:09.275 [W] [client/service.go:323] connect to server error: register control error", "error", "login_rejected", "refused this store's login."),
    ("login to the server failed: unknown store id. With loginFailExit enabled, no additional retries will be attempted", "error", "login_rejected", "(unknown store id)"),
    ("2026-09-28 15:47:14.301 [W] [client/control.go:172] [b48cf31869109ac9] [store1-cctv] start error: domain not allowed for this store", "error", "address_rejected", "did not allow"),
    ("2026-09-28 15:47:14.301 [W] [client/control.go:172] [b48cf31869109ac9] [store1-cctv] start error: new proxy [store1.store1-cctv] error", "error", "address_rejected", "for store store1. Check"),
    ("2026-09-28 15:47:14.301 [W] [client/control.go:172] [b48cf31869109ac9] [store1-cctv] start error: router config conflict", "error", "address_in_use", "already serves"),
    ("2026-09-28 15:47:14.301 [W] [client/control.go:172] [b48cf31869109ac9] [store1-cctv] start error: proxy [store1.store1-cctv] already exists", "error", "address_in_use", "already connected"),
    ("2026-09-28 15:47:09.275 [W] [client/service.go:323] connect to server error: send Login request to plugin error", "error", "auth_service_unavailable", "not answering"),
    # frp's own shared token (the optional server key).
    ("login to the server failed: token in login doesn't match token from configuration. With loginFailExit enabled, no additional retries will be attempted", "error", "server_key_rejected", "server key does not match"),
    # Connection problems.
    ("2026-09-28 15:47:09.288 [W] [client/service.go:323] connect to server error: tls: failed to verify certificate: x509: certificate signed by unknown authority", "error", "certificate", "certificate could not be verified"),
    ("2026-09-28 15:47:19.302 [W] [client/service.go:323] connect to server error: dial tcp 1.2.3.4:443: connect: connection refused", "error", "unreachable", "not reachable"),
    ("2026-09-28 15:47:26.233 [W] [client/service.go:323] [e54b32da31363efa] connect to server error: bad status", "error", "not_a_tunnel", "WebSocket handshake refused"),
    ("2026-09-28 15:47:26.233 [W] [client/service.go:323] connect to server error: dial tcp: lookup tunnel.example.com: no such host", "error", "unreachable", "does not resolve"),
    ("2026-09-28 15:47:26.233 [W] [client/service.go:323] connect to server error: EOF", "error", "unreachable", "not reachable"),
])
def test_log_line_states(line, state, kind, needle):
    svc = RemoteAccessService()
    svc.binary_finder = lambda: None
    svc._settings.update(hostname=HOST, store_id=STORE)
    svc.state = "starting"
    svc._last_verify = time.monotonic()  # no background verification from "connected"
    err = svc._handle_log_line("\x1b[1;34m" + line + "\x1b[0m")
    assert svc.state == state
    assert svc.status()["error_kind"] == kind
    if needle:
        assert needle in err and needle in svc.last_error
    else:
        assert err is None
    if state == "connected":
        assert svc.connected_since


def test_session_drop_goes_back_to_connecting():
    svc = RemoteAccessService()
    svc.binary_finder = lambda: None
    svc._last_verify = time.monotonic()
    svc._handle_log_line("2026-09-28 15:46:50.809 [I] [client/control.go:174] [ab12cd34ef] [x] start proxy success")
    assert svc.state == "connected"
    svc._handle_log_line("2026-09-28 15:47:26.231 [I] [client/service.go:312] [ab12cd34ef] try to connect to server...")
    assert svc.state == "starting" and svc.connected_since is None and "reconnecting" in svc.last_error
    assert svc.status()["error_kind"] == "connection_lost"
    svc._handle_log_line("2026-09-28 15:47:26.233 [W] [client/service.go:323] [ab12cd34ef] connect to server error: bad status")
    assert svc.state == "error"
    # frpc retries by itself; a retry does not hide the error until it succeeds.
    svc._handle_log_line("2026-09-28 15:47:28.245 [I] [client/service.go:312] [ab12cd34ef] try to connect to server...")
    assert svc.state == "error"
    svc._handle_log_line("2026-09-28 15:48:01.543 [I] [client/control.go:174] [ab12cd34ef] [x] start proxy success")
    assert svc.state == "connected" and svc.last_error is None and svc.status()["error_kind"] is None


def test_missing_binary_and_listen_host_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    svc.binary_finder = lambda: None
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error", timeout=3)
        assert "frpc) is not installed" in svc.last_error and "EDGE_TUNNEL=1" in svc.last_error
        assert svc.status()["tunnel_client_found"] is False
    finally:
        svc._stop_supervisor()
    monkeypatch.setattr(settings, "HOST", "192.168.1.20")
    run, reason = svc.should_run()
    assert run is False and "HOST=192.168.1.20" in reason
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    assert svc.should_run() == (True, None)


def test_missing_ca_bundle_refuses_to_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    svc.ca_finder = lambda: None
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "error", timeout=3)
        assert "CA certificate bundle" in svc.last_error
        assert svc._proc is None
    finally:
        svc._stop_supervisor()


def test_token_never_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    svc = _service(tmp_path)
    caplog.set_level("DEBUG", logger="edge.remote_access")
    secret_store.set_named_secret(settings.STORAGE_DIR, ra_mod.SERVER_KEY_SECRET_NAME, SERVER_KEY,
                                  settings.NVR_CREDENTIAL_KEY)
    svc._secret_redactions = (TOKEN, SERVER_KEY)
    svc._handle_log_line(f"2026-09-23 00:00:00.000 [E] [x.go:1] bad token {TOKEN} key {SERVER_KEY}")
    svc.apply()
    try:
        assert _wait(lambda: svc.state == "connected")
    finally:
        svc._stop_supervisor()
    assert TOKEN not in caplog.text and SERVER_KEY not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert svc._secret_redactions == (TOKEN, SERVER_KEY)  # both secrets redacted from frpc output


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

    alive = sp.run(["pgrep", "-f", str(FAKE_FRPC)], capture_output=True, text=True).stdout.split()
    assert not alive, alive


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


def test_verify_compares_device_id(monkeypatch):
    stub = types.ModuleType("app.services.device_identity")
    stub.get_identity = lambda: {"device_id": "dev-this-one", "device_name": "Test", "app_version": "x", "api_version": 1}
    monkeypatch.setitem(sys.modules, "app.services.device_identity", stub)
    svc = RemoteAccessService()
    svc.binary_finder = lambda: None
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

    err = svc.verify(client_factory=_fake_client(502, {}, seen))
    assert err["verified"] is False and "502" in err["verify_error"]


def test_verify_endpoint_and_hostname_change_clears_it(client, operator, monkeypatch):
    client.put("/api/v1/remote-access", headers=operator, json={"hostname": HOST})
    monkeypatch.setattr(ra_mod, "this_device_id", lambda: "dev-A")
    monkeypatch.setattr("httpx.Client", _fake_client(200, {"device_id": "dev-A"}, []))
    r = client.post("/api/v1/remote-access/verify", headers=operator)
    assert r.status_code == 200 and r.json()["verified"] is True
    r = client.put("/api/v1/remote-access", headers=operator, json={"hostname": "other." + HOST})
    assert r.json()["verified"] is False and r.json()["verified_at"] is None


# --------------------------------------------------------------------------- #
# bootstrap.py: frpc download (pinned version, verified checksum, arch-aware)
# --------------------------------------------------------------------------- #

def _bootstrap():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bootstrap

    return bootstrap


def test_bootstrap_frp_asset_names_and_pins():
    b = _bootstrap()
    v = b.FRP_VERSION
    assert b.frp_asset_name("Linux", "x86_64") == f"frp_{v}_linux_amd64.tar.gz"
    assert b.frp_asset_name("Linux", "aarch64") == f"frp_{v}_linux_arm64.tar.gz"
    assert b.frp_asset_name("Linux", "armv7l") == f"frp_{v}_linux_arm_hf.tar.gz"
    assert b.frp_asset_name("Darwin", "arm64") == f"frp_{v}_darwin_arm64.tar.gz"
    assert b.frp_asset_name("Windows", "AMD64") == f"frp_{v}_windows_amd64.zip"
    assert b.frp_asset_name("Linux", "sparc64") is None
    for arch in ("amd64", "arm64"):
        assert len(b.FRP_SHA256[f"frp_{v}_linux_{arch}.tar.gz"]) == 64


def test_bootstrap_checksum_file_parsing():
    b = _bootstrap()
    text = ("845b486c63686990e671f13cc5e3bd130ce7be659ab3c6f043008353451858cb  frp_0.71.0_android_arm64.tar.gz\n"
            "84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716  frp_0.71.0_linux_amd64.tar.gz\n")
    assert b.checksum_from_list(text, "frp_0.71.0_linux_amd64.tar.gz") == \
        "84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716"
    assert b.checksum_from_list(text, "frp_0.71.0_linux_arm64.tar.gz") is None


def test_bootstrap_extracts_only_frpc():
    b = _bootstrap()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("frp_0.71.0_linux_amd64/frps", b"server"), ("frp_0.71.0_linux_amd64/frpc", b"client"),
                           ("frp_0.71.0_linux_amd64/LICENSE", b"Apache")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    assert b.extract_frpc(buf.getvalue(), "frp_0.71.0_linux_amd64.tar.gz") == b"client"
