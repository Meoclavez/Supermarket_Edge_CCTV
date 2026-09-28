"""Recorder sub-stream upgrade (CIF -> D1) and per-camera stream selection.

Recorders are the Dahua hosts the configured cameras point at
(``rtsp://<host>:554/cam/realmonitor?channel=N&subtype=K``); the recorder id
is that host. Only hosts that cameras already use are accepted, so these
routes cannot be pointed at an arbitrary address.

* GET  /api/v1/recorders                              recorders and their channels
* GET  /api/v1/recorders/{id}/substreams              read-only: current sub-stream per channel and the plan
* POST /api/v1/recorders/{id}/substreams/d1           start the upgrade (body: channels or all_cif)
* GET  /api/v1/recorders/{id}/substreams/d1           progress / last result (+ recent audit entries)
* POST /api/v1/recorders/{id}/substreams/bitrate      raise the sub-stream bit rate (body: channels or all_below, kbps)
* GET  /api/v1/recorders/{id}/substreams/bitrate      progress / last result (same as .../d1)
* POST /api/v1/recorders/{id}/substreams/restore      write saved settings back (body: channels or all)
* GET  /api/v1/cameras/{id}/stream-selection          chosen stream, size and reason
* POST /api/v1/cameras/{id}/stream-selection/measure  re-measure the camera's sub-streams (read-only)
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import CameraModel
from app.services.actor import describe_actor
from app.services.auth_service import auth_service
from app.services.recorder_substreams import (
    BITRATE_MAX_KBPS,
    BITRATE_MIN_KBPS,
    RecorderBusy,
    cameras_by_recorder,
    recorder_credentials,
    recorder_substreams,
)
from app.services.stream_selection import stream_selection

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/recorders",
    tags=["Recorders"],
    dependencies=[Depends(auth_service.verify_api_access)],
)
camera_stream_router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["Cameras"],
    dependencies=[Depends(auth_service.verify_api_access)],
)


class SubstreamUpgradeRequest(BaseModel):
    channels: Optional[List[int]] = Field(None, description="Channel numbers (1-based) to upgrade")
    all_cif: bool = Field(False, description="Every channel of this recorder's cameras whose sub-stream is below D1")
    http_port: int = Field(80, ge=1, le=65535, description="The recorder's web (HTTP) port")
    try_when_caps_unknown: bool = Field(
        False, description="Also try D1 on channels whose supported sizes cannot be read; each change is "
                           "verified on the stream and restored if the camera does not deliver D1")

    @model_validator(mode="after")
    def _one_of(self):
        if not self.all_cif and not self.channels:
            raise ValueError("send channels or all_cif: true")
        return self


class SubstreamBitrateRequest(BaseModel):
    channels: Optional[List[int]] = Field(None, description="Channel numbers (1-based) to change")
    all_below: bool = Field(False, description="Every channel of this recorder's cameras whose sub-stream bit rate is below kbps")
    kbps: int = Field(768, ge=BITRATE_MIN_KBPS, le=BITRATE_MAX_KBPS, description="Target sub-stream bit rate (kbps)")
    allow_lower: bool = Field(False, description="With explicit channels only: also lower channels above kbps")
    http_port: int = Field(80, ge=1, le=65535, description="The recorder's web (HTTP) port")

    @model_validator(mode="after")
    def _one_of(self):
        if not self.all_below and not self.channels:
            raise ValueError("send channels or all_below: true")
        return self


class SubstreamRestoreRequest(BaseModel):
    channels: Optional[List[int]] = Field(None, description="Channels to restore; empty with all=true = every saved one")
    all: bool = False
    http_port: int = Field(80, ge=1, le=65535)

    @model_validator(mode="after")
    def _one_of(self):
        if not self.all and not self.channels:
            raise ValueError("send channels or all: true")
        return self


def _cam_dict(c: CameraModel) -> dict:
    features = c.features if isinstance(c.features, dict) else {}
    return {"id": c.id, "name": c.name, "rtsp_url": c.rtsp_url or "", "enabled": bool(c.is_ai_enabled),
            "stream_quality": features.get("stream_quality")}


async def _recorders(db: AsyncSession) -> dict[str, list[dict]]:
    res = await db.execute(select(CameraModel))
    return cameras_by_recorder(_cam_dict(c) for c in res.scalars().all())


async def _recorder_or_404(recorder_id: str, db: AsyncSession) -> tuple[str, list[dict]]:
    groups = await _recorders(db)
    cams = groups.get(recorder_id.strip())
    if not cams:
        raise HTTPException(status_code=404, detail="No camera uses this recorder (id = recorder address).")
    return recorder_id.strip(), cams


def _creds_or_409(host: str, cams: list[dict]) -> tuple[str, str]:
    user, pw, _ = recorder_credentials(host, cams)
    if not pw:
        raise HTTPException(status_code=409, detail="No sign-in is saved for this recorder. Save it under "
                                                    "Cameras & devices -> Dahua recorder first.")
    return user, pw


def _public_cam(c: dict) -> dict:
    return {"camera_id": c["id"], "name": c.get("name"), "channel": c["channel"], "subtype": c["subtype"],
            "enabled": c.get("enabled", True), "stream_quality": c.get("stream_quality") or "auto",
            "stream_selection": stream_selection.status(c["id"])}


def _validate_channels(channels: Optional[List[int]], cams: list[dict]) -> Optional[list[int]]:
    if channels is None:
        return None
    used = {c["channel"] for c in cams}
    bad = sorted({int(ch) for ch in channels} - used)
    if bad:
        raise HTTPException(status_code=400, detail=f"Channel(s) {bad} are not used by any camera on this recorder.")
    return sorted({int(ch) for ch in channels})


@router.get("")
async def list_recorders(db: AsyncSession = Depends(get_db)):
    groups = await _recorders(db)
    out = []
    for host, cams in sorted(groups.items()):
        _, pw, source = recorder_credentials(host, cams)
        out.append({
            "id": host, "host": host, "has_credentials": bool(pw), "credentials_from": source or None,
            "channels": sorted({c["channel"] for c in cams}),
            "cameras": [_public_cam(c) for c in cams],
            "running": recorder_substreams.is_running(host),
            "saved_channels": sorted(int(k) for k in recorder_substreams.backups(host)),
        })
    return {"recorders": out}


@router.get("/{recorder_id}/substreams")
async def preview_substreams(recorder_id: str, http_port: int = 80, db: AsyncSession = Depends(get_db)):
    """Read-only: each channel's current sub-stream and what the upgrade would do."""
    host, cams = await _recorder_or_404(recorder_id, db)
    if recorder_substreams.is_running(host):
        raise HTTPException(status_code=409, detail="A sub-stream change is running on this recorder.")
    user, pw = _creds_or_409(host, cams)
    return await recorder_substreams.preview(host, cams, username=user, password=pw, http_port=http_port)


