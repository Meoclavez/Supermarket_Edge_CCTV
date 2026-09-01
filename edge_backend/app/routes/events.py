"""Security events, emergency alerts, and video clip streaming routes backed by SQLite."""

import uuid
import logging
from datetime import datetime
from typing import List, Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, Depends, WebSocket
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.config import settings
from app.database import get_db, async_session_factory
from app.models.db_models import SecurityEventModel, CameraModel
from app.models.schemas import (
    SecurityEvent,
    SecurityEventCreate,
    SecurityEventListResponse,
    EventSeverity,
    EventType,
    BoundingBox,
    Keypoint,
    KinematicMetrics,
)
from app.services.clip_recorder import clip_recorder_service
from app.services.notification_service import notification_service
from app.services.auth_service import auth_service, general_rate_limiter
from app.routes import ResilientRoute

logger = logging.getLogger("EventRoutes")
router = APIRouter(
    prefix="/api/v1/events",
    tags=["Events & Clips"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute
)


@router.post("/trigger", response_model=SecurityEvent)
async def trigger_event(
    event_in: SecurityEventCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    authorized: bool = Depends(auth_service.verify_internal_key)
):
    """Ingest detection event from Hailo AI vision engine.

    Triggers pre/post event MP4 clip generation and dispatches APNs Critical Alerts
    and Android High-Priority alarms to mobile clients.
    """
    event_id = f"evt_{uuid.uuid4().hex[:8]}"

    # Lookup camera name & location from DB
    cam_stmt = select(CameraModel).where(CameraModel.id == event_in.camera_id)
    cam_res = await db.execute(cam_stmt)
    camera = cam_res.scalar_one_or_none()

    camera_name = camera.name if camera else event_in.camera_id
    location = camera.location if camera else "Monitored Zone"

    # Step 1: Save immediate snapshot from ring-buffer
    snapshot_url = clip_recorder_service.save_snapshot(event_in.camera_id, event_id)

    # Step 2: Persist event into SQLite DB
    db_event = SecurityEventModel(
        id=event_id,
        camera_id=event_in.camera_id,
        camera_name=camera_name,
        location=location,
        event_type=event_in.event_type.value,
        severity=event_in.severity.value,
        confidence=event_in.confidence,
        timestamp=datetime.utcnow(),
        snapshot_url=snapshot_url,
        clip_url=None,
        bounding_box=event_in.bounding_box.dict() if event_in.bounding_box else None,
        keypoints=[k.dict() for k in event_in.keypoints] if event_in.keypoints else None,
        kinematics=event_in.kinematics.dict() if event_in.kinematics else None,
        metadata_json=getattr(event_in, "metadata", None) or getattr(event_in, "metadata_json", None),
        acknowledged=False,
    )
    db.add(db_event)
    await db.commit()

    # Step 3: Schedule background clip capture & push notification
    async def process_and_notify():
        try:
            clip_url = await clip_recorder_service.record_event_clip(
                event_id=event_id,
                camera_id=event_in.camera_id,
                post_roll_seconds=settings.POST_EVENT_RECORD_SECONDS
            )
        except Exception as e:
            logger.error(f"Clip recording failed for {event_id}: {e}")
            clip_url = f"{settings.EDGE_BASE_URL}/api/v1/events/clips/{event_id}.mp4"

        # Update event clip URL in DB
        async with async_session_factory() as session:
            stmt = select(SecurityEventModel).where(SecurityEventModel.id == event_id)
            res = await session.execute(stmt)
            ev = res.scalar_one_or_none()
            if ev:
                ev.clip_url = clip_url
                await session.commit()

        # Construct Pydantic event and dispatch push
        event_pydantic = SecurityEvent(
            id=event_id,
            camera_id=event_in.camera_id,
            camera_name=camera_name,
            location=location,
            event_type=event_in.event_type,
            severity=event_in.severity,
            confidence=event_in.confidence,
            timestamp=datetime.utcnow(),
            snapshot_url=snapshot_url,
            clip_url=clip_url,
            metadata=getattr(event_in, "metadata", None) or getattr(event_in, "metadata_json", None),
            acknowledged=False
        )
        await notification_service.dispatch_event_notification(event_pydantic)

    background_tasks.add_task(process_and_notify)

    # Broadcast to active WebSocket clients
    from fastapi.encoders import jsonable_encoder
    import asyncio as aio
    
    event_dict = jsonable_encoder(SecurityEvent(
        id=event_id,
        camera_id=event_in.camera_id,
        camera_name=camera_name,
        location=location,
        event_type=event_in.event_type,
        severity=event_in.severity,
        confidence=event_in.confidence,
        timestamp=datetime.utcnow(),
        snapshot_url=snapshot_url,
        clip_url=None,
        bounding_box=event_in.bounding_box,
        keypoints=event_in.keypoints,
        kinematics=event_in.kinematics,
        metadata=getattr(event_in, "metadata", None) or getattr(event_in, "metadata_json", None),
        acknowledged=False,
    ))
    
    async def broadcast_ws():
        await ws_manager.broadcast_event(event_dict)
    
    background_tasks.add_task(broadcast_ws)

    return SecurityEvent(
        id=event_id,
        camera_id=event_in.camera_id,
        camera_name=camera_name,
        location=location,
        event_type=event_in.event_type,
        severity=event_in.severity,
        confidence=event_in.confidence,
        timestamp=datetime.utcnow(),
        snapshot_url=snapshot_url,
        clip_url=None,
        bounding_box=event_in.bounding_box,
        keypoints=event_in.keypoints,
        kinematics=event_in.kinematics,
        metadata=getattr(event_in, "metadata", None) or getattr(event_in, "metadata_json", None),
        acknowledged=False,
    )

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_event(self, event_data: dict):
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_json(event_data)
            except Exception:
                dead_connections.append(connection)
        
        for dead in dead_connections:
            self.disconnect(dead)

ws_manager = ConnectionManager()

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # We just keep the connection open and listen to ping/pong
            _ = await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)



