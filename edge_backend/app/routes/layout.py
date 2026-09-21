"""Blueprint editor API: store geometry, zone drawing, camera placement, discovery.

Everything the 2D floor plan shows is created and edited through these
endpoints and stored in the database, so an edit survives a restart. The old
floor plan had no editor at all and no persistence -- its geometry lived as
literal arrays in two languages and was rebuilt identically on every page load.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.db_models import (
    AIDecisionRecommendationModel,
    CameraModel,
    CustomerTrackModel,
    DiscoveredDeviceModel,
    DVRSegmentModel,
    IncidentArchiveModel,
    PlanogramItemModel,
    POSTransactionModel,
    RetailAnalyticsSummaryModel,
    SecurityEventModel,
    ShelfInteractionModel,
    StoreStructureModel,
    StoreZoneModel,
    TheftIncidentModel,
    ZoneVisitModel,
)
from app.services.auth_service import auth_service
from app.services.camera_discovery import camera_discovery_service
from app.services.camera_drivers import build_stream_urls, redact_url
from app.services.live_analytics_engine import live_engine
from app.services.pipeline_supervisor import pipeline_supervisor
from app.services.store_layout_service import (
    StoreLayoutError,
    store_layout_service,
)
from app.services.tracking_service import FloorProjector, floor_projector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/layout", tags=["Store Layout"])


# ----------------------------------------------------------------- schemas


class Point(BaseModel):
    x: float
    y: float


class LayoutUpdate(BaseModel):
    name: Optional[str] = None
    width_m: Optional[float] = Field(None, gt=0, le=1000)
    height_m: Optional[float] = Field(None, gt=0, le=1000)


class ZoneCreate(BaseModel):
    name: str
    category: str = "AISLE"
    polygon: list[Point]
    color: str = "#00d4ff"
    sku_id: Optional[str] = None


class ZoneUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    polygon: Optional[list[Point]] = None
    color: Optional[str] = None
    sku_id: Optional[str] = None


class CameraPlacement(BaseModel):
    floor_x: float = Field(..., ge=0)
    floor_y: float = Field(..., ge=0)
    floor_z: Optional[float] = Field(None, ge=0, le=20)
    azimuth_deg: Optional[float] = None
    fov_deg: Optional[float] = Field(None, gt=0, le=360)


class StructureCreate(BaseModel):
    kind: str
    name: str = ""
    polygon: list[Point]
    thickness_m: Optional[float] = None
    color: Optional[str] = None
    properties: Optional[dict[str, Any]] = None


class StructureUpdate(BaseModel):
    kind: Optional[str] = None
    name: Optional[str] = None
    polygon: Optional[list[Point]] = None
    thickness_m: Optional[float] = None
    color: Optional[str] = None
    properties: Optional[dict[str, Any]] = None
    sort_order: Optional[int] = None


class CalibrationRequest(BaseModel):
    """Four or more (image pixel, floor metre) correspondences.

    ``frame_width``/``frame_height`` are the native pixel size the image
    points were clicked in, so the UI can re-scale them onto a differently
    sized preview later.
    """

    image_points: list[Point]
    floor_points: list[Point]
    frame_width: Optional[int] = Field(None, gt=0, le=16384)
    frame_height: Optional[int] = Field(None, gt=0, le=16384)


class CalibrationTestRequest(BaseModel):
    image_points: list[Point]


class ResetRequest(BaseModel):
    """Typed acknowledgement: this wipes everything the operator configured."""

    confirm: str = ""


class DiscoveryRequest(BaseModel):
    subnet: Optional[str] = None
    include_usb: bool = True
    include_network: bool = True


class AdoptRequest(BaseModel):
    device_id: str
    name: str
    location: str = ""
    department: str = "GENERAL"
    username: Optional[str] = None
    password: Optional[str] = None
    channel: int = 1
    quality: str = "sub"
    floor_x: float = 1.0
    floor_y: float = 1.0
    azimuth_deg: float = 90.0
    fov_deg: float = 85.0
    stream_url: Optional[str] = None


# ------------------------------------------------------------------ layout


@router.get("")
async def get_layout(db: AsyncSession = Depends(get_db), _: bool = Depends(auth_service.verify_api_access)):
    """The blueprint: store extent, zones and placed cameras, all in metres."""
    layout = await store_layout_service.get_active_layout(db)
    zones = await store_layout_service.list_zones(db, layout.id)
    structures = await store_layout_service.list_structures(db, layout.id)
    payload = store_layout_service.serialize_layout(layout, zones, structures)
    payload["cameras"] = await store_layout_service.serialize_cameras(db, layout)
    payload["setup"] = store_layout_service.setup_block(zones, structures, payload["cameras"])
    status = live_engine.status()
    payload["pipeline"] = {
        "running": status["running"],
        "cameras_online": status["cameras_online"],
    }
    return payload


@router.put("")
async def update_layout(
    req: LayoutUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    try:
        layout = await store_layout_service.update_layout(
            db, layout.id, name=req.name, width_m=req.width_m, height_m=req.height_m
        )
    except StoreLayoutError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await pipeline_supervisor.reload_layout()
    zones = await store_layout_service.list_zones(db, layout.id)
    return store_layout_service.serialize_layout(layout, zones)


# ------------------------------------------------------------------- zones


@router.post("/zones", status_code=201)
async def create_zone(
    req: ZoneCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    try:
        zone = await store_layout_service.create_zone(
            db,
            layout,
            name=req.name,
            category=req.category,
            polygon=[p.model_dump() for p in req.polygon],
            color=req.color,
            sku_id=req.sku_id,
        )
    except StoreLayoutError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await pipeline_supervisor.reload_layout()
    return store_layout_service.serialize_zone(zone)


@router.put("/zones/{zone_id}")
async def update_zone(
    zone_id: str,
    req: ZoneUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    fields: dict[str, Any] = req.model_dump(exclude_unset=True)
    if "polygon" in fields and fields["polygon"] is not None:
        fields["polygon"] = [p if isinstance(p, dict) else p.model_dump() for p in req.polygon]
    try:
        zone = await store_layout_service.update_zone(db, layout, zone_id, **fields)
    except StoreLayoutError as e:
        raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))
    await pipeline_supervisor.reload_layout()
    return store_layout_service.serialize_zone(zone)


@router.delete("/zones/{zone_id}")
async def delete_zone(
    zone_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    if not await store_layout_service.delete_zone(db, layout.id, zone_id):
        raise HTTPException(status_code=404, detail="zone not found")
    await pipeline_supervisor.reload_layout()
    return {"deleted": zone_id}


# -------------------------------------------------------------- structures
#
# Rooms, walls, shelves, counters, doors, obstacles: what the operator draws
# so the plan looks like their store. Purely geometric -- the workers never
# see them, so no layout republish is needed after an edit.


@router.get("/structures")
async def list_structures(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    structures = await store_layout_service.list_structures(db, layout.id)
    return {
        "structures": [store_layout_service.serialize_structure(s) for s in structures],
        "total": len(structures),
    }


@router.post("/structures", status_code=201)
async def create_structure(
    req: StructureCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    try:
        structure = await store_layout_service.create_structure(
            db,
            layout,
            kind=req.kind,
            name=req.name,
            polygon=[p.model_dump() for p in req.polygon],
            thickness_m=req.thickness_m,
            color=req.color,
            properties=req.properties,
        )
    except StoreLayoutError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return store_layout_service.serialize_structure(structure)


@router.put("/structures/{structure_id}")
async def update_structure(
    structure_id: str,
    req: StructureUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    fields: dict[str, Any] = req.model_dump(exclude_unset=True)
    if fields.get("polygon") is not None:
        fields["polygon"] = [p if isinstance(p, dict) else p.model_dump() for p in req.polygon]
    try:
        structure = await store_layout_service.update_structure(db, layout, structure_id, **fields)
    except StoreLayoutError as e:
        raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))
    return store_layout_service.serialize_structure(structure)


@router.delete("/structures/{structure_id}")
async def delete_structure(
    structure_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    layout = await store_layout_service.get_active_layout(db)
    if not await store_layout_service.delete_structure(db, layout.id, structure_id):
        raise HTTPException(status_code=404, detail="structure not found")
    return {"deleted": True, "structure_id": structure_id}


# ---------------------------------------------------------- camera placement


@router.patch("/cameras/{camera_id}/placement")
async def place_camera(
    camera_id: str,
    req: CameraPlacement,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Move or re-aim a camera on the blueprint. Coordinates are metres.

    This write is durable. The previous implementation persisted to the same
    row but ``init_db`` overwrote every seeded camera's coordinates back to a
    hardcoded literal on each restart, so repositioning never actually stuck.
    """
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    layout = await store_layout_service.get_active_layout(db)
    cam.floor_x = min(max(req.floor_x, 0.0), layout.width_m)
    cam.floor_y = min(max(req.floor_y, 0.0), layout.height_m)
    if req.floor_z is not None:
        cam.floor_z = req.floor_z
    if req.azimuth_deg is not None:
        cam.azimuth_deg = req.azimuth_deg % 360.0
    if req.fov_deg is not None:
        cam.fov_deg = req.fov_deg
    await db.commit()
    await db.refresh(cam)
    return {
        "camera_id": cam.id,
        "floor_x": cam.floor_x,
        "floor_y": cam.floor_y,
        "floor_z": cam.floor_z,
        "azimuth_deg": cam.azimuth_deg,
        "fov_deg": cam.fov_deg,
    }


