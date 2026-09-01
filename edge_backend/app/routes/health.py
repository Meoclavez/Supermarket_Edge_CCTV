"""Health and telemetry endpoint for Docker healthchecks and monitoring."""

import os
import shutil
from fastapi import APIRouter
from sqlalchemy import select, func
from app.config import settings
from app.database import async_session_factory
from app.models.db_models import CameraModel, SecurityEventModel
from app.services.hailo_inference_service import hailo_inference_service
from app.routes import ResilientRoute

router = APIRouter(
    prefix="/api/v1/health", 
    tags=["Health & Telemetry"],
    route_class=ResilientRoute
)


from app.services.resilience import ServiceHealthTracker

@router.get("")
async def health_check():
    """Returns detailed hardware, storage, and service telemetry."""
    # Check disk usage
    total, used, free = shutil.disk_usage(str(settings.STORAGE_DIR))
    used_pct = round((used / total) * 100, 1)

    # Check hardware nodes
    vaapi_ok = os.path.exists(settings.VAAPI_DEVICE)
    hailo_ok = os.path.exists(settings.HAILO_DEVICE)

    # Query counts from DB
    async with async_session_factory() as session:
        cam_count = await session.scalar(select(func.count(CameraModel.id)))
        event_count = await session.scalar(select(func.count(SecurityEventModel.id)))

    active_tracks = len(hailo_inference_service.kinematic_engine.tracks)

    # Log file info
    log_path = "storage/logs/edge_cctv.log"
    log_size_mb = 0
    if os.path.exists(log_path):
        log_size_mb = round(os.path.getsize(log_path) / (1024 * 1024), 2)

    # Service Health Tracker Report
    health_tracker = ServiceHealthTracker()
    service_report = health_tracker.get_system_health_report()

    return {
        "status": "healthy",
        "version": settings.VERSION,
        "hardware": {
            "vaapi_device": settings.VAAPI_DEVICE,
            "vaapi_available": vaapi_ok,
            "hailo_device": settings.HAILO_DEVICE,
            "hailo_available": hailo_ok or hailo_inference_service.device_available,
        },
        "storage": {
            "used_percent": used_pct,
            "free_gb": round(free / (1024**3), 2),
            "retention_days": settings.STORAGE_RETENTION_DAYS,
        },
        "logs": {
            "path": log_path,
            "size_mb": log_size_mb,
        },
        "services": service_report["services"],
        "telemetry": {
            "total_cameras": cam_count,
            "total_events": event_count,
            "active_person_tracks": active_tracks,
        }
    }
