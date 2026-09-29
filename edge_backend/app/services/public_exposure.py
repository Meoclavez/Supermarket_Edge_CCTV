"""Hardening for when the dashboard is reachable from outside the store.

The public route is the owner's own VPS (``remote_access_service``, provider
``vps_tunnel``)::

    browser --https--> VPS reverse proxy --http--> frps ==tunnel==> frpc --> uvicorn (127.0.0.1)

frpc connects to uvicorn over loopback, so every tunnel request has a
loopback peer and ``Host`` equal to the configured public address (frps routes
by ``Host``, so no other name can arrive through it). On the way, the VPS
proxy appends the visitor's address to ``X-Forwarded-For`` (nginx, Traefik,
Caddy and Nginx Proxy Manager all do by default), and frps appends the VPS
proxy's own address after it and overwrites ``X-Forwarded-Proto`` with
``http`` (frp ``pkg/util/vhost/http.go``: ``SetXForwarded``). Hence:

* the visitor is the SECOND entry from the right of ``X-Forwarded-For``
  (:func:`client_ip`), or one further left per proxy configured in front of
  the VPS (e.g. a CDN, Settings -> Online access); entries further left came
  from the visitor and are ignored, as is ``X-Real-IP``, which frps passes
  through unchanged;
* the browser's connection was HTTPS (the VPS proxy terminates TLS), whatever
  ``X-Forwarded-Proto`` says (:func:`came_via_https_proxy`).

**Which proxy headers are believed.** Only those arriving from a loopback
peer. The launchers run uvicorn with ``--no-proxy-headers`` so the app sees the
real peer: with uvicorn's own rewriting on, a tunnel request's peer would
become the VPS proxy's address (the right-most entry) and every visitor would
share it. That degraded case is still safe (the request stays remote, the
address just is not the visitor's), and it is logged once. A client on the
LAN or tailnet that connects directly and sends forwarding headers is
ignored, so nobody can pick an address to dodge the lockout. Another proxy on
this machine (for example a local Caddy) is handled generically: its
right-most ``X-Forwarded-For`` entry is the client.

**Remote (public) vs. private.** :func:`is_remote_request` is true for
requests that name the configured public address (from any peer: a direct
client naming it only subjects itself to the stricter rules). The store LAN
and the owner's private Tailscale address (plain ``http://<100.x>:8000``)
are private. For remote requests, first-run setup (which creates the owner
account from a one-time code) and the API docs are refused, and nothing is
served while ``AUTH_DISABLED`` is on.

**Headers.** A Content-Security-Policy that allows scripts only from this
origin plus the pages' own inline blocks by hash (see :func:`content_security_policy`),
``frame-ancestors 'self'``, ``nosniff``, ``Referrer-Policy: same-origin``
(stream URLs may carry ``?token=``), and HSTS only on a response to a request
that actually arrived over HTTPS (the tunnel, or a local HTTPS proxy), never
on the plain-HTTP LAN or tailnet address.

CORS is limited to this device's own origins: the public ``https://<hostname>``,
``EDGE_BASE_URL`` and any explicit (non-``*``) entries in
``ALLOWED_CORS_ORIGINS``. The dashboard itself is same-origin and needs no
CORS; the phone app is native and sends no Origin.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

logger = logging.getLogger("edge.security")

HSTS_VALUE = "max-age=31536000"
# Paths that must never be served to the internet.
REMOTE_REFUSED_PREFIXES = ("/api/v1/setup/",)
# Read-only status the phone app checks on start; it reveals only whether
# setup is complete.
REMOTE_READONLY_ALLOWED = ("/api/v1/setup/status",)
REMOTE_REFUSED_EXACT = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
# No live pixels through the tunnel (docs/REMOTE_VIDEO_CONTRACT.md, rule 2):
# remote viewers get camera video only as direct peer-to-peer WebRTC
# (/api/v1/webrtc/*), never through the VPS. Any method. Recorded DVR video
# is refused too (the device is evidence-only). Stored evidence stills and
# short clips (event clips/snapshots, theft evidence, night-watch evidence,
# zone alert snapshots) stay downloadable on an explicit click.
VIDEO_DIRECT_ONLY = "video_direct_only"
REMOTE_VIDEO_REFUSED = tuple(re.compile(p) for p in (
    r"^/stream/?$",                                   # MJPEG
    r"^/api/v1/cameras/[^/]+/snapshot/?$",            # live JPEG
    r"^/api/v1/cameras/[^/]+/actions/snapshot/?$",    # live JPEG saved on demand
    r"^/api/v1/cameras/test-connection/?$",           # opens a camera and returns a frame
    r"^/api/v1/dvr/cameras/[^/]+/hls(?:/|$)",         # DVR HLS
    r"^/api/v1/dvr/segments/[^/]+/video/?$",          # recorded video segment
    r"^/api/v1/dvr/archives/[^/]+/download/?$",       # recorded video export
))
REMOTE_VIDEO_DETAIL = ("Live camera video is not sent through the online-access tunnel. Remote viewers get "
                       "it directly from this device (peer-to-peer WebRTC, /api/v1/webrtc/config).")


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return host == "localhost"


def _valid_ip(value: str) -> Optional[str]:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _header(headers, name: str) -> str:
    return headers.get(name) or ""


def _peer(request_or_scope) -> Optional[str]:
    client = request_or_scope.client if isinstance(request_or_scope, Request) else request_or_scope.get("client")
    if client is None:
        return None
    return client.host if hasattr(client, "host") else client[0]


# proxy_kind() results
TUNNEL, LOCAL_PROXY = "tunnel", "proxy"

_warned_rewritten_peer = False


def _host_only(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.rstrip(".")


def _xff_entries(xff: str) -> list[str]:
    return [part.strip() for part in (xff or "").split(",") if part.strip()]


def _right_most_ip(xff: str) -> Optional[str]:
    # One trusted hop (the proxy on this machine) appended or set the address
    # it saw, so the right-most valid entry is the client.
    for part in reversed(_xff_entries(xff)):
        ip = _valid_ip(part)
        if ip:
            return ip
    return None


def _tunnel_client_ip(xff: str, hops: int = 1) -> Optional[str]:
    """Visitor address of a tunnel request.

    ``hops`` is the number of trusted proxies before frps: the VPS's reverse
    proxy, plus any configured in front of it (Settings -> Online access,
    e.g. a CDN). Right-most entry: the VPS proxy, appended by frps. Each hop
    before it appended the address it saw, so the visitor is ``hops + 1``
    from the right (2nd with no CDN, 3rd with one). Entries further left were
    sent by the visitor and are never used. When there are fewer entries, a
    hop replaced the header instead of appending (e.g. a proxy that does not
    trust the one in front of it); the left-most entry is then the earliest
    address a trusted hop wrote. An entry that is not an IP falls back to the
    VPS proxy's.
    """
    entries = _xff_entries(xff)
    if not entries:
        return None
    idx = max(0, len(entries) - 1 - max(1, hops))
    return _valid_ip(entries[idx]) or _valid_ip(entries[-1])


def _forwarding_hops() -> int:
    try:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.forwarding_hops()
    except Exception:
        return 1


def _remote_hostname() -> str:
    try:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.hostname()
    except Exception:
        return ""


def _names_public_host(request) -> bool:
    hostname = _remote_hostname()
    return bool(hostname) and _host_only(request.headers.get("host", "")) == hostname


def proxy_kind(request) -> Optional[str]:
    """Which proxy on this machine delivered the request (None: not via a loopback proxy)."""
    if not _is_loopback(_peer(request)):
        return None
    if _names_public_host(request):
        return TUNNEL
    h = request.headers
    if h.get("x-forwarded-for") or h.get("x-forwarded-proto"):
        return LOCAL_PROXY
    return None


def _note_rewritten_peer(request) -> None:
    """Warn once when uvicorn already replaced the loopback peer of a tunnel request."""
    global _warned_rewritten_peer
    if _warned_rewritten_peer:
        return
    peer = _peer(request)
    entries = _xff_entries(request.headers.get("x-forwarded-for") or "")
    if peer and entries and _valid_ip(entries[-1]) == _valid_ip(peer) and _names_public_host(request):
        _warned_rewritten_peer = True
        logger.warning("A request through the tunnel arrived with uvicorn's proxy-header rewriting on: remote "
                       "visitors all appear as the VPS proxy's address. Start uvicorn with --no-proxy-headers "
                       "(deploy/edge-cctv.service and run.sh do).")


def client_ip(request: Request) -> str:
    """The real client address, for lockouts, rate limits and audit logs."""
    peer = _peer(request)
    kind = proxy_kind(request)
    if kind is not None:
        xff = _header(request.headers, "x-forwarded-for")
        ip = _tunnel_client_ip(xff, _forwarding_hops()) if kind == TUNNEL else _right_most_ip(xff)
        if ip:
            return ip
    elif peer and request.headers.get("x-forwarded-for"):
        _note_rewritten_peer(request)
    return peer or "unknown"


def is_remote_request(request: Request) -> bool:
    """True when this request came in from the public internet.

    That is: it names the configured public address. Checked for any peer:
    a direct client naming it only subjects itself to the stricter rules.
    ``Tailscale-Funnel-Request`` (Tailscale's public share, not used here) is
    treated as public too, so turning Funnel on by accident never opens
    first-run setup to the internet.
    """
    if _names_public_host(request):
        return True
    return bool(request.headers.get("tailscale-funnel-request"))


def came_via_https_proxy(request: Request) -> bool:
    """True when the browser's connection was HTTPS.

    A direct connection (or one whose scheme uvicorn already took from a
    trusted proxy's X-Forwarded-Proto) is judged by its scheme; forwarding
    headers are believed only from a loopback peer. Tunnel requests always
    were HTTPS: the VPS proxy terminates TLS, and frps then overwrites
    X-Forwarded-Proto with "http".
    """
    if request.url.scheme in ("https", "wss"):
        return True
    if not _is_loopback(_peer(request)):
        return False
    if proxy_kind(request) == TUNNEL:
        return True
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def is_live_video_path(path: str) -> bool:
    """True for an endpoint that serves live camera pixels (refused through the tunnel)."""
    path = re.sub(r"/{2,}", "/", path or "")
    return any(p.match(path) for p in REMOTE_VIDEO_REFUSED)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #

class PublicExposureMiddleware:
    """Pure ASGI (does not buffer the MJPEG stream or break websockets)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        # Request() asserts an http scope; HTTPConnection serves both (the
        # dashboard's /api/v1/events/ws failed with a 500 before this).
        request = Request(scope) if scope["type"] == "http" else HTTPConnection(scope)
        remote = is_remote_request(request)
        if remote:
            path = scope.get("path", "")
            refusal = self._refusal(path, scope.get("method", "GET"))
            code = None
            if refusal is None and is_live_video_path(path):
                refusal, code = REMOTE_VIDEO_DETAIL, VIDEO_DIRECT_ONLY
            if refusal:
                logger.warning(f"Refused remote request to {scope.get('path')} from {client_ip(request)}: {refusal}")
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"code": code, "detail": refusal} if code else {"detail": refusal}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    *self._security_headers(request, remote)]})
                await send({"type": "http.response.body", "body": body})
                return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        extra = self._security_headers(request, remote)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in extra if k not in existing
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _refusal(path: str, method: str = "GET") -> Optional[str]:
        if settings.AUTH_DISABLED:
            return "Remote access is refused while AUTH_DISABLED=true."
        if method in ("GET", "HEAD") and path in REMOTE_READONLY_ALLOWED:
            return None
        if path.startswith(REMOTE_REFUSED_PREFIXES) or path.rstrip("/") == "/api/v1/setup":
            return ("First-run setup is only available on the store network. Open the dashboard "
                    "on the local address to create the operator account.")
        if path in REMOTE_REFUSED_EXACT:
            return "Not available over remote access."
        return None

    @staticmethod
    def _security_headers(request: Request, remote: bool) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", content_security_policy(request).encode("latin-1")),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"referrer-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ]
        # Only when this request really came over HTTPS (the VPS tunnel or a
        # local HTTPS proxy): HSTS on the plain-HTTP LAN/tailnet address would
        # be ignored at best and, for a hostname, lock browsers out of it.
        if came_via_https_proxy(request):
            headers.append((b"strict-transport-security", HSTS_VALUE.encode()))
        return headers


