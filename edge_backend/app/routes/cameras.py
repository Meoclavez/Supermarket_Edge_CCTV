"""Camera management, discovery aliases, snapshots, feature toggles and per-camera helpers.

Every camera served here comes from the database. There is no built-in
fleet, no "current source" placeholder and no fallback list: an empty table
means no cameras, and that is what the API reports.
"""

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

import cv2
from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..models.db_models import CameraModel
from ..models.schemas import (
    CameraFeatureConfig,
    CameraFeed,
    CameraListResponse,
    CameraPositionUpdate,
    MuteCameraRequest,
)
from ..services.auth_service import auth_service
from ..services import camera_roles, camera_source
from ..services.camera_discovery import camera_discovery_service
from ..services.clip_recorder import clip_recorder_service
from ..services.duplicate_cameras import delete_impact, duplicate_guard
from ..services.feature_manager import feature_manager
from ..services.inference_backend import person_detector
from ..services.live_analytics_engine import _draw_skeleton, draw_ignore_areas, fit_width, live_engine, render_overlay
from ..services.no_signal_slate import render_no_signal
from ..services.privacy_mask import apply_privacy_masks, ignore_polygons, outside_ignore_regions

logger = logging.getLogger("Cameras")

router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["Cameras"],
    dependencies=[Depends(auth_service.verify_api_access)],
)

# Columns a client may set through the CameraFeed schema. ``id`` is handled
# separately so that a PUT can never re-key a row.
_WRITABLE_COLUMNS = set(CameraModel.__table__.columns.keys()) - {"id", "created_at", "updated_at"}


def _model_to_feed(c: CameraModel) -> CameraFeed:
    return CameraFeed(
        id=c.id,
        name=c.name,
        location=c.location,
        channel_number=c.channel_number if c.channel_number is not None else 1,
        department=c.department or "GENERAL",
        # Never echo a password: stored URLs are credential-free for cameras
        # added by URL, but adopted/NVR rows may still carry user:pw@.
        rtsp_url=camera_source.mask_url(c.rtsp_url),
        webrtc_url=c.webrtc_url or "",
        status=c.status,
        fps=c.fps,
        resolution=c.resolution,
        is_ai_enabled=c.is_ai_enabled,
        ai_models=list(c.ai_models or []),
        features=feature_manager.get_camera_features(c.id, stored=c.features),
        dvr_enabled=c.dvr_enabled,
        dvr_retention_days=c.dvr_retention_days,
        dvr_quota_gb=c.dvr_quota_gb,
        floor_x=c.floor_x,
        floor_y=c.floor_y,
        floor_z=c.floor_z,
        azimuth_deg=c.azimuth_deg,
        fov_deg=c.fov_deg,
        homography_matrix=c.homography_matrix,
        last_seen=c.last_seen,
        role=c.role if c.role in camera_roles.ROLE_PRESETS else None,
        pos_register_id=c.pos_register_id,
        duplicate_group=(dup := duplicate_guard.camera_fields(c.id))["duplicate_group"],
        duplicate_of=dup["duplicate_of"],
    )


def _writable_payload(cam_in: CameraFeed) -> Dict[str, object]:
    """Column values from a CameraFeed body.

    Fields the client left as ``None`` are dropped rather than written, so an
    unplaced camera keeps whatever the column default is instead of receiving
    an invented coordinate.
    """
    data = cam_in.model_dump()
    out: Dict[str, object] = {}
    for key, value in data.items():
        if key not in _WRITABLE_COLUMNS or value is None:
            continue
        out[key] = value.value if hasattr(value, "value") else value
    if "dvr_enabled" in out:
        # No continuous recording on this device (the store NAS records);
        # the legacy flag is stored off whatever a client sends.
        out["dvr_enabled"] = False
    if "features" in out:
        # Normalise to the current flag set; keys from older builds
        # (fall_detection, door_monitoring, ...) are dropped here.
        out["features"] = CameraFeatureConfig.model_validate(out["features"] or {}).model_dump()
    return out


def _live_feed(c: CameraModel) -> CameraFeed:
    """The camera as stored, with status and fps from the running pipeline.

    The stored ``status`` column is only the last persisted value (a new
    camera's "STARTING"); whether it delivers frames right now is known by its
    worker. Used by both the list and the single-camera GET, so they agree.
    """
    feed = _model_to_feed(c)
    rt = live_engine.runtimes.get(c.id)
    if not c.is_ai_enabled:
        # Turned off by the operator: OFF, never "offline", even before
        # the pipeline has picked the switch up.
        feed.status = "DISABLED"
        feed.fps = 0
    elif rt is not None:
        feed.status = rt.status
        feed.fps = int(round(rt.fps)) if rt.fps else 0
    else:
        feed.status = "OFFLINE"
        feed.fps = 0
    return feed


async def _get_camera_or_404(camera_id: str, db: AsyncSession) -> CameraModel:
    res = await db.execute(select(CameraModel).where(CameraModel.id == camera_id))
    cam = res.scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
    return cam


@router.get("/departments/list")
async def list_camera_departments(db: AsyncSession = Depends(get_db)):
    """Departments that have at least one configured camera, with counts."""
    stmt = select(CameraModel.department, func.count(CameraModel.id)).group_by(CameraModel.department)
    res = await db.execute(stmt)
    dept_counts = {dept: count for dept, count in res.fetchall() if dept}
    return {
        "status": "success",
        "total_departments": len(dept_counts),
        "departments": [
            {"department": dept, "camera_count": count}
            for dept, count in sorted(dept_counts.items(), key=lambda x: x[0])
        ],
    }


