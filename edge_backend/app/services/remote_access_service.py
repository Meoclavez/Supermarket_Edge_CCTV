"""Remote access: publish the local dashboard through the owner's own VPS.

One provider, ``vps_tunnel``: a reverse tunnel built on frp (fatedier/frp,
Apache-2.0). This device runs ``frpc`` as a child process; it dials OUT to
``frps`` on the owner's VPS, so the store needs no port forwarding and works
behind carrier-grade NAT. Browsers open ``https://<public hostname>``; the
VPS's existing reverse proxy terminates HTTPS with its own certificate and
hands the request to frps, which sends it down the tunnel to
``127.0.0.1:<PORT>`` here::

    browser --https--> VPS reverse proxy --http--> frps (vhost) ==tunnel==> frpc --> uvicorn

Transport from frpc to frps, chosen by the tunnel server address:

``wss://tunnel.example.com[:443]`` (recommended)
    WebSocket over TLS through the VPS's existing reverse proxy on 443, on a
    hostname of its own. No new port is opened on the VPS.
``tcp://vps.example.com:7000``
    frp's own TLS straight to a published frps port, for proxies that cannot
    pass WebSockets. frps must then present a certificate for that name.

Either way the server certificate is always verified (system CA bundle, or
``EDGE_TUNNEL_CA_FILE`` for a private CA): an unverified tunnel would let a
man in the middle read every dashboard session.

The tunnel server is multi-tenant: one frps serves many stores. Each store
logs in with its **store ID** (frp ``user``) and **store token** (sent as the
login metadata ``metadatas.token``); a server plugin checks the pair on
Login and allows each store only its own hostname(s) on NewProxy. frp's own
shared ``auth.token`` is used only if the server sets one (the optional
**server key**). Rejections are reported as such (``error_kind``), apart from
connection problems.

Settings live in ``system_setup`` under the key ``remote_access`` (JSON:
``enabled``, ``provider``, ``hostname``, ``server_url``, ``store_id`` plus the
last verification result). The store token and the server key are named
secrets encrypted with this machine's key. They reach frpc only through
environment variables that the generated config references as templates, so
they are never on the command line (where ``ps`` shows it), never in the
config file on disk, never logged and never returned by the API.

The tunnel only runs when remote access is enabled AND a hostname, a tunnel
server, a store ID and a store token are configured AND authentication is on. ``AUTH_DISABLED``
refuses it: an unauthenticated system must never be reachable from the
internet. How requests arriving through the tunnel are classified (remote,
real client address) is in :mod:`app.services.public_exposure`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger("edge.remote_access")

SETTINGS_KEY = "remote_access"
TOKEN_SECRET_NAME = "vps_tunnel_store_token"
SERVER_KEY_SECRET_NAME = "vps_tunnel_server_key"
# Secrets of providers that no longer exist; removed on start.
LEGACY_SECRET_NAMES = ("cloudflare_tunnel_token",)
PROVIDER = "vps_tunnel"
PROVIDERS = (PROVIDER,)
REPO_DIR = Path(__file__).resolve().parents[3]
# frpc reads the secrets from these variables through {{ .Envs.* }} templates.
FRPC_STORE_TOKEN_ENV = "EDGE_FRP_STORE_TOKEN"
FRPC_SERVER_KEY_ENV = "EDGE_FRP_AUTH_TOKEN"
MIN_TOKEN_LENGTH = 24
MAX_EXTRA_PROXIES = 2
MAX_TOKEN_LENGTH = 512
DEFAULT_WSS_PORT = 443
DEFAULT_TCP_PORT = 7000

# Process states reported to the dashboard.
STOPPED, STARTING, CONNECTED, ERROR = "stopped", "starting", "connected", "error"
# What an error is about (status()["error_kind"]), so a rejected store login or
# address is never mistaken for a network problem.
LOGIN_REJECTED = "login_rejected"          # store ID / store token refused by the server
ADDRESS_REJECTED = "address_rejected"      # hostname not allowed for this store
ADDRESS_IN_USE = "address_in_use"          # another connection already holds it
SERVER_KEY_REJECTED = "server_key_rejected"  # frp's shared auth.token does not match
AUTH_SERVICE = "auth_service_unavailable"  # the server's store check is not answering
UNREACHABLE = "unreachable"                # DNS, refused, timeout, reset
CERTIFICATE = "certificate"                # TLS verification failed
NOT_A_TUNNEL = "not_a_tunnel"              # WebSocket handshake refused (proxy route)
CONNECTION_LOST = "connection_lost"        # was connected; frpc is reconnecting
SETUP = "setup"                            # missing on this machine (binary, CA, config)
EXITED = "exited"                          # frpc stopped without a recognised reason

# Remote live video (WebRTC) settings, stored with the rest of Online access.
DEFAULT_STUN_SERVERS = ("stun:stun.ikorex.com.au:3478",)
MAX_STUN_SERVERS = 4
WEBRTC_MODE_AUTO, WEBRTC_MODE_FIXED = "auto", "fixed_port"
WEBRTC_MODES = (WEBRTC_MODE_AUTO, WEBRTC_MODE_FIXED)
DEFAULT_WEBRTC_PORT = 8555
DEFAULT_MAX_VIDEO_SESSIONS = 8
MAX_VIDEO_SESSIONS_LIMIT = 32
LAN_TRANSPORT_LOCAL, LAN_TRANSPORT_WEBRTC = "local", "webrtc"
LAN_TRANSPORTS = (LAN_TRANSPORT_LOCAL, LAN_TRANSPORT_WEBRTC)
WEBRTC_SETTING_KEYS = ("stun_servers", "webrtc_mode", "webrtc_port", "max_video_sessions", "live_transport_on_lan")

_HOSTNAME_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no spaces
_STORE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,38}[a-z0-9])?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# frpc: "2026-09-28 15:47:09.275 [W] [client/service.go:323] [e54b32da31363efa] message"
_FRPC_LINE_RE = re.compile(
    r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?\s+\[([TDIWE])\]\s+\[[^\]]*\]\s+(?:\[[0-9a-f]{6,}\]\s+)?(.*)$"
)
_FRPC_LEVELS = {"T": logging.DEBUG, "D": logging.DEBUG, "I": logging.INFO, "W": logging.WARNING, "E": logging.ERROR}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

class RemoteAccessError(ValueError):
    """A configuration the operator must correct; the message is shown as is."""


def _check_dns_name(raw: str, original: Optional[str], what: str) -> str:
    raw = raw.rstrip(".")
    if not _HOSTNAME_RE.match(raw) or raw.endswith(".local") or raw.endswith(".localhost"):
        raise RemoteAccessError(
            f"'{original}' is not a public {what}. Use a name on your own domain, "
            "for example cctv.yourstore.com.au."
        )
    return raw


def normalise_hostname(value: Optional[str]) -> str:
    """Accept ``cctv.example.com`` or a pasted ``https://cctv.example.com/``.

    Returns "" for empty input. Raises RemoteAccessError for anything that is
    not a public DNS name (IP addresses, ``localhost``, ports, paths).
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^[a-z]+://", "", raw).rstrip("/")
    if "/" in raw or ":" in raw or "@" in raw:
        raise RemoteAccessError(
            "Enter only the address, for example cctv.yourstore.com.au "
            "(no port, path or login)."
        )
    return _check_dns_name(raw, value, "address")