# Google Fonts is the only third-party origin the pages use (stylesheet +
# font files). Everything else, Chart.js included, is served from here.
FONT_STYLESHEETS = "https://fonts.googleapis.com"
FONT_FILES = "https://fonts.gstatic.com"
_SAFE_HOST_RE = re.compile(r"^(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::\d{1,5})?$")


def _inline_script_hashes() -> tuple[str, ...]:
    try:
        from app.services.web_delivery import inline_script_hashes

        return inline_script_hashes()
    except Exception as exc:  # a missing page must not take every response down
        logger.warning(f"Could not hash the pages' inline scripts: {exc}")
        return ()


def content_security_policy(request) -> str:
    """The Content-Security-Policy for this device's pages and API.

    Script *elements* are limited to this origin plus the pages' own inline
    blocks by SHA-256 (``script-src-elem``): no CDN, no injected ``<script>``,
    no ``eval``. Inline event-handler *attributes* (``onclick="..."``) are
    still allowed by ``script-src-attr 'unsafe-inline'`` because the pages
    and several modules still use them; ``script-src`` keeps ``'unsafe-inline'``
    only as the fallback for browsers without CSP level 3 (in CSP3 browsers the
    ``-elem``/``-attr`` directives take precedence). Images may be ``data:``
    (QR codes) and ``blob:`` (clips and snapshots saved from the page);
    ``connect-src`` names the WebSocket scheme for this host explicitly for
    browsers whose ``'self'`` does not cover ``ws:``/``wss:``.
    """
    host = (request.headers.get("host") or "").strip().lower()
    ws = f" ws://{host} wss://{host}" if host and _SAFE_HOST_RE.match(host) else ""
    hashes = " ".join(_inline_script_hashes())
    return "; ".join((
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        f"script-src-elem 'self' {hashes}".rstrip(),
        "script-src-attr 'unsafe-inline'",
        f"style-src 'self' 'unsafe-inline' {FONT_STYLESHEETS}",
        f"font-src 'self' data: {FONT_FILES}",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        f"connect-src 'self'{ws}",
        "worker-src 'self' blob:",
        "frame-src 'self'",
        "frame-ancestors 'self'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "manifest-src 'self'",
    ))