@router.post("/cameras/{camera_id}/calibrate")
async def calibrate_camera(
    camera_id: str,
    req: CalibrationRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Bind a camera's image plane to the store floor.

    Until a camera is calibrated its detections cannot be placed on the
    blueprint, so it contributes counts but no positions and appears as
    uncalibrated in the UI rather than dropping its people at the origin.
    """
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    if len(req.image_points) < 4 or len(req.image_points) != len(req.floor_points):
        raise HTTPException(
            status_code=400,
            detail="at least 4 matching image/floor point pairs are required",
        )

    H = FloorProjector.estimate_homography(
        [(p.x, p.y) for p in req.image_points],
        [(p.x, p.y) for p in req.floor_points],
    )
    if H is None:
        raise HTTPException(
            status_code=400,
            detail="points are degenerate (collinear or coincident); pick 4 well-spread floor points",
        )

    # Record the frame size the points were clicked in. The worker's live size
    # is preferred because it is what the pipeline actually reads; the client
    # value covers a camera that is offline at calibration time.
    rt = live_engine.runtimes.get(camera_id)
    frame_w = (rt.frame_width if rt is not None and rt.frame_width else None) or req.frame_width
    frame_h = (rt.frame_height if rt is not None and rt.frame_height else None) or req.frame_height

    cam.homography_matrix = H
    cam.calibration_points = {
        "image_points": [{"x": p.x, "y": p.y} for p in req.image_points],
        "floor_points": [{"x": p.x, "y": p.y} for p in req.floor_points],
        "frame_width": frame_w,
        "frame_height": frame_h,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    await db.commit()
    floor_projector.set_homography(camera_id, H)
    return {
        "camera_id": camera_id,
        "homography_matrix": H,
        "calibrated": True,
        "calibration_points": cam.calibration_points,
    }


@router.delete("/cameras/{camera_id}/calibrate")
async def clear_calibration(
    camera_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    cam.homography_matrix = None
    cam.calibration_points = None
    await db.commit()
    floor_projector.set_homography(camera_id, None)
    return {"camera_id": camera_id, "calibrated": False}


def _ensure_projector_loaded(cam: CameraModel) -> None:
    """Make the in-memory projector agree with the stored matrix.

    The projector is normally populated by the reconcile loop; a calibration
    saved moments ago or read before the first reconcile tick would otherwise
    project as "uncalibrated" despite a valid stored matrix.
    """
    if cam.homography_matrix and not floor_projector.has_homography(cam.id):
        floor_projector.set_homography(cam.id, cam.homography_matrix)


@router.get("/cameras/{camera_id}/calibration")
async def get_calibration(
    camera_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """The stored homography and the point pairs it was solved from."""
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    _ensure_projector_loaded(cam)
    rt = live_engine.runtimes.get(camera_id)
    frame_w, frame_h = store_layout_service.known_frame_size(cam, rt)
    return {
        "camera_id": camera_id,
        "has_homography": bool(cam.homography_matrix),
        "homography_matrix": cam.homography_matrix,
        "calibration_points": cam.calibration_points,
        "frame_width": frame_w,
        "frame_height": frame_h,
    }


@router.post("/cameras/{camera_id}/calibration/test")
async def test_calibration(
    camera_id: str,
    req: CalibrationTestRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Project image points through the stored homography.

    Lets the operator click anywhere in the camera view and see where that
    pixel lands on the plan. Without a homography every point comes back as
    null; nothing is guessed.
    """
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")
    _ensure_projector_loaded(cam)
    calibrated = floor_projector.has_homography(camera_id)
    out: list[Optional[dict]] = []
    for p in req.image_points:
        floor = floor_projector.to_floor(camera_id, p.x, p.y) if calibrated else None
        out.append(None if floor is None else {"x": round(floor[0], 3), "y": round(floor[1], 3)})
    return {"camera_id": camera_id, "calibrated": calibrated, "floor_points": out}


# --------------------------------------------------------------- discovery


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Devices the last scan found, and whether each is already in use."""
    res = await db.execute(select(DiscoveredDeviceModel).order_by(DiscoveredDeviceModel.last_seen.desc()))
    devices = []
    for d in res.scalars().all():
        devices.append(
            {
                "id": d.id,
                "transport": d.transport,
                "driver": d.driver,
                "host": d.host,
                "port": d.port,
                "device_path": d.device_path,
                "model_name": d.model_name,
                "manufacturer": d.manufacturer,
                "channels": d.channels,
                "requires_credentials": d.requires_credentials,
                "reachable": d.reachable,
                "adopted_camera_id": d.adopted_camera_id,
                "stream_urls": [
                    {**s, "url": redact_url(s.get("url", ""))} for s in (d.stream_urls or [])
                ],
                "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            }
        )
    return {"devices": devices, "total": len(devices)}


@router.post("/discover")
async def discover_devices(
    req: DiscoveryRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Scan for cameras: USB, ONVIF WS-Discovery, mDNS and an RTSP sweep.

    Returns only devices that actually answered. An empty result means no
    camera responded, and is reported as such.
    """
    try:
        found = await camera_discovery_service.discover(
            subnet=req.subnet,
            include_usb=req.include_usb,
            include_network=req.include_network,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    adopted_map = {}
    res = await db.execute(select(DiscoveredDeviceModel))
    for existing in res.scalars().all():
        adopted_map[existing.id] = existing.adopted_camera_id

    now = datetime.utcnow()
    for dev in found:
        row = await db.get(DiscoveredDeviceModel, dev.id)
        payload = dev.to_dict()
        if row is None:
            row = DiscoveredDeviceModel(id=dev.id, first_seen=now)
            db.add(row)
        row.transport = payload["transport"]
        row.driver = payload["driver"]
        row.host = payload["host"]
        row.port = payload["port"]
        row.device_path = payload["device_path"]
        row.model_name = payload["model_name"]
        row.manufacturer = payload["manufacturer"]
        row.channels = payload["channels"]
        row.requires_credentials = payload["requires_credentials"]
        row.stream_urls = payload["stream_urls"]
        row.reachable = True
        row.last_seen = now
        row.adopted_camera_id = adopted_map.get(dev.id)

    # Anything not seen this pass is marked unreachable rather than deleted,
    # so a camera that is merely powered off keeps its configuration.
    seen_ids = {d.id for d in found}
    res = await db.execute(select(DiscoveredDeviceModel))
    for row in res.scalars().all():
        if row.id not in seen_ids:
            row.reachable = False

    await db.commit()
    return {
        "found": len(found),
        "devices": [
            {**d.to_dict(), "stream_urls": [
                {**s, "url": redact_url(s["url"])} for s in d.to_dict()["stream_urls"]
            ]}
            for d in found
        ],
    }


@router.post("/devices/adopt", status_code=201)
async def adopt_device(
    req: AdoptRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Turn a discovered device into an active camera on the blueprint.

    This is how cameras enter the system now -- one at a time, from something
    that genuinely responded on the network, rather than from a fixed list of
    32 fabricated entries re-seeded on every boot.
    """
    device = await db.get(DiscoveredDeviceModel, req.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found; run a scan first")

    if device.transport == "usb":
        stream_url = device.device_path or ""
    elif req.stream_url:
        stream_url = req.stream_url
    else:
        profiles = build_stream_urls(
            device.driver,
            device.host or "",
            username=req.username,
            password=req.password,
            channel=req.channel,
            port=device.port,
        )
        preferred = [p for p in profiles if p.quality == req.quality] or profiles
        if not preferred:
            raise HTTPException(status_code=400, detail="could not build a stream URL for this device")
        stream_url = preferred[0].url

    if not stream_url:
        raise HTTPException(status_code=400, detail="device has no usable stream address")

    layout = await store_layout_service.get_active_layout(db)
    cam_id = f"cam_{uuid.uuid4().hex[:10]}"
    max_ch = await db.scalar(select(CameraModel.channel_number).order_by(CameraModel.channel_number.desc()).limit(1))

    cam = CameraModel(
        id=cam_id,
        name=req.name.strip() or f"Camera {(max_ch or 0) + 1}",
        location=req.location or (device.host or device.device_path or ""),
        rtsp_url=stream_url,
        status="STARTING",
        channel_number=(max_ch or 0) + 1,
        department=req.department.upper(),
        floor_x=min(max(req.floor_x, 0.0), layout.width_m),
        floor_y=min(max(req.floor_y, 0.0), layout.height_m),
        floor_z=3.0,
        azimuth_deg=req.azimuth_deg % 360.0,
        fov_deg=req.fov_deg,
        is_ai_enabled=True,
        ai_models=["person_detection"],
    )
    db.add(cam)
    device.adopted_camera_id = cam_id
    await db.commit()

    # Bring the new camera online immediately rather than waiting for the
    # next reconcile tick, so the operator sees video straight away.
    await pipeline_supervisor.reconcile_cameras()

    return {
        "camera_id": cam_id,
        "name": cam.name,
        "stream_url": redact_url(stream_url),
        "floor_x": cam.floor_x,
        "floor_y": cam.floor_y,
    }


@router.delete("/cameras/{camera_id}")
async def remove_camera(
    camera_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Remove a camera from the store. Frees its device for re-adoption."""
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="camera not found")

    res = await db.execute(
        select(DiscoveredDeviceModel).where(DiscoveredDeviceModel.adopted_camera_id == camera_id)
    )
    for dev in res.scalars().all():
        dev.adopted_camera_id = None

    await db.delete(cam)
    await db.commit()
    live_engine.stop_camera(camera_id)
    return {"removed": camera_id}


@router.get("/pipeline/status")
async def pipeline_status(_: bool = Depends(auth_service.verify_api_access)):
    """What the live pipeline is actually doing, per camera."""
    return live_engine.status()


@router.get("/live")
async def live_positions(_: bool = Depends(auth_service.verify_api_access)):
    """Where every tracked person is right now, in store metres.

    ``persons`` lists only confirmed tracks from calibrated cameras, because
    those are the only ones with a real floor position. ``detections`` gives
    every camera's image-space boxes regardless, for overlaying on its feed.
    With no cameras both lists are empty and ``running`` says whether the
    pipeline is up at all.
    """
    return live_engine.live_snapshot()


# -------------------------------------------------------------- fresh state

# Everything the operator configured or the pipeline observed, in an order
# that never leaves a child row pointing at a deleted parent.
_RESET_TABLES: list[tuple[str, Any]] = [
    ("zone_visits", ZoneVisitModel),
    ("customer_tracks", CustomerTrackModel),
    ("shelf_interactions", ShelfInteractionModel),
    ("retail_analytics_summaries", RetailAnalyticsSummaryModel),
    ("ai_decision_recommendations", AIDecisionRecommendationModel),
    ("theft_incidents", TheftIncidentModel),
    ("pos_transactions", POSTransactionModel),
    ("planogram_items", PlanogramItemModel),
    ("security_events", SecurityEventModel),
    ("dvr_segments", DVRSegmentModel),
    ("incident_archives", IncidentArchiveModel),
    ("cameras", CameraModel),
    ("discovered_devices", DiscoveredDeviceModel),
    ("store_zones", StoreZoneModel),
    ("store_structures", StoreStructureModel),
]


async def _reset_store(db: AsyncSession) -> dict[str, int]:
    """Return the installation to its first-run state. Returns rows removed."""
    from app.services.feature_manager import feature_manager

    # Stop every worker first so nothing writes against a row being deleted,
    # and forget every homography so a re-adopted camera starts uncalibrated.
    for cam_id in list(live_engine.runtimes.keys()):
        live_engine.stop_camera(cam_id)
        floor_projector.set_homography(cam_id, None)
    res = await db.execute(select(CameraModel.id))
    for (cam_id,) in res.all():
        floor_projector.set_homography(cam_id, None)

    removed: dict[str, int] = {}
    for label, model in _RESET_TABLES:
        count = await db.scalar(select(func.count()).select_from(model))
        if count:
            await db.execute(delete(model))
        removed[label] = int(count or 0)

    layout = await store_layout_service.get_active_layout(db)
    layout.name = "Store Floor"
    layout.width_m = settings.DEFAULT_STORE_WIDTH_M
    layout.height_m = settings.DEFAULT_STORE_HEIGHT_M
    layout.updated_at = datetime.utcnow()
    await db.commit()

    # In-memory per-camera feature flags belong to cameras that no longer exist.
    with feature_manager._lock:
        feature_manager._camera_features.clear()
    # The JSON-backed per-camera overlays (tripwires, intrusion polygons,
    # privacy masks, product shelf areas) reference cameras that are gone too.
    try:
        from app.services.ai_zone_service import ai_zone_service
        from app.services.shelf_interaction_service import shelf_interaction_service

        removed["camera_overlays"] = ai_zone_service.clear_all() or 0
        removed["shelf_product_zones"] = shelf_interaction_service.clear_all()
    except Exception as e:  # a missing config file must not abort the reset
        logger.warning(f"reset: could not clear JSON overlay stores: {e}")
    # Anything the workers buffered but the flush loop had not yet written
    # describes rows that were just deleted.
    live_engine.drain()

    await pipeline_supervisor.reconcile_cameras()
    await pipeline_supervisor.reload_layout()
    logger.warning(f"Operator reset the store to fresh state: {removed}")
    return removed


@router.post("/reset")
async def reset_store(
    req: ResetRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Wipe the blueprint, cameras and every observation; keep the settings.

    Requires the literal string ``RESET`` in ``confirm`` so a stray click
    cannot erase a configured store. The active layout row survives but is
    returned to its defaults, so the editor still has a canvas.
    """
    if req.confirm != "RESET":
        raise HTTPException(
            status_code=400,
            detail='set confirm to "RESET" to acknowledge this permanently deletes the store configuration and all observations',
        )
    removed = await _reset_store(db)
    layout = await store_layout_service.get_active_layout(db)
    return {
        "reset": True,
        "removed": removed,
        "total_rows": sum(removed.values()),
        "layout": store_layout_service.serialize_layout(layout, [], []),
    }


class PurgeRequest(BaseModel):
    """Explicit opt-in to clearing pre-existing fabricated records."""

    confirm: bool = False


@router.post("/purge-seed-data")
async def purge_seed_data(
    req: PurgeRequest,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Legacy alias for ``POST /reset``.

    Earlier builds seeded fixed cameras, hand-written AI recommendations and
    theft incidents on every boot, and this route removed them. It now
    performs the same full reset as ``/reset`` and is kept only so existing
    callers keep working.
    """
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="set confirm=true to acknowledge this permanently deletes records",
        )
    removed = await _reset_store(db)
    return {"purged": removed, "removed": removed, "total_rows": sum(removed.values())}