def parse_server_url(value: Optional[str]) -> dict[str, Any]:
    """``wss://tunnel.example.com[:port]`` or ``tcp://vps.example.com:port``.

    A bare name means ``wss://<name>``; ``https://`` is accepted as ``wss://``.
    Returns ``{"url", "protocol", "host", "port"}`` (``url`` canonical), or
    ``{}`` for empty input.
    """
    raw = (value or "").strip()
    if not raw:
        return {}
    text = raw if "://" in raw else f"wss://{raw}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise RemoteAccessError(f"'{raw}' is not a valid tunnel server address.")
    scheme = parts.scheme.lower()
    if scheme in ("https", "wss"):
        protocol, default_port = "wss", DEFAULT_WSS_PORT
    elif scheme == "tcp":
        protocol, default_port = "tcp", DEFAULT_TCP_PORT
    else:
        raise RemoteAccessError(
            "The tunnel server must start with wss:// (through the VPS's web proxy) or tcp:// "
            "(a dedicated port), for example wss://tunnel.yourdomain.com."
        )
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise RemoteAccessError(
            "Enter only the tunnel server name and optional port, for example wss://tunnel.yourdomain.com."
        )
    host = _check_dns_name((parts.hostname or "").lower(), raw, "tunnel server name")
    port = default_port if port is None else port
    if not 1 <= port <= 65535:
        raise RemoteAccessError("The tunnel server port must be between 1 and 65535.")
    shown_port = "" if (protocol == "wss" and port == DEFAULT_WSS_PORT) else f":{port}"
    return {"url": f"{protocol}://{host}{shown_port}", "protocol": protocol, "host": host, "port": port}


def validate_token(value: Optional[str], what: str = "store token", minimum: int = MIN_TOKEN_LENGTH) -> str:
    token = (value or "").strip()
    if not token:
        raise RemoteAccessError(f"Enter the {what}.")
    if not _TOKEN_RE.match(token) or len(token) > MAX_TOKEN_LENGTH:
        raise RemoteAccessError(f"The {what} must be a single word of letters, digits and symbols (no spaces).")
    if len(token) < minimum:
        raise RemoteAccessError(
            f"The {what} is too short ({len(token)} characters). Use the long random value "
            f"from your installer (at least {minimum} characters)."
        )
    return token


