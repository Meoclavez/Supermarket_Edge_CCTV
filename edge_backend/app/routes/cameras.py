"""Camera management, discovery aliases, snapshots, feature toggles and per-camera helpers.

Every camera served here comes from the database. There is no built-in
fleet, no "current source" placeholder and no fallback list: an empty table
means no cameras, and that is what the API reports.
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Response
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
from ..services.camera_discovery import camera_discovery_service
from ..services.clip_recorder import clip_recorder_service
from ..services.feature_manager import feature_manager
from ..services.inference_backend import person_detector
from ..services.live_analytics_engine import live_engine

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
        rtsp_url=c.rtsp_url,
        webrtc_url=c.webrtc_url or "",
        status=c.status,
        fps=c.fps,
        resolution=c.resolution,
        is_ai_enabled=c.is_ai_enabled,
        ai_models=list(c.ai_models or []),
        features=feature_manager.get_camera_features(c.id),
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


@router.post("", response_model=CameraFeed)
async def create_camera(cam_in: CameraFeed, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CameraModel).where(CameraModel.id == cam_in.id))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Camera ID '{cam_in.id}' already exists")

    new_cam = CameraModel(id=cam_in.id, **_writable_payload(cam_in))
    db.add(new_cam)
    await db.commit()
    await db.refresh(new_cam)
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

    if cam is None:
        cam = CameraModel(id=camera_id, **payload)
        db.add(cam)
    else:
        for key, value in payload.items():
            setattr(cam, key, value)

    await db.commit()
    await db.refresh(cam)
    return _model_to_feed(cam)


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
    try:
        live_engine.stop_camera(camera_id)
    except Exception as exc:  # the supervisor reconciles anyway
        logger.debug("stop_camera(%s) after delete: %s", camera_id, exc)
    return {
        "status": "success",
        "message": f"Camera {camera_id} deleted successfully",
        "camera_id": camera_id,
    }


@router.put("/{camera_id}/features", response_model=CameraFeatureConfig)
def update_camera_features(camera_id: str, config: CameraFeatureConfig):
    feature_manager.set_camera_features(camera_id, config)
    return config


@router.get("/{camera_id}/snapshot")
def get_camera_snapshot(camera_id: str, annotate: bool = True):
    """Return the most recent real frame captured from this camera.

    When no frame is available the response is a "NO SIGNAL" slate with
    ``X-Frame-Source: no-signal`` and the reason, never a drawn stand-in.
    With ``annotate`` the frame is overlaid with the detections the model
    genuinely produced for it.
    """
    frame = live_engine.get_frame(camera_id)
    if frame is None:
        rt = live_engine.runtimes.get(camera_id)
        if rt is None:
            reason = "Camera is not running. Add it from the device scan."
        else:
            reason = rt.last_error or f"Camera status: {rt.status}"

        slate = np.zeros((480, 854, 3), dtype=np.uint8)
        slate[:] = (18, 20, 26)
        cv2.putText(slate, "NO SIGNAL", (300, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (90, 96, 112), 2)
        cv2.putText(slate, str(reason)[:70], (60, 275), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (70, 76, 92), 1)
        ok, jpeg = cv2.imencode(".jpg", slate, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return Response(
            content=jpeg.tobytes() if ok else b"",
            media_type="image/jpeg",
            headers={"X-Frame-Source": "no-signal", "Cache-Control": "no-store"},
        )

    if annotate:
        for det in person_detector.detect(frame):
            x1, y1, x2, y2 = int(det.x1), int(det.y1), int(det.x2), int(det.y2)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 157), 2)
            cv2.putText(
                frame, f"person {det.confidence:.2f}", (x1, max(y1 - 8, 14)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 157), 1,
            )

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
        "url": f"/api/v1/events/snapshots/{filename}",
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
    cam = await _get_camera_or_404(camera_id, db)
    cam.muted_until = datetime.utcnow() + timedelta(minutes=req.duration_minutes)
    await db.commit()
    return {"status": "success", "camera_id": camera_id, "muted_minutes": req.duration_minutes}
