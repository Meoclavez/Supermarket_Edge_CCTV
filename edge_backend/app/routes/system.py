"""System hardware & resource telemetry routes.

Every value is measured at request time or reported as null. There are no
constants standing in for an unmeasured figure.
"""

import os
import time
from typing import Optional

from fastapi import APIRouter, Depends

from ..models.schemas import HardwareProfile, SystemStats
from ..services.auth_service import auth_service
from ..services.feature_manager import feature_manager
from ..services.hardware_detector import (
    current_hardware_profile,
    get_ram_info,
    probe_nvidia_gpu_utilisation,
)
from ..services.live_analytics_engine import live_engine

router = APIRouter(
    prefix="/api/v1/system",
    tags=["System"],
    dependencies=[Depends(auth_service.verify_api_access)],
)
START_TIME = time.time()


def _cpu_percent() -> Optional[float]:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        pass
    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count()
        if cores:
            return min(100.0, round((load1 / cores) * 100, 1))
    except Exception:
        pass
    return None


@router.get("/hardware", response_model=HardwareProfile)
def get_hardware_profile():
    """Decoder capability (probed) and the inference backend the detector actually runs."""
    return current_hardware_profile()


@router.get("/stats", response_model=SystemStats)
def get_system_stats():
    total_ram, avail_ram = get_ram_info()
    profile = current_hardware_profile()
    pipeline = live_engine.status()

    ram_used = (
        round(total_ram - avail_ram, 2)
        if total_ram is not None and avail_ram is not None
        else None
    )

    return SystemStats(
        cpu_usage_percent=_cpu_percent(),
        gpu_usage_percent=probe_nvidia_gpu_utilisation(),
        ram_used_gb=ram_used,
        ram_total_gb=total_ram,
        active_cameras=int(pipeline.get("cameras_online", 0)),
        cameras_total=int(pipeline.get("cameras_total", 0)),
        active_features_count=feature_manager.count_active_features(),
        decoder=profile.decoder_capability,
        inference_engine=profile.inference_backend,
        inference_provider=profile.inference_provider,
        shm_buffer_used_mb=None,
        uptime_seconds=round(time.time() - START_TIME, 1),
    )