def normalise_store_id(value: Optional[str]) -> str:
    """Store ID as issued by the installer: letters, digits, '-' and '_' (lower-cased)."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if not _STORE_ID_RE.match(raw):
        raise RemoteAccessError(
            "The store ID may contain only letters, digits, '-' and '_' (at most 40 characters), "
            "for example store1."
        )
    return raw


def proxy_name_for(store_id: str) -> str:
    """frp proxy name for this store (frps shows it as ``<store id>.<name>``)."""
    return f"{store_id or 'store'}-cctv"


_STUN_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalise_stun_server(value: Optional[str]) -> str:
    """``stun:host[:port]`` from ``stun:host:port``, ``host:port`` or ``host``.

    Relays are refused outright (``turn:``/``turns:``): remote video is direct
    only, and no TURN server exists anywhere in this system.
    """
    raw = (value or "").strip()
    low = raw.lower()
    if not raw:
        raise RemoteAccessError("A STUN server address is empty.")
    if low.startswith(("turn:", "turns:")):
        raise RemoteAccessError(
            f"'{raw}' is a TURN relay. Remote video never goes through a relay: enter STUN servers only "
            "(stun:host:port).")
    if low.startswith("stuns:"):
        raise RemoteAccessError(f"'{raw}': use a plain stun: address (UDP), for example stun:stun.example.com:3478.")
    body = low[5:] if low.startswith("stun:") else low
    body = re.sub(r"^//", "", body)
    if not body or "/" in body or "@" in body or "?" in body:
        raise RemoteAccessError(f"'{raw}' is not a STUN server address. Use stun:host:port.")
    if body.startswith("["):
        host, _, rest = body[1:].partition("]")
        port_txt = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            raise RemoteAccessError(f"'{raw}' is not a STUN server address. Use stun:host:port.")
        try:
            import ipaddress

            ipaddress.IPv6Address(host)
        except ValueError:
            raise RemoteAccessError(f"'{raw}' is not a STUN server address. Use stun:host:port.")
        host_txt = f"[{host}]"
    else:
        host, _, port_txt = body.partition(":")
        if not _STUN_HOST_RE.match(host):
            raise RemoteAccessError(f"'{raw}' is not a STUN server address. Use stun:host:port.")
        host_txt = host
    port = 3478
    if port_txt:
        if not port_txt.isdigit() or not 1 <= int(port_txt) <= 65535:
            raise RemoteAccessError(f"'{raw}': the STUN port must be between 1 and 65535.")
        port = int(port_txt)
    return f"stun:{host_txt}:{port}"


def normalise_stun_servers(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,]+", values) if v]
    if not isinstance(values, (list, tuple)):
        raise RemoteAccessError("STUN servers must be a list of stun:host:port addresses.")
    out: list[str] = []
    for value in values:
        norm = normalise_stun_server(str(value))
        if norm not in out:
            out.append(norm)
    if len(out) > MAX_STUN_SERVERS:
        raise RemoteAccessError(f"Enter at most {MAX_STUN_SERVERS} STUN servers.")
    return out


def _stored_stun_servers(value: Any) -> list[str]:
    """Stored list, silently dropping anything that is not a valid stun: address."""
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_STUN_SERVERS)
    out: list[str] = []
    for v in value:
        try:
            norm = normalise_stun_server(str(v))
        except RemoteAccessError:
            continue
        if norm not in out:
            out.append(norm)
    return out[:MAX_STUN_SERVERS]


def _toml_str(value: str) -> str:
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


# --------------------------------------------------------------------------- #
# Binaries and CA bundle
# --------------------------------------------------------------------------- #

def find_frpc() -> Optional[str]:
    """``FRPC_PATH``, then PATH, then ``<repo>/bin/frpc`` (bootstrap.py --with-tunnel)."""
    explicit = os.environ.get("FRPC_PATH")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("frpc")
    if found:
        return found
    for name in ("frpc", "frpc.exe"):
        local = REPO_DIR / "bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return None


_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Alpine (ca-certificates)
    "/etc/pki/tls/certs/ca-bundle.crt",    # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",              # openSUSE
    "/etc/ssl/cert.pem",                   # Alpine, macOS
)


def find_ca_bundle() -> Optional[str]:
    """CA certificates frpc verifies the tunnel server with.

    ``EDGE_TUNNEL_CA_FILE`` (a private CA) wins; then the system bundle; then
    certifi's bundle (installed with httpx).
    """
    explicit = os.environ.get("EDGE_TUNNEL_CA_FILE")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile, *_CA_CANDIDATES):
        if candidate and os.path.isfile(candidate):
            return candidate
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def frpc_version(binary: str, timeout: float = 5.0) -> Optional[str]:
    try:
        res = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip().splitlines()
    return out[0].strip()[:40] if res.returncode == 0 and out else None


# --------------------------------------------------------------------------- #
# frpc configuration
# --------------------------------------------------------------------------- #

def build_frpc_config(*, server: dict[str, Any], hostname: str, store_id: str, local_port: int, ca_file: str,
                      server_key: bool = False, connect_host: Optional[str] = None,
                      connect_port: Optional[int] = None) -> str:
    """frpc TOML for this store's one HTTP proxy.

    Contains no secret: the store token (and the optional server key) are
    template references to :data:`FRPC_STORE_TOKEN_ENV` / :data:`FRPC_SERVER_KEY_ENV`,
    filled in by frpc from its environment. ``connect_host``/``connect_port``
    override where frpc dials while the TLS name stays the tunnel server's
    (tests on one machine only).
    """
    lines = [
        "# Generated by Edge AI CCTV from Settings -> Online access.",
        "# Rewritten on every tunnel start: edit the settings, not this file.",
        f"serverAddr = {_toml_str(connect_host or server['host'])}",
        f"serverPort = {int(connect_port or server['port'])}",
        # Store identity for the server's Login check (multi-tenant frps).
        f"user = {_toml_str(store_id)}",
        f'metadatas.token = "{{{{ .Envs.{FRPC_STORE_TOKEN_ENV} }}}}"',
        'auth.method = "token"',
    ]
    if server_key:  # only when the server also sets frp's shared auth.token
        lines.append(f'auth.token = "{{{{ .Envs.{FRPC_SERVER_KEY_ENV} }}}}"')
    lines += [
        # Exit on a refused login: the app retries with backoff and reports why.
        "loginFailExit = true",
        f"transport.protocol = {_toml_str(server['protocol'])}",
        "transport.dialServerTimeout = 10",
        # Application-level heartbeat on top of the multiplexer's keepalive, so a
        # dead path (NAT timeout, proxy restart) is noticed within ~90 s.
        "transport.heartbeatInterval = 30",
        "transport.heartbeatTimeout = 90",
        "transport.tls.enable = true",
        f"transport.tls.serverName = {_toml_str(server['host'])}",
        # Setting a CA file is what turns certificate verification on in frpc.
        f"transport.tls.trustedCaFile = {_toml_str(ca_file)}",
        'log.to = "console"',
        'log.level = "info"',
        "log.disablePrintColor = true",
        "",
        "[[proxies]]",
        f"name = {_toml_str(proxy_name_for(store_id))}",
        'type = "http"',
        'localIP = "127.0.0.1"',
        f"localPort = {int(local_port)}",
        f"customDomains = [{_toml_str(hostname)}]",
        "",
    ]
    return "\n".join(lines)


def config_path() -> Path:
    return Path(settings.STORAGE_DIR) / "tunnel" / "frpc.toml"


def write_frpc_config(text: str, path: Optional[Path] = None) -> Path:
    """Write atomically with the directory 0700 and the file 0600."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


_NETWORK_WORDS = ("connection refused", "i/o timeout", "timeout", "unreachable", "no route to host",
                  "connection reset", "broken pipe", "eof")


