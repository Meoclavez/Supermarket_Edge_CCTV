"""Health and telemetry endpoint for Docker healthchecks and monitoring.

Every figure is read at request time from the thing it describes: disk usage
from the filesystem, counts from the database, tracks and camera state from
the live pipeline, and the accelerator from the person detector's own status.

The ``services`` map follows the same rule. A component that is absent
(``NOT_PRESENT``), not set up (``NOT_CONFIGURED``), switched off
(``DISABLED``), present but unused (``NOT_IN_USE``) or never observed
(``NOT_CHECKED``) is reported as exactly that, with no success time. Only a
real observation produces ``HEALTHY``, and the top-level ``status`` is
derived from the observed entries alone.
"""

import logging
import os
import shutil
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session_factory
from app.models.db_models import CameraModel, SecurityEventModel
from app.models.schemas import EventType
from app.routes import ResilientRoute
from app.services.resilience import ServiceHealthTracker

logger = logging.getLogger("Health")

router = APIRouter(
    prefix="/api/v1/health",
    tags=["Health & Telemetry"],
    route_class=ResilientRoute,
)


def _pipeline_status() -> dict:
    """Live engine and detector state; empty values if either is unavailable."""
    out = {"running": False, "cameras_online": None, "live_tracks": None, "detector": {}, "cameras": []}
    try:
        from app.services.live_analytics_engine import live_engine

        st = live_engine.status()
        out.update({
            "running": bool(st.get("running")),
            "cameras_online": st.get("cameras_online"),
            "live_tracks": st.get("live_tracks"),
            "detector": st.get("detector") or {},
            "cameras": st.get("cameras") or [],
        })
    except Exception as exc:  # health must answer even if the pipeline is broken
        logger.warning("Pipeline status unavailable for health check: %s", exc)
        out["error"] = str(exc)
    return out


def _hailo_service(detector: dict) -> dict:
    """The NPU as the detector found it; NOT_PRESENT without a device node."""
    T = ServiceHealthTracker
    if not os.path.exists(settings.HAILO_DEVICE):
        return T.entry(T.NOT_PRESENT, f"no Hailo device at {settings.HAILO_DEVICE}", consecutive_failures=None)
    hailo = detector.get("hailo") or {}
    if not hailo:
        return T.entry(T.NOT_CHECKED, "Hailo device present; the detector has not probed it yet",
                       consecutive_failures=None)
    if hailo.get("selected") and detector.get("available"):
        return T.entry(T.HEALTHY, last_success_time=time.time())
    return T.entry(T.NOT_IN_USE, hailo.get("reason") or "Hailo device present but person detection does not use it",
                   consecutive_failures=None)


def _camera_services(cameras: list, pipeline: dict) -> dict:
    """One ``rtsp_cam_<id>`` entry per configured camera, from its live worker."""
    T = ServiceHealthTracker
    runtimes = {str(r.get("camera_id")): r for r in (pipeline.get("cameras") or [])}
    now = time.time()
    out = {}
    for cam_id, cam_name in cameras:
        rt = runtimes.get(str(cam_id))
        if rt is None:
            entry = T.entry(T.NOT_CHECKED, "no live worker for this camera (analytics pipeline not running)",
                            consecutive_failures=None)
        else:
            since = rt.get("seconds_since_frame")
            last_frame = round(now - since, 3) if since is not None else None
            status = rt.get("status")
            if status == "ONLINE":
                entry = T.entry(T.HEALTHY, None, last_frame, 0)
            elif status == "DISABLED":
                entry = T.entry(T.DISABLED, None, last_frame, None)
            elif status == "OFFLINE":
                entry = T.entry(T.FAILED, rt.get("last_error") or "no video from the camera", last_frame, None)
            elif status == "AUTH_FAILED":  # camera rejected the login; retries paused
                entry = T.entry(T.FAILED, rt.get("last_error") or "camera rejected the username/password",
                                last_frame, None)
            else:  # STARTING: still connecting, nothing observed yet
                entry = T.entry(T.NOT_CHECKED, rt.get("last_error"), last_frame, None)
        entry["name"] = cam_name
        out[f"rtsp_cam_{cam_id}"] = entry
    return out


