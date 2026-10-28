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

import asyncio
import contextvars
import json
import logging
import math
import sqlite3
import time
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import coverage_geometry
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
    # A high-security restricted area (cash office, safe room): its settings,
    # arming and triggers live in services/restricted_areas.py; any zone can
    # be used for one, this category only marks it on the map.
    "RESTRICTED",
    "EXCLUDED",
)


class StoreLayoutError(ValueError):
    """Raised when a caller supplies geometry the store cannot accept."""


# Products a zone sells (store_zones.products): short category names, not SKUs.
MAX_ZONE_PRODUCTS = 40
MAX_PRODUCT_NAME_LEN = 64


def normalize_products(value: Any) -> list[str]:
    """A clean product list from a list or a comma/newline-separated string.

    Blank entries are dropped, whitespace collapsed and duplicates removed
    (case-insensitively, first spelling kept), in the order given.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise StoreLayoutError("products must be a list of names or a comma-separated string")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise StoreLayoutError("each product must be a name (text)")
        name = " ".join(item.split())
        if not name:
            continue
        if len(name) > MAX_PRODUCT_NAME_LEN:
            raise StoreLayoutError(f"product name longer than {MAX_PRODUCT_NAME_LEN} characters: '{name[:20]}...'")
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    if len(out) > MAX_ZONE_PRODUCTS:
        raise StoreLayoutError(f"a zone can list at most {MAX_ZONE_PRODUCTS} products")
    return out


# Structure kinds: what the operator draws so the plan resembles their store.
# WALL and DOOR are polylines with a thickness; the rest are closed polygons.
# None of these affect analytics -- zones remain the attribution regions.
# OUTDOOR is a polygon for space outside the building on the same site (car
# park, loading dock, side lane); ``properties.area_type`` may label it.
STRUCTURE_KINDS = ("ROOM", "WALL", "SHELF", "COUNTER", "DOOR", "OBSTACLE", "OUTDOOR")
POLYLINE_KINDS = ("WALL", "DOOR")

# Limits of the site (store_layouts.width_m/height_m: the whole drawable
# canvas, building plus outdoor areas) and of one extend call per side.
SITE_MIN_M = 1.0
SITE_MAX_M = 1000.0
EXTEND_MAX_M = 500.0
# Positions within this distance of the site edge still count as inside.
EDGE_TOLERANCE_M = 1e-6

STRUCTURE_DEFAULT_COLORS = {
    "ROOM": "#3a4358",
    "WALL": "#c7ccd8",
    "SHELF": "#7b8cff",
    "COUNTER": "#f2a541",
    "DOOR": "#5ad38a",
    "OBSTACLE": "#8b93a7",
    "OUTDOOR": "#6b8f71",
}


class ItemsOutsideError(StoreLayoutError):
    """A site change would leave placed items outside the map.

    ``items`` lists them as ``{"type", "id", "name"}`` so the UI can say which
    ones; nothing was changed.
    """

    def __init__(self, items: list[dict], message: str = ""):
        self.items = items
        n = len(items)
        super().__init__(message or (
            f"{n} item{'s' if n != 1 else ''} would end up outside the map; move "
            f"{'them' if n != 1 else 'it'} first or keep that space"))

    def detail(self) -> dict:
        return {"code": "items_outside", "message": str(self), "items": self.items}


class SiteBusyError(StoreLayoutError):
    """A site change could not get the database write lock; nothing changed."""

    def detail(self) -> dict:
        return {"code": "site_busy", "message": str(self)}


# Site changes (extend / fit / resize): how long one attempt waits for the
# SQLite write lock, how many whole attempts are made, the pause between them,
# and how long a request waits behind other site changes of this process.
SITE_CHANGE_LOCK_TIMEOUT_S = 15.0
SITE_CHANGE_ATTEMPTS = 3
SITE_CHANGE_RETRY_DELAY_S = 0.5
SITE_CHANGE_QUEUE_TIMEOUT_S = 60.0
# database.py sets this on every connection (PRAGMA busy_timeout).
_DEFAULT_BUSY_TIMEOUT_MS = 15000

_site_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _site_lock() -> asyncio.Lock:
    """One lock per event loop: site changes of this process run one at a time."""
    loop = asyncio.get_running_loop()
    lock = _site_locks.get(loop)
    if lock is None:
        lock = _site_locks[loop] = asyncio.Lock()
    return lock


def _is_lock_error(e: BaseException) -> bool:
    """SQLite 'database is locked' / 'database table is locked' / SQLITE_BUSY."""
    if isinstance(e, OperationalError):
        e = e.orig if e.orig is not None else e
    if not isinstance(e, (sqlite3.OperationalError, OperationalError)):
        return False
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


async def _begin_immediate(session: AsyncSession, timeout_s: float) -> None:
    """Take the SQLite write lock now, waiting up to ``timeout_s``.

    With the lock held from the start, every read of the change sees the
    latest committed map and no write inside it can hit ``database is
    locked``: the operation commits whole or not at all.
    """
    conn = await session.connection()
    await conn.exec_driver_sql(f"PRAGMA busy_timeout = {max(1, int(timeout_s * 1000))}")
    try:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
    finally:
        await conn.exec_driver_sql(f"PRAGMA busy_timeout = {_DEFAULT_BUSY_TIMEOUT_MS}")


def translate_homography(matrix: Any, dx: float, dy: float) -> Optional[list[list[float]]]:
    """``T(dx, dy) @ H`` for an image-to-floor homography (row-major 3x3).

    H maps image pixels to floor metres; moving the floor origin so every
    floor point gains (dx, dy) composes a translation on the floor side. The
    same pixel then projects to the same physical spot in the new frame.
    Returns None for a matrix that is not a 3x3 of numbers.
    """
    try:
        rows = [[float(v) for v in row] for row in matrix]
    except (TypeError, ValueError):
        return None
    if len(rows) != 3 or any(len(r) != 3 for r in rows):
        return None
    r0, r1, r2 = rows
    return [
        [r0[i] + dx * r2[i] for i in range(3)],
        [r1[i] + dy * r2[i] for i in range(3)],
        list(r2),
    ]


def _duplicate_fields(camera_id: str) -> dict:
    """duplicate_group / duplicate_of for a camera (services/duplicate_cameras.py)."""
    try:
        from app.services.duplicate_cameras import duplicate_guard

        f = duplicate_guard.camera_fields(camera_id)
        return {"duplicate_group": f["duplicate_group"], "duplicate_of": f["duplicate_of"]}
    except Exception:  # noqa: BLE001 - the layout must load regardless
        return {"duplicate_group": None, "duplicate_of": None}


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


# Adds (:dx, :dy) to the floor point {"x", "y"} of every trajectory sample of
# customer_tracks, in place; image-only samples ({"u", "v"}) are untouched.
_TRANSLATE_TRAJECTORIES_SQL = """
UPDATE customer_tracks SET trajectory_points = (
  SELECT json_group_array(
    CASE WHEN json_type(je.value, '$.x') IN ('integer', 'real')
          AND json_type(je.value, '$.y') IN ('integer', 'real')
      THEN json_set(je.value, '$.x', json_extract(je.value, '$.x') + :dx,
                              '$.y', json_extract(je.value, '$.y') + :dy)
      ELSE json(je.value) END)
  FROM (SELECT value FROM json_each(customer_tracks.trajectory_points) ORDER BY key) AS je)