def classify_connect_error(reason: str) -> tuple[str, str]:
    """(error_kind, message) for frpc's "connect to server error" / "login ... failed"."""
    msg = re.sub(r"\s+", " ", reason or "").strip()
    low = msg.lower()
    if "token in login doesn't match" in low:
        return SERVER_KEY_REJECTED, ("The tunnel server's shared server key does not match. Enter the server key "
                                     "from your installer, or remove it if they did not give you one.")
    if "x509" in low or "certificate" in low:
        return CERTIFICATE, f"The tunnel server's certificate could not be verified ({msg[:160]})."
    if "no such host" in low or "server misbehaving" in low:
        return UNREACHABLE, f"The tunnel server name does not resolve in DNS ({msg[:160]})."
    if "bad status" in low:
        return NOT_A_TUNNEL, ("The tunnel server address answered, but not as a tunnel (WebSocket handshake "
                              "refused). Check the VPS reverse-proxy route for the tunnel hostname.")
    if any(w in low for w in _NETWORK_WORDS):
        return UNREACHABLE, f"The tunnel server is not reachable ({msg[:160]})."
    if "request to plugin error" in low:
        return AUTH_SERVICE, ("The tunnel server could not check this store's login right now (its store "
                              "check is not answering). It retries by itself.")
    detail = "" if not msg or low == "register control error" else f" ({msg[:160]})"
    return LOGIN_REJECTED, f"The tunnel server refused this store's login{detail}. Check the Store ID and Store token."


def classify_proxy_error(reason: str, hostname: str = "", store_id: str = "") -> tuple[str, str]:
    """(error_kind, message) for frpc's "[proxy] start error: ..."."""
    msg = re.sub(r"\s+", " ", reason or "").strip()
    low = msg.lower()
    if "router config conflict" in low:
        return ADDRESS_IN_USE, (f"Another connection already serves {hostname or 'this public address'} on the "
                                "tunnel server. Each store needs its own public address.")
    if "already exists" in low:
        return ADDRESS_IN_USE, (f"Store {store_id or '?'} is already connected to the tunnel server (another device "
                                "with the same store ID, or an old connection that has not timed out yet).")
    if "request to plugin error" in low:
        return AUTH_SERVICE, ("The tunnel server could not check the public address right now (its store "
                              "check is not answering).")
    detail = "" if not msg or re.fullmatch(r"new proxy \[[^\]]*\] error", low) else f" ({msg[:160]})"
    return ADDRESS_REJECTED, (f"The tunnel server did not allow {hostname or 'this address'} for store "
                              f"{store_id or '?'}{detail}. Check that the public address is the one assigned "
                              "to this store.")


# --------------------------------------------------------------------------- #
# Settings persistence (sync sqlite so any thread, and middleware, can read it)
# --------------------------------------------------------------------------- #

def _default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": PROVIDER,
        "hostname": "",
        "server_url": "",
        "store_id": "",
        # Proxies between the visitor and the VPS's own reverse proxy (e.g. a CDN
        # in front of the server). public_exposure uses it to find the visitor's
        # address in X-Forwarded-For; see there.
        "extra_proxies": 0,
        "verified": False,
        "verified_at": None,
        "verified_hostname": None,
        "verify_error": None,
        # Remote live video: direct peer-to-peer WebRTC (docs/REMOTE_VIDEO_CONTRACT.md).
        # STUN only: the backend never hands out a turn:/turns: URL.
        "stun_servers": list(DEFAULT_STUN_SERVERS),
        "webrtc_mode": WEBRTC_MODE_AUTO,        # auto | fixed_port
        "webrtc_port": DEFAULT_WEBRTC_PORT,     # fixed_port only (UDP+TCP)
        "max_video_sessions": DEFAULT_MAX_VIDEO_SESSIONS,
        "live_transport_on_lan": LAN_TRANSPORT_LOCAL,  # local | webrtc
    }


def _db_path() -> Path:
    return Path(settings.DATABASE_PATH)


def load_settings() -> dict[str, Any]:
    data = _default_settings()
    path = _db_path()
    if not path.exists():
        return data
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (SETTINGS_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"Could not read remote access settings: {exc}")
        return data
    if row:
        try:
            stored = json.loads(row[0])
            if isinstance(stored, dict):
                data.update({k: stored[k] for k in data if k in stored})
        except ValueError:
            logger.warning("Stored remote access settings are not valid JSON; using defaults")
    _sanitise_webrtc(data)
    if data.get("provider") not in PROVIDERS:
        # Settings from a removed provider (Cloudflare Tunnel, direct): keep the
        # hostname, but nothing runs until a tunnel server and token are entered.
        data.update(provider=PROVIDER, enabled=False)
    return data


def _sanitise_webrtc(data: dict[str, Any]) -> None:
    """Stored WebRTC settings as the code expects them (defaults for anything invalid)."""
    data["stun_servers"] = _stored_stun_servers(data.get("stun_servers"))
    if data.get("webrtc_mode") not in WEBRTC_MODES:
        data["webrtc_mode"] = WEBRTC_MODE_AUTO
    port = data.get("webrtc_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        data["webrtc_port"] = DEFAULT_WEBRTC_PORT
    n = data.get("max_video_sessions")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_VIDEO_SESSIONS_LIMIT:
        data["max_video_sessions"] = DEFAULT_MAX_VIDEO_SESSIONS
    if data.get("live_transport_on_lan") not in LAN_TRANSPORTS:
        data["live_transport_on_lan"] = LAN_TRANSPORT_LOCAL


def save_settings(data: dict[str, Any]) -> None:
    clean = {k: data.get(k) for k in _default_settings()}
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    try:
        conn.execute(
            "INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(clean), datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")),
        )
        conn.commit()
    finally:
        conn.close()


def _secret_api():
    from app.services import secret_store

    return secret_store


def _has_secret(name: str) -> bool:
    try:
        return _secret_api().has_named_secret(settings.STORAGE_DIR, name)
    except Exception:
        return False