@router.get("", response_model=SecurityEventListResponse)
async def list_events(
    limit: int = 50,
    severity: Optional[EventSeverity] = None,
    event_type: Optional[EventType] = None,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve paginated event history with filtering from SQLite."""
    stmt = select(SecurityEventModel).order_by(desc(SecurityEventModel.timestamp)).limit(limit)
    if severity:
        stmt = stmt.where(SecurityEventModel.severity == severity.value)
    if event_type:
        stmt = stmt.where(SecurityEventModel.event_type == event_type.value)

    res = await db.execute(stmt)
    db_events = res.scalars().all()

    events = [
        SecurityEvent(
            id=e.id,
            camera_id=e.camera_id,
            camera_name=e.camera_name,
            location=e.location,
            event_type=EventType(e.event_type),
            severity=EventSeverity(e.severity),
            confidence=e.confidence,
            timestamp=e.timestamp,
            clip_url=e.clip_url,
            snapshot_url=e.snapshot_url,
            bounding_box=BoundingBox(**e.bounding_box) if e.bounding_box else None,
            keypoints=[Keypoint(**k) for k in e.keypoints] if e.keypoints else None,
            kinematics=KinematicMetrics(**e.kinematics) if e.kinematics else None,
            acknowledged=e.acknowledged,
            acknowledged_at=e.acknowledged_at
        )
        for e in db_events
    ]
    return SecurityEventListResponse(events=events, total=len(events))


@router.get("/{event_id}", response_model=SecurityEvent)
async def get_event(event_id: str, db: AsyncSession = Depends(get_db)):
    """Retrieve single event details by ID."""
    stmt = select(SecurityEventModel).where(SecurityEventModel.id == event_id)
    res = await db.execute(stmt)
    e = res.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Event not found")

    return SecurityEvent(
        id=e.id,
        camera_id=e.camera_id,
        camera_name=e.camera_name,
        location=e.location,
        event_type=EventType(e.event_type),
        severity=EventSeverity(e.severity),
        confidence=e.confidence,
        timestamp=e.timestamp,
        clip_url=e.clip_url,
        snapshot_url=e.snapshot_url,
        bounding_box=BoundingBox(**e.bounding_box) if e.bounding_box else None,
        keypoints=[Keypoint(**k) for k in e.keypoints] if e.keypoints else None,
        kinematics=KinematicMetrics(**e.kinematics) if e.kinematics else None,
        acknowledged=e.acknowledged,
        acknowledged_at=e.acknowledged_at
    )


@router.get("/clips/{filename}")
async def stream_event_clip(
    filename: str,
    token_payload: dict = Depends(auth_service.verify_clip_access)
):
    """Stream recorded MP4 event clip with path traversal protection and token verification."""
    safe_path = auth_service.sanitize_and_resolve_file(settings.CLIPS_DIR, filename)

    return FileResponse(
        path=str(safe_path),
        media_type="video/mp4",
        filename=safe_path.name,
        headers={"Accept-Ranges": "bytes"}
    )


@router.get("/snapshots/{filename}")
async def get_event_snapshot(
    filename: str,
    token_payload: dict = Depends(auth_service.verify_clip_access)
):
    """Retrieve JPEG event snapshot with path traversal protection."""
    safe_path = auth_service.sanitize_and_resolve_file(settings.SNAPSHOTS_DIR, filename)

    return FileResponse(
        path=str(safe_path),
        media_type="image/jpeg",
        filename=safe_path.name
    )


@router.post("/{event_id}/acknowledge")
async def acknowledge_event(event_id: str, db: AsyncSession = Depends(get_db)):
    """Acknowledge and dismiss an emergency alert in SQLite."""
    stmt = select(SecurityEventModel).where(SecurityEventModel.id == event_id)
    res = await db.execute(stmt)
    event = res.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    event.acknowledged = True
    event.acknowledged_at = datetime.utcnow()
    await db.commit()

    return {"status": "success", "event_id": event_id, "acknowledged_at": event.acknowledged_at}