@router.post("/{recorder_id}/substreams/d1", status_code=202)
async def start_d1_upgrade(recorder_id: str, body: SubstreamUpgradeRequest, request: Request,
                           db: AsyncSession = Depends(get_db)):
    host, cams = await _recorder_or_404(recorder_id, db)
    channels = None if body.all_cif else _validate_channels(body.channels, cams)
    user, pw = _creds_or_409(host, cams)
    actor = await describe_actor(request, db)
    try:
        run = recorder_substreams.start("upgrade", host, cams, channels, actor,
                                        username=user, password=pw, http_port=body.http_port,
                                        try_unknown=body.try_when_caps_unknown)
    except RecorderBusy as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return run


@router.post("/{recorder_id}/substreams/bitrate", status_code=202)
async def start_bitrate_raise(recorder_id: str, body: SubstreamBitrateRequest, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """Raise each chosen channel's sub-stream bit rate (only the bit rate), verified, restored on failure."""
    host, cams = await _recorder_or_404(recorder_id, db)
    channels = None if body.all_below else _validate_channels(body.channels, cams)
    user, pw = _creds_or_409(host, cams)
    actor = await describe_actor(request, db)
    try:
        run = recorder_substreams.start("bitrate", host, cams, channels, actor,
                                        username=user, password=pw, http_port=body.http_port,
                                        kbps=body.kbps, allow_lower=body.allow_lower and channels is not None)
    except RecorderBusy as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return run


@router.get("/{recorder_id}/substreams/bitrate")
@router.get("/{recorder_id}/substreams/d1")
async def get_d1_result(recorder_id: str, db: AsyncSession = Depends(get_db)):
    host, _ = await _recorder_or_404(recorder_id, db)
    return {"recorder": host, "running": recorder_substreams.is_running(host),
            "run": recorder_substreams.last_run(host),
            "saved_channels": sorted(int(k) for k in recorder_substreams.backups(host)),
            "audit": recorder_substreams.audit_entries(host, 30)}


@router.post("/{recorder_id}/substreams/restore", status_code=202)
async def start_restore(recorder_id: str, body: SubstreamRestoreRequest, request: Request,
                        db: AsyncSession = Depends(get_db)):
    host, cams = await _recorder_or_404(recorder_id, db)
    channels = None if body.all else _validate_channels(body.channels, cams)
    if not recorder_substreams.backups(host):
        raise HTTPException(status_code=409, detail="No saved sub-stream settings for this recorder.")
    user, pw = _creds_or_409(host, cams)
    actor = await describe_actor(request, db)
    try:
        run = recorder_substreams.start("restore", host, cams, channels, actor,
                                        username=user, password=pw, http_port=body.http_port)
    except RecorderBusy as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return run


# ------------------------------------------------------------- stream selection

async def _camera_or_404(camera_id: str, db: AsyncSession) -> CameraModel:
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return cam


@camera_stream_router.get("/{camera_id}/stream-selection")
async def get_stream_selection(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await _camera_or_404(camera_id, db)
    d = _cam_dict(cam)
    decision = stream_selection.decide(cam.id, d["rtsp_url"], d["stream_quality"])
    return {**decision.to_dict(), "measuring": stream_selection.probing == cam.id,
            "measured": {st: stream_selection.measured(cam.id, d["rtsp_url"], st) for st in (1, 2)}}


@camera_stream_router.post("/{camera_id}/stream-selection/measure")
async def measure_stream_selection(camera_id: str, db: AsyncSession = Depends(get_db)):
    """Measure the camera's sub-streams now (one frame each, read-only), then apply the choice."""
    cam = await _camera_or_404(camera_id, db)
    d = _cam_dict(cam)
    if stream_selection.probing is not None:
        raise HTTPException(status_code=409, detail="Another camera's streams are being measured; try again shortly.")
    result = await stream_selection.measure_camera(cam.id, d["rtsp_url"])
    if result.get("error"):
        raise HTTPException(status_code=400, detail=f"Cannot measure: {result['error']}.")
    decision = stream_selection.decide(cam.id, d["rtsp_url"], d["stream_quality"])
    try:
        from app.services.pipeline_supervisor import pipeline_supervisor

        if getattr(pipeline_supervisor, "_running", False):
            await pipeline_supervisor.reconcile_cameras()
    except Exception as exc:  # noqa: BLE001 - the periodic reconcile applies it
        logger.debug(f"reconcile after measuring {camera_id}: {exc}")
    return {**decision.to_dict(), "measured": result.get("measured")}