def _notification_service() -> dict:
    """Phone push: NOT_CONFIGURED without FCM credentials, else the last real send."""
    T = ServiceHealthTracker
    try:
        from app.services.alert_dispatcher import fcm_provider

        configured = fcm_provider.configured()
    except Exception as exc:
        return T.entry(T.FAILED, f"push provider unreadable: {exc}", consecutive_failures=None)
    if not configured:
        return T.entry(T.NOT_CONFIGURED, "no FCM service account stored; phones get no push alerts",
                       consecutive_failures=None)
    return T().get("notification") or T.unchecked_status("configured; no push has been sent yet")


def _overall(services: dict) -> str:
    """healthy/degraded/unhealthy from observed entries only."""
    db = services.get("database") or {}
    if db.get("status") == ServiceHealthTracker.FAILED:
        return "unhealthy"
    if any(s.get("status") in ServiceHealthTracker.FAILURE_STATES for s in services.values()):
        return "degraded"
    return "healthy"


@router.get("")
async def health_check():
    """Returns hardware, storage, pipeline and service telemetry."""
    tracker = ServiceHealthTracker()
    total, used, free = shutil.disk_usage(str(settings.STORAGE_DIR))
    used_pct = round((used / total) * 100, 1)

    cam_count = alert_count = unacknowledged = None
    cameras: list = []
    try:
        async with async_session_factory() as session:
            cameras = [tuple(r) for r in (await session.execute(select(CameraModel.id, CameraModel.name))).all()]
            cam_count = len(cameras)
            alert_count = await session.scalar(
                select(func.count(SecurityEventModel.id)).where(
                    SecurityEventModel.event_type.in_([t.value for t in EventType])
                )
            )
            unacknowledged = await session.scalar(
                select(func.count(SecurityEventModel.id)).where(
                    SecurityEventModel.event_type.in_([t.value for t in EventType]),
                    SecurityEventModel.acknowledged.is_(False),
                )
            )
        tracker.record_success("database")
    except Exception as exc:  # report the failure instead of a bare 500
        logger.error("Database check failed in health endpoint: %s", exc)
        tracker.report_status("database", ServiceHealthTracker.FAILED, str(exc))

    pipeline = _pipeline_status()
    detector = pipeline.get("detector") or {}

    log_path = settings.STORAGE_DIR / "logs" / "edge_cctv.log"
    log_size_mb = round(os.path.getsize(log_path) / (1024 * 1024), 2) if log_path.exists() else 0

    # Whatever subsystems have reported (recorders, database, go2rtc on a
    # real WebRTC negotiation), then the components read at request time.
    services = tracker.get_system_health_report()["services"]
    services.setdefault("go2rtc", ServiceHealthTracker.unchecked_status(
        "not probed; checked when a client negotiates WebRTC"))
    services["hailo"] = _hailo_service(detector)
    services["notification"] = _notification_service()
    services.update(_camera_services(cameras, pipeline))

    overall = _overall(services)
    body = {
        "status": overall,
        "version": settings.VERSION,
        "hardware": {
            "vaapi_device": settings.VAAPI_DEVICE,
            "vaapi_available": os.path.exists(settings.VAAPI_DEVICE),
            "hailo_device": settings.HAILO_DEVICE,
            "hailo_available": os.path.exists(settings.HAILO_DEVICE),
            "inference_backend": detector.get("backend"),
            "inference_provider": detector.get("provider"),
            "inference_available": detector.get("available"),
            # "compiling" while an AMD GPU is compiled for in the background;
            # inference_provider is then what serves meanwhile (the CPU).
            "inference_gpu_compile": (detector.get("gpu_compile") or {}).get("state"),
        },
        "storage": {
            "used_percent": used_pct,
            "free_gb": round(free / (1024**3), 2),
            "retention_days": settings.STORAGE_RETENTION_DAYS,
        },
        "logs": {
            "path": str(log_path),
            "size_mb": log_size_mb,
        },
        "services": services,
        "telemetry": {
            "total_cameras": cam_count,
            "cameras_online": pipeline.get("cameras_online"),
            "pipeline_running": pipeline.get("running"),
            "total_alerts": alert_count,
            "unacknowledged_alerts": unacknowledged,
            # Kept under its old key for existing clients.
            "total_events": alert_count,
            "active_person_tracks": pipeline.get("live_tracks"),
        },
    }
    # 503 only when the database itself is down, so container healthchecks
    # (curl -f) restart a broken instance but not one with an offline camera.
    return JSONResponse(body, status_code=503) if overall == "unhealthy" else body
