"""Health and telemetry endpoint for Docker healthchecks and monitoring.

Every figure is read at request time from the thing it describes: disk usage
from the filesystem, counts from the database, tracks and camera state from
the live pipeline, and the accelerator from the person detector's own status.
"""

import logging
import os
import shutil

from fastapi import APIRouter
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
    out = {"running": False, "cameras_online": None, "live_tracks": None, "detector": {}}
    try:
        from app.services.live_analytics_engine import live_engine

        st = live_engine.status()
        out.update({
            "running": bool(st.get("running")),
            "cameras_online": st.get("cameras_online"),
            "live_tracks": st.get("live_tracks"),
            "detector": st.get("detector") or {},
        })
    except Exception as exc:  # health must answer even if the pipeline is broken
        logger.warning("Pipeline status unavailable for health check: %s", exc)
        out["error"] = str(exc)
    return out


@router.get("")
async def health_check():
    """Returns hardware, storage, pipeline and service telemetry."""
    total, used, free = shutil.disk_usage(str(settings.STORAGE_DIR))
    used_pct = round((used / total) * 100, 1)

    async with async_session_factory() as session:
        cam_count = await session.scalar(select(func.count(CameraModel.id)))
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

    pipeline = _pipeline_status()
    detector = pipeline.get("detector") or {}

    log_path = settings.STORAGE_DIR / "logs" / "edge_cctv.log"
    log_size_mb = round(os.path.getsize(log_path) / (1024 * 1024), 2) if log_path.exists() else 0

    service_report = ServiceHealthTracker().get_system_health_report()

    return {
        "status": "healthy",
        "version": settings.VERSION,
        "hardware": {
            "vaapi_device": settings.VAAPI_DEVICE,
            "vaapi_available": os.path.exists(settings.VAAPI_DEVICE),
            "hailo_device": settings.HAILO_DEVICE,
            "hailo_available": os.path.exists(settings.HAILO_DEVICE),
            "inference_backend": detector.get("backend"),
            "inference_provider": detector.get("provider"),
            "inference_available": detector.get("available"),
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
        "services": service_report["services"],
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
