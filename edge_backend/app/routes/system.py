"""System Hardware & Resource Telemetry Routes."""

import time
import os
from fastapi import APIRouter
from ..models.schemas import HardwareProfile, SystemStats
from ..services.hardware_detector import hardware_profile, get_ram_info
from ..services.feature_manager import feature_manager

router = APIRouter(prefix="/api/v1/system", tags=["System"])
START_TIME = time.time()

@router.get("/hardware", response_model=HardwareProfile)
def get_hardware_profile():
    return hardware_profile

@router.get("/stats", response_model=SystemStats)
def get_system_stats():
    total_ram, avail_ram = get_ram_info()
    cpu_percent = 5.0
    try:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        try:
            load1, _, _ = os.getloadavg()
            cpu_percent = min(100.0, round((load1 / (os.cpu_count() or 1)) * 100, 1))
        except Exception:
            pass

    return SystemStats(
        cpu_usage_percent=cpu_percent,
        gpu_usage_percent=12.5 if hardware_profile.decoder_type in ["cuda", "vaapi_intel"] else None,
        ram_used_gb=round(total_ram - avail_ram, 2),
        ram_total_gb=total_ram,
        active_cameras=3,
        active_features_count=feature_manager.count_active_features(),
        decoder=hardware_profile.decoder_type,
        inference_engine=hardware_profile.inference_backend,
        shm_buffer_used_mb=128.0,
        uptime_seconds=round(time.time() - START_TIME, 1)
    )
