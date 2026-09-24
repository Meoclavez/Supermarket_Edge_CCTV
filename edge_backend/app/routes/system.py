"""System hardware & resource telemetry routes.

Every value is measured at request time or reported as null. There are no
constants standing in for an unmeasured figure.
"""

import os
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends

from ..models.schemas import HardwareProfile, SystemStats
from ..services.auth_service import auth_service
from ..services.feature_manager import feature_manager
from ..services.hardware_detector import (
    current_hardware_profile,
    get_ram_info,
    probe_gpu_utilisation,
)
from ..services.live_analytics_engine import live_engine
from ..services import preflight

router = APIRouter(
    prefix="/api/v1/system",
    tags=["System"],
    dependencies=[Depends(auth_service.verify_api_access)],
)
START_TIME = time.time()


_cpu_lock = threading.Lock()
_cpu_prev: Optional[tuple[float, float, float]] = None   # (monotonic, busy s, total s)
_cpu_value: Optional[float] = None
CPU_MIN_WINDOW_S = 1.0     # readings closer together than this reuse the last value
CPU_STALE_S = 60.0         # older baseline: measure a fresh short window instead


def _cpu_busy_total(psutil) -> tuple[float, float]:
    t = psutil.cpu_times()
    # Linux counts guest time inside user time as well; drop it like psutil does.
    total = sum(t) - getattr(t, "guest", 0.0) - getattr(t, "guest_nice", 0.0)
    return total - t.idle - getattr(t, "iowait", 0.0), total


def _cpu_percent() -> Optional[float]:
    """Machine-wide CPU busy % since the previous reading.

    Not psutil.cpu_percent(interval=None): psutil keeps that baseline per
    calling thread, and sync routes run on a pool of threads, so each thread's
    first call returned 0.0, which the dashboard showed as a real reading.
    """
    global _cpu_prev, _cpu_value
    try:
        import psutil

        with _cpu_lock:
            now = time.monotonic()
            if _cpu_prev is not None and _cpu_value is not None and now - _cpu_prev[0] < CPU_MIN_WINDOW_S:
                return _cpu_value
            if _cpu_prev is None or now - _cpu_prev[0] > CPU_STALE_S:
                _cpu_prev = (now, *_cpu_busy_total(psutil))
                time.sleep(0.25)
            busy, total = _cpu_busy_total(psutil)
            _, busy0, total0 = _cpu_prev
            if total > total0:
                _cpu_value = round(min(100.0, max(0.0, 100.0 * (busy - busy0) / (total - total0))), 1)
                _cpu_prev = (time.monotonic(), busy, total)
            if _cpu_value is not None:
                return _cpu_value
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


@router.get("/hardware")
def get_hardware_profile():
    """Decoder capability (probed), the inference backend the detector actually
    runs, and the summary of the most recent startup preflight."""
    profile = HardwareProfile.model_validate(current_hardware_profile()).model_dump()
    profile["preflight"] = preflight.summary(preflight.last_result())
    return profile


@router.get("/preflight")
def get_preflight(probe: bool = False):
    """Re-run the read-only installation checks now.

    ``probe=true`` additionally creates a real ONNX Runtime session in a child
    process and reports which provider it landed on (takes a second or two).
    The loaded detector's own provider is always folded in, so a GPU machine
    that ended up on the CPU is reported with the command that fixes it.
    """
    result = preflight.run_preflight(session_probe=probe)
    try:
        from ..services.inference_backend import person_detector

        preflight.add_live_status(result, person_detector.status())
    except Exception:  # detector module unavailable: the static checks still stand
        pass
    return result


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
        gpu_usage_percent=probe_gpu_utilisation(),
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
