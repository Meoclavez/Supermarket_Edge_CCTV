"""Hardening for when the dashboard is reachable from the internet.

With remote access on, every request from the internet reaches uvicorn from
``cloudflared`` (or the operator's Caddy) on the loopback interface. That has
three consequences handled here:

* **Client IP.** ``request.client.host`` is 127.0.0.1 for every remote user,
  so lockouts and rate limits would either lock out everyone at once or never
  distinguish attackers. :func:`client_ip` trusts ``CF-Connecting-IP`` /
  ``X-Forwarded-For`` **only** when the TCP peer is loopback (the proxy on
  this machine). A LAN client that sends those headers is ignored, so nobody
  on the store network can spoof an address to dodge the lockout.
* **Remote requests.** :func:`is_remote_request` is true when the request
  names the configured public hostname or arrived through a local proxy that
  added Cloudflare headers. First-run setup (which creates the owner account
  from a one-time code) and the API docs refuse such requests, and nothing is
  served remotely while ``AUTH_DISABLED`` is on.
* **Headers.** ``X-Content-Type-Options``, ``frame-ancestors 'self'`` (Camera
  Studio embeds same-origin iframes, so ``'self'`` and not ``'none'``),
  ``Referrer-Policy: same-origin`` (stream URLs carry ``?token=``), and HSTS
  only on responses that went out through the remote HTTPS hostname, never on
  the plain-HTTP LAN address.

CORS is limited to this device's own origins: the public ``https://<hostname>``,
``EDGE_BASE_URL`` and any explicit (non-``*``) entries in
``ALLOWED_CORS_ORIGINS``. The dashboard itself is same-origin and needs no
CORS; the phone app is native and sends no Origin.
"""

from __future__ import annotations

import ipaddress
import json
import logging
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


def client_ip(request: Request) -> str:
    """The real client address, for lockouts, rate limits and audit logs."""
    peer = _peer(request)
    if _is_loopback(peer):
        headers = request.headers
        cf = _valid_ip(_header(headers, "cf-connecting-ip"))
        if cf:
            return cf
        xff = _header(headers, "x-forwarded-for")
        if xff:
            # One trusted hop (the proxy on this machine): it appended the
            # address it saw, so the right-most valid entry is the client.
            for part in reversed(xff.split(",")):
                ip = _valid_ip(part)
                if ip:
                    return ip
    return peer or "unknown"


def _host_only(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _remote_hostname() -> str:
    try:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.hostname()
    except Exception:
        return ""


def is_remote_request(request: Request) -> bool:
    """True when this request came in from the internet through the public hostname."""
    hostname = _remote_hostname()
    if hostname and _host_only(request.headers.get("host", "")) == hostname:
        return True
    if _is_loopback(_peer(request)):
        h = request.headers
        if h.get("cf-connecting-ip") or h.get("cf-ray"):
            return True
    return False


def came_via_https_proxy(request: Request) -> bool:
    """Only a loopback proxy's X-Forwarded-Proto is believed."""
    if not _is_loopback(_peer(request)):
        return request.url.scheme == "https"
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    visitor = request.headers.get("cf-visitor") or ""
    return proto == "https" or '"https"' in visitor or request.url.scheme == "https"


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
            (b"content-security-policy", b"frame-ancestors 'self'"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"referrer-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ]
        if remote and came_via_https_proxy(request):
            headers.append((b"strict-transport-security", HSTS_VALUE.encode()))
        return headers


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