WHERE json_valid(trajectory_points) AND json_type(trajectory_points) = 'array'
  AND trajectory_points LIKE '%"x"%'
"""

# Limits the update above to the rows a pre-scan found holding floor points,
# plus every row inserted since (rowid > max_rowid) or started recently
# (still being recorded, so possibly given floor points since the scan).
_PRESCANNED_ROWS_SQL = """
  AND (rowid IN (SELECT value FROM json_each(:rowids)) OR rowid > :max_rowid OR start_time >= :since)
"""
_PRESCAN_TRACKS_SQL = """
SELECT rowid FROM customer_tracks
WHERE json_valid(trajectory_points) AND json_type(trajectory_points) = 'array'
  AND trajectory_points LIKE '%"x"%'
"""
# A track is recorded for minutes; 48 h also covers any local/UTC mix-up.
PRESCAN_RECENT_WINDOW = timedelta(hours=48)

# Set by _site_change for the duration of one change: {"rowids", "max_rowid", "since"}.
_TRACK_PRESCAN: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("track_prescan", default=None)


async def _prescan_tracks(session: AsyncSession) -> Optional[dict]:
    """Shopper paths with floor points, found without holding the write lock.

    None (the change then scans inside its transaction, as before) when the
    scan fails, e.g. SQLite without JSON1.
    """
    from sqlalchemy import text

    try:
        since = (datetime.now(timezone.utc).replace(tzinfo=None) - PRESCAN_RECENT_WINDOW).isoformat(sep=" ")
        max_rowid = (await session.execute(text("SELECT COALESCE(MAX(rowid), 0) FROM customer_tracks"))).scalar()
        rows = (await session.execute(text(_PRESCAN_TRACKS_SQL))).scalars().all()
        await session.rollback()
        return {"rowids": json.dumps([int(r) for r in rows]), "max_rowid": int(max_rowid or 0), "since": since}
    except Exception as e:  # noqa: BLE001 - fall back to the in-transaction scan
        logger.warning(f"site change: shopper path pre-scan failed ({type(e).__name__}: {e})")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


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
        clamp: bool = False,
    ) -> StoreLayoutModel:
        """Rename and/or set the site size (the right and bottom edges move).

        Shrinking is refused with ``ItemsOutsideError`` when a zone, structure
        or camera would end up outside, unless ``clamp`` is set: then those
        items are pulled inside the new edges (shapes can lose their form).
        A site change: one write transaction, serialised (``_site_change``).
        """
        layout = await db.get(StoreLayoutModel, layout_id)
        if layout is None:
            raise StoreLayoutError(f"layout {layout_id} not found")
        new_w_req = self._check_site_size(width_m, "width") if width_m is not None else None
        new_h_req = self._check_site_size(height_m, "depth") if height_m is not None else None

        async def op(s: AsyncSession, lay: StoreLayoutModel) -> None:
            new_w = new_w_req if new_w_req is not None else float(lay.width_m)
            new_h = new_h_req if new_h_req is not None else float(lay.height_m)
            if name is not None:
                lay.name = name
            if new_w < lay.width_m or new_h < lay.height_m:
                outside = await self.items_outside(s, lay, 0.0, 0.0, new_w, new_h)
                if outside and not clamp:
                    raise ItemsOutsideError(outside)
                if outside:
                    await self._clamp_items(s, lay, new_w, new_h)
                await self._keep_unplaced_inside(s, new_w, new_h)
            lay.width_m = new_w
            lay.height_m = new_h

        await self._site_change(db, layout, op, "resize")
        return layout

    @staticmethod
    def _check_site_size(value: Any, what: str) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise StoreLayoutError(f"map {what} must be a number")
        if not math.isfinite(v) or not SITE_MIN_M <= v <= SITE_MAX_M:
            raise StoreLayoutError(
                f"map {what} must be between {SITE_MIN_M:g} and {SITE_MAX_M:g} metres")
        return round(v, 3)

    # ------------------------------------------------------- site changes
    #
    # Extend, fit and resize read every zone, structure and camera, compute
    # the change and write it back. Before 2026-10-21 that was a deferred
    # transaction on the request's session: two overlapping "Fit to content"
    # clicks each read the map before either wrote, then one translated the
    # zones from its stale read while the structures and cameras (read after
    # it had waited for the other's write lock) moved twice -- zones and
    # structures ended up metres apart. A site change now runs in its own
    # session that takes the write lock first (BEGIN IMMEDIATE), so it reads
    # the map as the last committed change left it, and commits all or
    # nothing; a lock error retries the whole change from a fresh read.

    async def _site_change(self, db: AsyncSession, layout: StoreLayoutModel, op, what: str,
                           will_translate=None):
        """Run ``op(session, layout)`` as one all-or-nothing write transaction.

        Serialised in this process (an asyncio lock) and against every other
        connection (BEGIN IMMEDIATE before the first read). On ``database is
        locked`` the whole operation is retried from a fresh read; when the
        lock cannot be had, ``SiteBusyError`` is raised and nothing changed.
        Afterwards the caller's session sees the committed state.

        ``will_translate(session, layout)``: a read-only guess whether the
        change moves the origin. When it does, the shopper paths holding floor
        points are found BEFORE the write lock is taken (a full scan of
        customer_tracks: ~18 s on the 2.8 GB Pearcedale database), so the
        lock is held for well under a second instead of the whole scan, and
        the pipeline's writers are not starved (``_prescan_tracks``).
        """
        from app.database import async_session_factory

        layout_id = layout.id
        last: Optional[BaseException] = None
        # End the caller's transaction first: it gives its pooled connection
        # back (requests queued on the lock below must not hold the whole pool
        # while the running change needs one) and holds no snapshot or lock.
        await db.commit()
        try:
            async with asyncio.timeout(SITE_CHANGE_QUEUE_TIMEOUT_S):
                await _site_lock().acquire()
        except TimeoutError:
            raise SiteBusyError("another map size change is still running; try again in a moment")
        token = None
        try:
            if will_translate is not None:
                async with async_session_factory() as s:
                    fresh = await s.get(StoreLayoutModel, layout_id)
                    if fresh is not None and await will_translate(s, fresh):
                        token = _TRACK_PRESCAN.set(await _prescan_tracks(s))
            result = None
            for attempt in range(SITE_CHANGE_ATTEMPTS):
                try:
                    async with async_session_factory() as s:
                        try:
                            await _begin_immediate(s, SITE_CHANGE_LOCK_TIMEOUT_S)
                            locked_at = time.monotonic()
                            fresh = await s.get(StoreLayoutModel, layout_id)
                            if fresh is None:
                                raise StoreLayoutError(f"layout {layout_id} not found")
                            result = await op(s, fresh)
                            await s.commit()
                            if not (isinstance(result, dict) and result.get("unchanged")):
                                logger.info(f"Map {what} committed; write lock held "
                                            f"{time.monotonic() - locked_at:.2f} s")
                        except BaseException:
                            await s.rollback()
                            raise
                    last = None
                    break
                except Exception as e:  # noqa: BLE001 - only lock errors are retried
                    if not _is_lock_error(e):
                        raise
                    last = e
                    logger.warning(f"Map {what}: database busy, retrying the whole change "
                                   f"({attempt + 1}/{SITE_CHANGE_ATTEMPTS}): {e}")
                    if attempt + 1 < SITE_CHANGE_ATTEMPTS:
                        await asyncio.sleep(SITE_CHANGE_RETRY_DELAY_S * (attempt + 1))
            if last is not None:
                raise SiteBusyError(
                    "the database is busy, so the map was not changed; try again in a moment") from last
        finally:
            if token is not None:
                _TRACK_PRESCAN.reset(token)
            _site_lock().release()
        # The request's session may hold rows loaded before the change.
        db.expire_all()
        await db.refresh(layout)
        return result

    async def placed_items(self, db: AsyncSession, layout: StoreLayoutModel) -> list[dict]:
        """Every zone, structure and camera with the floor points it occupies."""
        items: list[dict] = []
        for z in await self.list_zones(db, layout.id):
            items.append({"type": "zone", "id": z.id, "name": z.name, "points": list(z.polygon or [])})
        for st in await self.list_structures(db, layout.id):
            items.append({"type": "structure", "id": st.id, "name": st.name, "kind": st.kind,
                          "points": list(st.polygon or [])})
        res = await db.execute(select(CameraModel).order_by(CameraModel.channel_number))
        for cam in res.scalars().all():
            if cam.floor_x is None or cam.floor_y is None:
                continue
            # A camera never put on the map sits at a default spot (often 0, 0):
            # it must not pin "Fit to content" to the origin or block a shrink.
            # Site changes keep it inside the map instead (_keep_unplaced_inside).
            if getattr(cam, "map_placed", None) is False:
                continue
            items.append({"type": "camera", "id": cam.id, "name": cam.name,
                          "points": [{"x": cam.floor_x, "y": cam.floor_y}]})
        return items

    async def items_outside(
        self, db: AsyncSession, layout: StoreLayoutModel,
        dx: float, dy: float, new_w: float, new_h: float,
    ) -> list[dict]:
        """Items that, moved by (dx, dy), would not fit in ``new_w`` x ``new_h``."""
        tol = EDGE_TOLERANCE_M
        out: list[dict] = []
        for it in await self.placed_items(db, layout):
            for p in it["points"]:
                try:
                    x, y = float(p["x"]) + dx, float(p["y"]) + dy
                except (KeyError, TypeError, ValueError):
                    continue
                if x < -tol or y < -tol or x > new_w + tol or y > new_h + tol:
                    entry = {"type": it["type"], "id": it["id"], "name": it["name"]}
                    if it.get("kind"):
                        entry["kind"] = it["kind"]
                    out.append(entry)
                    break
        return out

    async def content_bounds(self, db: AsyncSession, layout: StoreLayoutModel) -> Optional[tuple[float, float, float, float]]:
        """(min_x, min_y, max_x, max_y) over every placed item, or None when the map is empty."""
        xs: list[float] = []
        ys: list[float] = []
        for it in await self.placed_items(db, layout):
            for p in it["points"]:
                try:
                    xs.append(float(p["x"]))
                    ys.append(float(p["y"]))
                except (KeyError, TypeError, ValueError):
                    continue
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    async def extend_site(
        self,
        db: AsyncSession,
        layout: StoreLayoutModel,
        *,
        left_m: float = 0.0,
        top_m: float = 0.0,
        right_m: float = 0.0,
        bottom_m: float = 0.0,
    ) -> dict:
        """Add (positive) or remove (negative) space on each side of the site.

        Space added on the left or top moves the origin, so every zone,
        structure, camera position and camera calibration (floor points and
        homography) is translated by (+left, +top) and stays on the same
        physical spot. Removing space is refused with ``ItemsOutsideError``
        when an item would end up outside. Recorded floor positions in the
        same database (shopper paths, shelf reaches) are translated too.
        Everything happens in one write transaction (``_site_change``).
        Returns ``{"dx", "dy", "width_m", "height_m", "translated": {...}}``.
        """
        sides = self._check_sides(left_m, top_m, right_m, bottom_m)
        if not any(abs(v) > 0 for v in sides.values()):
            raise StoreLayoutError("give at least one side an amount of space to add or remove")

        async def op(s: AsyncSession, lay: StoreLayoutModel) -> dict:
            return await self._extend_in(s, lay, sides)

        async def will_translate(s: AsyncSession, lay: StoreLayoutModel) -> bool:
            return bool(sides["left"] or sides["top"])

        return self._after_site_change(await self._site_change(db, layout, op, "extend", will_translate))

    async def fit_site(self, db: AsyncSession, layout: StoreLayoutModel, margin_m: float = 2.0) -> dict:
        """Shrink-wrap the site to the bounding box of every item plus ``margin_m``.

        Idempotent: the bounds are read inside the change's write lock, so a
        second fit (also one that overlapped the first) finds the map fitted
        and returns ``unchanged``.
        """
        try:
            margin = float(margin_m)
        except (TypeError, ValueError):
            raise StoreLayoutError("margin_m must be a number")
        if not math.isfinite(margin) or not 0.0 <= margin <= 50.0:
            raise StoreLayoutError("margin_m must be between 0 and 50 metres")

        async def op(s: AsyncSession, lay: StoreLayoutModel) -> dict:
            sides = await self._fit_sides(s, lay, margin)
            if sides is None:
                return {"dx": 0.0, "dy": 0.0, "width_m": lay.width_m, "height_m": lay.height_m,
                        "translated": {}, "unchanged": True}
            return await self._extend_in(s, lay, sides)

        async def will_translate(s: AsyncSession, lay: StoreLayoutModel) -> bool:
            sides = await self._fit_sides(s, lay, margin)
            return bool(sides and (sides["left"] or sides["top"]))

        return self._after_site_change(await self._site_change(db, layout, op, "fit", will_translate))

    async def _fit_sides(self, db: AsyncSession, layout: StoreLayoutModel, margin: float) -> Optional[dict]:
        """The sides a fit adds (+) or removes (-), or None when the map already fits."""
        bounds = await self.content_bounds(db, layout)
        if bounds is None:
            raise StoreLayoutError("the map is empty: draw or place something first")
        min_x, min_y, max_x, max_y = bounds
        # The new site, in the current frame, before rounding to centimetres.
        x0, y0 = min_x - margin, min_y - margin
        w = max(SITE_MIN_M, (max_x + margin) - x0)
        h = max(SITE_MIN_M, (max_y + margin) - y0)
        left = round(-x0, 3)
        top = round(-y0, 3)
        right = round((x0 + w) - float(layout.width_m), 3)
        bottom = round((y0 + h) - float(layout.height_m), 3)
        if not any(abs(v) >= 0.001 for v in (left, top, right, bottom)):
            return None
        return self._check_sides(left, top, right, bottom)

    @staticmethod
    def _check_sides(left_m: Any, top_m: Any, right_m: Any, bottom_m: Any) -> dict:
        sides = {}
        for key, value in (("left", left_m), ("top", top_m), ("right", right_m), ("bottom", bottom_m)):
            try:
                v = float(value or 0.0)
            except (TypeError, ValueError):
                raise StoreLayoutError(f"{key}_m must be a number")
            if not math.isfinite(v) or v > EXTEND_MAX_M or v < -SITE_MAX_M:
                raise StoreLayoutError(f"{key}_m must be at most {EXTEND_MAX_M:g} metres")
            sides[key] = round(v, 3)
        return sides

    async def _extend_in(self, db: AsyncSession, layout: StoreLayoutModel, sides: dict) -> dict:
        """The extend itself, inside ``_site_change``'s transaction. No commit."""
        new_w = round(float(layout.width_m) + sides["left"] + sides["right"], 3)
        new_h = round(float(layout.height_m) + sides["top"] + sides["bottom"], 3)
        if not SITE_MIN_M <= new_w <= SITE_MAX_M or not SITE_MIN_M <= new_h <= SITE_MAX_M:
            raise StoreLayoutError(
                f"the map would be {new_w:g} x {new_h:g} m; each side must stay between "
                f"{SITE_MIN_M:g} and {SITE_MAX_M:g} metres")
        dx, dy = sides["left"], sides["top"]
        if any(v < 0 for v in sides.values()):
            outside = await self.items_outside(db, layout, dx, dy, new_w, new_h)
            if outside:
                raise ItemsOutsideError(outside)
        translated = await self._translate_all(db, layout, dx, dy) if (dx or dy) else {}
        await self._keep_unplaced_inside(db, new_w, new_h)
        layout.width_m = new_w
        layout.height_m = new_h
        return {"dx": dx, "dy": dy, "width_m": new_w, "height_m": new_h, "translated": translated}

    def _after_site_change(self, change: dict) -> dict:
        """After the commit: install translated calibrations in the running projector."""
        translated = change.get("translated") or {}
        homographies = translated.pop("_homographies", [])
        if change.get("dx") or change.get("dy"):
            self._reload_projectors_after_translate(homographies)
        return change

    async def _translate_all(self, db: AsyncSession, layout: StoreLayoutModel, dx: float, dy: float) -> dict:
        """Move every floor-metre record of this site by (dx, dy). No commit."""
        from sqlalchemy import text, update

        from app.models.db_models import ShelfInteractionModel

        def move(points: Any) -> list[dict]:
            out = []
            for p in points or []:
                q = dict(p)
                q["x"] = round(float(p["x"]) + dx, 3)
                q["y"] = round(float(p["y"]) + dy, 3)
                out.append(q)
            return out

        counts = {"zones": 0, "structures": 0, "cameras": 0, "calibrations": 0,
                  "shelf_interactions": 0, "customer_tracks": 0}
        for z in await self.list_zones(db, layout.id):
            z.polygon = move(z.polygon)
            counts["zones"] += 1
        for st in await self.list_structures(db, layout.id):
            st.polygon = move(st.polygon)
            counts["structures"] += 1
        homographies: list[tuple[str, list, Any]] = []
        res = await db.execute(select(CameraModel))
        for cam in res.scalars().all():
            if cam.floor_x is not None and cam.floor_y is not None:
                cam.floor_x = round(float(cam.floor_x) + dx, 3)
                cam.floor_y = round(float(cam.floor_y) + dy, 3)
                counts["cameras"] += 1
            cp = cam.calibration_points
            if isinstance(cp, dict) and isinstance(cp.get("floor_points"), list):
                cp = dict(cp)
                try:
                    cp["floor_points"] = move(cp["floor_points"])
                except (KeyError, TypeError, ValueError):
                    pass
                cam.calibration_points = cp
            if cam.homography_matrix:
                moved = translate_homography(cam.homography_matrix, dx, dy)
                if moved is not None:
                    cam.homography_matrix = moved
                    counts["calibrations"] += 1
                    homographies.append((cam.id, moved, cam.calibration_points))

        # Recorded floor positions live in the same database: shelf reaches
        # (floor_x/floor_y) and the floor part ({"x","y"}) of shopper paths.
        r = await db.execute(
            update(ShelfInteractionModel)
            .where(ShelfInteractionModel.floor_x.is_not(None), ShelfInteractionModel.floor_y.is_not(None))
            .values(floor_x=ShelfInteractionModel.floor_x + dx, floor_y=ShelfInteractionModel.floor_y + dy)
        )
        counts["shelf_interactions"] = int(r.rowcount or 0)
        try:
            async with db.begin_nested():
                prescan = _TRACK_PRESCAN.get()
                if prescan is None:
                    r = await db.execute(text(_TRANSLATE_TRAJECTORIES_SQL), {"dx": dx, "dy": dy})
                else:
                    r = await db.execute(text(_TRANSLATE_TRAJECTORIES_SQL + _PRESCANNED_ROWS_SQL),
                                         {"dx": dx, "dy": dy, **prescan})
                counts["customer_tracks"] = int(r.rowcount or 0)
        except Exception as e:  # noqa: BLE001 - SQLite without JSON1: layout still moves
            if _is_lock_error(e):
                raise   # never commit a site change with the shopper paths left behind
            logger.warning(f"site extend: shopper paths not translated ({type(e).__name__}: {e})")
            counts["customer_tracks"] = None
        counts["_homographies"] = homographies
        logger.info(f"Site origin moved by ({dx:+g}, {dy:+g}) m: {counts | {'_homographies': len(homographies)}}")
        return counts

    @staticmethod
    def _reload_projectors_after_translate(homographies: list) -> None:
        """Install the translated matrices in the running projector at once."""
        if not homographies:
            return
        try:
            from app.services.frame_geometry import calibration_frame_size
            from app.services.tracking_service import floor_projector

            for cam_id, matrix, cp in homographies:
                floor_projector.set_homography(cam_id, matrix, calibration_frame_size(cp))
        except Exception as e:  # noqa: BLE001 - the reconcile loop reloads them anyway
            logger.warning(f"site extend: projector reload deferred to the reconcile loop: {e}")

    async def _keep_unplaced_inside(self, db: AsyncSession, w: float, h: float) -> None:
        """Cameras not placed on the map yet stay at a spot inside the site. No commit."""
        res = await db.execute(select(CameraModel))
        for cam in res.scalars().all():
            if getattr(cam, "map_placed", None) is not False:
                continue
            if cam.floor_x is not None:
                cam.floor_x = min(max(float(cam.floor_x), 0.0), w)
            if cam.floor_y is not None:
                cam.floor_y = min(max(float(cam.floor_y), 0.0), h)

    async def _clamp_items(self, db: AsyncSession, layout: StoreLayoutModel, w: float, h: float) -> None:
        """Pull every zone, structure and camera inside a smaller site. No commit."""
        for z in await self.list_zones(db, layout.id):
            z.polygon = validate_polygon(z.polygon or [], w, h)
        for st in await self.list_structures(db, layout.id):
            st.kind, st.polygon = validate_structure(st.kind, st.polygon or [], w, h)
        res = await db.execute(select(CameraModel))
        for cam in res.scalars().all():
            if cam.floor_x is not None:
                cam.floor_x = min(max(float(cam.floor_x), 0.0), w)
            if cam.floor_y is not None:
                cam.floor_y = min(max(float(cam.floor_y), 0.0), h)

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
        products: Any = None,
    ) -> StoreZoneModel:
        category = (category or "AISLE").upper()
        if category not in ZONE_CATEGORIES:
            raise StoreLayoutError(
                f"unknown zone category '{category}'; expected one of {', '.join(ZONE_CATEGORIES)}"
            )
        pts = validate_polygon(polygon, layout.width_m, layout.height_m)
        product_list = normalize_products(products)

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
            products=product_list,
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
        if "products" in fields:
            zone.products = normalize_products(fields["products"])

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
            "products": list(zone.products or []),
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
        zone_ids = {getattr(z, "id", None) for z in zones}
        linked = sum(1 for c in cameras
                     if not c.get("has_homography") and c.get("watch_zone_id") in zone_ids)
        return {
            "configured": bool(zones or structures or cameras),
            "zones": len(zones),
            "structures": len(structures),
            "cameras": len(cameras),
            "cameras_calibrated": calibrated,
            # Uncalibrated cameras counting into a zone they are linked to.
            "cameras_zone_linked": linked,
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
                    # Zone an uncalibrated camera counts into (m0017); unused once calibrated.
                    "watch_zone_id": getattr(cam, "watch_zone_id", None),
                    # Put on the map by the operator (m0020); False = still at a default spot.
                    "map_placed": bool(getattr(cam, "map_placed", False)),
                    # Reach on the floor (m0022): None = the lens-angle default below.
                    "range_m": getattr(cam, "range_m", None),
                    "range_default_m": coverage_geometry.default_range_m(cam.fov_deg),
                    # "auto" (calibrated footprint when there is one) | "calibrated" | "manual".
                    "coverage_mode": getattr(cam, "coverage_mode", None) or "auto",
                    # Floor the calibrated picture really shows ({polygon, near_m, far_m,
                    # area_m2}, metres), None when uncalibrated or unusable.
                    "calibrated_footprint": self._footprint(cam),
                    "calibration_points": cam.calibration_points,
                    # Native pixel size of the frames the worker is reading.
                    # Calibration image points are in this space; the client
                    # must scale from its displayed size to these dimensions.
                    "frame_width": frame_w,
                    "frame_height": frame_h,
                    "resolution": cam.resolution,
                    "fps": cam.fps,
                    "last_seen": cam.last_seen.isoformat() if cam.last_seen else None,
                    **_duplicate_fields(cam.id),
                }
            )
        return out

    @staticmethod
    def _footprint(cam: CameraModel) -> Optional[dict]:
        if not cam.homography_matrix:
            return None
        try:
            return coverage_geometry.camera_footprint({
                "homography_matrix": cam.homography_matrix,
                "calibration_points": cam.calibration_points or {},
                "map_placed": bool(getattr(cam, "map_placed", False)),
                "floor_x": cam.floor_x, "floor_y": cam.floor_y,
            })
        except Exception as e:  # a malformed stored matrix never breaks the layout
            logger.debug(f"footprint for {cam.id} unavailable: {e}")
            return None

    async def coverage_report(self, db: AsyncSession) -> dict:
        """What every camera sees and how much of the store is covered
        (services/coverage_geometry.analyse over the active layout)."""
        layout = await self.get_active_layout(db)
        zones = await self.list_zones(db, layout.id)
        structures = await self.list_structures(db, layout.id)
        cameras = await self.serialize_cameras(db, layout)
        report = coverage_geometry.analyse(
            {"width_m": layout.width_m, "height_m": layout.height_m},
            [self.serialize_structure(s) for s in structures],
            [self.serialize_zone(z) for z in zones],
            cameras,
        )
        report["layout_id"] = layout.id
        report["width_m"] = layout.width_m
        report["height_m"] = layout.height_m
        return report

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
