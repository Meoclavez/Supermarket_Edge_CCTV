"""Hardening for when the dashboard is reachable from outside the store.

Two routes reach the dashboard from outside, both through a proxy on this
machine that connects to uvicorn over loopback:

``tailscale serve`` (private: the owner's and staff's tailnet)
    ``https://<machine>.<tailnet>.ts.net`` with a real certificate. Tailscale
    keeps the ``Host`` header (the ``.ts.net`` name), replaces
    ``X-Forwarded-For`` with the caller's tailnet address, sets
    ``X-Forwarded-Proto: https`` and ``X-Forwarded-Host``, and adds
    ``Tailscale-User-Login``/``-Name``/``-Profile-Pic`` for a user's device
    (not for tagged devices). Client-supplied copies of all of these are
    removed first. A Funnel request (public internet) additionally carries
    ``Tailscale-Funnel-Request: ?1``.
``cloudflared`` (public: the owner's domain)
    ``Host`` is the public hostname; Cloudflare adds ``CF-Connecting-IP``,
    ``CF-Ray``, ``CF-Visitor`` and appends to ``X-Forwarded-For``.

**Which proxy headers are believed.** Only those arriving from a loopback
peer. uvicorn's own ProxyHeadersMiddleware (on by default, trusting
127.0.0.1/::1) usually has already replaced ``scope["client"]`` and
``scope["scheme"]`` from ``X-Forwarded-For``/``-Proto`` before the app runs;
this module gives the same answers whether or not that happened, so it is
correct under the systemd unit, ``run.sh`` and the tests alike. A client on
the LAN or tailnet that connects directly and sends these headers is ignored,
so nobody can spoof an address to dodge the lockout (:func:`client_ip`).

**Remote (public) vs. private.** :func:`is_remote_request` is true for
requests that name the configured public hostname, carry Cloudflare headers,
or come through Tailscale Funnel. Tailnet requests through ``tailscale serve``
are private, like the store LAN: only devices the owner admitted to the
tailnet can make them. For remote requests, first-run setup (which creates
the owner account from a one-time code) and the API docs are refused, and
nothing is served while ``AUTH_DISABLED`` is on. Being classified remote only
ever adds restrictions, so a direct client forging Cloudflare or Funnel
headers gains nothing.

**Headers.** A Content-Security-Policy that allows scripts only from this
origin plus the pages' own inline blocks by hash (see :func:`content_security_policy`),
``frame-ancestors 'self'``, ``nosniff``, ``Referrer-Policy: same-origin``
(stream URLs may carry ``?token=``), and HSTS only on a response to a request
that actually arrived over HTTPS (Cloudflare or ``tailscale serve``), never on
the plain-HTTP LAN or tailnet address.

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


TAILNET_SUFFIX = ".ts.net"
TAILSCALE_IDENTITY_HEADERS = ("tailscale-user-login", "tailscale-user-name", "tailscale-headers-info")

# proxy_kind() results
TAILSCALE, TAILSCALE_FUNNEL, CLOUDFLARE, LOCAL_PROXY = "tailscale", "tailscale_funnel", "cloudflare", "proxy"


def _right_most_ip(xff: str) -> Optional[str]:
    # One trusted hop (the proxy on this machine) appended or set the address
    # it saw, so the right-most valid entry is the client.
    for part in reversed((xff or "").split(",")):
        ip = _valid_ip(part)
        if ip:
            return ip
    return None


def _host_only(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_tailnet_host(host_header: str) -> bool:
    """``<machine>.<tailnet>.ts.net``: the only names ``tailscale serve`` answers for with HTTPS."""
    return _host_only(host_header).rstrip(".").endswith(TAILNET_SUFFIX)


def proxy_kind(request) -> Optional[str]:
    """Which proxy on this machine delivered the request (None: not via a loopback proxy).

    ``tailscale`` is checked first: tailscaled passes other client headers
    through unchanged, so a tailnet user could add ``CF-Connecting-IP``; for a
    ``.ts.net`` host those are ignored.
    """
    if not _is_loopback(_peer(request)):
        return None
    h = request.headers
    if h.get("tailscale-funnel-request"):
        return TAILSCALE_FUNNEL
    if is_tailnet_host(h.get("host", "")) or any(h.get(n) for n in TAILSCALE_IDENTITY_HEADERS):
        return TAILSCALE
    if h.get("cf-connecting-ip") or h.get("cf-ray"):
        return CLOUDFLARE
    if h.get("x-forwarded-for") or h.get("x-forwarded-proto"):
        return LOCAL_PROXY
    return None


def client_ip(request: Request) -> str:
    """The real client address, for lockouts, rate limits and audit logs."""
    peer = _peer(request)
    kind = proxy_kind(request)
    if kind is not None:
        headers = request.headers
        if kind == CLOUDFLARE:
            cf = _valid_ip(_header(headers, "cf-connecting-ip"))
            if cf:
                return cf
        ip = _right_most_ip(_header(headers, "x-forwarded-for"))
        if ip:
            return ip
    return peer or "unknown"


def _remote_hostname() -> str:
    try:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.hostname()
    except Exception:
        return ""


def is_remote_request(request: Request) -> bool:
    """True when this request came in from the public internet.

    That is: it names the configured public hostname, or carries Cloudflare
    or Tailscale Funnel headers (from any peer: uvicorn may already have
    replaced the loopback peer with the visitor's address, and a direct client
    forging them only subjects itself to the stricter rules). Tailnet requests
    through ``tailscale serve`` are private and return False.
    """
    h = request.headers
    hostname = _remote_hostname()
    if hostname and _host_only(h.get("host", "")) == hostname:
        return True
    if h.get("tailscale-funnel-request"):
        return True
    if is_tailnet_host(h.get("host", "")):
        return False
    return bool(h.get("cf-connecting-ip") or h.get("cf-ray"))


def came_via_https_proxy(request: Request) -> bool:
    """True when the browser's connection was HTTPS.

    A direct connection (or one whose scheme uvicorn already took from a
    trusted proxy's X-Forwarded-Proto) is judged by its scheme; forwarding
    headers are believed only from a loopback peer.
    """
    if request.url.scheme in ("https", "wss"):
        return True
    if not _is_loopback(_peer(request)):
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    return proxy_kind(request) == CLOUDFLARE and '"https"' in (request.headers.get("cf-visitor") or "")


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
            refusal = self._refusal(scope.get("path", ""), scope.get("method", "GET"))
            if refusal:
                logger.warning(f"Refused remote request to {scope.get('path')} from {client_ip(request)}: {refusal}")
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"detail": refusal}).encode()
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
        # Only when this request really came over HTTPS (Cloudflare or
        # tailscale serve): HSTS on the plain-HTTP LAN/tailnet address would
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