def token_configured() -> bool:
    return _has_secret(TOKEN_SECRET_NAME)


def server_key_configured() -> bool:
    return _has_secret(SERVER_KEY_SECRET_NAME)


def _read_secret(name: str) -> Optional[str]:
    raw = _secret_api().get_named_secret(settings.STORAGE_DIR, name, settings.NVR_CREDENTIAL_KEY)
    return raw.decode("utf-8") if raw else None


def _read_token() -> Optional[str]:
    return _read_secret(TOKEN_SECRET_NAME)


def _remove_legacy_secrets() -> None:
    for name in LEGACY_SECRET_NAMES:
        try:
            if _secret_api().delete_named_secret(settings.STORAGE_DIR, name):
                logger.info(f"Removed the stored secret '{name}' of a remote-access provider that no longer exists")
        except Exception as exc:
            logger.warning(f"Could not remove legacy secret '{name}': {exc.__class__.__name__}")


def this_device_id() -> Optional[str]:
    """The device id served at /api/v1/device/identity (device_identity.py)."""
    try:
        from app.services.device_identity import get_identity

        return (get_identity() or {}).get("device_id")
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(f"device identity unavailable: {exc}")
        return None
    try:
        conn = sqlite3.connect(str(_db_path()), timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = 'device_id'").fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _listen_host_problem() -> Optional[str]:
    """frpc forwards to 127.0.0.1: uvicorn must listen there too."""
    host = (settings.HOST or "").strip().strip("[]").lower()
    if host in ("", "0.0.0.0", "::", "127.0.0.1", "localhost"):
        return None
    return (f"HOST={settings.HOST}: the tunnel forwards to 127.0.0.1:{settings.PORT}, which this server does not "
            "listen on. Set HOST=0.0.0.0 or HOST=127.0.0.1 in edge_backend/.env and restart.")


# --------------------------------------------------------------------------- #
# Process manager
# --------------------------------------------------------------------------- #

class RemoteAccessService:
    """Supervises one frpc child process according to the settings."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: dict[str, Any] = _default_settings()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.state = STOPPED
        self.last_error: Optional[str] = None
        self.error_kind: Optional[str] = None
        self.restarts = 0
        self.connected_since: Optional[str] = None
        self.started_at: Optional[float] = None
        self.backoff_base = 2.0
        self.backoff_max = 60.0
        self.stable_after = 60.0  # seconds up before the backoff resets
        self.verify_interval = 600.0
        self._last_verify = 0.0
        self._secret_redactions: tuple[str, ...] = ()
        self._version_cache: Optional[tuple[str, Optional[str]]] = None
        # Injectable for tests.
        self.binary_finder: Callable[[], Optional[str]] = find_frpc
        self.ca_finder: Callable[[], Optional[str]] = find_ca_bundle
        self.connect_override: Optional[tuple[str, int]] = None
        self.config_file: Optional[Path] = None  # default: <STORAGE_DIR>/tunnel/frpc.toml
        # Environment variables frpc inherits from this process (plus the token).
        self.env_passthrough: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")

    # -- settings -----------------------------------------------------------

    def reload_settings(self) -> dict[str, Any]:
        data = load_settings()
        with self._lock:
            self._settings = data
        return dict(data)

    @property
    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def hostname(self) -> str:
        """Configured public address (even when disabled): requests using it are remote."""
        with self._lock:
            return self._settings.get("hostname") or ""

    def webrtc_settings(self) -> dict[str, Any]:
        """Remote live video settings (a copy; sanitised on load)."""
        with self._lock:
            data = {k: self._settings.get(k) for k in WEBRTC_SETTING_KEYS}
        _sanitise_webrtc(data)
        return data

    def forwarding_hops(self) -> int:
        """Trusted proxies that appended to X-Forwarded-For before frps: the VPS proxy + extras."""
        with self._lock:
            extra = self._settings.get("extra_proxies") or 0
        return 1 + (extra if isinstance(extra, int) and 0 <= extra <= MAX_EXTRA_PROXIES else 0)

    def public_url(self) -> Optional[str]:
        """``https://<hostname>`` while remote access is enabled, else None."""
        s = self.settings
        if s.get("enabled") and s.get("hostname"):
            return f"https://{s['hostname']}"
        return None

    def update(self, *, enabled: Optional[bool] = None, provider: Optional[str] = None,
               hostname: Optional[str] = None, server_url: Optional[str] = None,
               store_id: Optional[str] = None, token: Optional[str] = None, clear_token: bool = False,
               server_key: Optional[str] = None, clear_server_key: bool = False,
               extra_proxies: Optional[int] = None, stun_servers: Optional[list[str]] = None,
               webrtc_mode: Optional[str] = None, webrtc_port: Optional[int] = None,
               max_video_sessions: Optional[int] = None,
               live_transport_on_lan: Optional[str] = None) -> dict[str, Any]:
        """Validate and persist a change, then start/stop/restart the tunnel to match."""
        with self._lock:
            old = dict(self._settings)
            new = dict(old)
            if provider is not None and provider not in PROVIDERS:
                raise RemoteAccessError(f"Unknown provider '{provider}'. Use: {PROVIDER}.")
            new["provider"] = PROVIDER
            if hostname is not None:
                new["hostname"] = normalise_hostname(hostname)
            if server_url is not None:
                new["server_url"] = parse_server_url(server_url).get("url", "")
            if store_id is not None:
                new["store_id"] = normalise_store_id(store_id)
            if extra_proxies is not None:
                if not isinstance(extra_proxies, int) or isinstance(extra_proxies, bool) \
                        or not 0 <= extra_proxies <= MAX_EXTRA_PROXIES:
                    raise RemoteAccessError(f"Proxies in front of the server must be 0 to {MAX_EXTRA_PROXIES}.")
                new["extra_proxies"] = extra_proxies
            if stun_servers is not None:
                new["stun_servers"] = normalise_stun_servers(stun_servers)
            if webrtc_mode is not None:
                if webrtc_mode not in WEBRTC_MODES:
                    raise RemoteAccessError("Video connection mode must be 'auto' (no router change) or "
                                            "'fixed_port' (a UDP port forwarded on the store router).")
                new["webrtc_mode"] = webrtc_mode
            if webrtc_port is not None:
                if not isinstance(webrtc_port, int) or isinstance(webrtc_port, bool) \
                        or not 1024 <= webrtc_port <= 65535:
                    raise RemoteAccessError("The video port must be between 1024 and 65535.")
                new["webrtc_port"] = webrtc_port
            if max_video_sessions is not None:
                if not isinstance(max_video_sessions, int) or isinstance(max_video_sessions, bool) \
                        or not 1 <= max_video_sessions <= MAX_VIDEO_SESSIONS_LIMIT:
                    raise RemoteAccessError(
                        f"Live video sessions must be between 1 and {MAX_VIDEO_SESSIONS_LIMIT}.")
                new["max_video_sessions"] = max_video_sessions
            if live_transport_on_lan is not None:
                if live_transport_on_lan not in LAN_TRANSPORTS:
                    raise RemoteAccessError("Live video on the store network must be 'local' or 'webrtc'.")
                new["live_transport_on_lan"] = live_transport_on_lan
            new_token = validate_token(token) if token else None
            new_key = validate_token(server_key, "server key", 16) if server_key else None
            if enabled is not None:
                new["enabled"] = bool(enabled)

            will_have_token = bool(new_token) or (token_configured() and not clear_token)
            if new["enabled"]:
                if settings.AUTH_DISABLED:
                    raise RemoteAccessError(
                        "Online access cannot be enabled while AUTH_DISABLED=true: the dashboard would be "
                        "open to anyone on the internet. Remove AUTH_DISABLED and restart first."
                    )
                if not new["hostname"]:
                    raise RemoteAccessError("Enter the public address before enabling online access.")
                if not new["server_url"]:
                    raise RemoteAccessError("Enter the tunnel server before enabling online access.")
                if not new["store_id"]:
                    raise RemoteAccessError("Enter the store ID before enabling online access.")
                if not will_have_token:
                    raise RemoteAccessError("Enter the store token before enabling online access.")

            if new["hostname"] != old.get("hostname"):
                new.update(verified=False, verified_at=None, verified_hostname=None, verify_error=None)

            api = _secret_api()
            if new_token:
                api.set_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME, new_token, settings.NVR_CREDENTIAL_KEY)
                logger.info("Store token stored (encrypted)")
            elif clear_token:
                api.delete_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME)
                logger.info("Store token removed")
            if new_key:
                api.set_named_secret(settings.STORAGE_DIR, SERVER_KEY_SECRET_NAME, new_key, settings.NVR_CREDENTIAL_KEY)
                logger.info("Tunnel server key stored (encrypted)")
            elif clear_server_key:
                api.delete_named_secret(settings.STORAGE_DIR, SERVER_KEY_SECRET_NAME)
                logger.info("Tunnel server key removed")
            save_settings(new)
            self._settings = new
            # Everything frpc was started with: any change needs a new process.
            restart = bool(new_token or new_key) or clear_token or clear_server_key \
                or any(new[k] != old.get(k) for k in ("hostname", "server_url", "store_id"))
        logger.info(
            f"Online access settings saved: enabled={new['enabled']} hostname={new['hostname'] or '-'} "
            f"server={new['server_url'] or '-'} store={new['store_id'] or '-'}"
        )
        if any(new.get(k) != old.get(k) for k in WEBRTC_SETTING_KEYS):
            logger.info(f"Remote video settings saved: mode={new['webrtc_mode']} port={new['webrtc_port']} "
                        f"stun={','.join(new['stun_servers']) or '-'} max_sessions={new['max_video_sessions']} "
                        f"lan={new['live_transport_on_lan']}")
            try:
                from app.services.webrtc_sessions import webrtc_sessions

                webrtc_sessions.settings_changed()
            except Exception as exc:  # the video service must never block saving settings
                logger.warning(f"Remote video could not apply the new settings: {exc.__class__.__name__}")
        self.apply(restart=restart)
        return self.status()

    # -- desired state --------------------------------------------------------

    def should_run(self) -> tuple[bool, Optional[str]]:
        s = self.settings
        if not s.get("enabled"):
            return False, None
        if settings.AUTH_DISABLED:
            return False, "AUTH_DISABLED=true: the tunnel is not started while authentication is off."
        if not s.get("hostname"):
            return False, "No public address configured."
        if not s.get("server_url"):
            return False, "No tunnel server configured."
        if not s.get("store_id"):
            return False, "No store ID configured."
        if not token_configured():
            return False, "No store token configured."
        problem = _listen_host_problem()
        if problem:
            return False, problem
        return True, None

    def apply(self, restart: bool = False) -> None:
        """Start or stop the tunnel so it matches the settings."""
        run, reason = self.should_run()
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
        if running and (not run or restart):
            self._stop_supervisor()
            running = False
        if run and not running:
            self._start_supervisor()
        elif not run:
            with self._lock:
                self.state = ERROR if reason else STOPPED
                self.last_error = reason
                self.error_kind = SETUP if reason else None
                self.connected_since = None

    def _start_supervisor(self) -> None:
        with self._lock:
            self._stop = threading.Event()
            self.state = STARTING
            self.last_error = None
            self.error_kind = None
            self.connected_since = None
            self.restarts = 0
            self._thread = threading.Thread(target=self._supervise, args=(self._stop,),
                                            name="remote-access-tunnel", daemon=True)
            self._thread.start()

    def _stop_supervisor(self, timeout: float = 15.0) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
        stop.set()
        self._terminate_process()
        if thread is not None and thread is not threading.current_thread():
            # The supervisor may be between "should I start?" and Popen when
            # stop is requested; keep terminating until the thread is gone so
            # a child started in that window never outlives the service.
            deadline = time.monotonic() + timeout
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(0.5)
                if thread.is_alive():
                    self._terminate_process()
        with self._lock:
            self._thread = None
            self.state = STOPPED
            self.connected_since = None
        if thread is not None:
            logger.info("Online access tunnel stopped")

    def _terminate_process(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _redact(self, line: str) -> str:
        for secret in self._secret_redactions:
            if secret:
                line = line.replace(secret, "[REDACTED]")
        return line

    def _fail(self, message: str, kind: str = SETUP) -> None:
        with self._lock:
            self.state = ERROR
            self.last_error = message
            self.error_kind = kind
            self.connected_since = None
        logger.error(f"Online access: {message}")

    def _prepare(self) -> Optional[tuple[list[str], dict[str, str]]]:
        """Command and environment for frpc, or None (error state set)."""
        binary = self.binary_finder()
        if not binary:
            self._fail("The tunnel program (frpc) is not installed. Re-run the installer with EDGE_TUNNEL=1 "
                       "(or ./run.sh --with-tunnel), then save again.")
            return None
        try:
            token = _read_token()
            server_key = _read_secret(SERVER_KEY_SECRET_NAME) if server_key_configured() else None
        except Exception as exc:
            logger.error(f"Tunnel secrets could not be read: {exc.__class__.__name__}")
            token = server_key = None
        if not token:
            self._fail("The stored store token could not be read on this machine. Enter it again.")
            return None
        s = self.settings
        try:
            server = parse_server_url(s.get("server_url"))
            hostname = normalise_hostname(s.get("hostname"))
            store_id = normalise_store_id(s.get("store_id"))
        except RemoteAccessError as exc:
            self._fail(str(exc))
            return None
        ca_file = self.ca_finder()
        if not ca_file:
            self._fail("No CA certificate bundle found to verify the tunnel server. Install the "
                       "ca-certificates package, or set EDGE_TUNNEL_CA_FILE for a private CA.")
            return None
        override = self.connect_override or (None, None)
        text = build_frpc_config(server=server, hostname=hostname, store_id=store_id, local_port=settings.PORT,
                                 ca_file=ca_file, server_key=bool(server_key),
                                 connect_host=override[0], connect_port=override[1])
        try:
            path = write_frpc_config(text, self.config_file)
        except OSError as exc:
            self._fail(f"The tunnel configuration could not be written: {exc.strerror or exc}")
            return None
        self._secret_redactions = tuple(x for x in (token, server_key) if x)
        # A minimal environment: frpc gets its secrets and nothing else of ours
        # (the service environment carries .env secrets it has no use for).
        env = {k: v for k, v in os.environ.items() if k in self.env_passthrough}
        env[FRPC_STORE_TOKEN_ENV] = token
        if server_key:
            env[FRPC_SERVER_KEY_ENV] = server_key
        return [binary, "-c", str(path)], env

    def _supervise(self, stop: threading.Event) -> None:
        backoff = self.backoff_base
        while not stop.is_set():
            prepared = self._prepare()
            if prepared is None:
                if stop.wait(min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue
            cmd, env = prepared
            if stop.is_set():
                break
            with self._lock:
                self.state = STARTING
                self.connected_since = None
            logger.info(f"Starting frpc ({cmd[0]}) for {self.hostname() or '-'} via "
                        f"{self.settings.get('server_url') or '-'}")
            try:
                proc = subprocess.Popen(
                    cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                self._fail(f"The tunnel program could not be started: {exc.strerror or exc}")
                if stop.wait(min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue

            started = time.monotonic()
            with self._lock:
                self._proc = proc
                self.started_at = time.time()
            if stop.is_set():  # stop requested while we were starting
                self._terminate_process()
            last_err: Optional[str] = None
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                last_err = self._handle_log_line(line) or last_err
                if stop.is_set():
                    break
            code = proc.wait()
            with self._lock:
                self._proc = None
                self.connected_since = None
            if stop.is_set():
                break

            uptime = time.monotonic() - started
            if uptime >= self.stable_after:
                backoff = self.backoff_base
            delay = min(backoff, self.backoff_max)
            with self._lock:
                self.state = ERROR
                self.restarts += 1
                if not last_err:
                    self.error_kind = EXITED
                self.last_error = (last_err or f"The tunnel program stopped (exit code {code}).") \
                    + f" Retrying in {delay:.0f}s."
            logger.warning(f"Online access: frpc exited with code {code}; {self.last_error}")
            if stop.wait(delay):
                break
            backoff = min(backoff * 2, self.backoff_max)

        with self._lock:
            self.state = STOPPED
            self.connected_since = None

    def _handle_log_line(self, line: str) -> Optional[str]:
        """Log an frpc line and update the connection state. Returns an error text."""
        line = self._redact(_ANSI_RE.sub("", line))
        m = _FRPC_LINE_RE.match(line)
        level, msg = (m.group(1), m.group(2)) if m else ("I", line)
        logger.log(_FRPC_LEVELS.get(level, logging.INFO), f"frpc: {msg}")
        s = self.settings
        hostname, store_id = s.get("hostname") or "", s.get("store_id") or ""
        low = msg.lower()
        found: Optional[tuple[str, str]] = None
        if "start proxy success" in low:
            with self._lock:
                self.state = CONNECTED
                self.last_error = None
                self.error_kind = None
                self.connected_since = _utcnow_iso()
            self._maybe_verify_soon()
        elif "try to connect to server" in low:
            with self._lock:
                if self.state == CONNECTED:
                    # frpc lost the session and is reconnecting by itself.
                    self.state = STARTING
                    self.connected_since = None
                    self.last_error = "Connection to the tunnel server lost; reconnecting."
                    self.error_kind = CONNECTION_LOST
                elif self.state != ERROR:
                    self.state = STARTING
        elif "start error:" in low:
            found = classify_proxy_error(msg.split("start error:", 1)[1], hostname, store_id)
        elif "connect to server error:" in low:
            found = classify_connect_error(msg.split("connect to server error:", 1)[1])
        elif low.startswith("login to the server failed:"):
            found = classify_connect_error(msg.split(":", 1)[1].split(". With loginFailExit", 1)[0])
        elif level == "E":
            found = (EXITED, re.sub(r"\s+", " ", msg).strip()[:300])
        if found:
            with self._lock:
                self.state = ERROR
                self.error_kind, self.last_error = found
                self.connected_since = None
            return found[1]
        return None

    def _maybe_verify_soon(self) -> None:
        if time.monotonic() - self._last_verify < 30:
            return
        self._last_verify = time.monotonic()

        def later():
            time.sleep(3)
            if self.state == CONNECTED:
                self.verify()

        threading.Thread(target=later, name="remote-access-verify", daemon=True).start()

    # -- verification ---------------------------------------------------------

    def verify(self, timeout: float = 10.0, client_factory: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
        """Fetch https://<hostname>/api/v1/device/identity and compare device_id.

        Proves the public address reaches THIS device, not another store's box
        or a stale route on the VPS.
        """
        import httpx

        s = self.settings
        hostname = s.get("hostname")
        result: dict[str, Any] = {"verified": False, "verified_at": None, "verify_error": None}
        if not hostname:
            result["verify_error"] = "No public address configured."
        else:
            mine = this_device_id()
            url = f"https://{hostname}/api/v1/device/identity"
            try:
                factory = client_factory or httpx.Client
                with factory(timeout=timeout, follow_redirects=False) as client:
                    res = client.get(url, headers={"Accept": "application/json"})
                if res.status_code != 200:
                    result["verify_error"] = f"{url} answered HTTP {res.status_code}."
                else:
                    body = res.json()
                    theirs = body.get("device_id") if isinstance(body, dict) else None
                    if not mine:
                        result["verify_error"] = "This device has no device id yet (device identity service missing)."
                    elif theirs == mine:
                        result.update(verified=True, verified_at=_utcnow_iso())
                    elif theirs:
                        result["verify_error"] = (
                            f"{hostname} reaches a different device (device id {str(theirs)[:8]}...). "
                            "Check the VPS route for this address."
                        )
                    else:
                        result["verify_error"] = f"{url} did not return a device identity."
            except Exception as exc:  # DNS, TLS, timeout, non-JSON
                result["verify_error"] = f"{hostname} is not reachable: {exc.__class__.__name__}: {str(exc)[:160]}"
        with self._lock:
            new = dict(self._settings)
            new.update(result, verified_hostname=hostname if result["verified"] else None)
            self._settings = new
        try:
            save_settings(new)
        except sqlite3.Error as exc:
            logger.warning(f"Could not persist verification result: {exc}")
        if result["verified"]:
            logger.info(f"Online access verified: https://{hostname} reaches this device")
        else:
            logger.warning(f"Online access verification failed: {result['verify_error']}")
        return result

    def _periodic_verify(self) -> None:
        if self.state == CONNECTED and time.monotonic() - self._last_verify > self.verify_interval:
            self._last_verify = time.monotonic()
            threading.Thread(target=self.verify, name="remote-access-verify", daemon=True).start()

    # -- status -----------------------------------------------------------------

    def _binary_info(self) -> tuple[Optional[str], Optional[str]]:
        binary = self.binary_finder()
        if not binary:
            return None, None
        with self._lock:
            cached = self._version_cache
        if cached and cached[0] == binary:
            return binary, cached[1]
        version = frpc_version(binary)
        with self._lock:
            self._version_cache = (binary, version)
        return binary, version

    def status(self) -> dict[str, Any]:
        self._periodic_verify()
        s = self.settings
        with self._lock:
            state, last_error, error_kind = self.state, self.last_error, self.error_kind
            restarts, connected_since = self.restarts, self.connected_since
        binary, version = self._binary_info()
        hostname = s.get("hostname") or ""
        return {
            "enabled": bool(s.get("enabled")),
            "provider": PROVIDER,
            "hostname": hostname,
            "server_url": s.get("server_url") or "",
            "store_id": s.get("store_id") or "",
            "extra_proxies": self.forwarding_hops() - 1,
            "public_url": self.public_url(),
            "token_configured": token_configured(),
            "server_key_configured": server_key_configured(),
            "process": state,
            "connected_since": connected_since,
            "last_error": last_error,
            "error_kind": error_kind if state == ERROR or error_kind == CONNECTION_LOST else None,
            "restarts": restarts,
            "verified": bool(s.get("verified")) and s.get("verified_hostname") == hostname,
            "verified_at": s.get("verified_at"),
            "verify_error": s.get("verify_error"),
            "tunnel_client_found": bool(binary),
            "tunnel_client_version": version,
            "proxy_name": proxy_name_for(s["store_id"]) if s.get("store_id") else None,
            "auth_disabled": bool(settings.AUTH_DISABLED),
            # 127.0.0.1 rather than localhost: with HOST=127.0.0.1 uvicorn
            # listens on IPv4 only, and "localhost" may resolve to ::1 first.
            "local_origin": f"http://127.0.0.1:{settings.PORT}",
            # Remote live video (direct WebRTC, STUN only).
            **self.webrtc_settings(),
        }

    # -- lifespan ---------------------------------------------------------------

    async def start(self) -> None:
        import asyncio

        await asyncio.to_thread(self.reload_settings)
        await asyncio.to_thread(_remove_legacy_secrets)
        run, reason = self.should_run()
        if reason:
            logger.warning(f"Online access enabled but not started: {reason}")
        self.apply()

    async def stop(self) -> None:
        import asyncio

        await asyncio.to_thread(self._stop_supervisor)


remote_access_service = RemoteAccessService()
