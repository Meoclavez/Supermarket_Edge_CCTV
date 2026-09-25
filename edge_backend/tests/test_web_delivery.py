"""Dashboard delivery: gzip, content-hash caching, ETags, CSP, API docs.

Everything runs in-process (TestClient / raw ASGI calls); no server is started.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import os
import re
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from app.config import settings
from app.main import app
from app.services import web_delivery
from app.services.remote_access_service import remote_access_service

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
GZ = {"Accept-Encoding": "gzip, deflate, br"}
HOST = "cctv.example-store.com.au"


@pytest.fixture
def client():
    return TestClient(app)


def _raw(client, url, headers=None, method="GET"):
    """(response, bytes exactly as sent on the wire)."""
    with client.stream(method, url, headers=headers or {}) as r:
        return r, b"".join(r.iter_raw())


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# HTML: hashed asset URLs, ETag / 304, gzip, no-cache
# --------------------------------------------------------------------------- #

def test_dashboard_is_gzipped_revalidated_and_hash_versioned(client):
    r, wire = _raw(client, "/dashboard", GZ)
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["cache-control"] == "no-cache"
    assert "accept-encoding" in r.headers["vary"].lower()
    html = gzip.decompress(wire).decode()
    assert len(wire) < len(html) / 3

    refs = re.findall(r'(?:src|href)="(/static/[^"?]+)\?v=([^"]+)"', html)
    assert len(refs) >= 20
    for path, version in refs:
        assert version == _sha12(STATIC / path[len("/static/"):]), path
    assert "?v=2.3.0" not in html
    assert "cdn.jsdelivr.net" not in html

    etag = r.headers["etag"]
    again = client.get("/dashboard", headers={**GZ, "If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag and again.headers["cache-control"] == "no-cache"
    # The analytics alias is the same page.
    assert client.get("/analytics", headers={"If-None-Match": etag}).status_code == 304


def test_dashboard_without_gzip_is_identity(client):
    r, wire = _raw(client, "/dashboard", {"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    assert wire.startswith(b"<!DOCTYPE html>")
    r, _ = _raw(client, "/dashboard", {"Accept-Encoding": "gzip;q=0, identity"})
    assert "content-encoding" not in r.headers


def test_studio_page_is_served_the_same_way(client):
    r = client.get("/dashboard/studio", headers=GZ)
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert re.search(r'/static/js/studio\.js\?v=[0-9a-f]{12}"', r.text)


def test_hashes_follow_file_changes(tmp_path, monkeypatch):
    (tmp_path / "js").mkdir()
    asset = tmp_path / "js" / "a.js"
    asset.write_text("console.log(1);\n" * 100)
    (tmp_path / "index.html").write_text(
        '<script>var x=1;</script><script src="/static/js/a.js?v=1.0" defer></script>'
        '<link href="/static/missing.css?v=9">')
    monkeypatch.setattr(web_delivery, "STATIC_DIR", tmp_path)
    pages = web_delivery._Pages()

    first = pages.get("index.html")
    v1 = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
    assert f'/static/js/a.js?v={v1}"' in first.body.decode()
    assert '/static/missing.css?v=9"' in first.body.decode()  # unknown files untouched
    assert pages.get("index.html") is first  # cached while nothing changed

    asset.write_text("console.log(2);\n" * 100)
    st = asset.stat()
    os.utime(asset, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = pages.get("index.html")
    v2 = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
    assert v2 != v1 and f"?v={v2}" in second.body.decode()
    assert second.etag != first.etag
    expected = base64.b64encode(hashlib.sha256(b"var x=1;").digest()).decode()
    assert second.script_hashes == (f"'sha256-{expected}'",)


# --------------------------------------------------------------------------- #
# Static files
# --------------------------------------------------------------------------- #

def test_hashed_static_is_immutable_and_gzipped(client):
    path = STATIC / "js" / "analytics.js"
    url = f"/static/js/analytics.js?v={_sha12(path)}"
    r, wire = _raw(client, url, GZ)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-encoding"] == "gzip"
    assert int(r.headers["content-length"]) == len(wire)
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.headers["content-type"].startswith("text/javascript")
    assert gzip.decompress(wire) == path.read_bytes()
    # Revalidation still works on the gzipped variant.
    assert client.get(url, headers={**GZ, "If-None-Match": r.headers["etag"]}).status_code == 304


def test_unhashed_or_stale_static_must_revalidate(client):
    for url in ("/static/js/analytics.js", "/static/js/analytics.js?v=2.3.0"):
        r = client.get(url, headers=GZ)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", url


def test_static_edge_cases_are_not_gzipped(client):
    path = STATIC / "css" / "style.css"
    url = f"/static/css/style.css?v={_sha12(path)}"
    head = client.head(url, headers=GZ)
    assert head.status_code == 200 and "content-encoding" not in head.headers
    ranged, wire = _raw(client, url, {**GZ, "Range": "bytes=0-99"})
    assert ranged.status_code == 206 and "content-encoding" not in ranged.headers and len(wire) == 100
    plain, wire = _raw(client, url, {"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and wire == path.read_bytes()
    small, wire = _raw(client, "/static/favicon.svg", GZ)  # < 1 KB
    assert "content-encoding" not in small.headers and wire == (STATIC / "favicon.svg").read_bytes()
    assert client.get("/static/../app/main.py").status_code == 404


def test_vendored_chart_js_is_local_and_verified(client):
    chart = STATIC / "vendor" / "chartjs" / "chart.umd.min.js"
    note = (STATIC / "vendor" / "chartjs" / "SOURCE.txt").read_text()
    assert hashlib.sha256(chart.read_bytes()).hexdigest() in note
    assert chart.read_bytes().startswith(b"/*!\n * Chart.js v4.4.1")
    assert "MIT" in (STATIC / "vendor" / "chartjs" / "LICENSE.md").read_text()
    r = client.get(f"/static/vendor/chartjs/chart.umd.min.js?v={_sha12(chart)}", headers=GZ)
    assert r.status_code == 200 and r.headers["cache-control"].endswith("immutable")


def test_pages_load_scripts_locally_and_deferred():
    for page in ("index.html", "studio.html"):
        html = (STATIC / page).read_text()
        srcs = re.findall(r"<script\b([^>]*)\bsrc=\"([^\"]+)\"([^>]*)>", html)
        assert srcs, page
        for before, src, after in srcs:
            assert src.startswith("/static/"), f"{page}: third-party script {src}"
            deferred = "defer" in before + after
            # auth.js must run before anything else can fetch; the rest defer.
            assert deferred == (not src.startswith("/static/js/auth.js")), f"{page}: {src}"
        names = [s for _b, s, _a in srcs]
        assert names[0].startswith("/static/js/auth.js")
        assert names[-1].startswith("/static/js/theme.js")  # same run order as before defer
    index = (STATIC / "index.html").read_text()
    assert index.index("vendor/chartjs/chart.umd.min.js") < index.index("/static/js/analytics.js")


# --------------------------------------------------------------------------- #
# Middleware: what is compressed, and never buffering a stream
# --------------------------------------------------------------------------- #

def _mini_app():
    big = {"rows": [{"n": i, "label": "visitor count by hour"} for i in range(200)]}

    async def many(request):
        return JSONResponse(big)

    async def few(request):
        return JSONResponse({"ok": True})

    async def jpeg(request):
        return Response(b"\xff\xd8" + b"\x00" * 5000, media_type="image/jpeg")

    async def encoded(request):
        return Response(gzip.compress(b"x" * 5000), media_type="text/plain", headers={"Content-Encoding": "gzip"})

    async def cached(request):
        return JSONResponse(big, headers={"Cache-Control": "private, max-age=60"})

    return Starlette(routes=[
        Route("/api/v1/many", many), Route("/api/v1/few", few), Route("/img", jpeg),
        Route("/enc", encoded), Route("/api/v1/cached", cached), Route("/page", many),
    ])


def test_middleware_compresses_text_only_and_marks_api_uncacheable():
    c = TestClient(web_delivery.DeliveryMiddleware(_mini_app()))
    r, wire = _raw(c, "/api/v1/many", GZ)
    assert r.headers["content-encoding"] == "gzip" and r.headers["cache-control"] == "no-store"
    assert int(r.headers["content-length"]) == len(wire) and gzip.decompress(wire).startswith(b'{"rows"')
    r, wire = _raw(c, "/api/v1/few", GZ)
    assert "content-encoding" not in r.headers and r.headers["cache-control"] == "no-store"
    r, _ = _raw(c, "/api/v1/cached", GZ)
    assert r.headers["cache-control"] == "private, max-age=60"  # a route's own policy is kept
    r, wire = _raw(c, "/img", GZ)
    assert "content-encoding" not in r.headers and wire.startswith(b"\xff\xd8")
    r, wire = _raw(c, "/enc", GZ)
    assert r.headers["content-encoding"] == "gzip" and gzip.decompress(wire) == b"x" * 5000  # not twice
    r, _ = _raw(c, "/page", GZ)
    assert "cache-control" not in r.headers  # non-API responses are left to their route
    r, wire = _raw(c, "/api/v1/many", {"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers and "accept-encoding" in r.headers["vary"].lower()


async def _drive(asgi, path, accept="gzip"):
    """Call an ASGI app directly and record each message as it is sent."""
    sent = []
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"accept-encoding", accept.encode())], "http_version": "1.1", "scheme": "http",
             "server": ("t", 80), "client": ("127.0.0.1", 1), "root_path": ""}

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await asgi(scope, receive, send)
    return sent


def _stream_app(media_type, chunks, gate):
    async def gen():
        for i, chunk in enumerate(chunks):
            if i:
                await gate.wait()  # the next chunk is only produced after the test looked
                gate.clear()
            yield chunk

    async def endpoint(request):
        return StreamingResponse(gen(), media_type=media_type)

    return Starlette(routes=[Route("/s", endpoint)])


def test_mjpeg_stream_passes_through_chunk_by_chunk():
    frames = [b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + bytes([i]) * 3000 + b"\r\n" for i in range(3)]

    async def run():
        gate = asyncio.Event()
        mw = web_delivery.DeliveryMiddleware(_stream_app("multipart/x-mixed-replace; boundary=frame", frames, gate))
        sent = []
        task = asyncio.create_task(_drive_into(mw, "/s", sent))
        for n in (2, 3):  # start + first frame, then each later frame, delivered as produced
            await _until(lambda: len(sent) >= n)
            gate.set()
        await asyncio.wait_for(task, 5)
        return sent

    sent = asyncio.run(run())
    start = sent[0]
    assert start["type"] == "http.response.start"
    assert b"content-encoding" not in dict(start["headers"])
    bodies = [m["body"] for m in sent[1:] if m.get("body")]
    assert bodies == frames


def test_text_stream_is_compressed_without_buffering():
    chunks = [b"line %d " % i * 200 for i in range(3)]

    async def run():
        gate = asyncio.Event()
        mw = web_delivery.DeliveryMiddleware(_stream_app("text/plain", chunks, gate))
        sent = []
        task = asyncio.create_task(_drive_into(mw, "/s", sent))
        seen = []
        for n in (2, 3):
            await _until(lambda: len(sent) >= n)
            seen.append(sent[-1]["body"])
            gate.set()
        await asyncio.wait_for(task, 5)
        return sent, seen

    sent, seen = asyncio.run(run())
    headers = dict(sent[0]["headers"])
    assert headers[b"content-encoding"] == b"gzip" and b"content-length" not in headers
    # Each chunk was flushed as soon as it arrived: the first piece alone
    # decompresses to the first chunk.
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert d.decompress(seen[0]) == chunks[0]
    wire = b"".join(m.get("body", b"") for m in sent[1:])
    assert gzip.decompress(wire) == b"".join(chunks)


async def _drive_into(asgi, path, sent):
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"accept-encoding", b"gzip")], "http_version": "1.1", "scheme": "http",
             "server": ("t", 80), "client": ("127.0.0.1", 1), "root_path": ""}

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await asgi(scope, receive, send)


async def _until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("timed out: the middleware held a chunk back")
        await asyncio.sleep(0.01)


def test_event_stream_and_websocket_untouched():
    sse = [b"data: " + b"x" * 2000 + b"\n\n", b"data: y\n\n"]

    async def run():
        gate = asyncio.Event()
        gate.set()
        return await _drive(web_delivery.DeliveryMiddleware(_stream_app("text/event-stream", sse, gate)), "/s")

    sent = asyncio.run(run())
    assert b"content-encoding" not in dict(sent[0]["headers"])
    assert [m["body"] for m in sent[1:] if m.get("body")] == sse

    calls = []

    async def ws_app(scope, receive, send):
        calls.append(scope["type"])

    asyncio.run(web_delivery.DeliveryMiddleware(ws_app)({"type": "websocket", "path": "/ws", "headers": []},
                                                        None, None))
    assert calls == ["websocket"]


def test_real_api_responses_are_uncacheable(client):
    for path in ("/api/v1/health", "/api/v1/no-such-route"):
        r = client.get(path, headers=GZ)
        assert "no-store" in r.headers["cache-control"], path


# --------------------------------------------------------------------------- #
# API docs and security headers
# --------------------------------------------------------------------------- #

def test_api_docs_only_with_debug(client, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", False)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.get("/", headers={"Accept": "application/json"}).json()["docs_url"] is None
    assert app.openapi()["paths"]  # the schema itself still builds (used by tests)

    monkeypatch.setattr(settings, "DEBUG", True)
    assert client.get("/openapi.json").json()["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200 and "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    assert client.get("/redoc").status_code == 200
    # Never through the public hostname, DEBUG or not.
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    monkeypatch.setitem(remote_access_service._settings, "hostname", HOST)
    remote = TestClient(app, base_url=f"https://{HOST}")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert remote.get(path).status_code == 403, path


def test_csp_allows_only_own_scripts_and_the_pages_inline_blocks(client):
    csp = client.get("/dashboard").headers["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
    for page in ("index.html", "studio.html"):
        for block in re.findall(r"<script>(.*?)</script>", (STATIC / page).read_text(), re.S):
            digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in directives["script-src-elem"], page
    assert "'unsafe-inline'" not in directives["script-src-elem"]
    assert not [s for s in directives["script-src-elem"] if s.startswith("http")]
    assert "'unsafe-eval'" not in csp
    assert directives["object-src"] == ["'none'"] and directives["frame-ancestors"] == ["'self'"]
    assert {"data:", "blob:"} <= set(directives["img-src"])
    assert "ws://testserver" in directives["connect-src"]


def test_csp_host_is_sanitised():
    from starlette.requests import Request

    from app.services.public_exposure import content_security_policy

    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "scheme": "http",
                   "server": ("x", 80), "client": ("1.2.3.4", 1),
                   "headers": [(b"host", b"evil.com; script-src *")]})
    csp = content_security_policy(req)
    assert "evil.com" not in csp and "script-src *" not in csp
