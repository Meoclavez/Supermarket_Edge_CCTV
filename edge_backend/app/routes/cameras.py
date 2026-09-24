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
from ..services.feature_manager import feature_manager
from ..services.inference_backend import person_detector
from ..services.live_analytics_engine import _draw_skeleton, live_engine
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
    if "features" in out:
        # Normalise to the current flag set; keys from older builds
        # (fall_detection, door_monitoring, ...) are dropped here.
        out["features"] = CameraFeatureConfig.model_validate(out["features"] or {}).model_dump()
    return out


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
    value overrides it. A camera without a worker is reported OFFLINE.
    """
    stmt = select(CameraModel).order_by(CameraModel.channel_number.asc())
    if department:
        stmt = stmt.where(CameraModel.department == department)
    res = await db.execute(stmt)
    cams = res.scalars().all()

    feeds = []
    for c in cams:
        feed = _model_to_feed(c)
        rt = live_engine.runtimes.get(c.id)
        if rt is not None:
            feed.status = rt.status
            feed.fps = int(round(rt.fps)) if rt.fps else 0
        else:
            feed.status = "OFFLINE"
            feed.fps = 0
        if status and feed.status != status:
            continue
        feeds.append(feed)

    return CameraListResponse(cameras=feeds, total=len(feeds))


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


@router.get("/{camera_id}", response_model=CameraFeed)
async def get_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await _get_camera_or_404(camera_id, db)
    return _model_to_feed(cam)


@router.put("/{camera_id}", response_model=CameraFeed)
async def update_camera(camera_id: str, cam_in: CameraFeed, db: AsyncSession = Depends(get_db)):
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
    if cam is not None and "features" in payload:
        _keep_unsent_settings(payload["features"], cam_in.features, cam.features)
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
    if "features" in payload:
        feature_manager.set_camera_features(camera_id, CameraFeatureConfig.model_validate(payload["features"]))
    if source_changed:
        # A new URL or inline credentials: reconnect now (also ends a pause
        # after a rejected login) instead of on the next reconcile tick.
        await _restart_worker(camera_id)
    return _model_to_feed(cam)


async def _restart_worker(camera_id: str) -> dict:
    """Apply a changed source now and wake the worker from any retry pause."""
    try:
        from ..services.pipeline_supervisor import pipeline_supervisor

        if getattr(pipeline_supervisor, "_running", False):
            await asyncio.wait_for(pipeline_supervisor.reconcile_cameras(), timeout=10)
    except Exception as exc:  # noqa: BLE001 - the periodic reconcile picks it up
        logger.debug("reconcile for %s: %s", camera_id, exc)
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


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await _get_camera_or_404(camera_id, db)
    await db.delete(cam)
    await db.commit()
    camera_roles.role_cache.invalidate()
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
_NON_FLAG_SETTINGS = ("person_max_frame_fraction",)


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
    cam.features = config.model_dump()
    await db.commit()
    feature_manager.set_camera_features(camera_id, config)
    return config


@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str, annotate: bool = True):
    """Return the most recent real frame captured from this camera.

    When no frame is available the response is a "NO SIGNAL" slate with
    ``X-Frame-Source: no-signal`` and the reason, never a drawn stand-in.
    With ``annotate`` the frame is overlaid with the detections (boxes and
    pose skeletons) the model genuinely produced for it. Privacy masks are
    always burned in; the model itself runs on the unmasked frame, and
    AI_IGNORE regions drop detections as in the live pipeline.
    """
    raw = live_engine.get_raw_frame(camera_id)
    frame = apply_privacy_masks(raw, camera_id) if raw is not None else None
    if frame is None:
        rt = live_engine.runtimes.get(camera_id)
        if rt is None:
            reason = "Camera is not running. Add it from the device scan."
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
        dets = outside_ignore_regions(
            person_detector.detect(raw, **({"max_frame_fraction": max_frac} if max_frac is not None else {})),
            ignore_polygons(camera_id, w, h))
        if frame is raw:
            frame = raw.copy()
        for det in dets:
            x1, y1, x2, y2 = int(det.x1), int(det.y1), int(det.x2), int(det.y2)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 157), 2)
            cv2.putText(
                frame, f"person {det.confidence:.2f}", (x1, max(y1 - 8, 14)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 157), 1,
            )
            if det.keypoints is not None:
                _draw_skeleton(frame, det.keypoints, (0, 255, 157))

    ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        raise HTTPException(status_code=500, detail="failed to encode frame")
    return Response(
        content=jpeg.tobytes(),
        media_type="image/jpeg",
        headers={"X-Frame-Source": "live", "Cache-Control": "no-store"},
    )


# ---------------- Per-camera helpers ----------------

@router.get("/{camera_id}/live-status")
async def get_camera_live_status(camera_id: str, db: AsyncSession = Depends(get_db)):
    """The pipeline/status entry for this one camera.

    A configured camera without a running worker is reported with
    ``running: false`` and null telemetry. An unknown camera is a 404.
    """
    cam = await _get_camera_or_404(camera_id, db)
    rt = live_engine.runtimes.get(camera_id)
    if rt is None:
        return {
            "camera_id": cam.id,
            "name": cam.name,
            "running": False,
            "status": "OFFLINE",
            "enabled": bool(cam.is_ai_enabled),
            "frames_read": 0,
            "detections_last_frame": None,
            "live_tracks": None,
            "fps": None,
            "has_frame": False,
            "seconds_since_frame": None,
            "last_error": "No pipeline worker is running for this camera.",
            "calibrated": bool(cam.homography_matrix),
            "frame_width": None,
            "frame_height": None,
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
    """Why a clip cannot be produced right now, or None when it can."""
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