@router.get("", response_model=CameraListResponse)
async def list_cameras(
    department: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List configured cameras, with status taken from the running pipeline.

    The stored ``status`` column is only the last persisted value; whether a
    camera is delivering frames right now is known by its worker, so the live
    value overrides it. A camera without a worker is reported OFFLINE, and a
    camera turned off (``is_ai_enabled`` false) DISABLED.
    """
    stmt = select(CameraModel).order_by(CameraModel.channel_number.asc())
    if department:
        stmt = stmt.where(CameraModel.department == department)
    res = await db.execute(stmt)
    cams = res.scalars().all()

    feeds = []
    for c in cams:
        feed = _live_feed(c)
        if status and feed.status != status:
            continue
        feeds.append(feed)

    return CameraListResponse(cameras=feeds, total=len(feeds))


# ---------------- Duplicate camera guard ----------------
#
# One physical camera configured twice (the same recorder channel on two
# streams, or a recorder channel and the camera's own address) is analysed
# twice and counted twice. services/duplicate_cameras.py finds such groups;
# one primary per group feeds store totals and the others are excluded until
# the operator resolves them here. A setup warning, never a failure.

async def _cams_by_recorder(db: AsyncSession) -> dict:
    from ..services.recorder_substreams import cameras_by_recorder

    res = await db.execute(select(CameraModel))
    return cameras_by_recorder({"id": c.id, "name": c.name, "rtsp_url": c.rtsp_url or ""}
                               for c in res.scalars().all())


@router.get("/duplicates")
async def list_duplicate_cameras(db: AsyncSession = Depends(get_db)):
    """Duplicate groups, which camera of each feeds store totals, and the recorder device lists used.

    Also starts, in the background, the daily read of each Dahua recorder's
    connected-camera list (never read, or read over 24 h ago; one login).
    """
    duplicate_guard.invalidate()
    report = duplicate_guard.report()
    try:
        started = duplicate_guard.schedule_stale_refresh(await _cams_by_recorder(db))
    except Exception as exc:  # noqa: BLE001 - the report stands without it
        logger.debug("recorder device list refresh not started: %s", exc)
        started = []
    report["refresh_started"] = started
    return report


class RecorderDevicesRefreshRequest(BaseModel):
    host: Optional[str] = Field(None, max_length=128, description="One recorder; omitted = every recorder in use")
    http_port: int = Field(80, ge=1, le=65535)


@router.post("/duplicates/refresh")
async def refresh_recorder_device_lists(body: RecorderDevicesRefreshRequest = Body(default_factory=RecorderDevicesRefreshRequest),
                                        db: AsyncSession = Depends(get_db)):
    """Read the connected-camera list of the recorder(s) now (one login each; a rejected one is not retried)."""
    groups = await _cams_by_recorder(db)
    if body.host is not None:
        host = body.host.strip()
        if host not in groups:
            raise HTTPException(status_code=404, detail="No camera uses this recorder (id = recorder address).")
        hosts = [host]
    else:
        hosts = sorted(groups)
    results = []
    for host in hosts:
        results.append(await duplicate_guard.refresh_recorder(host, groups[host], http_port=body.http_port))
    public = [{k: r.get(k) for k in ("host", "ok", "error", "read_at", "devices", "source", "auth_failed")}
              | {"channels_mapped": len(r.get("channels") or {})} for r in results]
    return {"recorders": public, **duplicate_guard.report()}


class DuplicatePairRequest(BaseModel):
    camera_ids: list[str] = Field(..., min_length=2, max_length=20)


class DuplicatePrimaryRequest(BaseModel):
    camera_id: str


async def _existing_ids_or_404(ids: list[str], db: AsyncSession) -> list[str]:
    ids = list(dict.fromkeys(str(i) for i in ids))
    res = await db.execute(select(CameraModel.id).where(CameraModel.id.in_(ids)))
    found = {r[0] for r in res.all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown camera(s): {', '.join(missing[:20])}")
    return ids


@router.post("/duplicates/dismiss")
async def dismiss_duplicate(body: DuplicatePairRequest, db: AsyncSession = Depends(get_db)):
    """"They're different cameras": never group these cameras again (stored); all count in store totals."""
    ids = await _existing_ids_or_404(body.camera_ids, db)
    if len(ids) < 2:
        raise HTTPException(status_code=422, detail="Name at least two different cameras.")
    duplicate_guard.dismiss(ids)
    logger.info("Cameras marked as different (not duplicates): %s", ", ".join(ids))
    return duplicate_guard.report()


@router.post("/duplicates/undismiss")
async def undismiss_duplicate(body: DuplicatePairRequest, db: AsyncSession = Depends(get_db)):
    """Undo "They're different cameras" for these cameras."""
    duplicate_guard.undismiss(await _existing_ids_or_404(body.camera_ids, db))
    return duplicate_guard.report()


@router.post("/duplicates/primary")
async def set_duplicate_primary(body: DuplicatePrimaryRequest, db: AsyncSession = Depends(get_db)):
    """Make this camera the one of its duplicate group that feeds store totals."""
    await _get_camera_or_404(body.camera_id, db)
    duplicate_guard.invalidate()
    report = duplicate_guard.report()
    group = next((g for g in report["groups"] if any(c["camera_id"] == body.camera_id for c in g["cameras"])), None)
    if group is None:
        raise HTTPException(status_code=409, detail="This camera is not in a duplicate group.")
    duplicate_guard.set_primary(body.camera_id, [c["camera_id"] for c in group["cameras"]])
    logger.info("Duplicate group %s: %s now feeds store totals", group["id"], body.camera_id)
    return duplicate_guard.report()


def _refuse_duplicate(url: str, *, exclude_id: Optional[str] = None, allow: bool = False,
                      extra: tuple = ()) -> list[dict]:
    """409 when ``url`` is a camera already configured, unless ``allow``. Returns the matches."""
    found = duplicate_guard.conflicts(url, exclude_id=exclude_id, extra=extra)
    if found and not allow:
        raise HTTPException(status_code=409, detail=duplicate_guard.conflict_detail(url, found))
    return found


_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DEPARTMENT_RE = re.compile(r"^[A-Z0-9 _&/-]{1,40}$")


class CameraCreateRequest(CameraFeed):
    """Body of ``POST /api/v1/cameras``: a CameraFeed plus how to reach it.

    ``id`` and ``location`` are optional (an id is generated). ``source_type``
    is one of rtsp, http, onvif, usb, file (inferred from the URL when
    omitted). ``username``/``password`` are stored encrypted per camera and
    never written into ``rtsp_url``.
    """

    id: Optional[str] = None
    location: str = ""
    source_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    # The same physical camera is already configured: add it anyway (e.g. a
    # main-stream close-up). The copy is excluded from store totals.
    allow_duplicate: bool = False


class CameraUpdateRequest(CameraFeed):
    """Body of ``PUT /api/v1/cameras/{id}``: a CameraFeed, plus the duplicate override."""

    allow_duplicate: bool = False


class TestConnectionRequest(BaseModel):
    source_type: Optional[str] = None
    url: str
    username: Optional[str] = None
    password: Optional[str] = None
    timeout_s: float = Field(camera_source.DEFAULT_TIMEOUT_S, ge=1.0, le=camera_source.MAX_TIMEOUT_S)
    # The role the operator intends for this camera (validated, echoed with its
    # mounting tip so the add form can show it next to the preview).
    role: Optional[str] = None


def _role_or_422(value) -> Optional[str]:
    try:
        return camera_roles.normalise_role(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _sent_feature_keys(features) -> set:
    if isinstance(features, CameraFeatureConfig):
        return set(features.model_fields_set)
    if isinstance(features, dict):
        return set(features.keys())
    return set()


async def _normalised_source(source_type, url, username, password, timeout_s=camera_source.DEFAULT_TIMEOUT_S):
    """Validate a source and resolve ONVIF to its RTSP URI. 422 on bad input."""
    try:
        src = camera_source.normalize_source(source_type, url, username, password)
        if src.source_type == "onvif":
            info = await camera_source.resolve_source_url(src, timeout_s)
            src = camera_source.CameraSource("rtsp", info["url"], src.username, src.password)
        return src
    except camera_source.SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/test-connection")
async def test_camera_connection(req: TestConnectionRequest):
    """Open a stream for real, grab one frame and return its size, fps and a preview.

    Nothing is stored. The response never contains credentials; ``url`` and
    ``resolved_url`` are redacted. Validation problems are a 422; a source
    that is valid but does not deliver video is ``success: false`` with the
    reason, so the form can show it inline.
    """
    role = _role_or_422(req.role)
    try:
        src = camera_source.normalize_source(req.source_type, req.url, req.username, req.password)
    except camera_source.SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    result = await camera_source.test_connection(src, req.timeout_s)
    if role and isinstance(result, dict):
        p = camera_roles.ROLE_PRESETS[role]
        result = {**result, "role": role, "role_label": p.label, "mounting_tip": p.mounting_tip}
    return result


@router.post("", response_model=CameraFeed)
async def create_camera(cam_in: CameraCreateRequest, db: AsyncSession = Depends(get_db)):
    """Add a camera. Validated server-side; credentials go to the encrypted store."""
    cam_id = (cam_in.id or "").strip() or f"cam_{uuid.uuid4().hex[:10]}"
    if not _CAMERA_ID_RE.match(cam_id):
        raise HTTPException(status_code=422, detail="Camera id may only contain letters, digits, '_', '-', '.', ':'.")
    name = (cam_in.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Enter a camera name.")
    if len(name) > 120:
        raise HTTPException(status_code=422, detail="Camera name is too long (max 120 characters).")
    department = (cam_in.department or "GENERAL").strip().upper() or "GENERAL"
    if not _DEPARTMENT_RE.match(department):
        raise HTTPException(status_code=422, detail="Department may only contain letters, digits, spaces and _&/-.")
    location = (cam_in.location or "").strip()
    if len(location) > 120:
        raise HTTPException(status_code=422, detail="Location is too long (max 120 characters).")
    role = _role_or_422(cam_in.role)

    src = await _normalised_source(cam_in.source_type, cam_in.rtsp_url, cam_in.username, cam_in.password)

    res = await db.execute(select(CameraModel).where(CameraModel.id == cam_id))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Camera ID '{cam_id}' already exists")
    dup = _refuse_duplicate(src.url, allow=cam_in.allow_duplicate)

    payload = _writable_payload(cam_in)
    payload.update(name=name, department=department, location=location, rtsp_url=src.url)
    payload.pop("role", None)
    if role:
        # The role's defaults, with any feature value the client sent explicitly on top.
        payload["role"] = role
        features = camera_roles.default_features(role)
        sent = _sent_feature_keys(cam_in.features) if "features" in cam_in.model_fields_set else set()
        explicit = payload.get("features") or {}
        features.update({k: explicit[k] for k in sent if k in explicit})
        payload["features"] = CameraFeatureConfig.model_validate(features).model_dump()
    # Nothing has been measured yet: the worker reports status, fps and
    # resolution once it reads frames. Only an explicit client value is kept.
    sent = cam_in.model_fields_set
    if "status" not in sent:
        payload["status"] = "STARTING"
    if "fps" not in sent:
        payload["fps"] = 0
    if "resolution" not in sent:
        payload["resolution"] = "unknown"
    if "channel_number" not in sent:
        max_ch = await db.scalar(
            select(CameraModel.channel_number).order_by(CameraModel.channel_number.desc()).limit(1)
        )
        payload["channel_number"] = (max_ch or 0) + 1

    if src.has_credentials:
        try:
            camera_source.store_credentials(cam_id, src.username, src.password)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not store credentials for camera %s: %s", cam_id, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Could not store the camera credentials securely.") from None

    new_cam = CameraModel(id=cam_id, **payload)
    db.add(new_cam)
    try:
        await db.commit()
    except Exception:
        if src.has_credentials:
            camera_source.delete_credentials(cam_id)
        raise
    await db.refresh(new_cam)
    camera_roles.role_cache.invalidate()
    duplicate_guard.invalidate()
    if dup:
        logger.warning("Camera %s added although it is the same camera as %s (confirmed by the operator)",
                       cam_id, ", ".join(d["camera_id"] for d in dup))
    if new_cam.features:
        feature_manager.set_camera_features(cam_id, CameraFeatureConfig.model_validate(new_cam.features))
    logger.info("Camera %s added (%s %s)", cam_id, src.source_type, camera_source.mask_url(src.url))

    # Start its worker now rather than on the next reconcile tick.
    try:
        from ..services.pipeline_supervisor import pipeline_supervisor

        if getattr(pipeline_supervisor, "_running", False):
            await asyncio.wait_for(pipeline_supervisor.reconcile_cameras(), timeout=10)
    except Exception as exc:  # noqa: BLE001 - the periodic reconcile picks it up
        logger.debug("reconcile after create_camera: %s", exc)
    return _model_to_feed(new_cam)


# ---------------- On / off ----------------
#
# The switch is the existing ``is_ai_enabled`` column, which the pipeline
# supervisor has always used to decide whether a camera gets a worker. Off
# means no worker, no connection, no decode, no inference and no clip buffer;
# nothing is recorded, and the camera reports DISABLED ("OFF" on the
# dashboard), never offline. The bulk route is declared before
# PUT /{camera_id} so "enabled" is never taken for a camera id.

class CameraEnabledRequest(BaseModel):
    enabled: bool


class CamerasEnabledRequest(BaseModel):
    camera_ids: list[str] = Field(..., min_length=1, max_length=500)
    enabled: bool


async def _reconcile_now(camera_id: str = "") -> None:
    """Apply stored camera changes to the workers now, not on the next tick."""
    try:
        from ..services.pipeline_supervisor import pipeline_supervisor

        if getattr(pipeline_supervisor, "_running", False):
            await asyncio.wait_for(pipeline_supervisor.reconcile_cameras(), timeout=10)
    except Exception as exc:  # noqa: BLE001 - the periodic reconcile picks it up
        logger.debug("reconcile for %s: %s", camera_id or "cameras", exc)


def _switch_state(cam: CameraModel) -> dict:
    rt = live_engine.runtimes.get(cam.id)
    if not cam.is_ai_enabled:
        status = "DISABLED"
    else:
        status = rt.status if rt is not None and rt.enabled else "OFFLINE"
    return {"camera_id": cam.id, "name": cam.name, "enabled": bool(cam.is_ai_enabled), "status": status}


async def _set_enabled(camera_ids: list[str], enabled: bool, db: AsyncSession) -> list[dict]:
    """Store the switch for every camera (all or none), then apply it."""
    ids = list(dict.fromkeys(str(i) for i in camera_ids))
    res = await db.execute(select(CameraModel).where(CameraModel.id.in_(ids)))
    found = {c.id: c for c in res.scalars().all()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown camera(s): {', '.join(missing[:20])}")
    changed = [found[i] for i in ids if bool(found[i].is_ai_enabled) != enabled]
    for cam in changed:
        cam.is_ai_enabled = enabled
    if changed:
        await db.commit()
        duplicate_guard.invalidate()
        logger.info("Turned %s %d camera(s): %s", "on" if enabled else "off", len(changed),
                    ", ".join(c.id for c in changed[:20]))
        await _reconcile_now()
    return [_switch_state(found[i]) for i in ids]


@router.put("/enabled")
async def set_cameras_enabled(body: CamerasEnabledRequest, db: AsyncSession = Depends(get_db)):
    """Turn many cameras on or off at once (e.g. every camera that is offline).

    Unknown ids fail the whole request with 404 and change nothing.
    """
    cams = await _set_enabled(body.camera_ids, body.enabled, db)
    return {"enabled": body.enabled, "updated": len(cams), "cameras": cams}


@router.put("/{camera_id}/enabled")
async def set_camera_enabled(camera_id: str, body: CameraEnabledRequest, db: AsyncSession = Depends(get_db)):
    """Turn one camera on (its worker starts now) or off (its worker stops now)."""
    return (await _set_enabled([camera_id], body.enabled, db))[0]


@router.get("/{camera_id}", response_model=CameraFeed)
async def get_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await _get_camera_or_404(camera_id, db)
    return _live_feed(cam)


@router.put("/{camera_id}", response_model=CameraFeed)
async def update_camera(camera_id: str, cam_in: CameraUpdateRequest, db: AsyncSession = Depends(get_db)):
    """Upsert a camera from a full CameraFeed body (PUT semantics)."""
    res = await db.execute(select(CameraModel).where(CameraModel.id == camera_id))
    cam = res.scalar_one_or_none()
    payload = _writable_payload(cam_in)
    source_changed = False
    if "rtsp_url" in payload:
        url = str(payload["rtsp_url"] or "")
        if camera_source.is_masked(url) or (cam is not None and not url):
            # The client echoed back the redacted URL from a GET: keep the stored one.
            payload.pop("rtsp_url")
        elif url and "://" in url and "@" in urlsplit(url).netloc:
            # Credentials typed inline move to the encrypted store.
            try:
                payload["rtsp_url"] = camera_source.detach_credentials(camera_id, url)
                source_changed = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not store credentials for camera %s: %s", camera_id, type(exc).__name__)
                raise HTTPException(status_code=500, detail="Could not store the camera credentials securely.") from None

    if "rtsp_url" in payload and (cam is None or payload["rtsp_url"] != cam.rtsp_url):
        source_changed = True
        if payload["rtsp_url"]:
            _refuse_duplicate(str(payload["rtsp_url"]), exclude_id=camera_id, allow=cam_in.allow_duplicate)
    old_quality = (cam.features or {}).get("stream_quality") if cam is not None and isinstance(cam.features, dict) else None
    if cam is not None and "features" in payload:
        _keep_unsent_settings(payload["features"], cam_in.features, cam.features)
    if cam is not None:
        # On/off is changed only through PUT /{id}/enabled: the field defaults
        # to true, and the Settings dialog echoes the value it loaded, so a
        # save would otherwise silently turn a switched-off camera back on.
        payload.pop("is_ai_enabled", None)
    # Setting a role here stores it without touching the toggles; use
    # PUT /{id}/role with apply_defaults to apply the preset.
    if "role" in payload:
        payload["role"] = _role_or_422(payload["role"])
    if "pos_register_id" in payload:
        payload["pos_register_id"] = str(payload["pos_register_id"]).strip() or None
    if cam is None:
        cam = CameraModel(id=camera_id, **payload)
        db.add(cam)
    else:
        for key, value in payload.items():
            setattr(cam, key, value)

    await db.commit()
    await db.refresh(cam)
    camera_roles.role_cache.invalidate()
    duplicate_guard.invalidate()
    if "features" in payload:
        feature_manager.set_camera_features(camera_id, CameraFeatureConfig.model_validate(payload["features"]))
    new_quality = (payload.get("features") or {}).get("stream_quality") if "features" in payload else old_quality
    if source_changed or (old_quality or "auto") != (new_quality or "auto"):
        # A new URL or inline credentials: reconnect now (also ends a pause
        # after a rejected login) instead of on the next reconcile tick. A
        # changed Stream quality reopens the worker on the chosen stream.
        await _restart_worker(camera_id)
    return _model_to_feed(cam)


async def _restart_worker(camera_id: str) -> dict:
    """Apply a changed source now and wake the worker from any retry pause."""
    await _reconcile_now(camera_id)
    woke = live_engine.reconnect_camera(camera_id)
    rt = live_engine.runtimes.get(camera_id)
    return {"reconnecting": woke, "status": rt.status if rt is not None else "OFFLINE",
            "last_error": rt.last_error if rt is not None else None}


class CameraCredentialsRequest(BaseModel):
    username: Optional[str] = Field(None, max_length=128)
    password: Optional[str] = Field(None, max_length=256)


@router.put("/{camera_id}/credentials")
async def update_camera_credentials(camera_id: str, body: CameraCredentialsRequest,
                                    db: AsyncSession = Depends(get_db)):
    """Replace the camera's stored login and reconnect straight away.

    This is how an operator clears AUTH_FAILED: the worker stops retrying a
    rejected login (to keep the camera from locking the account) until the
    credentials change or Reconnect is pressed. Empty username and password
    remove the stored login. The password is never echoed back.
    """
    await _get_camera_or_404(camera_id, db)
    try:
        username = camera_source._check_text((body.username or "").strip(), "Username", 128) or None
        password = camera_source._check_text(body.password or "", "Password", 256) or None
    except camera_source.SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        if username or password:
            camera_source.store_credentials(camera_id, username, password)
        else:
            camera_source.delete_credentials(camera_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not store credentials for camera %s: %s", camera_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not store the camera credentials securely.") from None
    return {"camera_id": camera_id, "has_credentials": bool(username or password),
            **(await _restart_worker(camera_id))}


@router.post("/{camera_id}/reconnect")
async def reconnect_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    """Retry the connection now, including after a rejected login."""
    await _get_camera_or_404(camera_id, db)
    return {"camera_id": camera_id, **(await _restart_worker(camera_id))}


@router.patch("/{camera_id}/position")
async def update_camera_position(
    camera_id: str, pos: CameraPositionUpdate, db: AsyncSession = Depends(get_db)
):
    """Place the camera on the blueprint. All coordinates are metres."""
    cam = await _get_camera_or_404(camera_id, db)
    cam.floor_x = pos.floor_x
    cam.floor_y = pos.floor_y
    if pos.floor_z is not None:
        cam.floor_z = pos.floor_z
    cam.azimuth_deg = pos.azimuth_deg
    cam.fov_deg = pos.fov_deg
    await db.commit()

    return {
        "status": "success",
        "camera_id": camera_id,
        "floor_x": cam.floor_x,
        "floor_y": cam.floor_y,
        "floor_z": cam.floor_z,
        "azimuth_deg": cam.azimuth_deg,
        "fov_deg": cam.fov_deg,
    }


@router.get("/{camera_id}/delete-impact")
async def get_camera_delete_impact(camera_id: str, db: AsyncSession = Depends(get_db)):
    """Read-only: what removing this camera deletes and what it keeps, with counts."""
    await _get_camera_or_404(camera_id, db)
    out = await delete_impact(db, camera_id)
    dup_of = duplicate_guard.camera_fields(camera_id)["duplicate_of"]
    out["duplicate_of"] = dup_of
    if dup_of:
        out["note"] += (" This camera is a duplicate: its kept history stays excluded from store totals, "
                        "so nothing is counted twice.")
    return out


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await _get_camera_or_404(camera_id, db)
    duplicate_guard.forget_camera(camera_id)
    await db.delete(cam)
    await db.commit()
    camera_roles.role_cache.invalidate()
    duplicate_guard.invalidate()
    camera_source.delete_credentials(camera_id)
    try:
        live_engine.stop_camera(camera_id)
    except Exception as exc:  # the supervisor reconciles anyway
        logger.debug("stop_camera(%s) after delete: %s", camera_id, exc)
    return {
        "status": "success",
        "message": f"Camera {camera_id} deleted successfully",
        "camera_id": camera_id,
    }


# Per-camera settings that are not on/off flags. A client that does not know
# them (an older dashboard or the mobile app sending only the toggles) must
# not reset them, so a key absent from the request keeps its stored value.
_NON_FLAG_SETTINGS = ("person_max_frame_fraction", "night_watch", "decode_max_width", "stream_quality",
                      # Newer switches an older client (mobile app) does not send.
                      "static_figure_filter", "static_figure_seconds", "theft_clip")


def _keep_unsent_settings(target: Dict[str, object], sent: object, stored: object) -> None:
    if isinstance(sent, CameraFeatureConfig):
        sent_keys = sent.model_fields_set
    elif isinstance(sent, dict):
        sent_keys = set(sent.keys())
    else:
        sent_keys = set()
    if not isinstance(stored, dict):
        return
    for key in _NON_FLAG_SETTINGS:
        if key not in sent_keys and stored.get(key) is not None:
            target[key] = stored.get(key)


@router.put("/{camera_id}/features", response_model=CameraFeatureConfig)
async def update_camera_features(camera_id: str, config: CameraFeatureConfig, db: AsyncSession = Depends(get_db)):
    """Set this camera's analytics toggles and detector settings.

    Stored on the camera row so they survive restarts. Settings omitted from
    the body (``person_max_frame_fraction``) keep their stored value; send
    ``null`` to return to the server default.
    """
    cam = await _get_camera_or_404(camera_id, db)
    merged = config.model_dump()
    _keep_unsent_settings(merged, config, cam.features)
    config = CameraFeatureConfig.model_validate(merged)
    old_quality = (cam.features or {}).get("stream_quality") if isinstance(cam.features, dict) else None
    cam.features = config.model_dump()
    await db.commit()
    feature_manager.set_camera_features(camera_id, config)
    if (old_quality or "auto") != (config.stream_quality or "auto"):
        # Stream quality changed: reopen the worker on the chosen stream (the
        # reconcile also queues a read-only measurement if Auto needs one).
        await _reconcile_now(camera_id)
    return config


# JPEG quality of live pictures (tiles, the live view): past ~80 the file
# grows fast for detail nobody sees at tile size.
SNAPSHOT_JPEG_QUALITY = 80


@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str, annotate: bool = True, overlay: bool = False, max_width: int = 0):
    """Return the most recent real frame captured from this camera.

    When no frame is available the response is a "NO SIGNAL" slate with
    ``X-Frame-Source: no-signal`` and the reason, never a drawn stand-in.
    With ``annotate`` the frame is overlaid with the detections (boxes and
    pose skeletons) the model genuinely produced for it. Privacy masks are
    always burned in; the model itself runs on the unmasked frame, and
    AI_IGNORE regions drop detections as in the live pipeline.

    ``overlay`` (used with ``annotate=false``) draws the tracker's current
    boxes from the worker's own snapshot, exactly like ``/stream?overlay=1``.
    No inference runs, so the dashboard's camera tiles can refresh from this
    cheaply instead of each holding an MJPEG connection open.

    ``max_width`` (> 0) scales the picture down to at most that many pixels
    wide, aspect kept, never up: a tile asks for about its on-screen size
    (a 3072x2048 frame is ~850 KB at native size, too slow for a tile over a
    remote link). Without it the picture is native, for evidence and
    downloads. The size actually served is in ``X-Frame-Size``.
    """
    raw = live_engine.get_raw_frame(camera_id)
    frame = apply_privacy_masks(raw, camera_id) if raw is not None else None
    if frame is None:
        rt = live_engine.runtimes.get(camera_id)
        if rt is None:
            reason = "Camera is not running. Add it from the device scan."
        elif not rt.enabled:
            reason = "Camera is turned off. Turn it on from the Cameras view."
        else:
            reason = rt.last_error or f"Camera status: {rt.status}"

        slate = render_no_signal(str(reason), 854, 480)
        ok, jpeg = cv2.imencode(".jpg", slate, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return Response(
            content=jpeg.tobytes() if ok else b"",
            media_type="image/jpeg",
            headers={"X-Frame-Source": "no-signal", "Cache-Control": "no-store"},
        )

    if annotate:
        h, w = raw.shape[:2]
        max_frac = feature_manager.get_setting(camera_id, "person_max_frame_fraction")
        polys = ignore_polygons(camera_id, w, h)
        # Raw detections of this frame, after the camera's ignore areas, each
        # labelled with what the live pipeline makes of it: a box on a static
        # (or remembered static) figure is drawn grey and "not counted", so a
        # poster is never shown as a green person.
        dets = outside_ignore_regions(
            person_detector.detect(raw, camera_id=camera_id,
                                   **({"max_frame_fraction": max_frac} if max_frac is not None else {})),
            polys)
        if frame is raw:
            frame = raw.copy()
        draw_ignore_areas(frame, polys)
        rt = live_engine.runtimes.get(camera_id)
        worker = live_engine.workers.get(camera_id)
        live_tracks = rt.get_tracks() if rt is not None and (rt.frame_width, rt.frame_height) == (w, h) else []
        for det in dets:
            label, colour = snapshot_label(det, live_tracks, getattr(worker, "tracker", None), (w, h))
            x1, y1, x2, y2 = int(det.x1), int(det.y1), int(det.x2), int(det.y2)
            cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)
            cv2.putText(frame, label, (x1, max(y1 - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
            if det.keypoints is not None:
                _draw_skeleton(frame, det.keypoints, colour)
        frame, _ = fit_width(frame, max_width)
    else:
        # Scale first, then draw: boxes and labels keep a legible size.
        frame, scale = fit_width(frame, max_width)
        rt = live_engine.runtimes.get(camera_id) if overlay else None
        if rt is not None:
            # ``frame`` is this request's own copy (get_raw_frame / resize copy).
            frame = render_overlay(frame, rt, scale=scale)

    ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), SNAPSHOT_JPEG_QUALITY])
    if not ok:
        raise HTTPException(status_code=500, detail="failed to encode frame")
    h, w = frame.shape[:2]
    return Response(
        content=jpeg.tobytes(),
        media_type="image/jpeg",
        headers={"X-Frame-Source": "live", "X-Frame-Size": f"{w}x{h}", "Cache-Control": "no-store"},
    )


def snapshot_label(det, live_tracks: list, tracker, frame_size) -> tuple:
    """(label, BGR colour) for a raw detection on the annotated snapshot.

    Matched to the live track it overlaps most (IoU >= 0.5): static ->
    "static figure, not counted" (grey); pending static -> "pending static,
    not counted" (grey-blue); else a box on a remembered static figure ->
    "remembered static figure, not counted" (grey); a track not yet seen
    moving -> "person (pending)" (amber); otherwise "person" (green).
    """
    from ..services.live_analytics_engine import PENDING_COLOUR, PENDING_STATIC_COLOUR, STATIC_COLOUR
    from ..services.tracking_service import _iou

    box = (float(det.x1), float(det.y1), float(det.x2), float(det.y2))
    best, best_iou = None, 0.5
    for t in live_tracks:
        iou = _iou(box, (t["x1"], t["y1"], t["x2"], t["y2"]))
        if iou >= best_iou:
            best, best_iou = t, iou
    if best is not None and best.get("motion_state") == "static":
        return "static figure, not counted", STATIC_COLOUR
    if best is not None and best.get("pending_static"):
        return "pending static, not counted", PENDING_STATIC_COLOUR
    try:
        remembered = tracker is not None and tracker.is_remembered_static(box, det.keypoints, frame_size)
    except Exception:  # noqa: BLE001 - a label must never fail the snapshot
        remembered = False
    if remembered:
        return "remembered static figure, not counted", STATIC_COLOUR
    if best is not None and best.get("confirmed") and best.get("motion_state") == "pending":
        return f"person (pending) {det.confidence:.2f}", PENDING_COLOUR
    return f"person {det.confidence:.2f}", (0, 255, 157)


# ---------------- Per-camera helpers ----------------

@router.get("/{camera_id}/live-status")
async def get_camera_live_status(camera_id: str, db: AsyncSession = Depends(get_db)):
    """The pipeline/status entry for this one camera.

    A configured camera without a running worker is reported with
    ``running: false`` and null telemetry. An unknown camera is a 404.
    """
    cam = await _get_camera_or_404(camera_id, db)
    rt = live_engine.runtimes.get(camera_id)
    if rt is None or not rt.enabled:
        off = not cam.is_ai_enabled or rt is not None
        return {
            "camera_id": cam.id,
            "name": cam.name,
            "running": False,
            "status": "DISABLED" if off else "OFFLINE",
            "enabled": bool(cam.is_ai_enabled),
            "frames_read": 0,
            "detections_last_frame": None,
            "live_tracks": None,
            "fps": None,
            "has_frame": False,
            "seconds_since_frame": None,
            "last_error": None if off else "No pipeline worker is running for this camera.",
            "calibrated": bool(cam.homography_matrix),
            "frame_width": None,
            "frame_height": None,
            # Static-figure telemetry: not measured without a worker.
            "static_tracks": None,
            "pending_tracks": None,
            "ignored_detections_last": None,
            "static_memory_count": None,
            "boxes": [],
        }

    entry = dict(rt.to_dict())
    entry.setdefault("camera_id", camera_id)
    entry["running"] = True
    entry.setdefault("calibrated", bool(cam.homography_matrix))
    entry.setdefault("frame_width", getattr(rt, "frame_width", None))
    entry.setdefault("frame_height", getattr(rt, "frame_height", None))
    return entry


@router.post("/{camera_id}/actions/snapshot")
async def save_camera_snapshot(camera_id: str, db: AsyncSession = Depends(get_db)):
    """Persist the camera's current real frame to ``settings.SNAPSHOTS_DIR``.

    409 when the camera has no frame: nothing is written and nothing is
    pretended.
    """
    await _get_camera_or_404(camera_id, db)
    frame = live_engine.get_frame(camera_id)
    if frame is None:
        rt = live_engine.runtimes.get(camera_id)
        reason = (
            "no pipeline worker is running for this camera"
            if rt is None
            else (rt.last_error or f"camera status is {rt.status}")
        )
        raise HTTPException(status_code=409, detail=f"Camera has no frame to save: {reason}")

    snapshots_dir = Path(settings.SNAPSHOTS_DIR)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")[:-3]
    filename = f"{camera_id}_{ts}.jpg"
    path = snapshots_dir / filename
    ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok or not path.exists():
        raise HTTPException(status_code=500, detail="failed to write snapshot")
    from ..services.evidence_storage import note_evidence_written

    note_evidence_written(path)

    h, w = frame.shape[:2]
    return {
        "saved": True,
        "filename": filename,
        # <img>/<a> cannot send a bearer header; the snapshot route takes a short-lived
        # clip-access token like event clips do.
        "url": f"/api/v1/events/snapshots/{filename}?token={auth_service.generate_clip_token(filename)}",
        "camera_id": camera_id,
        "frame_width": int(w),
        "frame_height": int(h),
        "bytes": path.stat().st_size,
    }


def _clip_buffer_reason(camera_id: str) -> Optional[str]:
    """Why a clip cannot be produced right now, or None when it can.

    Where no pre-event ring is kept (CLIP_PRE_EVENT_BUFFER), the clip is its
    post-roll, recorded from now on: that needs a camera that is streaming.
    """
    if not clip_recorder_service.keeps_ring(camera_id):
        rt = live_engine.runtimes.get(camera_id)
        if rt is None or rt.status != "ONLINE" or not rt.last_frame_at or time.time() - rt.last_frame_at > 5.0:
            return "the camera is not delivering video"
        return None
    buf = clip_recorder_service.buffers.get(camera_id)
    if buf is None:
        return "no frame ring buffer exists for this camera (nothing feeds clip_recorder for it)"
    with buf._lock:
        buffered = len(buf.buffer)
        latest_ts = buf.latest_frame_time
    if buffered == 0:
        return "the camera's ring buffer holds no frames"
    age = time.time() - latest_ts if latest_ts else None
    if age is None or age > 5.0:
        return f"the camera's ring buffer is stale (last frame {age:.0f}s ago)" if age else \
            "the camera's ring buffer has never received a frame"
    return None


@router.post("/{camera_id}/actions/clip")
async def export_camera_clip(
    camera_id: str,
    post_roll_seconds: int = 5,
    db: AsyncSession = Depends(get_db),
):
    """Build an MP4 from the camera's real buffered frames plus a short post-roll.

    Only succeeds when ``clip_recorder`` genuinely holds live frames for the
    camera. Otherwise 501 with the reason; a fake success is never returned.
    """
    await _get_camera_or_404(camera_id, db)
    reason = _clip_buffer_reason(camera_id)
    if reason is not None:
        raise HTTPException(status_code=501, detail=f"clip export not available: {reason}")

    post_roll = max(1, min(int(post_roll_seconds), 30))
    event_id = f"manual_{camera_id}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    clip_url = await clip_recorder_service.record_event_clip(
        event_id=event_id, camera_id=camera_id, post_roll_seconds=post_roll
    )
    filename = f"{event_id}.mp4"
    path = Path(settings.CLIPS_DIR) / filename
    if not clip_url or not path.exists():
        raise HTTPException(
            status_code=501,
            detail="clip export not available: recorder produced no file (disk full or encoder failure)",
        )
    return {
        "saved": True,
        "filename": filename,
        "url": f"/api/v1/events/clips/{filename}",
        "camera_id": camera_id,
        "bytes": path.stat().st_size,
        "post_roll_seconds": post_roll,
    }


# ---------------- Discovery aliases ----------------

@router.post("/scan")
async def scan_network_and_devices():
    """Discover cameras. Alias of /api/v1/layout/discover; reports only what answered."""
    found = await camera_discovery_service.discover()
    return {"status": "success", "count": len(found), "sources": [d.to_dict() for d in found]}


@router.post("/api/rescan")
async def studio_rescan():
    found = await camera_discovery_service.discover()
    return {"status": "success", "sources": [d.to_dict() for d in found]}


@router.post("/{camera_id}/mute")
async def mute_camera(
    camera_id: str,
    req: MuteCameraRequest = Body(default_factory=MuteCameraRequest),
    db: AsyncSession = Depends(get_db),
):
    """Hold back loss-prevention phone pushes for this camera (e.g. during restocking).

    Alerts are still logged and still reach the dashboard; only pushes stop.
    ``duration_minutes: 0`` unmutes.
    """
    cam = await _get_camera_or_404(camera_id, db)
    cam.muted_until = (
        datetime.utcnow() + timedelta(minutes=req.duration_minutes) if req.duration_minutes > 0 else None
    )
    await db.commit()
    return {
        "status": "success",
        "camera_id": camera_id,
        "muted_minutes": req.duration_minutes,
        "muted_until": cam.muted_until,
    }
