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


def _evidence_summary() -> Optional[dict]:
    """The evidence limit's last pass (no scan here); None if the module is unavailable."""
    try:
        from app.services.evidence_storage import evidence_storage

        st = evidence_storage.status()
    except Exception:  # noqa: BLE001
        return None
    return {k: st.get(k) for k in (
        "status", "error", "used_bytes", "files", "oldest", "cap_bytes", "cap_source",
        "effective_cap_bytes", "limited_by", "retention_days", "last_run_at", "last_cleanup_at",
        "last_deleted", "deleted_files_total", "note")}


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
        evidence_storage=_evidence_summary(),
    )


@router.get("/shadow-trial")
def get_shadow_trial():
    """The shadow pose-model trial (services/shadow_trial.py): state and the
    measured, paired comparison with the live model. Shadow only: none of it
    reaches counts or alerts. Empty tallies are null, never zero-filled."""
    from ..services.shadow_trial import shadow_trial

    return shadow_trial.status()


@router.get("/shadow-trial/samples/{filename}")
def get_shadow_trial_sample(filename: str):
    """One review image of the trial (a listed sample only; privacy masks burned in)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from ..services.shadow_trial import shadow_trial

    path = shadow_trial.sample_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="no such trial sample")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


# --------------------------------------------------------------------------
# Backups, restore and factory reset (Settings -> "Backups and reset", backups.js).
# All of it is for an owner or administrator only.
# --------------------------------------------------------------------------

import asyncio
import logging
import signal
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete as _sa_delete, func as _sa_func, select as _sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..services.auth_service import require_admin
from ..services.backup_service import (
    MAX_UPLOAD_BYTES,
    backup_kind,
    backup_service,
)

_backup_log = logging.getLogger("SystemBackups")

# Restore replaces the database under a running process that caches layout,
# cameras, site settings and sessions in memory, so it finishes with a restart.
RESTART_DELAY_S = 2.0


def restart_supported() -> bool:
    """True when a service manager brings this process back after it exits.

    systemd (``deploy/edge-cctv.service``, Restart=always): the main process has
    INVOCATION_ID and is a child of PID 1. Docker (``restart: unless-stopped``):
    /.dockerenv exists and the app is the container's PID 1 (entrypoint execs
    uvicorn). Anything else (``./run.sh`` in a terminal, tests) is not
    restarted automatically, so the dashboard says a manual restart is needed.
    """
    try:
        if os.environ.get("INVOCATION_ID") and os.getppid() == 1:
            return True
        if Path("/.dockerenv").exists() and os.getpid() == 1:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _schedule_restart() -> None:
    """SIGTERM this process shortly after the response is sent (graceful uvicorn stop)."""
    loop = asyncio.get_running_loop()
    loop.call_later(RESTART_DELAY_S, lambda: os.kill(os.getpid(), signal.SIGTERM))


def _store_time(iso_utc: Optional[str]) -> dict:
    """Backup time (ISO, UTC) -> store time (SITE_TIMEZONE / host zone) for display."""
    from datetime import datetime, timezone

    from ..services.timeutil import to_local

    if not iso_utc:
        return {"time_local": None, "time_label": None}
    try:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        local = to_local(dt)
        return {"time_local": local.isoformat(), "time_label": local.strftime("%Y-%m-%d %H:%M")}
    except Exception:  # noqa: BLE001
        return {"time_local": None, "time_label": None}


def _public_entry(b: dict) -> dict:
    out = {k: v for k, v in b.items() if k != "filepath"}
    out["kind"] = backup_kind(b.get("tag", ""))
    out.update(_store_time(b.get("timestamp")))
    return out


def backups_payload() -> dict:
    items = [_public_entry(b) for b in backup_service.list_backups()]
    return {
        "backups": items,
        "total": len(items),
        "restart_supported": restart_supported(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "contents": ("The database: store layout, cameras, settings, user accounts and recorded figures. "
                     "Evidence images and clips are not included."),
    }


class BackupNowRequest(BaseModel):
    tag: str = "manual"


@router.get("/backups", dependencies=[Depends(require_admin)])
def list_backups():
    """Every backup, newest first, with its kind and time in store time."""
    return backups_payload()


@router.post("/backup", dependencies=[Depends(require_admin)])
def backup_now(req: Optional[BackupNowRequest] = None):
    """Take a backup now (tag ``manual`` unless one is given). Never deduplicated."""
    tag = (req.tag if req and req.tag else "manual")
    try:
        res = backup_service.create_backup(tag=tag, dedupe=False)
    except Exception as e:  # noqa: BLE001
        _backup_log.error(f"Manual backup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")
    return {**_public_entry(res), "status": res.get("status", "success")}


@router.get("/backups/{filename}/download", dependencies=[Depends(require_admin)])
def download_backup(filename: str):
    """Stream one listed backup file (.db or .db.gz) as an attachment."""
    try:
        path = backup_service.resolve_backup_path(filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No such backup.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup name.")
    media = "application/gzip" if path.name.endswith(".gz") else "application/vnd.sqlite3"
    return FileResponse(path, media_type=media, filename=path.name,
                        headers={"Cache-Control": "private, no-store"})


async def _stop_pipeline_for_restore() -> None:
    """Stop camera workers and the heatmap recorder so they flush now, not into the restored file."""
    try:
        from ..services.heatmap_history import heatmap_recorder

        await heatmap_recorder.stop()
    except Exception as e:  # noqa: BLE001
        _backup_log.warning(f"restore: heatmap recorder stop failed: {e}")
    try:
        from ..services.pipeline_supervisor import pipeline_supervisor

        await pipeline_supervisor.stop()
    except Exception as e:  # noqa: BLE001
        _backup_log.warning(f"restore: pipeline stop failed: {e}")


async def restore_backup_safely(filename: str) -> dict:
    """Validate, take a safety backup, restore, then restart (when supervised)."""
    try:
        info = await asyncio.to_thread(backup_service.inspect_snapshot, filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Backup file not found: {filename}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        safety = await asyncio.to_thread(backup_service.create_backup, "pre-restore", False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Nothing was changed: the safety backup failed ({e}).")

    supervised = restart_supported()
    if supervised:
        await _stop_pipeline_for_restore()
    try:
        await asyncio.to_thread(backup_service.restore_backup, filename)
    except Exception as e:  # noqa: BLE001
        _backup_log.error(f"Restore of {filename} failed: {e}")
        if supervised:
            _schedule_restart()  # bring the stopped cameras back on the unchanged database
        code = 400 if isinstance(e, ValueError) else 500
        raise HTTPException(status_code=code, detail=f"Restore failed: {e}")

    _backup_log.warning(f"Database restored from {filename}; safety backup {safety.get('filename')}")
    if supervised:
        _schedule_restart()
        message = ("Restored. The service is restarting now to load it; this page reconnects when it is back. "
                   "You may need to sign in again.")
    else:
        message = ("Restored. Restart the server to finish: until then some screens still show the "
                   "old settings. You may need to sign in again.")
    return {
        "status": "success",
        "filename": filename,
        "message": message,
        "safety_backup": safety.get("filename"),
        "restart": "scheduled" if supervised else "manual",
        "schema_version": info.get("schema_version"),
    }


@router.post("/backups/{filename}/restore", dependencies=[Depends(require_admin)])
async def restore_backup(filename: str):
    """Restore a listed backup. A safety backup of the current database is taken first."""
    return await restore_backup_safely(filename)


@router.post("/restore/{filename}", dependencies=[Depends(require_admin)])
async def restore_backup_legacy(filename: str):
    """Older path for the same safe restore."""
    return await restore_backup_safely(filename)


@router.post("/backups/upload", dependencies=[Depends(require_admin)])
async def upload_backup(request: Request, filename: str = ""):
    """Add a downloaded backup (raw request body: .db or .db.gz) to the list.

    It is checked (SQLite, integrity, this system's tables, database version not
    newer than this release) and saved as an ``uploaded`` backup. Restoring it
    is a separate, confirmed step.
    """
    import shutil

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than the 2 GB limit.")
    backup_service.backups_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(backup_service.backups_dir)).free
    need = int(declared) * 3 if declared and declared.isdigit() else 0
    if need and free < need:
        raise HTTPException(status_code=507, detail="Not enough free disk space to check this backup.")
    tmp = backup_service.backups_dir / f".upload_{uuid.uuid4().hex}.tmp"
    size = 0
    try:
        with open(tmp, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="The file is larger than the 2 GB limit.")
                fh.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The upload was empty.")
        try:
            entry = await asyncio.to_thread(backup_service.import_upload, tmp, filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp.unlink(missing_ok=True)
    return {"status": "success", **_public_entry(entry)}


class FactoryResetRequest(BaseModel):
    confirm: str = ""


# What a factory reset deletes on top of POST /api/v1/layout/reset: recorded
# data in tables that reset leaves alone.
def _extra_reset_tables():
    from ..models.db_models import (
        AnalysisRunModel,
        HeatmapSnapshotModel,
        QueueVisitModel,
        TripwireEventModel,
    )

    return [
        ("tripwire_events", TripwireEventModel),
        ("queue_visits", QueueVisitModel),
        ("heatmap_snapshots", HeatmapSnapshotModel),
        ("analysis_runs", AnalysisRunModel),
    ]


FACTORY_RESET_KEEPS = [
    "user accounts and passwords",
    "site settings",
    "saved recorder passwords",
    "paired phones",
    "online access",
    "device settings (.env)",
    "saved evidence images and clips (removed by the storage limit as usual)",
    "all backups",
]


@router.post("/factory-reset", dependencies=[Depends(require_admin)])
async def factory_reset(req: FactoryResetRequest, db: AsyncSession = Depends(get_db)):
    """Back up, then delete the layout, cameras and every recorded figure.

    Needs ``{"confirm": "RESET"}``. Aborts without changing anything if the
    backup fails.
    """
    if (req.confirm or "") != "RESET":
        raise HTTPException(status_code=400, detail='Type RESET to confirm the factory reset.')
    try:
        backup = await asyncio.to_thread(backup_service.create_backup, "pre-reset", False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Nothing was deleted: the backup failed ({e}).")

    from .layout import _reset_store

    removed = await _reset_store(db)
    for label, model in _extra_reset_tables():
        count = await db.scalar(_sa_select(_sa_func.count()).select_from(model))
        if count:
            await db.execute(_sa_delete(model))
        removed[label] = int(count or 0)
    await db.commit()
    _backup_log.warning(f"Factory reset done (backup {backup.get('filename')}): {removed}")
    return {
        "reset": True,
        "backup": backup.get("filename"),
        "removed": removed,
        "total_rows": sum(removed.values()),
        "kept": FACTORY_RESET_KEEPS,
    }
