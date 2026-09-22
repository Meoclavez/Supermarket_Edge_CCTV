"""Edge AI CCTV Surveillance Core - FastAPI Application Entrypoint."""

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure edge_backend directory is in sys.path for robust relative/absolute imports
_EDGE_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _EDGE_BACKEND_DIR not in sys.path:
    sys.path.insert(0, _EDGE_BACKEND_DIR)

from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import cv2
import numpy as np

import logging

from .config import settings
from .database import init_db
from .routes import cameras, events, webrtc, system, zones, health, setup, dvr, analytics, theft, layout, dahua
from .services.live_analytics_engine import live_engine
from .services.pipeline_supervisor import pipeline_supervisor


def _check_deployment_safety() -> list[str]:
    """Refuse to start quietly with development defaults still in place.

    Each of these makes the store's system trivially accessible, and each is
    easy to leave unchanged when copying a working dev setup onto site
    hardware, so they are named explicitly at startup rather than discovered
    later.
    """
    problems: list[str] = []
    if settings.DEBUG:
        problems.append(
            "DEBUG=true disables authentication on every API route. "
            "Set DEBUG=false before putting this on a store network."
        )
    if "change_in_prod" in settings.JWT_SECRET or settings.JWT_SECRET.startswith("CHANGE_ME"):
        problems.append(
            "JWT_SECRET is still the shipped default, so anyone can mint a "
            "valid session. Generate one with: "
            'python3 -c "import secrets; print(secrets.token_urlsafe(64))"'
        )
    if settings.INTERNAL_SERVICE_KEY == "edge_ai_vision_internal_secret":
        problems.append("INTERNAL_SERVICE_KEY is still the shipped default.")
    return problems


@asynccontextmanager
async def lifespan(app: FastAPI):
    for problem in _check_deployment_safety():
        logging.getLogger("edge.security").warning(f"INSECURE CONFIGURATION: {problem}")

    await init_db()
    # Bring up the capture/detect/track pipeline. Without this the database
    # never receives an observation and every metric would have to be faked.
    await pipeline_supervisor.start()
    try:
        yield
    finally:
        await pipeline_supervisor.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Decentralized Edge AI CCTV with Studio & Multi-Cam Dashboard",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path in ("/dashboard", "/dashboard/studio", "/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Register API Routers
app.include_router(cameras.router)
app.include_router(zones.router)
app.include_router(events.router)
app.include_router(webrtc.router)
app.include_router(system.router)
app.include_router(health.router)
app.include_router(setup.router, prefix="/api/v1")
app.include_router(dvr.router)
app.include_router(analytics.router)
app.include_router(analytics.system_router)
app.include_router(theft.router)
app.include_router(layout.router)
app.include_router(dahua.router)

# Mount Static Files
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return RedirectResponse(url="/dashboard")
    return {
        "status": "online",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "health_url": "/api/v1/health",
        "docs_url": "/docs"
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_home_view():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        with open(index_file, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard Loading...</h1>")

@app.get("/dashboard/analytics", response_class=HTMLResponse)
@app.get("/analytics", response_class=HTMLResponse)
def dashboard_analytics_view():
    """The analytics view is a tab of the single dashboard page.

    ``static/analytics.html`` was a byte-for-byte copy of ``index.html`` and
    has been removed; both routes serve the one page and the client opens the
    analytics tab from the URL hash.
    """
    return dashboard_home_view()

@app.get("/dashboard/studio", response_class=HTMLResponse)
@app.get("/studio", response_class=HTMLResponse)
def dashboard_studio_view():
    studio_file = STATIC_DIR / "studio.html"
    if studio_file.exists():
        with open(studio_file, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Studio Loading...</h1>")

# Live MJPEG for the Studio canvas and the dashboard camera matrix.
def _encode_jpeg(frame, quality: int = 80):
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


def _placeholder_frame(message: str, width: int = 960, height: int = 540) -> bytes:
    """A plain slate stating why there is no picture.

    Deliberately carries no bounding boxes, confidence scores or fake
    telemetry: an operator must never be shown something that looks like a
    live analysed feed when no camera is connected.
    """
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = (18, 20, 26)
    cv2.putText(frame, "NO SIGNAL", (int(width / 2) - 110, int(height / 2) - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (90, 96, 112), 2)
    cv2.putText(frame, message[:64], (int(width / 2) - 200, int(height / 2) + 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (70, 76, 92), 1)
    return _encode_jpeg(frame) or b""


@app.get("/stream")
async def mjpeg_stream(request: Request, camera_id: str | None = None, fps: int = 0, overlay: int = 0):
    """Stream real frames captured by the live pipeline.

    This used to synthesise a bouncing "PERSON 0.94" rectangle with OpenCV and
    serve it as a live AI feed for every camera at once. It now serves the
    actual most-recent frame for the requested camera, or an explicit
    no-signal slate when that camera is not delivering video.

    ``overlay=1`` draws the tracker's current boxes and ids on each frame from
    the worker's own snapshot -- no extra inference -- so what is shown is
    exactly what is being counted. The default is the raw frame.
    """
    from .services.live_analytics_engine import render_overlay

    async def iter_frames():
        delay = 1.0 / max(1, min(fps or 25, 30))
        try:
            while True:
                cam = camera_id
                if cam is None:
                    # No camera named: show the first one that is actually online.
                    online = [c for c, rt in live_engine.runtimes.items() if rt.status == "ONLINE"]
                    cam = online[0] if online else None

                frame = live_engine.get_frame(cam) if cam else None
                if frame is None:
                    rt = live_engine.runtimes.get(cam) if cam else None
                    reason = (rt.last_error or rt.status) if rt else "no camera configured"
                    payload = _placeholder_frame(str(reason))
                    await asyncio.sleep(0.5)
                else:
                    if overlay:
                        rt = live_engine.runtimes.get(cam)
                        if rt is not None:
                            # get_frame() already returned a copy, so drawing here
                            # never touches the frame the worker is analysing.
                            frame = render_overlay(frame, rt)
                    payload = _encode_jpeg(frame)
                    # Dashboard tiles ask for a low frame rate: a wall of 32 feeds
                    # re-encoding at full rate would spend the whole CPU budget on
                    # JPEG for thumbnails nobody is inspecting frame by frame.
                    await asyncio.sleep(delay)

                if payload:
                    chunk = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n"
                        + payload + b"\r\n"
                    )
                    yield chunk
        except (asyncio.CancelledError, ConnectionResetError):
            pass

    return StreamingResponse(
        iter_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, pre-check=0, post-check=0, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "close",
        },
    )