def _origin_of(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def configured_origins(extra: Iterable[str] = ()) -> set[str]:
    origins: set[str] = set()
    for value in [*(settings.ALLOWED_CORS_ORIGINS or []), getattr(settings, "EDGE_BASE_URL", ""), *extra]:
        if value and value.strip() != "*":
            origin = _origin_of(value)
            if origin:
                origins.add(origin)
    hostname = _remote_hostname()
    if hostname:
        origins.add(f"https://{hostname}")
    return origins


class OwnOriginCORSMiddleware(CORSMiddleware):
    """CORS for this device's own origins only (evaluated per request, so a
    hostname saved in Settings takes effect without a restart)."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app, allow_origins=[], allow_credentials=True,
                         allow_methods=["*"], allow_headers=["*"])
        if "*" in (settings.ALLOWED_CORS_ORIGINS or []):
            logger.warning("ALLOWED_CORS_ORIGINS contains '*': ignored. Only this device's own "
                           "origins (and explicit entries) are allowed cross-origin.")

    def is_allowed_origin(self, origin: str) -> bool:
        origin = (origin or "").lower().rstrip("/")
        if not origin:
            return False
        return origin in configured_origins()


def install(app) -> None:
    """Add CORS + public-exposure middleware (outermost last)."""
    app.add_middleware(OwnOriginCORSMiddleware)
    app.add_middleware(PublicExposureMiddleware)
