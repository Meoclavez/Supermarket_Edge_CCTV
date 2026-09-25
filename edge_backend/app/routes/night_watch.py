"""Night watch settings, status, events and evidence (services/night_watch.py).

    GET  /api/v1/cameras/{id}/night-watch   the camera's settings, store zone, live state
    PUT  /api/v1/cameras/{id}/night-watch   save the settings (NightWatchConfig)
    GET  /api/v1/night-watch                every camera: armed now, next window
    GET  /api/v1/night-watch/events         recent NIGHT_INTRUSION / NIGHT_MOTION, with store time
    GET  /api/v1/night-watch/evidence/{f}   an evidence still (.jpg) or clip (.mp4)

The settings live in the camera row's ``features`` JSON (``night_watch``),
like the other per-camera settings, and reach the pipeline through
``feature_manager``. Times are store-local (SITE_TIMEZONE, else the host's
zone); every instant is also given in UTC so a viewer elsewhere can show
their own time next to it.
"""

from __future__ import annotations

import time
from datetime import timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import CameraModel, SecurityEventModel
from app.models.schemas import CameraFeatureConfig, NightWatchConfig
from app.routes import ResilientRoute
from app.services.auth_service import auth_service
from app.services.feature_manager import feature_manager
from app.services.live_analytics_engine import live_engine
from app.services.night_watch import ARMED_STATES, night_watch, time_info, zone_info

router = APIRouter(
    prefix="/api/v1",
    tags=["Night watch"],
    dependencies=[Depends(auth_service.verify_api_access)],
    route_class=ResilientRoute,
)

NIGHT_TYPES = ("NIGHT_INTRUSION", "NIGHT_MOTION")


async def _camera(db: AsyncSession, camera_id: str) -> CameraModel:
    cam = (await db.execute(select(CameraModel).where(CameraModel.id == camera_id))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
    return cam


def _streaming(camera_id: str) -> bool:
    rt = live_engine.runtimes.get(camera_id)
    return rt is not None and rt.status == "ONLINE"


def _camera_view(cam: CameraModel) -> dict:
    stored = CameraFeatureConfig.model_validate(cam.features or {}).night_watch
    return {
        "camera_id": cam.id,
        "camera_name": cam.name,
        "configured": stored is not None,
        "config": (stored or NightWatchConfig()).model_dump(),
        "store_timezone": zone_info(),
        "status": night_watch.camera_status(cam.id, streaming=_streaming(cam.id)),
    }


@router.get("/cameras/{camera_id}/night-watch")
async def get_camera_night_watch(camera_id: str, db: AsyncSession = Depends(get_db)):
    """The camera's night-watch settings (defaults when never set), the store zone and its state."""
    return _camera_view(await _camera(db, camera_id))


@router.put("/cameras/{camera_id}/night-watch")
async def put_camera_night_watch(camera_id: str, config: NightWatchConfig, db: AsyncSession = Depends(get_db)):
    """Save the camera's night watch; the running worker picks it up on its next frame."""
    cam = await _camera(db, camera_id)
    features = dict(cam.features or {})
    features["night_watch"] = config.model_dump()
    merged = CameraFeatureConfig.model_validate(features)
    cam.features = merged.model_dump()
    await db.commit()
    await db.refresh(cam)
    feature_manager.set_camera_features(camera_id, merged)
    return _camera_view(cam)


@router.get("/night-watch")
async def night_watch_summary(db: AsyncSession = Depends(get_db)):
    """Which cameras are armed now, which are set up, and the next window, in store time."""
    now = time.time()
    cams = (await db.execute(select(CameraModel).order_by(CameraModel.name))).scalars().all()
    rows, next_arm = [], None
    for cam in cams:
        st = night_watch.camera_status(cam.id, now=now, streaming=_streaming(cam.id))
        if not st.get("enabled"):
            continue
        rows.append({"camera_id": cam.id, "camera_name": cam.name, **st})
        na = st.get("next_arm")
        if na and (next_arm is None or na["ts"] < next_arm["ts"]):
            next_arm = na
    return {
        "store_timezone": zone_info(now),
        "now": time_info(now),
        "cameras": rows,
        "cameras_enabled": len(rows),
        "armed_now": [r["camera_id"] for r in rows if r["state"] in ARMED_STATES],
        "unavailable": [r["camera_id"] for r in rows if r["state"] == "unavailable"],
        "next_arm": next_arm,
        "delivery": night_watch.status(),
    }


@router.get("/night-watch/events")
async def night_watch_events(limit: int = Query(30, ge=1, le=200), camera_id: Optional[str] = None,
                             db: AsyncSession = Depends(get_db)):
    """Night-watch alerts and motion events, newest first, with UTC and store-local times."""
    stmt = (select(SecurityEventModel).where(SecurityEventModel.event_type.in_(NIGHT_TYPES))
            .order_by(desc(SecurityEventModel.timestamp)).limit(limit))
    if camera_id:
        stmt = stmt.where(SecurityEventModel.camera_id == camera_id)
    rows = (await db.execute(stmt)).scalars().all()
    events = []
    for e in rows:
        meta = e.metadata_json or {}
        ts = e.timestamp.replace(tzinfo=timezone.utc).timestamp()   # stored naive UTC
        events.append({
            "id": e.id, "camera_id": e.camera_id, "camera_name": e.camera_name,
            "event_type": e.event_type, "severity": e.severity,
            "title": meta.get("title"), "body": meta.get("body"),
            "confidence": e.confidence if meta.get("confidence_measured") else None,
            "at": time_info(ts),
            "snapshot_url": e.snapshot_url, "clip_url": e.clip_url,
            # Deleted by the evidence storage limit (services/evidence_storage.py).
            "evidence_expired": e.evidence_expired_at is not None,
            "acknowledged": bool(e.acknowledged),
        })
    return {"events": events, "total": len(events), "store_timezone": zone_info()}


@router.get("/night-watch/evidence/{filename}")
async def night_watch_evidence(filename: str):
    """An evidence still or clip saved with a night-watch event (this device's SSD only)."""
    path = night_watch.evidence_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="No such night-watch evidence (it may have been pruned)")
    return FileResponse(str(path), media_type="video/mp4" if filename.endswith(".mp4") else "image/jpeg")
