"""Fast, cache-friendly delivery of the dashboard over a slow uplink.

The edge box usually sits on store Wi-Fi behind a home-grade uplink, and the
dashboard is opened over Tailscale or a Cloudflare Tunnel from far away
(measured: ~360 ms RTT, ~55 KB/s per request). Three things make a load cheap
there, and all three live in this module:

* **Compression.** :class:`DeliveryMiddleware` gzips text responses (HTML,
  CSS, JS, JSON, SVG) of at least 1 KB. It is pure ASGI and never holds back
  a streaming body: a response sent in several chunks is compressed chunk by
  chunk with a sync flush, so every byte the app sends goes out immediately.
  Everything that is not text passes through untouched: the multipart MJPEG
  ``/stream``, ``image/*``, ``video/*``, ``text/event-stream``, websockets and
  anything already carrying a ``Content-Encoding``. Brotli is not used: no
  brotli module is installed and adding a native dependency for a few percent
  over gzip is not worth it on the edge box.
* **Content-hash cache busting.** Pages are served through
  :func:`html_response`, which rewrites every local ``/static/...`` URL in
  ``src``/``href`` to ``?v=<hash of the file>``. The hashes are computed once
  per file and recomputed only when its mtime or size changes, so an edited
  asset gets a new URL on the next page load without anyone bumping a version
  string. :class:`CachedStaticFiles` then serves a request whose ``v`` matches
  the file's current hash with ``Cache-Control: public, max-age=31536000,
  immutable`` (a repeat visit fetches only the HTML), and anything else with
  ``no-cache`` (revalidate with the ETag; never stale). Text assets are
  gzipped once per file version and kept in memory.
* **Revalidation, not refetching, for HTML.** Pages carry an ETag and
  ``Cache-Control: no-cache``; an unchanged page costs one 304. API responses
  (``/api/...``) get ``Cache-Control: no-store`` unless the route set its own:
  they carry store data and must never be cached by a browser or a proxy.

The pages' inline ``<script>`` blocks are hashed here too
(:func:`inline_script_hashes`) so the Content-Security-Policy in
``public_exposure.py`` can allow exactly those scripts and nothing else inline.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
import re
import threading
import zlib
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
HTML_CACHE_CONTROL = "no-cache"
API_CACHE_CONTROL = "no-store"

MINIMUM_SIZE = 1024
GZIP_LEVEL = 6
# Media types worth compressing. Anything else (images, video, multipart
# MJPEG, event streams, archives, fonts) is sent as is.
COMPRESSIBLE_TYPES = frozenset({
    "text/html", "text/css", "text/plain", "text/javascript", "text/csv", "text/xml", "text/markdown",
    "application/javascript", "application/json", "application/problem+json", "application/xml",
    "application/manifest+json", "application/geo+json", "image/svg+xml",
})
PAGES = ("index.html", "studio.html")


def _media_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_compressible_type(content_type: str) -> bool:
    return _media_type(content_type) in COMPRESSIBLE_TYPES


def accepts_gzip(accept_encoding: str) -> bool:
    """True when the client lists gzip (or ``*``) with a non-zero quality."""
    for part in (accept_encoding or "").lower().split(","):
        token, _, params = part.strip().partition(";")
        if token.strip() not in ("gzip", "*"):
            continue
        q = 1.0
        for p in params.split(";"):
            name, _, value = p.strip().partition("=")
            if name == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q > 0:
            return True
    return False


def _append_vary(headers: MutableHeaders, value: str = "Accept-Encoding") -> None:
    current = headers.get("vary", "")
    names = [v.strip().lower() for v in current.split(",") if v.strip()]
    if "*" in names or value.lower() in names:
        return
    headers["vary"] = f"{current}, {value}" if current else value


# --------------------------------------------------------------------------- #
# Content hashes of static files (cached per mtime + size)
# --------------------------------------------------------------------------- #

class _AssetHashes:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, int, str]] = {}

    def version(self, full_path: str | os.PathLike, stat_result: Optional[os.stat_result] = None) -> Optional[str]:
        """Short content hash of a file, or None when it does not exist."""
        path = os.fspath(full_path)
        try:
            st = stat_result or os.stat(path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(path)
        if hit and hit[:2] == key:
            return hit[2]
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        except OSError:
            return None
        with self._lock:
            self._cache[path] = (*key, digest)
        return digest


asset_hashes = _AssetHashes()


def static_file(url_path: str) -> Optional[Path]:
    """``/static/js/app.js`` -> the file under STATIC_DIR (None if outside or missing)."""
    if not url_path.startswith("/static/"):
        return None
    root = STATIC_DIR.resolve()
    candidate = (root / url_path[len("/static/"):]).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


class _GzipCache:
    """Compressed bytes of static text files, one entry per file version."""

    def __init__(self, max_entries: int = 256) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, int, bytes]] = {}
        self.max_entries = max_entries

    def get(self, full_path: str | os.PathLike, st: os.stat_result) -> Optional[bytes]:
        path = os.fspath(full_path)
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(path)
        if hit and hit[:2] == key:
            return hit[2]
        try:
            with open(path, "rb") as fh:
                data = gzip.compress(fh.read(), compresslevel=9, mtime=0)
        except OSError:
            return None
        with self._lock:
            if len(self._cache) >= self.max_entries and path not in self._cache:
                self._cache.pop(next(iter(self._cache)))
            self._cache[path] = (*key, data)
        return data


gzip_cache = _GzipCache()


# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #

class CachedStaticFiles(StaticFiles):
    """StaticFiles with content-hash caching and pre-gzipped text assets."""

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if status_code != 200:
            return response
        requested = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v") or [""])[0]
        current = asset_hashes.version(full_path, stat_result)
        response.headers["Cache-Control"] = IMMUTABLE if requested and requested == current else REVALIDATE
        if not isinstance(response, FileResponse):
            return response  # 304 Not Modified
        request_headers = Headers(scope=scope)
        media_type = response.headers.get("content-type", "")
        if not is_compressible_type(media_type) or stat_result.st_size < MINIMUM_SIZE:
            return response
        headers = MutableHeaders(raw=response.raw_headers)
        _append_vary(headers)
        if (scope.get("method") != "GET" or "range" in request_headers
                or not accepts_gzip(request_headers.get("accept-encoding", ""))):
            return response
        body = gzip_cache.get(full_path, stat_result)
        if body is None:
            return response
        out = Response(content=body, status_code=200, media_type=None)
        out.raw_headers = [(k, v) for k, v in response.raw_headers
                           if k not in (b"content-length", b"accept-ranges")]
        out_headers = MutableHeaders(raw=out.raw_headers)
        out_headers["content-encoding"] = "gzip"
        out_headers["content-length"] = str(len(body))
        return out


# --------------------------------------------------------------------------- #
# HTML pages
# --------------------------------------------------------------------------- #

_ASSET_ATTR_RE = re.compile(r"""(\b(?:src|href)=)(["'])(/static/[^"'?#]+)(?:\?[^"'#]*)?\2""")
_INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.S | re.I)


class _Page:
    __slots__ = ("key", "body", "gz", "etag", "script_hashes")

    def __init__(self, key, body: bytes, script_hashes: tuple[str, ...]) -> None:
        self.key = key
        self.body = body
        self.gz = gzip.compress(body, compresslevel=9, mtime=0)
        self.etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        self.script_hashes = script_hashes


class _Pages:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source: dict[str, tuple[tuple[int, int], str, list[str], tuple[str, ...]]] = {}
        self._pages: dict[str, _Page] = {}

    def source(self, name: str) -> Optional[tuple[tuple[int, int], str, list[str], tuple[str, ...]]]:
        """(file key, text, referenced /static URLs, inline-script hashes), re-read on change."""
        path = STATIC_DIR / name
        try:
            st = path.stat()
        except OSError:
            return None
        skey = (st.st_mtime_ns, st.st_size)
        with self._lock:
            src = self._source.get(name)
        if not src or src[0] != skey:
            text = path.read_text(encoding="utf-8")
            refs = sorted({m.group(3) for m in _ASSET_ATTR_RE.finditer(text)})
            src = (skey, text, refs, script_hashes(text))
            with self._lock:
                self._source[name] = src
        return src

    def get(self, name: str) -> Optional[_Page]:
        src = self.source(name)
        if src is None:
            return None
        skey, text, refs, hashes = src
        versions = tuple((ref, self._version(ref)) for ref in refs)
        key = (skey, versions)
        with self._lock:
            page = self._pages.get(name)
        if page is not None and page.key == key:
            return page
        page = _Page(key, rewrite_asset_urls(text, dict(versions)).encode("utf-8"), hashes)
        with self._lock:
            self._pages[name] = page
        return page

    @staticmethod
    def _version(ref: str) -> Optional[str]:
        full = static_file(ref)
        return asset_hashes.version(full) if full else None


pages = _Pages()


def rewrite_asset_urls(html: str, versions: Optional[dict[str, Optional[str]]] = None) -> str:
    """Point every local asset URL at ``?v=<content hash>`` (unknown files are left alone)."""

    def sub(m: re.Match) -> str:
        ref = m.group(3)
        if versions is not None and ref in versions:
            version = versions[ref]
        else:
            full = static_file(ref)
            version = asset_hashes.version(full) if full else None
        if not version:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{ref}?v={version}{m.group(2)}"

    return _ASSET_ATTR_RE.sub(sub, html)


def script_hashes(html: str) -> tuple[str, ...]:
    """CSP source expressions (``'sha256-...'``) for each inline <script> block."""
    out = []
    for m in _INLINE_SCRIPT_RE.finditer(html):
        digest = base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
        out.append(f"'sha256-{digest}'")
    return tuple(dict.fromkeys(out))


def inline_script_hashes() -> tuple[str, ...]:
    """Hashes of the inline scripts of every served page (for script-src-elem)."""
    found: list[str] = []
    for name in PAGES:
        src = pages.source(name)  # one stat per page per call; re-hashed only on change
        if src is not None:
            found.extend(src[3])
    return tuple(dict.fromkeys(found))


def _etag_matches(if_none_match: str, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == bare for tag in if_none_match.split(","))


def html_response(request: Request, name: str, fallback: str = "<h1>Dashboard Loading...</h1>") -> Response:
    """A page with hashed asset URLs, an ETag (304 when unchanged) and gzip."""
    page = pages.get(name)
    if page is None:
        return HTMLResponse(content=fallback, headers={"Cache-Control": "no-store"})
    headers = {"Cache-Control": HTML_CACHE_CONTROL, "ETag": page.etag, "Vary": "Accept-Encoding"}
    if _etag_matches(request.headers.get("if-none-match", ""), page.etag):
        return Response(status_code=304, headers=headers)
    if accepts_gzip(request.headers.get("accept-encoding", "")):
        headers["Content-Encoding"] = "gzip"
        return Response(content=page.gz, media_type="text/html; charset=utf-8", headers=headers)
    return Response(content=page.body, media_type="text/html; charset=utf-8", headers=headers)


# --------------------------------------------------------------------------- #
# Compression + cache policy middleware
# --------------------------------------------------------------------------- #

class DeliveryMiddleware:
    """gzip for text responses and ``no-store`` for the API. Pure ASGI.

    The ``http.response.start`` message of a compressible response is held
    only until the first body message arrives (never longer): a single-message
    body is compressed whole; a multi-message body is compressed chunk by
    chunk, each flushed at once.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = MINIMUM_SIZE, level: int = GZIP_LEVEL) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.level = level

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope.get("path", "").startswith("/api/")
        wants_gzip = scope.get("method") != "HEAD" and accepts_gzip(Headers(scope=scope).get("accept-encoding", ""))
        held: Optional[Message] = None
        compressor = None
        passthrough = False

        async def send_wrapper(message: Message) -> None:
            nonlocal held, compressor, passthrough
            kind = message["type"]
            if kind == "http.response.start":
                headers = MutableHeaders(scope=message)
                if api and "cache-control" not in headers:
                    headers["Cache-Control"] = API_CACHE_CONTROL
                if not self._eligible(message.get("status", 200), headers):
                    passthrough = True
                    await send(message)
                    return
                _append_vary(headers)
                if not wants_gzip:
                    passthrough = True
                    await send(message)
                    return
                held = message
                return
            if passthrough or held is None:
                await send(message)
                return
            if kind != "http.response.body":
                # e.g. http.response.pathsend: cannot compress, send as is.
                start, held = held, None
                passthrough = True
                await send(start)
                await send(message)
                return
            body = message.get("body", b"")
            more = message.get("more_body", False)
            if compressor is None:
                start = held
                headers = MutableHeaders(scope=start)
                if not more:
                    held = None
                    passthrough = True
                    if len(body) >= self.minimum_size:
                        body = gzip.compress(body, compresslevel=self.level, mtime=0)
                        headers["Content-Encoding"] = "gzip"
                        headers["Content-Length"] = str(len(body))
                        message = {**message, "body": body}
                    await send(start)
                    await send(message)
                    return
                del headers["content-length"]
                headers["Content-Encoding"] = "gzip"
                compressor = zlib.compressobj(self.level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
                await send(start)
            data = compressor.compress(body) + compressor.flush(zlib.Z_SYNC_FLUSH if more else zlib.Z_FINISH)
            await send({"type": "http.response.body", "body": data, "more_body": more})

        await self.app(scope, receive, send_wrapper)

    def _eligible(self, status: int, headers: MutableHeaders) -> bool:
        if status < 200 or status in (204, 206, 304):
            return False
        if "content-encoding" in headers:
            return False
        if "no-transform" in headers.get("cache-control", "").lower():
            return False
        if not is_compressible_type(headers.get("content-type", "")):
            return False
        length = headers.get("content-length")
        if length is not None:
            try:
                if int(length) < self.minimum_size:
                    return False
            except ValueError:
                return False
        return True


# --------------------------------------------------------------------------- #
# API docs: only with DEBUG=true
# --------------------------------------------------------------------------- #

# Swagger UI and ReDoc load their bundles from a CDN and run an inline
# bootstrap script, so these two pages get their own, looser policy.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: https://fastapi.tiangolo.com "
    "https://cdn.redoc.ly; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; "
    "object-src 'none'; base-uri 'self'"
)


def install_docs(app) -> None:
    """``/docs``, ``/redoc`` and ``/openapi.json``, answering 404 unless DEBUG=true.

    Create the FastAPI app with ``docs_url=None, redoc_url=None,
    openapi_url=None`` and call this instead. The schema lists every route of
    the system, so it is not published on a production box; the dashboard and
    the phone app never read it. ``DEBUG`` is read per request.
    """
    from fastapi.exceptions import HTTPException
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
    from fastapi.responses import JSONResponse

    from app.config import settings

    def _require_debug() -> None:
        if not settings.DEBUG:
            raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_schema():
        _require_debug()
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    def swagger_docs():
        _require_debug()
        page = get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - API")
        page.headers["Content-Security-Policy"] = DOCS_CSP
        return page

    @app.get("/redoc", include_in_schema=False)
    def redoc_docs():
        _require_debug()
        page = get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - API")
        page.headers["Content-Security-Policy"] = DOCS_CSP
        return page


def install(app) -> None:
    """Add the compression / cache-policy middleware."""
    app.add_middleware(DeliveryMiddleware)
