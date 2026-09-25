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
from fastapi import Depends, FastAPI, Response, Request
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import cv2

import logging

# Redact tokens/passwords from every log record (uvicorn.access included)
# before anything else can log.
from .services.log_redaction import install_log_redaction

install_log_redaction()

from .config import settings
from .database import init_db
from .routes import cameras, events, webrtc, system, zones, health, setup, dvr, analytics, theft, layout, dahua
from .services.auth_service import auth_service
from .services.live_analytics_engine import live_engine
from .services.no_signal_slate import render_no_signal
from .services.pipeline_supervisor import pipeline_supervisor


def _check_deployment_safety() -> list[str]:
    """Refuse to start quietly with development defaults still in place.

    Each of these makes the store's system trivially accessible, and each is
    easy to leave unchanged when copying a working dev setup onto site
    hardware, so they are named explicitly at startup rather than discovered
    later.
    """
    problems: list[str] = []
    # DEBUG only raises log verbosity; it no longer touches authentication.
    if settings.AUTH_DISABLED:
        problems.append(
            "\n" + "!" * 72 + "\n"
            "  AUTH_DISABLED=true: AUTHENTICATION IS OFF ON EVERY API ROUTE.\n"
            "  Anyone who can reach this port can view cameras and change settings.\n"
            "  Use only on an isolated development machine; unset it for a store.\n"
            + "!" * 72
        )
    # Secrets never have a shipped default any more: placeholders are treated
    # as unset and replaced from the machine-local store (secret_store.py).
    # What can still go wrong is a weak value supplied through env/.env, or a
    # store that could not be written (secrets then change on every restart).
    from .services.secret_store import secret_sources

    for name, source in sorted(secret_sources().items()):
        if source == "ephemeral":
            problems.append(
                f"{name} could not be saved under STORAGE_DIR/secrets, so it changes on every "
                "restart (sessions drop, stored NVR passwords become unreadable). "
                "Make STORAGE_DIR writable."
            )
        elif source == "env" and len(getattr(settings, name, "") or "") < 32:
            problems.append(
                f"{name} from the environment/.env is shorter than 32 characters. "
                "Remove it to use the generated per-machine secret, or generate one with: "
                'python3 -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
    return problems


async def _announce_setup_code() -> None:
    """When no operator account exists, issue and log the one-time setup code.

    Never fatal: the code is also issued lazily on the first /auth/status call.
    """
    try:
        from .services.setup_service import ensure_setup_code_if_needed

        await ensure_setup_code_if_needed(reason="server start", announce_existing=True)
    except Exception as exc:
        logging.getLogger("edge.setup").error(f"Could not issue the first-run setup code: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    edge_root = logging.getLogger("edge")
    if not edge_root.handlers and not logging.getLogger().handlers:
        # Nothing configures logging under uvicorn, so INFO lines would be dropped.
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
        edge_root.addHandler(_handler)
        edge_root.setLevel(logging.INFO)

    for problem in _check_deployment_safety():
        logging.getLogger("edge.security").warning(f"INSECURE CONFIGURATION: {problem}")

    # Startup order: preflight -> init_db -> initialise_inference -> pipeline.
    # 1. Preflight is read-only and never blocks startup: it logs each problem
    #    with the command that fixes it (./run.sh does the fixing).
    startup_log = logging.getLogger("edge.startup")
    from .services import preflight

    try:
        preflight_report = await asyncio.to_thread(preflight.run_preflight)
        preflight.log_report(preflight_report, startup_log)
    except Exception as exc:  # a broken check must not take the service down
        preflight_report = None
        startup_log.exception(f"Preflight crashed: {exc}")

    await init_db()
    # Stable device id (generated once, never changes) + store name: phones and
    # the remote-access check use it to be sure they reach THIS box.
    try:
        from .services.device_identity import ensure_identity

        _ident = await asyncio.to_thread(ensure_identity)
        startup_log.info(f"Device identity: {_ident['device_id']} ({_ident['device_name']})")
    except Exception as exc:
        startup_log.error(f"Could not initialise the device identity: {exc}")
    await _announce_setup_code()  # first run only: prints the one-time setup code

    # 2. Load and warm up the models explicitly, so the provider actually in
    #    use is known (and logged) before any camera worker starts.
    try:
        from .services.inference_backend import initialise_inference
    except ImportError:
        initialise_inference = None
        startup_log.warning("inference_backend.initialise_inference not available; models load lazily")
    if initialise_inference is not None:
        try:
            status = await asyncio.to_thread(initialise_inference)
            if status.get("available"):
                startup_log.info(
                    f"Inference: provider={status.get('execution_provider') or status.get('provider')} "
                    f"model={status.get('model')} warmup_ms={status.get('warmup_steady_ms') or status.get('warmup_ms')}"
                )
            else:
                startup_log.error(f"Inference UNAVAILABLE: {status.get('error')} (no detections will be produced)")
            if preflight_report is not None:
                before = len(preflight_report["warnings"])
                preflight.add_live_status(preflight_report, status)
                for issue in preflight_report["warnings"][before:]:
                    startup_log.warning(f"PREFLIGHT WARNING [{issue['check']}] {issue['message']} | fix: {issue['fix']}")
        except Exception as exc:
            startup_log.exception(f"Inference initialisation failed: {exc}")

    # 3. Bring up the capture/detect/track pipeline. Without this the database
    # never receives an observation and every metric would have to be faked.
    await pipeline_supervisor.start()

    # 4. Bind pose analytics to this loop so loss-prevention alerts go out as
    # soon as an incident is detected, not after the first theft API request.
    from .services.pose_analytics import pose_analytics
    await pose_analytics.start()

    # 4b. Recorded heatmap history: hourly snapshots, daily roll-ups, backfill.
    from .services.heatmap_history import heatmap_recorder
    try:
        await heatmap_recorder.start()
    except Exception as exc:  # heatmap history must never block the store system
        startup_log.exception(f"Heatmap recorder failed to start: {exc}")

    # 4c. Scheduled business analysis (rules only), so the dashboard's
    # recommendations list is filled by a plain GET (recommendations_service).
    from .services.recommendations_service import recommendations_service
    try:
        await recommendations_service.start()
    except Exception as exc:
        startup_log.exception(f"Analysis scheduler failed to start: {exc}")

    # 4d. Evidence storage limit: stills/clips on this device's disk, oldest
    # deleted first above EVIDENCE_MAX_GB / STORAGE_RETENTION_DAYS. Refuses
    # (and says so in health) if STORAGE_DIR is on a network share.
    from .services.evidence_storage import evidence_storage
    try:
        await evidence_storage.start()
    except Exception as exc:
        startup_log.exception(f"Evidence storage limit failed to start: {exc}")

    # 5. Remote access (online dashboard): runs cloudflared only when enabled,
    # a hostname and a token are configured, and authentication is on.
    from .services.remote_access_service import remote_access_service
    try:
        await remote_access_service.start()
    except Exception as exc:  # never block the store system on the tunnel
        startup_log.exception(f"Remote access failed to start: {exc}")
    # Learn about SIGTERM/SIGINT when it arrives, not after uvicorn has drained
    # connections: open /stream responses poll this flag and end, otherwise
    # the drain waits for them forever (see services/shutdown_signal.py).
    from .services import shutdown_signal
    shutdown_signal.install()
    try:
        yield
    finally:
        shutdown_signal.request_shutdown()
        # Before the pipeline: its final partial-hour record reads the live engine.
        await heatmap_recorder.stop()
        await pipeline_supervisor.stop()
        await asyncio.to_thread(pose_analytics.stop)
        await remote_access_service.stop()
        await recommendations_service.stop()
        await evidence_storage.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Decentralized Edge AI CCTV with Studio & Multi-Cam Dashboard",
    lifespan=lifespan,
    # /docs, /redoc and /openapi.json exist only with DEBUG=true (web_delivery.install_docs).
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS for this device's own origins only, security headers, and refusal of
# first-run setup / API docs through the public hostname (public_exposure.py).
from .services import public_exposure
from .services import web_delivery

public_exposure.install(app)
# gzip for text responses, Cache-Control: no-store on the API; pages and
# static files set their own caching (content-hash URLs, ETags).
web_delivery.install(app)
web_delivery.install_docs(app)


# Register API Routers
app.include_router(cameras.router)
app.include_router(zones.router)
app.include_router(events.router)
app.include_router(events.ws_router)
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
from .routes import pairing as pairing_routes  # noqa: E402

app.include_router(pairing_routes.device_router)
app.include_router(pairing_routes.pairing_router)
app.include_router(pairing_routes.push_router)
from .routes import remote_access as remote_access_routes  # noqa: E402

app.include_router(remote_access_routes.router)
from .routes import camera_roles as camera_roles_routes  # noqa: E402

app.include_router(camera_roles_routes.router)
from .routes import insights as insights_routes  # noqa: E402

app.include_router(insights_routes.router)
from .routes import night_watch as night_watch_routes  # noqa: E402

app.include_router(night_watch_routes.router)

# Mount Static Files
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Content-hash URLs are cached for a year; anything else revalidates (web_delivery.py).
app.mount("/static", web_delivery.CachedStaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request /favicon.ico regardless of <link rel="icon">."""
    from fastapi.responses import FileResponse

    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})

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
        "docs_url": "/docs" if settings.DEBUG else None,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_home_view(request: Request):
    """index.html with content-hashed asset URLs, an ETag and gzip (web_delivery.py)."""
    return web_delivery.html_response(request, "index.html", "<h1>Dashboard Loading...</h1>")

@app.get("/dashboard/analytics", response_class=HTMLResponse)
@app.get("/analytics", response_class=HTMLResponse)
def dashboard_analytics_view(request: Request):
    """The analytics view is a tab of the single dashboard page.

    ``static/analytics.html`` was a byte-for-byte copy of ``index.html`` and
    has been removed; both routes serve the one page and the client opens the
    analytics tab from the URL hash.
    """
    return dashboard_home_view(request)

@app.get("/dashboard/studio", response_class=HTMLResponse)
@app.get("/studio", response_class=HTMLResponse)
def dashboard_studio_view(request: Request):
    return web_delivery.html_response(request, "studio.html", "<h1>Studio Loading...</h1>")

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
    return _encode_jpeg(render_no_signal(message, width, height)) or b""


@app.get("/stream")
async def mjpeg_stream(request: Request, camera_id: str | None = None, fps: int = 0, overlay: int = 0,
                       max_width: int = 0, _auth: bool = Depends(auth_service.verify_api_access)):
    """Stream real frames captured by the live pipeline.

    This used to synthesise a bouncing "PERSON 0.94" rectangle with OpenCV and
    serve it as a live AI feed for every camera at once. It now serves the
    actual most-recent frame for the requested camera, or an explicit
    no-signal slate when that camera is not delivering video.

    ``overlay=1`` draws the tracker's current boxes and ids on each frame from
    the worker's own snapshot -- no extra inference -- so what is shown is
    exactly what is being counted. The default is the raw frame.

    ``max_width`` (> 0) scales frames down to at most that width, as for
    ``/api/v1/cameras/{id}/snapshot``: the enlarged dashboard tile asks for
    about its on-screen size instead of a 3072x2048 frame per picture.
    """
    from .services.live_analytics_engine import fit_width, render_overlay
    from .services.shutdown_signal import shutdown_requested

    async def iter_frames():
        delay = 1.0 / max(1, min(fps or 25, 30))
        # The camera delivers at most DECODE_MAX_FPS new frames; a client
        # asking for more gets the same JPEG again instead of a re-encode.
        last_key, last_payload = None, None
        try:
            # Ends when the server is asked to stop; an endless response would
            # otherwise hold uvicorn's graceful shutdown open indefinitely.
            while not shutdown_requested.is_set():
                cam = camera_id
                if cam is None:
                    # No camera named: show the first one that is actually online.
                    online = [c for c, rt in live_engine.runtimes.items() if rt.status == "ONLINE"]
                    cam = online[0] if online else None

                rt_now = live_engine.runtimes.get(cam) if cam else None
                key = None
                if rt_now is not None and rt_now.frames_read:
                    key = (cam, rt_now.frames_read, rt_now._tracks_at if overlay else None)
                if key is not None and key == last_key and last_payload:
                    frame, payload = None, last_payload
                else:
                    frame = live_engine.get_frame(cam) if cam else None
                    payload = None
                if payload is not None:
                    await asyncio.sleep(delay)
                elif frame is None:
                    rt = live_engine.runtimes.get(cam) if cam else None
                    if rt is not None and not rt.enabled:
                        reason = "camera is turned off"
                    else:
                        reason = (rt.last_error or rt.status) if rt else "no camera configured"
                    payload = _placeholder_frame(str(reason))
                    await asyncio.sleep(0.5)
                else:
                    frame, scale = fit_width(frame, max_width)
                    if overlay:
                        rt = live_engine.runtimes.get(cam)
                        if rt is not None:
                            # get_frame() already returned a copy, so drawing here
                            # never touches the frame the worker is analysing.
                            frame = render_overlay(frame, rt, scale=scale)
                    payload = _encode_jpeg(frame)
                    last_key, last_payload = key, payload
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

