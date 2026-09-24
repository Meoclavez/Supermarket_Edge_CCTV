"""Dahua NVR Management, Channel Discovery, and Credential Persistence API.

Provides dedicated endpoints for Dahua NVR/DVR multi-channel systems:
- Persistent credential storage (survives restarts/updates)
- Fast network probe & channel sweep (detects which channels are active)
- Bulk channel adoption onto the store blueprint with sub/main stream preferences
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import CameraModel
from app.services.auth_service import auth_service
from app.services import camera_source
from app.services.camera_drivers import redact_url
from app.services.dahua_probe_service import dahua_probe_service
from app.services.nvr_credential_service import nvr_credential_service
from app.services.pipeline_supervisor import pipeline_supervisor
from app.services.store_layout_service import store_layout_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/dahua",
    tags=["Dahua NVR"],
    dependencies=[Depends(auth_service.verify_api_access)],
)


class NVRConfigPayload(BaseModel):
    username: str = Field(default="admin", description="NVR administrator username")
    password: str = Field(default="", description="NVR administrator password")
    host: Optional[str] = Field(default=None, description="NVR IP or hostname")
    port: int = Field(default=554, description="RTSP Port")
    default_channels: int = Field(default=16, description="Default number of channels to probe")


class NVRProbePayload(BaseModel):
    host: str = Field(..., description="Dahua NVR IP or hostname")
    username: Optional[str] = Field(default=None, description="Optional override username")
    password: Optional[str] = Field(default=None, description="Optional override password")
    port: int = Field(default=554, description="RTSP port")
    max_channels: int = Field(default=16, ge=1, le=64, description="Number of channels to scan")
    save_credentials: bool = Field(default=True, description="Persist these credentials on disk if successful")


class ChannelAdoptItem(BaseModel):
    channel: int
    name: Optional[str] = None
    department: str = "GENERAL"
    quality: str = "sub"  # "sub" (subtype=1) or "main" (subtype=0)


class DahuaBatchAdoptPayload(BaseModel):
    host: str
    port: int = 554
    username: Optional[str] = None
    password: Optional[str] = None
    channels: List[ChannelAdoptItem] = Field(..., description="Channels to adopt")


@router.get("/credentials")
async def get_dahua_credentials():
    """Retrieve saved Dahua NVR credentials (passwords masked for safety)."""
    return {
        "status": "success",
        "credentials": nvr_credential_service.get_safe_credentials(),
    }


@router.post("/credentials")
async def save_dahua_credentials(payload: NVRConfigPayload):
    """Persist Dahua NVR credentials on disk in storage/nvr_credentials.json."""
    saved = nvr_credential_service.save_credentials(
        username=payload.username,
        password=payload.password,
        host=payload.host,
        port=payload.port,
        default_channels=payload.default_channels,
    )
    return {
        "status": "success",
        "message": "NVR credentials saved to disk successfully",
        "credentials": saved,
    }


@router.post("/probe")
async def probe_dahua_nvr(payload: NVRProbePayload):
    """Probe a Dahua NVR, test credentials, and sweep channels 1..N for active video feeds."""
    host = payload.host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="Host IP or hostname is required")

    u = payload.username
    p = payload.password

    # Save credentials if requested and user supplied a password
    if payload.save_credentials and p:
        nvr_credential_service.save_credentials(
            username=u or "admin",
            password=p,
            host=host,
            port=payload.port,
            default_channels=payload.max_channels,
        )

    summary = await dahua_probe_service.probe_nvr(
        host=host,
        username=u,
        password=p,
        port=payload.port,
        max_channels=payload.max_channels,
    )

    return {
        "status": "success" if summary.reachable and summary.authenticated else "warning",
        "probe": summary.to_dict(),
    }


@router.post("/adopt", status_code=201)
async def adopt_dahua_channels(
    payload: DahuaBatchAdoptPayload,
    db: AsyncSession = Depends(get_db),
):
    """Adopt multiple channels from a Dahua NVR directly onto the blueprint as active cameras."""
    host = payload.host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="NVR host is required")
    if not payload.channels:
        raise HTTPException(status_code=400, detail="At least one channel must be specified")

    # Resolve credentials: payload -> saved on disk -> empty
    saved_u, saved_p = nvr_credential_service.get_auth_for_host(host)
    username = payload.username or saved_u or "admin"
    password = payload.password if payload.password is not None else saved_p

    # Save if new credentials provided
    if payload.password:
        nvr_credential_service.save_credentials(
            username=username,
            password=password,
            host=host,
            port=payload.port,
        )

    layout = await store_layout_service.get_active_layout(db)
    max_ch = await db.scalar(select(CameraModel.channel_number).order_by(CameraModel.channel_number.desc()).limit(1))
    current_channel_num = max_ch or 0

    from app.services.dahua_probe_service import build_dahua_url

    adopted: List[Dict[str, Any]] = []
    stored_creds: List[str] = []

    # Calculate staggered positions on floorplan
    cols = 4
    spacing_x = min(layout.width_m / (cols + 1), 6.0)
    spacing_y = min(layout.height_m / 4.0, 5.0)

    for i, item in enumerate(payload.channels):
        current_channel_num += 1
        cam_id = f"cam_{uuid.uuid4().hex[:10]}"
        ch = item.channel
        subtype = 0 if item.quality == "main" else 1

        # The stored URL carries no credentials; they are kept encrypted per
        # camera and injected only when the stream is opened.
        stream_url = build_dahua_url(host=host, port=payload.port, channel=ch, subtype=subtype)
        try:
            stream_url = camera_source.detach_credentials(cam_id, stream_url, username, password)
        except Exception as exc:  # noqa: BLE001
            for done in stored_creds:
                camera_source.delete_credentials(done)
            logger.error(f"Could not store credentials for camera {cam_id}: {type(exc).__name__}")
            raise HTTPException(status_code=500, detail="Could not store the camera credentials securely.") from None
        stored_creds.append(cam_id)

        col_idx = i % cols
        row_idx = i // cols
        floor_x = min((col_idx + 1) * spacing_x, layout.width_m - 1.0)
        floor_y = min((row_idx + 1) * spacing_y, layout.height_m - 1.0)

        name = item.name.strip() if item.name and item.name.strip() else f"Dahua NVR Ch {ch}"

        cam = CameraModel(
            id=cam_id,
            name=name,
            location=f"Dahua NVR {host} Ch {ch}",
            rtsp_url=stream_url,
            status="STARTING",
            channel_number=current_channel_num,
            department=item.department.upper() if item.department else "GENERAL",
            floor_x=round(floor_x, 2),
            floor_y=round(floor_y, 2),
            floor_z=3.2,
            azimuth_deg=(col_idx * 45) % 360.0,
            fov_deg=85.0,
            is_ai_enabled=True,
            ai_models=["person_detection"],
        )
        db.add(cam)
        adopted.append({
            "camera_id": cam_id,
            "channel": ch,
            "name": name,
            "department": cam.department,
            "stream_url": redact_url(stream_url),
            "quality": "sub" if subtype == 1 else "main",
            "floor_x": cam.floor_x,
            "floor_y": cam.floor_y,
        })

    try:
        await db.commit()
    except Exception:
        for done in stored_creds:
            camera_source.delete_credentials(done)
        raise

    # Reconcile supervisor so feeds start immediately
    try:
        await pipeline_supervisor.reconcile_cameras()
    except Exception as exc:
        logger.error(f"Error reconciling pipeline supervisor after Dahua channel adoption: {exc}")

    return {
        "status": "success",
        "adopted_count": len(adopted),
        "host": host,
        "cameras": adopted,
    }
