"""Loss-prevention alert routes: alert log, acknowledgement, websocket feed, device registration, media.

Alerts are prompts for staff review (suspicious behaviour, camera offline),
not findings. They reach staff phones through ``notification_service`` and
the dashboard through ``/api/v1/events/ws``.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_factory, get_db
from app.models.db_models import CameraModel, SecurityEventModel
from app.models.schemas import (
    BoundingBox,
    EventSeverity,
    EventType,
    Keypoint,
    SecurityEvent,
    SecurityEventCreate,
    SecurityEventListResponse,
)
from app.routes import ResilientRoute
from app.services.auth_service import auth_service, general_rate_limiter
from app.services.clip_recorder import clip_recorder_service
from app.services.notification_service import alert_hub, notification_service

logger = logging.getLogger("EventRoutes")
router = APIRouter(
    prefix="/api/v1/events",
    tags=["Loss-prevention alerts"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)

# Kept as a module attribute for callers that imported it from here.
ws_manager = alert_hub

_RETAIL_TYPES = [t.value for t in EventType]
_SEVERITIES = {s.value for s in EventSeverity}


def _row_to_event(e: SecurityEventModel) -> SecurityEvent:
    meta = e.metadata_json or {}
    measured = meta.get("confidence_measured", True)
    return SecurityEvent(
        id=e.id,
        camera_id=e.camera_id,
        camera_name=e.camera_name,
        location=e.location,
        event_type=EventType(e.event_type),
        # Rows from older builds may carry a severity no longer offered.
        severity=EventSeverity(e.severity) if e.severity in _SEVERITIES else EventSeverity.HIGH,
        confidence=e.confidence if measured else None,
        description=meta.get("body"),
        timestamp=e.timestamp,
        clip_url=e.clip_url,
        snapshot_url=e.snapshot_url,
        bounding_box=BoundingBox(**e.bounding_box) if e.bounding_box else None,
        keypoints=[Keypoint(**k) for k in e.keypoints] if e.keypoints else None,
        metadata=meta or None,
        acknowledged=e.acknowledged,
        acknowledged_at=e.acknowledged_at,
        evidence_expired_at=e.evidence_expired_at,
    )


@router.post("/trigger", response_model=SecurityEvent)
async def trigger_event(
    event_in: SecurityEventCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    authorized: bool = Depends(auth_service.verify_internal_key),
):
    """Ingest a loss-prevention alert from an internal service (internal key only).

    Saves the ring-buffer snapshot, logs the alert, broadcasts it to the
    dashboard websocket and, in the background, records the clip and pushes
    it to staff devices (unless the camera's alerts are muted).
    """
    cam = (await db.execute(select(CameraModel).where(CameraModel.id == event_in.camera_id))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera '{event_in.camera_id}' not found")

    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    snapshot_url = clip_recorder_service.save_snapshot(event_in.camera_id, event_id)
    metadata = dict(event_in.metadata or event_in.metadata_json or {})
    if event_in.description:
        metadata.setdefault("body", event_in.description)

    db.add(SecurityEventModel(
        id=event_id,
        camera_id=cam.id,
        camera_name=cam.name,
        location=cam.location,
        event_type=event_in.event_type.value,
        severity=event_in.severity.value,
        confidence=event_in.confidence if event_in.confidence is not None else 0.0,
        timestamp=datetime.utcnow(),
        snapshot_url=snapshot_url,
        clip_url=None,
        bounding_box=event_in.bounding_box.model_dump() if event_in.bounding_box else None,
        keypoints=[k.model_dump() for k in event_in.keypoints] if event_in.keypoints else None,
        metadata_json={**metadata, "confidence_measured": event_in.confidence is not None},
        acknowledged=False,
    ))
    await db.commit()

    event = SecurityEvent(
        id=event_id,
        camera_id=cam.id,
        camera_name=cam.name,
        location=cam.location,
        event_type=event_in.event_type,
        severity=event_in.severity,
        confidence=event_in.confidence,
        description=event_in.description,
        timestamp=datetime.utcnow(),
        snapshot_url=snapshot_url,
        bounding_box=event_in.bounding_box,
        keypoints=event_in.keypoints,
        metadata=metadata or None,
    )

    async def record_clip_and_push():
        clip_url = None
        try:
            clip_url = await clip_recorder_service.record_event_clip(
                event_id=event_id,
                camera_id=event_in.camera_id,
                post_roll_seconds=settings.POST_EVENT_RECORD_SECONDS,
            )
        except Exception as exc:
            logger.error("Clip recording failed for %s: %s", event_id, exc)
        if clip_url:
            async with async_session_factory() as session:
                row = (await session.execute(
                    select(SecurityEventModel).where(SecurityEventModel.id == event_id)
                )).scalar_one_or_none()
                if row:
                    row.clip_url = clip_url
                    await session.commit()
        try:
            await notification_service.dispatch_event_notification(event.model_copy(update={"clip_url": clip_url}))
        except Exception as exc:
            logger.error("Push fan-out failed for %s: %s", event_id, exc)

    # Background tasks run in order: broadcast first, or the dashboard/phone
    # alert would wait POST_EVENT_RECORD_SECONDS for the clip to finish.
    background_tasks.add_task(alert_hub.broadcast_event, jsonable_encoder(event))
    background_tasks.add_task(record_clip_and_push)
    return event


# The websocket lives on its own router: the HTTP dependencies on ``router``
# (API-key header, bearer scheme, rate limiter) cannot resolve for a websocket
# handshake, so the feed authenticates inside the handler instead.
ws_router = APIRouter(prefix="/api/v1/events", tags=["Loss-prevention alerts"])


def _websocket_authorised(websocket: WebSocket) -> bool:
    """Same rules as HTTP: a current operator session or an unrevoked phone
    token for this device, from ``?token=``, a Bearer header or the cookie."""
    if settings.AUTH_DISABLED:
        return True
    raw = websocket.query_params.get("token")
    if not raw:
        header = websocket.headers.get("authorization", "")
        if header.startswith("Bearer "):
            raw = header.split(" ", 1)[1]
    if not raw:
        raw = websocket.cookies.get("edge_cctv_token")
    return bool(raw and auth_service.verify_session_token(raw))


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Live alert feed for the dashboard and paired phones. Each message is one alert as JSON."""
    if not _websocket_authorised(websocket):
        # 1008 = policy violation: the client should re-authenticate, not retry blindly.
        # Accept first: a close before accept becomes an HTTP 403 handshake
        # rejection under uvicorn, and the client never sees the 1008 code.
        await websocket.accept()
        await websocket.close(code=1008)
        return
    await alert_hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        alert_hub.disconnect(websocket)


_DEVICES_GONE = ("Unbound push-token registration was removed. Pair the phone with this device "
                 "(POST /api/v1/pairing/claim or /api/v1/auth/login with a 'phone' object) and update "
                 "its token with PUT /api/v1/pairing/devices/me/push.")


@router.post("/devices", status_code=410)
async def register_device_gone():
    raise HTTPException(status_code=410, detail=_DEVICES_GONE)


@router.delete("/devices/{device_token}", status_code=410)
async def unregister_device_gone(device_token: str):
    raise HTTPException(status_code=410, detail=_DEVICES_GONE)


@router.get("", response_model=SecurityEventListResponse)
async def list_events(
    limit: int = Query(50, ge=1, le=500),
    severity: Optional[EventSeverity] = None,
    event_type: Optional[EventType] = None,
    acknowledged: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """Alert history, newest first.

    Rows left by older builds with non-retail types (e.g. fall detection) are
    not served; they have no meaning in this product.
    """
    stmt = (
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type.in_(_RETAIL_TYPES))
        .order_by(desc(SecurityEventModel.timestamp))
        .limit(limit)
    )
    if severity:
        stmt = stmt.where(SecurityEventModel.severity == severity.value)
    if event_type:
        stmt = stmt.where(SecurityEventModel.event_type == event_type.value)
    if acknowledged is not None:
        stmt = stmt.where(SecurityEventModel.acknowledged == acknowledged)

    rows = (await db.execute(stmt)).scalars().all()
    events = [_row_to_event(e) for e in rows]
    return SecurityEventListResponse(events=events, total=len(events))


@router.get("/clips/{filename}")
async def stream_event_clip(filename: str, token_payload: dict = Depends(auth_service.verify_clip_access)):
    """Stream a recorded MP4 alert clip (path traversal protected, token verified)."""
    safe_path = auth_service.sanitize_and_resolve_file(settings.CLIPS_DIR, filename)
    return FileResponse(path=str(safe_path), media_type="video/mp4", filename=safe_path.name,
                        headers={"Accept-Ranges": "bytes"})


@router.get("/snapshots/{filename}")
async def get_event_snapshot(filename: str, token_payload: dict = Depends(auth_service.verify_clip_access)):
    """Return a JPEG alert snapshot (path traversal protected)."""
    safe_path = auth_service.sanitize_and_resolve_file(settings.SNAPSHOTS_DIR, filename)
    return FileResponse(path=str(safe_path), media_type="image/jpeg", filename=safe_path.name)


async def _get_retail_event(event_id: str, db: AsyncSession) -> SecurityEventModel:
    row = (await db.execute(
        select(SecurityEventModel).where(SecurityEventModel.id == event_id)
    )).scalar_one_or_none()
    if row is None or row.event_type not in _RETAIL_TYPES:
        raise HTTPException(status_code=404, detail="Alert not found")
    return row


@router.get("/{event_id}", response_model=SecurityEvent)
async def get_event(event_id: str, db: AsyncSession = Depends(get_db)):
    return _row_to_event(await _get_retail_event(event_id, db))


@router.post("/{event_id}/acknowledge")
async def acknowledge_event(event_id: str, db: AsyncSession = Depends(get_db)):
    """Mark an alert as seen by staff."""
    row = await _get_retail_event(event_id, db)
    row.acknowledged = True
    row.acknowledged_at = datetime.utcnow()
    await db.commit()
    return {"status": "success", "event_id": event_id, "acknowledged_at": row.acknowledged_at}
