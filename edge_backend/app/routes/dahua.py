"""Dahua NVR Management, Channel Discovery, and Credential Persistence API.

Provides dedicated endpoints for Dahua NVR/DVR multi-channel systems:
- Persistent credential storage (survives restarts/updates)
- Fast network probe & channel sweep (detects which channels are active)
- Bulk channel adoption onto the store blueprint with sub/main stream preferences
"""

from __future__ import annotations

import logging
import re
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

# Same rule as a camera added by URL (routes/cameras.py).
_DEPARTMENT_RE = re.compile(r"^[A-Z0-9 _&/-]{1,40}$")

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
    http_port: int = Field(default=80, ge=1, le=65535,
                           description="The recorder's web (HTTP) port, used read-only for channel names")
    save_credentials: bool = Field(default=True, description="Persist these credentials on disk if successful")


class ChannelAdoptItem(BaseModel):
    channel: int = Field(..., ge=1, le=256)
    # The operator's name for the camera (the dashboard pre-fills the channel
    # name read from the recorder). Empty: "Recorder channel <N>".
    name: Optional[str] = Field(default=None, max_length=120)
    # Optional label. Empty is stored as GENERAL, the camera table's "no
    # department" value (the same as a camera added by URL).
    department: Optional[str] = None
    # Camera purpose (services/camera_roles.py). None: no purpose; nothing is
    # assigned automatically.
    role: Optional[str] = None
    quality: str = "sub"  # "sub" (subtype=1) or "main" (subtype=0)


class DahuaBatchAdoptPayload(BaseModel):
    host: str
    port: int = 554
    username: Optional[str] = None
    password: Optional[str] = None
    channels: List[ChannelAdoptItem] = Field(..., description="Channels to adopt")
    # Channels that are already configured (same recorder channel on another
    # stream, or the camera it connects to) are refused with 409 unless set.
    allow_duplicate: bool = False


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
        http_port=payload.http_port,
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

    from app.models.schemas import CameraFeatureConfig
    from app.services import camera_roles

    # Validate every channel before anything (credentials included) is stored.
    roles: Dict[int, Optional[str]] = {}
    departments: Dict[int, str] = {}
    for idx, item in enumerate(payload.channels):
        try:
            roles[idx] = camera_roles.normalise_role(item.role)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Channel {item.channel}: {exc}") from None
        dept = (item.department or "").strip().upper() or "GENERAL"
        if not _DEPARTMENT_RE.match(dept):
            raise HTTPException(status_code=422, detail=(
                f"Channel {item.channel}: department may only contain letters, digits, spaces and _&/-."))
        departments[idx] = dept

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

    from app.services.dahua_probe_service import build_dahua_url
    from app.services.duplicate_cameras import duplicate_guard

    # Duplicate guard: before anything is stored, every requested channel is
    # checked against the configured cameras and the earlier channels of this
    # request. All-or-nothing: a 409 lists each conflicting channel.
    pending: List[Dict[str, Any]] = []
    clashes: List[Dict[str, Any]] = []
    for item in payload.channels:
        subtype = 0 if item.quality == "main" else 1
        url = build_dahua_url(host=host, port=payload.port, channel=item.channel, subtype=subtype)
        found = duplicate_guard.conflicts(url, extra=pending)
        if found:
            clashes.append({"channel": item.channel, "existing": found,
                            "detail": duplicate_guard.conflict_detail(url, found)["message"]})
        pending.append({"id": f"pending-ch{item.channel}-{len(pending)}", "name": f"channel {item.channel} (this request)",
                        "rtsp_url": url, "location": f"Dahua NVR {host} Ch {item.channel}"})
    if clashes and not payload.allow_duplicate:
        chans = ", ".join(str(c["channel"]) for c in clashes)
        raise HTTPException(status_code=409, detail={
            "code": "duplicate_camera",
            "message": (f"Channel(s) {chans} of {host} are already added as cameras. Use the existing cameras "
                        "(leave those channels out), or add them anyway only for a genuine second stream; "
                        "the copies are then excluded from store totals."),
            "channels": clashes,
            "hint": "Send allow_duplicate: true to add them anyway.",
        })

    layout = await store_layout_service.get_active_layout(db)
    max_ch = await db.scalar(select(CameraModel.channel_number).order_by(CameraModel.channel_number.desc()).limit(1))
    current_channel_num = max_ch or 0

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

        name = " ".join((item.name or "").split())[:120] or f"Recorder channel {ch}"
        role = roles[i]

        cam = CameraModel(
            id=cam_id,
            name=name,
            location=f"Dahua NVR {host} Ch {ch}",
            rtsp_url=stream_url,
            status="STARTING",
            channel_number=current_channel_num,
            department=departments[i],
            floor_x=round(floor_x, 2),
            floor_y=round(floor_y, 2),
            floor_z=3.2,
            azimuth_deg=(col_idx * 45) % 360.0,
            fov_deg=85.0,
            is_ai_enabled=True,
            ai_models=["person_detection"],
        )
        if role:
            # The purpose the operator picked, with its default analysis
            # switches (as when a camera is added with a purpose).
            cam.role = role
            cam.features = CameraFeatureConfig.model_validate(camera_roles.default_features(role)).model_dump()
        db.add(cam)
        adopted.append({
            "camera_id": cam_id,
            "channel": ch,
            "name": name,
            "department": cam.department,
            "role": role,
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
    duplicate_guard.invalidate()
    camera_roles.role_cache.invalidate()

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
