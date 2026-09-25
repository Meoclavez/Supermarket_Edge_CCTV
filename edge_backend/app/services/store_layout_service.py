"""Store blueprint geometry: the single source of truth for the 2D floor plan.

Before this service existed the blueprint was defined three separate times --
``STORE_ZONES`` in ``routes/analytics.py`` (normalised 0-1 polygons over a
1000x800 space), ``SUPERMARKET_ZONES`` in the orphaned seed generator (metres
over 50x30), and a literal ``this.zones`` array in ``static/js/floorplan.js``
(absolute pixels over 1200x750). None of the three were kept in sync, none were
persisted, and each carried its own invented footfall/dwell numbers.

This module replaces all three. There is now exactly one blueprint, stored in
``store_layouts`` / ``store_zones``, expressed in a single coordinate system:

    Real-world METRES. Origin at the top-left of the floor plan.
    x increases to the right, y increases downward.

Pixels exist only inside the renderer, which scales metres to the canvas.
Zone rows carry geometry and identity only; every metric shown against a zone
is derived from ``zone_visits`` at query time, never stored on the zone.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import (
    CameraModel,
    StoreLayoutModel,
    StoreStructureModel,
    StoreZoneModel,
)

logger = logging.getLogger(__name__)

# Zone kinds the analytics layer understands. ENTRANCE and CHECKOUT carry
# special meaning in the funnel (store entry count, conversion point); SHELF
# zones may bind a SKU for planogram/ROI work; AISLE and DEPARTMENT are
# ordinary dwell regions; EXCLUDED is never counted at all.
ZONE_CATEGORIES = (
    "ENTRANCE",
    "EXIT",
    "AISLE",
    "DEPARTMENT",
    "SHELF",
    "CHECKOUT",
    "STOCKROOM",
    "EXCLUDED",
)


class StoreLayoutError(ValueError):
    """Raised when a caller supplies geometry the store cannot accept."""


# Structure kinds: what the operator draws so the plan resembles their store.
# WALL and DOOR are polylines with a thickness; the rest are closed polygons.
# None of these affect analytics -- zones remain the attribution regions.
STRUCTURE_KINDS = ("ROOM", "WALL", "SHELF", "COUNTER", "DOOR", "OBSTACLE")
POLYLINE_KINDS = ("WALL", "DOOR")

STRUCTURE_DEFAULT_COLORS = {
    "ROOM": "#3a4358",
    "WALL": "#c7ccd8",
    "SHELF": "#7b8cff",
    "COUNTER": "#f2a541",
    "DOOR": "#5ad38a",
    "OBSTACLE": "#8b93a7",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean_points(polygon: Any, width_m: float, height_m: float, min_points: int, what: str) -> list[dict]:
    """Shared coercion + clamp for zone polygons and structure geometry."""
    if not isinstance(polygon, Iterable) or isinstance(polygon, (str, bytes)):
        raise StoreLayoutError(f"{what} must be a list of points")

    cleaned: list[dict] = []
    for i, pt in enumerate(polygon):
        if isinstance(pt, dict):
            x, y = pt.get("x"), pt.get("y")
        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
            x, y = pt[0], pt[1]
        else:
            raise StoreLayoutError(f"point {i} is not a coordinate")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise StoreLayoutError(f"point {i} has non-numeric coordinates")
        if not (math.isfinite(x) and math.isfinite(y)):
            raise StoreLayoutError(f"point {i} is not a finite coordinate")
        # Clamp rather than reject: a drag that overshoots the wall by a few
        # centimetres is an ordinary editing gesture, not an error worth
        # discarding the whole shape over.
        x = min(max(x, 0.0), width_m)
        y = min(max(y, 0.0), height_m)
        cleaned.append({"x": round(x, 3), "y": round(y, 3)})

    if len(cleaned) < min_points:
        raise StoreLayoutError(f"{what} needs at least {min_points} points")
    return cleaned


def validate_structure(kind: Any, polygon: Any, width_m: float, height_m: float) -> tuple[str, list[dict]]:
    """Validate a structure's kind and geometry. Returns (kind, points).

    Mirrors ``validate_polygon``: clamp to the store bounds, enforce the
    minimum vertex count for the kind, and reject unknown kinds.
    """
    kind = str(kind or "").upper()
    if kind not in STRUCTURE_KINDS:
        raise StoreLayoutError(
            f"unknown structure kind '{kind}'; expected one of {', '.join(STRUCTURE_KINDS)}"
        )
    if kind in POLYLINE_KINDS:
        pts = _clean_points(polygon, width_m, height_m, 2, f"a {kind.lower()} polyline")
    else:
        pts = _clean_points(polygon, width_m, height_m, 3, f"a {kind.lower()} polygon")
    return kind, pts


def polyline_length_m(points: list[dict]) -> float:
    """Sum of segment lengths in metres (open polyline, not closed)."""
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    return total


def validate_polygon(polygon: Any, width_m: float, height_m: float) -> list[dict]:
    """Coerce and bounds-check an operator-drawn polygon.

    Accepts a list of ``{"x": float, "y": float}`` or ``[x, y]`` pairs in metres.
    A polygon needs at least three vertices to enclose any area; anything less
    would silently produce a zone that can never match a track.
    """
    if not isinstance(polygon, Iterable):
        raise StoreLayoutError("polygon must be a list of points")

    cleaned: list[dict] = []
    for i, pt in enumerate(polygon):
        if isinstance(pt, dict):
            x, y = pt.get("x"), pt.get("y")
        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
            x, y = pt[0], pt[1]
        else:
            raise StoreLayoutError(f"point {i} is not a coordinate")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise StoreLayoutError(f"point {i} has non-numeric coordinates")
        # Clamp rather than reject: a drag that overshoots the wall by a few
        # centimetres is an ordinary editing gesture, not an error worth
        # discarding the whole shape over.
        x = min(max(x, 0.0), width_m)
        y = min(max(y, 0.0), height_m)
        cleaned.append({"x": round(x, 3), "y": round(y, 3)})

    if len(cleaned) < 3:
        raise StoreLayoutError("a zone polygon needs at least 3 points")
    return cleaned


def polygon_area_m2(polygon: list[dict]) -> float:
    """Shoelace area in square metres. Used for density normalisation."""
    n = len(polygon)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        a, b = polygon[i], polygon[(i + 1) % n]
        total += a["x"] * b["y"] - b["x"] * a["y"]
    return abs(total) / 2.0


def point_in_polygon(x: float, y: float, polygon: list[dict]) -> bool:
    """Ray-casting point-in-polygon test in metre space."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["x"], polygon[i]["y"]
        xj, yj = polygon[j]["x"], polygon[j]["y"]
        if (yi > y) != (yj > y):
            denom = (yj - yi) or 1e-12
            if x < (xj - xi) * (y - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


class StoreLayoutService:
    """CRUD for the blueprint. All coordinates in metres."""

    async def get_active_layout(self, db: AsyncSession) -> Optional[StoreLayoutModel]:
        res = await db.execute(
            select(StoreLayoutModel).where(StoreLayoutModel.is_active.is_(True)).limit(1)
        )
        layout = res.scalar_one_or_none()
        if layout is None:
            # Fall back to any layout, then create one, so the editor always
            # has a canvas even if is_active was cleared by hand.
            res = await db.execute(select(StoreLayoutModel).limit(1))
            layout = res.scalar_one_or_none()
        if layout is None:
            layout = StoreLayoutModel(
                id="layout_default",
                store_id=settings.STORE_ID,
                name="Store Floor",
                width_m=settings.DEFAULT_STORE_WIDTH_M,
                height_m=settings.DEFAULT_STORE_HEIGHT_M,
                is_active=True,
            )
            db.add(layout)
            await db.commit()
            await db.refresh(layout)
        return layout

    async def update_layout(
        self,
        db: AsyncSession,
        layout_id: str,
        *,
        name: Optional[str] = None,
        width_m: Optional[float] = None,
        height_m: Optional[float] = None,
    ) -> StoreLayoutModel:
        layout = await db.get(StoreLayoutModel, layout_id)
        if layout is None:
            raise StoreLayoutError(f"layout {layout_id} not found")
        if name is not None:
            layout.name = name
        if width_m is not None:
            if not 1.0 <= float(width_m) <= 1000.0:
                raise StoreLayoutError("store width must be between 1 and 1000 metres")
            layout.width_m = float(width_m)
        if height_m is not None:
            if not 1.0 <= float(height_m) <= 1000.0:
                raise StoreLayoutError("store height must be between 1 and 1000 metres")
            layout.height_m = float(height_m)
        await db.commit()
        await db.refresh(layout)
        return layout

    async def list_zones(self, db: AsyncSession, layout_id: str) -> list[StoreZoneModel]:
        res = await db.execute(
            select(StoreZoneModel)
            .where(StoreZoneModel.layout_id == layout_id)
            .order_by(StoreZoneModel.sort_order, StoreZoneModel.name)
        )
        return list(res.scalars().all())

    async def create_zone(
        self,
        db: AsyncSession,
        layout: StoreLayoutModel,
        *,
        name: str,
        category: str,
        polygon: Any,
        color: str = "#00d4ff",
        sku_id: Optional[str] = None,
    ) -> StoreZoneModel:
        category = (category or "AISLE").upper()
        if category not in ZONE_CATEGORIES:
            raise StoreLayoutError(
                f"unknown zone category '{category}'; expected one of {', '.join(ZONE_CATEGORIES)}"
            )
        pts = validate_polygon(polygon, layout.width_m, layout.height_m)

        max_order = await db.scalar(
            select(func.max(StoreZoneModel.sort_order)).where(
                StoreZoneModel.layout_id == layout.id
            )
        )
        zone = StoreZoneModel(
            id=_new_id("zone"),
            layout_id=layout.id,
            name=name.strip() or "Untitled zone",
            category=category,
            polygon=pts,
            color=color,
            sku_id=sku_id,
            sort_order=(max_order or 0) + 1,
        )
        db.add(zone)
        await db.commit()
        await db.refresh(zone)
        return zone

    async def update_zone(
        self,
        db: AsyncSession,
        layout: StoreLayoutModel,
        zone_id: str,
        **fields: Any,
    ) -> StoreZoneModel:
        zone = await db.get(StoreZoneModel, zone_id)
        if zone is None or zone.layout_id != layout.id:
            raise StoreLayoutError(f"zone {zone_id} not found in this layout")

        if (name := fields.get("name")) is not None:
            zone.name = str(name).strip() or zone.name
        if (category := fields.get("category")) is not None:
            category = str(category).upper()
            if category not in ZONE_CATEGORIES:
                raise StoreLayoutError(f"unknown zone category '{category}'")
            zone.category = category
        if (polygon := fields.get("polygon")) is not None:
            zone.polygon = validate_polygon(polygon, layout.width_m, layout.height_m)
        if (color := fields.get("color")) is not None:
            zone.color = str(color)
        if "sku_id" in fields:
            zone.sku_id = fields["sku_id"] or None

        await db.commit()
        await db.refresh(zone)
        return zone

    async def delete_zone(self, db: AsyncSession, layout_id: str, zone_id: str) -> bool:
        zone = await db.get(StoreZoneModel, zone_id)
        if zone is None or zone.layout_id != layout_id:
            return False
        await db.delete(zone)
        await db.commit()
        return True

    # ------------------------------------------------------------ structures

    async def list_structures(self, db: AsyncSession, layout_id: str) -> list[StoreStructureModel]:
        res = await db.execute(
            select(StoreStructureModel)
            .where(StoreStructureModel.layout_id == layout_id)
            .order_by(StoreStructureModel.sort_order, StoreStructureModel.name)
        )
        return list(res.scalars().all())

    async def create_structure(
        self,
        db: AsyncSession,
        layout: StoreLayoutModel,
        *,
        kind: str,
        name: str,
        polygon: Any,
        thickness_m: Optional[float] = None,
        color: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> StoreStructureModel:
        kind, pts = validate_structure(kind, polygon, layout.width_m, layout.height_m)
        thickness = self._validate_thickness(thickness_m) if thickness_m is not None else 0.2
        if properties is not None and not isinstance(properties, dict):
            raise StoreLayoutError("properties must be an object")

        max_order = await db.scalar(
            select(func.max(StoreStructureModel.sort_order)).where(
                StoreStructureModel.layout_id == layout.id
            )
        )
        structure = StoreStructureModel(
            id=_new_id("st"),
            layout_id=layout.id,
            kind=kind,
            name=(name or "").strip() or f"Untitled {kind.lower()}",
            polygon=pts,
            thickness_m=thickness,
            color=str(color) if color else STRUCTURE_DEFAULT_COLORS[kind],
            sort_order=(max_order or 0) + 1,
            properties=dict(properties or {}),
        )
        db.add(structure)
        await db.commit()
        await db.refresh(structure)
        return structure

    async def update_structure(
        self,
        db: AsyncSession,
        layout: StoreLayoutModel,
        structure_id: str,
        **fields: Any,
    ) -> StoreStructureModel:
        structure = await db.get(StoreStructureModel, structure_id)
        if structure is None or structure.layout_id != layout.id:
            raise StoreLayoutError(f"structure {structure_id} not found in this layout")

        # A kind change re-validates the existing geometry against the new
        # minimum vertex count, and a geometry change is validated against
        # the (possibly new) kind, so the two are resolved together.
        new_kind = fields.get("kind")
        new_polygon = fields.get("polygon")
        if new_kind is not None or new_polygon is not None:
            kind, pts = validate_structure(
                new_kind if new_kind is not None else structure.kind,
                new_polygon if new_polygon is not None else structure.polygon,
                layout.width_m,
                layout.height_m,
            )
            structure.kind = kind
            structure.polygon = pts

        if (name := fields.get("name")) is not None:
            structure.name = str(name).strip() or structure.name
        if (thickness := fields.get("thickness_m")) is not None:
            structure.thickness_m = self._validate_thickness(thickness)
        if (color := fields.get("color")) is not None:
            structure.color = str(color)
        if (props := fields.get("properties")) is not None:
            if not isinstance(props, dict):
                raise StoreLayoutError("properties must be an object")
            structure.properties = dict(props)
        if (order := fields.get("sort_order")) is not None:
            structure.sort_order = int(order)

        await db.commit()
        await db.refresh(structure)
        return structure

    async def delete_structure(self, db: AsyncSession, layout_id: str, structure_id: str) -> bool:
        structure = await db.get(StoreStructureModel, structure_id)
        if structure is None or structure.layout_id != layout_id:
            return False
        await db.delete(structure)
        await db.commit()
        return True

    @staticmethod
    def _validate_thickness(value: Any) -> float:
        try:
            t = float(value)
        except (TypeError, ValueError):
            raise StoreLayoutError("thickness_m must be a number")
        if not math.isfinite(t) or not 0.01 <= t <= 5.0:
            raise StoreLayoutError("thickness_m must be between 0.01 and 5 metres")
        return round(t, 3)

    def serialize_structure(self, s: StoreStructureModel) -> dict:
        pts = s.polygon or []
        is_line = s.kind in POLYLINE_KINDS
        return {
            "id": s.id,
            "kind": s.kind,
            "name": s.name,
            "polygon": pts,
            "thickness_m": s.thickness_m,
            "color": s.color,
            "sort_order": s.sort_order,
            "properties": s.properties or {},
            "area_m2": 0.0 if is_line else round(polygon_area_m2(pts), 2),
            "length_m": round(polyline_length_m(pts), 2) if is_line else None,
        }

    async def zone_for_point(
        self, db: AsyncSession, layout_id: str, x_m: float, y_m: float
    ) -> Optional[StoreZoneModel]:
        """Which zone contains this floor point? Used to attribute a track.

        Zones are tested in sort order, so a SHELF bay drawn inside an AISLE
        wins only if the operator ordered it above the aisle.
        """
        for zone in await self.list_zones(db, layout_id):
            if zone.category == "EXCLUDED":
                continue
            if point_in_polygon(x_m, y_m, zone.polygon or []):
                return zone
        return None

    def serialize_zone(self, zone: StoreZoneModel) -> dict:
        return {
            "id": zone.id,
            "name": zone.name,
            "category": zone.category,
            "polygon": zone.polygon or [],
            "color": zone.color,
            "sku_id": zone.sku_id,
            "sort_order": zone.sort_order,
            "area_m2": round(polygon_area_m2(zone.polygon or []), 2),
        }

    def serialize_layout(
        self,
        layout: StoreLayoutModel,
        zones: list[StoreZoneModel],
        structures: Optional[list[StoreStructureModel]] = None,
    ) -> dict:
        structures = structures or []
        return {
            "layout_id": layout.id,
            "store_id": layout.store_id,
            "name": layout.name,
            # The renderer needs only these two numbers to map metres to pixels.
            "width_m": layout.width_m,
            "height_m": layout.height_m,
            "units": "metres",
            "zone_categories": list(ZONE_CATEGORIES),
            "structure_kinds": list(STRUCTURE_KINDS),
            "zones": [self.serialize_zone(z) for z in zones],
            "structures": [self.serialize_structure(s) for s in structures],
            "updated_at": layout.updated_at.isoformat() if layout.updated_at else None,
        }

    @staticmethod
    def setup_block(zones: list, structures: list, cameras: list[dict]) -> dict:
        """Whether the operator has configured anything yet.

        A fresh install has an empty plan; the UI uses ``configured`` to decide
        whether to show the first-run setup panel instead of an empty canvas.
        """
        calibrated = sum(1 for c in cameras if c.get("has_homography"))
        return {
            "configured": bool(zones or structures or cameras),
            "zones": len(zones),
            "structures": len(structures),
            "cameras": len(cameras),
            "cameras_calibrated": calibrated,
        }

    async def serialize_cameras(self, db: AsyncSession, layout: StoreLayoutModel) -> list[dict]:
        """Cameras placed on the blueprint, with their coverage wedge.

        ``floor_x``/``floor_y`` are stored in metres in the same frame as the
        zone polygons, so a camera and the zone it watches share one coordinate
        system for the first time.
        """
        # Import here: the engine imports this module for its polygon test, so
        # a module-level import would be circular.
        from app.services.live_analytics_engine import live_engine

        res = await db.execute(select(CameraModel).order_by(CameraModel.channel_number))
        out: list[dict] = []
        for cam in res.scalars().all():
            # Live worker state wins over the stored column, which is only the
            # last value persisted and goes stale the moment a feed drops.
            rt = live_engine.runtimes.get(cam.id)
            frame_w, frame_h = self.known_frame_size(cam, rt)
            out.append(
                {
                    "camera_id": cam.id,
                    "name": cam.name,
                    "department": cam.department,
                    "role": getattr(cam, "role", None),
                    "channel_number": cam.channel_number,
                    # A camera turned off is DISABLED (shown as OFF), never offline.
                    "status": "DISABLED" if not cam.is_ai_enabled else (rt.status if rt is not None else "OFFLINE"),
                    "fps_actual": round(rt.fps, 1) if rt is not None else None,
                    "last_error": rt.last_error if rt is not None else None,
                    "floor_x": cam.floor_x,
                    "floor_y": cam.floor_y,
                    "floor_z": cam.floor_z,
                    "azimuth_deg": cam.azimuth_deg,
                    "fov_deg": cam.fov_deg,
                    "is_ai_enabled": cam.is_ai_enabled,
                    "has_homography": bool(cam.homography_matrix),
                    "calibration_points": cam.calibration_points,
                    # Native pixel size of the frames the worker is reading.
                    # Calibration image points are in this space; the client
                    # must scale from its displayed size to these dimensions.
                    "frame_width": frame_w,
                    "frame_height": frame_h,
                    "resolution": cam.resolution,
                    "fps": cam.fps,
                    "last_seen": cam.last_seen.isoformat() if cam.last_seen else None,
                }
            )
        return out

    @staticmethod
    def known_frame_size(cam: CameraModel, rt) -> tuple[Optional[int], Optional[int]]:
        """Last known native frame size: live worker first, else the size
        recorded when the operator calibrated, else unknown (None)."""
        if rt is not None and rt.frame_width and rt.frame_height:
            return int(rt.frame_width), int(rt.frame_height)
        cp = cam.calibration_points or {}
        w, h = cp.get("frame_width"), cp.get("frame_height")
        if w and h:
            return int(w), int(h)
        return None, None


store_layout_service = StoreLayoutService()
