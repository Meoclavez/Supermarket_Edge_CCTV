"""FastAPI routes for 24-Hour Timeline, Dynamic HLS, Incident Clip Exports, and Storage Health."""

import os
from datetime import datetime
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc

from app.config import settings
from app.database import get_db
from app.models.db_models import CameraModel, DVRSegmentModel, SecurityEventModel, IncidentArchiveModel
from app.models.schemas import (
    CameraTimelineResponse,
    TimelineSegment,
    TimelineGap,
    TimelineEventMarker,
    DVRExportRequest,
    IncidentArchiveResponse,
    IncidentArchiveListResponse,
    StorageHealthResponse
)
from app.services.dvr_recorder import dvr_recorder_service
from app.services.auth_service import auth_service, general_rate_limiter
from app.routes import ResilientRoute

router = APIRouter(
    prefix="/api/v1",
    tags=["DVR & Timeline"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute
)


# ---------------- 1. 24-Hour Timeline API ----------------

@router.get("/cameras/{camera_id}/timeline", response_model=CameraTimelineResponse)
async def get_camera_timeline(
    camera_id: str,
    date_str: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$", description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve 24-hour recorded video segments, offline gaps, and aligned AI event markers."""
    cam_stmt = select(CameraModel).where(CameraModel.id == camera_id)
    camera = (await db.execute(cam_stmt)).scalar_one_or_none()
    if not camera:
        raise HTTPException(status_code=404, detail=f"Camera {camera_id} not found")

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    segments, gaps, total_duration = await dvr_recorder_service.get_timeline_data(camera_id, target_date)

    start_dt = datetime.combine(target_date, datetime.min.time())
    end_dt = datetime.combine(target_date, datetime.max.time())

    evt_stmt = (
        select(SecurityEventModel)
        .where(
            and_(
                SecurityEventModel.camera_id == camera_id,
                SecurityEventModel.timestamp >= start_dt,
                SecurityEventModel.timestamp <= end_dt
            )
        )
        .order_by(SecurityEventModel.timestamp.asc())
    )
    db_events = (await db.execute(evt_stmt)).scalars().all()
    token = auth_service.generate_stream_token(camera_id)

    timeline_segments = [
        TimelineSegment(
            id=s.id,
            camera_id=s.camera_id,
            start_time=s.start_time,
            end_time=s.end_time,
            duration_seconds=s.duration_seconds,
            file_size_bytes=s.file_size_bytes,
            stream_url=f"{settings.EDGE_BASE_URL}/api/v1/dvr/segments/{s.id}/video?token={token}"
        )
        for s in segments
    ]

    event_markers = [
        TimelineEventMarker(
            id=e.id,
            event_type=e.event_type,
            severity=e.severity,
            confidence=e.confidence,
            timestamp=e.timestamp,
            snapshot_url=e.snapshot_url,
            clip_url=e.clip_url,
            bounding_box=e.bounding_box
        )
        for e in db_events
    ]

    timeline_gaps = [TimelineGap(**g) for g in gaps]
    hls_master = f"{settings.EDGE_BASE_URL}/api/v1/dvr/cameras/{camera_id}/hls/{date_str}/index.m3u8?token={token}"

    return CameraTimelineResponse(
        camera_id=camera.id,
        camera_name=camera.name,
        date=date_str,
        total_recorded_seconds=total_duration,
        total_segments=len(timeline_segments),
        hls_master_url=hls_master,
        segments=timeline_segments,
        events=event_markers,
        gaps=timeline_gaps
    )


# ---------------- 2. Dynamic HLS Playlist Streaming ----------------

@router.get("/dvr/cameras/{camera_id}/hls/{date_str}/index.m3u8")
async def get_hls_playlist(
    camera_id: str,
    date_str: str,
    token_payload: dict = Depends(auth_service.verify_stream_access)
):
    """Serve dynamic 24-hour HLS m3u8 playlist with byte-level range compatibility."""
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")

    token = auth_service.generate_stream_token(camera_id)
    playlist_content = await dvr_recorder_service.generate_hls_playlist(camera_id, target_date, token)

    return Response(content=playlist_content, media_type="application/vnd.apple.mpegurl")


# ---------------- 3. Segment Video Streaming Route ----------------

@router.get("/dvr/segments/{segment_id}/video")
async def stream_dvr_segment_video(
    segment_id: str,
    token_payload: dict = Depends(auth_service.verify_clip_access),
    db: AsyncSession = Depends(get_db)
):
    """Streams 1-minute MP4 segment with HTTP 206 Partial Content support."""
    stmt = select(DVRSegmentModel).where(DVRSegmentModel.id == segment_id)
    seg = (await db.execute(stmt)).scalar_one_or_none()
    if not seg:
        raise HTTPException(status_code=404, detail="DVR segment not found")

    file_path = Path(seg.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Segment video file missing on disk")

    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=file_path.name,
        headers={"Accept-Ranges": "bytes"}
    )


# ---------------- 4. Custom Incident Clip Export & Archiving ----------------

@router.post("/cameras/{camera_id}/export", response_model=IncidentArchiveResponse)
async def export_custom_incident_clip(
    camera_id: str,
    export_req: DVRExportRequest,
    db: AsyncSession = Depends(get_db)
):
    """Stitches and exports a custom time window into a downloadable lossless MP4."""
    try:
        archive = await dvr_recorder_service.export_incident_clip(
            camera_id=camera_id,
            start_time=export_req.start_time,
            end_time=export_req.end_time,
            title=export_req.title,
            description=export_req.description
        )
    except FileNotFoundError as fnf:
        raise HTTPException(status_code=404, detail=str(fnf))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stitch export clip: {e}")

    return IncidentArchiveResponse(
        id=archive.id,
        camera_id=archive.camera_id,
        camera_name=archive.camera_name,
        title=archive.title,
        description=archive.description,
        start_time=archive.start_time,
        end_time=archive.end_time,
        duration_seconds=archive.duration_seconds,
        file_size_bytes=archive.file_size_bytes,
        status=archive.status,
        download_url=archive.download_url,
        created_at=archive.created_at
    )


@router.get("/dvr/archives", response_model=IncidentArchiveListResponse)
async def list_incident_archives(
    camera_id: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Lists saved incident video archives."""
    stmt = select(IncidentArchiveModel).order_by(desc(IncidentArchiveModel.created_at)).limit(limit)
    if camera_id:
        stmt = stmt.where(IncidentArchiveModel.camera_id == camera_id)

    res = await db.execute(stmt)
    archives = res.scalars().all()

    items = [
        IncidentArchiveResponse(
            id=a.id,
            camera_id=a.camera_id,
            camera_name=a.camera_name,
            title=a.title,
            description=a.description,
            start_time=a.start_time,
            end_time=a.end_time,
            duration_seconds=a.duration_seconds,
            file_size_bytes=a.file_size_bytes,
            status=a.status,
            download_url=a.download_url,
            created_at=a.created_at
        )
        for a in archives
    ]
    return IncidentArchiveListResponse(archives=items, total=len(items))


@router.get("/dvr/archives/{archive_id}/download")
async def download_incident_archive(
    archive_id: str,
    token_payload: dict = Depends(auth_service.verify_clip_access),
    db: AsyncSession = Depends(get_db)
):
    """Downloads exported incident MP4 file."""
    stmt = select(IncidentArchiveModel).where(IncidentArchiveModel.id == archive_id)
    archive = (await db.execute(stmt)).scalar_one_or_none()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")

    file_path = Path(archive.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Archive file missing on disk")

    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=f"{archive.title.replace(' ', '_')}_{archive.id}.mp4",
        headers={"Accept-Ranges": "bytes"}
    )


# ---------------- 5. Storage Quota & Health Telemetry ----------------

@router.get("/storage/health", response_model=StorageHealthResponse)
async def get_storage_health():
    """Retrieve comprehensive disk health, SMART indicators, external drive detection, and per-camera quotas."""
    report = await dvr_recorder_service.get_storage_health_report()
    return StorageHealthResponse(**report)
