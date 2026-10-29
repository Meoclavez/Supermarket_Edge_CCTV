"""Retail metrics computed from observations, not from literals.

Every figure this module returns is an aggregate over rows the live pipeline
actually wrote: ``zone_visits`` (a person dwelling in a zone),
``customer_tracks`` (a person's path), and ``pos_transactions`` (real sales
ingested from the till).

The contract is strict: when there are no observations, the answer is ``None``
-- not zero, and never a plausible-looking constant. ``None`` means "not
observed" and the UI renders it as an explicit empty state. Zero would be a
claim that nobody came in, which is a different and usually false statement.

This replaces route handlers whose "live analytics" were literals such as
``footfall = max(3420, track_count)`` against a table that was never written,
so the floor always won and the figure never changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Float, String, and_, case, func, or_, select, true, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.timeutil import local_day_bounds_utc, local_now, to_local, utcnow

from app.models.db_models import (
    CameraModel,
    CustomerTrackModel,
    POSTransactionModel,
    ShelfInteractionModel,
    StoreZoneModel,
    ZoneVisitModel,
)

logger = logging.getLogger(__name__)


def day_bounds(day: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Store-local midnight-to-midnight window for a trading day, as naive UTC.

    Stored observation times are naive UTC (services/timeutil.py); the trading
    day is the store's (SITE_TIMEZONE, else the host zone). ``day`` may be
    aware or naive store-local; None means today.
    """
    return local_day_bounds_utc(day)


def _visit_is_real():
    """A zone visit long enough to be a shopper, not a flickering track fragment.

    Visits already need ZONE_DWELL_MIN_SECONDS to open; this additionally
    applies FOOTFALL_MIN_TRACK_SECONDS so footfall, dwell and funnel figures
    ignore fragments if the dwell floor is lowered. Closed visits need that
    dwell; open ones must have started at least that long ago.
    """
    min_s = float(settings.FOOTFALL_MIN_TRACK_SECONDS)
    return or_(
        and_(ZoneVisitModel.exited_at.isnot(None), ZoneVisitModel.dwell_seconds >= min_s),
        and_(ZoneVisitModel.exited_at.is_(None),
             ZoneVisitModel.entered_at <= utcnow() - timedelta(seconds=min_s)),
    )


def _track_is_real():
    """A finished track that lasted FOOTFALL_MIN_TRACK_SECONDS with FOOTFALL_MIN_TRACK_HITS hits.

    Occlusion and detector flicker split one person into many ~1 s tracks;
    counting each as a shopper inflated footfall roughly thirtyfold on a live
    test. Rows written before ``hits`` existed (NULL) are judged on duration.
    """
    duration = (func.julianday(CustomerTrackModel.end_time) - func.julianday(CustomerTrackModel.start_time)) * 86400.0
    return and_(
        CustomerTrackModel.end_time.isnot(None),
        duration >= float(settings.FOOTFALL_MIN_TRACK_SECONDS),
        or_(CustomerTrackModel.hits.is_(None), CustomerTrackModel.hits >= int(settings.FOOTFALL_MIN_TRACK_HITS)),
    )


# ------------------------------------------------------------------ heatmap binning
# Shared by the live endpoint (RetailMetricsService.heatmap) and the recorded
# history (services/heatmap_history.py), so a recorded bucket is the live
# heatmap of the same window by construction.

HEATMAP_KINDS = ("presence", "dwell", "interaction")
# Gaps longer than this between two trajectory points of one track are
# not credited as dwell: the person was not observed in between.
HEATMAP_DWELL_MAX_GAP_SEC = 5.0
PRESENCE_WEIGHTINGS = ("samples", "tracks")


def duplicate_camera_ids() -> list[str]:
    """Cameras excluded from store-wide totals as duplicates of another camera.

    services/duplicate_cameras.py: when one physical camera is configured more
    than once, only its group's primary camera feeds store totals (footfall,
    in store now, dwell, zones, funnel, floor heatmaps, forecast). Per-camera
    views still show every camera's own data.
    """
    try:
        from app.services.duplicate_cameras import excluded_camera_ids

        return excluded_camera_ids()
    except Exception as e:  # noqa: BLE001 - no exclusions rather than no totals
        logger.debug(f"duplicate camera exclusion unavailable: {e}")
        return []


def _not_duplicate(column):
    """SQL condition: the row's camera is not an excluded duplicate."""
    ids = duplicate_camera_ids()
    return column.notin_(ids) if ids else true()


async def staff_only_camera_ids(db: AsyncSession) -> list[str]:
    """Cameras excluded from customer heatmaps: roles that never count customers (stockroom).

    Uses services/camera_roles.py (NON_FOOTFALL_ROLES); no role = included,
    exactly as before roles existed.
    """
    try:
        from app.services.camera_roles import non_footfall_camera_ids

        return list(await non_footfall_camera_ids(db))
    except Exception as e:  # unmigrated DB / roles unavailable: no exclusions
        logger.debug(f"camera roles unavailable for heatmaps: {e}")
        return []


def heatmap_unit(kind: str, presence_weighting: str = "samples") -> str:
    if kind == "dwell":
        return "seconds"
    if kind == "interaction":
        return "interactions"
    return "visitors" if presence_weighting == "tracks" else "observations"


def _cell(x: float, y: float, width: float, height: float, grid_w: int, grid_h: int) -> Optional[tuple[int, int]]:
    """(row, col) of a point, or None outside [0, width] x [0, height]."""
    if not (0 <= x <= width and 0 <= y <= height):
        return None
    gx = min(int(x / max(width, 1e-6) * grid_w), grid_w - 1)
    gy = min(int(y / max(height, 1e-6) * grid_h), grid_h - 1)
    return gy, gx


def bin_heatmap_points(points, width: float, height: float, grid_w: int, grid_h: int) -> tuple[list[list[float]], int]:
    """Count (x, y) points per cell (shelf-interaction positions). None coordinates are skipped."""
    grid = [[0.0] * grid_w for _ in range(grid_h)]
    samples = 0
    for x, y in points:
        if x is None or y is None:
            continue
        c = _cell(float(x), float(y), width, height, grid_w, grid_h)
        if c is not None:
            grid[c[0]][c[1]] += 1.0
            samples += 1
    return grid, samples


def bin_heatmap_paths(
    paths,
    kind: str,
    width: float,
    height: float,
    grid_w: int,
    grid_h: int,
    *,
    xkey: str = "x",
    ykey: str = "y",
    window: Optional[tuple[float, float]] = None,
    presence_weighting: str = "samples",
    max_gap: float = HEATMAP_DWELL_MAX_GAP_SEC,
) -> tuple[list[list[float]], int]:
    """Bin trajectory points into a ``grid_h`` x ``grid_w`` grid. Returns (grid, samples).

    ``paths`` yields ``(points, untimed_in_window)``: ``points`` is a track's
    ``trajectory_points`` (dicts with ``xkey``/``ykey`` and epoch ``t``);
    points lacking the keys are ignored (floor space skips image-only points
    and vice versa). With ``window`` = (t_from, t_to) epoch seconds only points
    with ``t_from <= t < t_to`` count; untimed legacy points count when
    ``untimed_in_window``. ``samples`` = points binned.

    presence/samples: +1 per point. presence/tracks: +1 per (track, cell).
    dwell: + seconds to the track's next point (gaps over ``max_gap`` and
    non-increasing times are not credited).
    """
    grid = [[0.0] * grid_w for _ in range(grid_h)]
    samples = 0
    for points, untimed_in_window in paths:
        pts = []
        for p in points or []:
            try:
                pts.append((float(p[xkey]), float(p[ykey]), p.get("t")))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        seen: set = set()
        for i, (x, y, t) in enumerate(pts):
            if window is not None:
                if t is None:
                    if not untimed_in_window:
                        continue
                elif not (window[0] <= float(t) < window[1]):
                    continue
            if kind == "dwell":
                if i + 1 >= len(pts) or t is None or pts[i + 1][2] is None:
                    continue
                dt = float(pts[i + 1][2]) - float(t)
                if dt <= 0 or dt > max_gap:
                    continue
                weight = dt
            else:
                weight = 1.0
            c = _cell(x, y, width, height, grid_w, grid_h)
            if c is None:
                continue
            samples += 1
            if kind == "presence" and presence_weighting == "tracks":
                if c in seen:
                    continue
                seen.add(c)
            grid[c[0]][c[1]] += weight
    return grid, samples


def _zone_dimensions(polygon: list, category: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """(area m^2, shelf run length m for SHELF zones) of a zone polygon."""
    from app.services import coverage_geometry as cg

    pts = cg._pts(polygon)
    area = cg.polygon_area(pts)
    if area <= 0:
        return None, None
    length = None
    if str(category or "").upper() == "SHELF":
        length = round(cg.oriented_box(pts)[0], 2) or None
    return round(area, 2), length


# A step between two consecutive floor points of one track implying a speed
# above PATH_MAX_SPEED_MPS (or, without timestamps, longer than
# PATH_MAX_STEP_M) is a tracking jump (an id switch or a projection glitch),
# not walking: that step is not counted.
PATH_MAX_STEP_M = 4.0
PATH_MAX_SPEED_MPS = 3.0


def _raw_points():
    """trajectory_points as stored text: decoded on a worker thread, not on the event loop."""
    return type_coerce(CustomerTrackModel.trajectory_points, String)


def _decode_points(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def paths_retained_since(now: Optional[datetime] = None) -> Optional[datetime]:
    """Oldest start_time whose raw path points are still kept (db_maintenance), naive UTC; None = all."""
    from app.services.db_maintenance import _points_retention_days

    days = _points_retention_days()
    if days <= 0:
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - timedelta(days=days)


def path_length_m(points: list) -> Optional[float]:
    """Metres walked along one track's recorded floor points ({x, y, t}).

    Image-only samples ({u, v}) are skipped; implausible jumps are not counted.
    None when the track has fewer than two floor points.
    """
    prev = None
    total = 0.0
    n = 0
    for p in points or []:
        if not isinstance(p, dict) or "x" not in p or "y" not in p:
            continue
        try:
            x, y = float(p["x"]), float(p["y"])
            t = float(p["t"]) if p.get("t") is not None else None
        except (TypeError, ValueError):
            continue
        n += 1
        if prev is not None:
            px, py, pt = prev
            step = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
            dt = (t - pt) if (t is not None and pt is not None) else None
            # With timestamps the speed decides; without, a single step may not exceed PATH_MAX_STEP_M.
            ok = (step / dt <= PATH_MAX_SPEED_MPS) if (dt is not None and dt > 0) else step <= PATH_MAX_STEP_M
            if ok:
                total += step
        prev = (x, y, t)
    return round(total, 2) if n >= 2 else None


@dataclass
class ZoneMetrics:
    """What was actually observed in one zone over a window."""

    zone_id: str
    name: str
    category: str
    visits: int = 0
    unique_visitors: int = 0
    avg_dwell_seconds: Optional[float] = None
    total_dwell_seconds: float = 0.0
    interactions: int = 0
    occupancy_now: int = 0
    # Product categories the operator says the zone sells (store_zones.products).
    products: list = field(default_factory=list)
    # Real size of the zone on the map (m^2) and, for SHELF zones, the length
    # of the shelf run (long side, m). None when the polygon has no area.
    area_m2: Optional[float] = None
    shelf_length_m: Optional[float] = None
    # Shelf reaches recorded against this zone (shelf_interactions.shelf_zone_id),
    # None when none were recorded in the window.
    reaches: Optional[int] = None
    # CHECKOUT zones: the queue right now in floor metres (services/queue_length.py),
    # with a reason instead of a number when no calibrated camera sees the zone.
    queue: Optional[dict] = None

    @property
    def engagement_rate(self) -> Optional[float]:
        """Share of visits that included a shelf interaction."""
        if not self.visits:
            return None
        return round(self.interactions / self.visits * 100.0, 1)

    def to_dict(self) -> dict:
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "category": self.category,
            "visits": self.visits,
            "unique_visitors": self.unique_visitors,
            "avg_dwell_seconds": self.avg_dwell_seconds,
            "total_dwell_seconds": round(self.total_dwell_seconds, 1),
            "interactions": self.interactions,
            "engagement_rate_pct": self.engagement_rate,
            "occupancy_now": self.occupancy_now,
            "products": list(self.products),
            "observed": self.visits > 0,
            # Space-normalised figures (real map metres). Null when not observed
            # or when the zone has no area: never 0 for "nothing seen".
            "area_m2": self.area_m2,
            "visits_per_m2": self.visits_per_m2,
            "dwell_seconds_per_m2": self.dwell_seconds_per_m2,
            "people_per_m2_now": self.people_per_m2_now,
            "shelf_length_m": self.shelf_length_m,
            "reaches": self.reaches,
            "reaches_per_shelf_m": self.reaches_per_shelf_m,
            # CHECKOUT zones only (None elsewhere): first to last person, metres.
            "queue_length_m": self.queue.get("length_m") if self.queue else None,
            "queue": self.queue,
        }

    def _per_area(self, value: Optional[float], digits: int = 3) -> Optional[float]:
        if value is None or not self.area_m2 or self.area_m2 <= 0:
            return None
        return round(value / self.area_m2, digits)

    @property
    def visits_per_m2(self) -> Optional[float]:
        """Visits per square metre of zone: traffic density, fair between big and small zones."""
        return self._per_area(self.visits) if self.visits > 0 else None

    @property
    def dwell_seconds_per_m2(self) -> Optional[float]:
        """Shopper-seconds spent per square metre (total dwell / area)."""
        return self._per_area(self.total_dwell_seconds, 1) if self.visits > 0 and self.total_dwell_seconds > 0 else None

    @property
    def people_per_m2_now(self) -> Optional[float]:
        """People in the zone now per square metre; only once the zone has been observed."""
        return self._per_area(self.occupancy_now) if self.visits > 0 else None

    @property
    def reaches_per_shelf_m(self) -> Optional[float]:
        """Shelf reaches per metre of shelf run (SHELF zones with recorded reaches)."""
        if not self.reaches or not self.shelf_length_m or self.shelf_length_m <= 0:
            return None
        return round(self.reaches / self.shelf_length_m, 2)


class RetailMetricsService:
    """Aggregates over real observations. Returns None where nothing was seen."""

    # ------------------------------------------------------------ primitives

    async def has_any_observations(self, db: AsyncSession) -> bool:
        """Whether the pipeline has ever recorded anything.

        Drives the dashboard's global empty state, so the UI can say
        "no cameras reporting" rather than rendering a grid of zeros.
        """
        count = await db.scalar(select(func.count(ZoneVisitModel.id)))
        return bool(count)

    async def footfall(
        self, db: AsyncSession, start: datetime, end: datetime
    ) -> Optional[int]:
        """People who came into the store in the window.

        Source precedence (never summed, so nobody is counted twice):

        1. **Entrance tripwires** -- when any footfall-counting tripwire
           recorded a crossing in the window, footfall is the number of "in"
           crossings on those lines. A line at the door counts each entry once,
           whereas track ids fragment when a shopper is occluded and one person
           walking past several cameras gets several ids. Every entrance needs
           a line for this to be complete.
        2. **Zone visits** -- distinct track ids with a zone visit, so one
           shopper walking through six aisles is one person, not six.
        3. **Raw tracks** -- a store with no zones drawn still sees people.

        ``footfall_source`` reports which one produced the figure.

        Camera roles (services/camera_roles.py): lines on door cameras are
        preferred over other lines, and stockroom cameras never count in any
        of the three sources. Cameras without a role behave as before.
        """
        entries = await self._tripwire_entries(db, start, end)
        if entries is not None:
            return entries
        excluded = await self._non_footfall_cameras(db)
        count = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end,
                     _visit_is_real(), ZoneVisitModel.camera_id.notin_(excluded))
            )
        )
        if count:
            return int(count)
        # Fall back to raw tracks: a store with no zones drawn yet still sees
        # people, and reporting nothing at all would hide a working camera.
        # Short fragments are not people (see _track_is_real).
        track_count = await db.scalar(
            select(func.count(func.distinct(CustomerTrackModel.track_id))).where(
                and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end,
                     _track_is_real(), CustomerTrackModel.camera_id.notin_(excluded))
            )
        )
        return int(track_count) if track_count else None

    @staticmethod
    async def _non_footfall_cameras(db: AsyncSession) -> list[str]:
        """Camera ids that never count store footfall: stockroom role, and duplicate cameras."""
        from app.services.camera_roles import non_footfall_camera_ids

        return sorted(set(await non_footfall_camera_ids(db)) | set(duplicate_camera_ids()))

    @staticmethod
    async def _tripwire_entries(db: AsyncSession, start: datetime, end: datetime) -> Optional[int]:
        try:
            from app.services.tripwire_engine import tripwire_entries

            return await tripwire_entries(db, start, end)
        except Exception as e:  # table missing on an unmigrated DB, etc.
            logger.debug(f"tripwire footfall unavailable: {e}")
            return None

    async def footfall_source(self, db: AsyncSession, start: datetime, end: datetime) -> Optional[str]:
        """Which signal ``footfall`` used for this window: tripwire | zone_visits | tracks | None."""
        if await self._tripwire_entries(db, start, end) is not None:
            return "tripwire"
        excluded = await self._non_footfall_cameras(db)
        if await db.scalar(select(func.count(ZoneVisitModel.id)).where(
                and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end, _visit_is_real(),
                     ZoneVisitModel.camera_id.notin_(excluded)))):
            return "zone_visits"
        if await db.scalar(select(func.count(CustomerTrackModel.id)).where(
                and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end,
                     _track_is_real(), CustomerTrackModel.camera_id.notin_(excluded)))):
            return "tracks"
        return None

    async def active_shoppers(self, db: AsyncSession) -> int:
        """People currently inside a zone, from visits with no exit time yet."""
        count = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(ZoneVisitModel.exited_at.is_(None), _not_duplicate(ZoneVisitModel.camera_id))
            )
        )
        return int(count or 0)

    async def avg_dwell_seconds(
        self, db: AsyncSession, start: datetime, end: datetime
    ) -> Optional[float]:
        """Mean completed dwell. Open visits are excluded as incomplete."""
        avg = await db.scalar(
            select(func.avg(ZoneVisitModel.dwell_seconds)).where(
                and_(
                    ZoneVisitModel.entered_at >= start,
                    ZoneVisitModel.entered_at < end,
                    ZoneVisitModel.exited_at.isnot(None),
                    ZoneVisitModel.dwell_seconds > 0,
                    _visit_is_real(),
                    _not_duplicate(ZoneVisitModel.camera_id),
                )
            )
        )
        return round(float(avg), 1) if avg else None

    async def pos_totals(
        self, db: AsyncSession, start: datetime, end: datetime
    ) -> tuple[Optional[float], Optional[int]]:
        """Revenue and distinct transaction count from ingested POS data."""
        row = (
            await db.execute(
                select(
                    func.sum(POSTransactionModel.amount),
                    func.count(func.distinct(POSTransactionModel.transaction_id)),
                ).where(
                    and_(
                        POSTransactionModel.timestamp >= start,
                        POSTransactionModel.timestamp < end,
                    )
                )
            )
        ).one()
        revenue, txns = row
        return (
            round(float(revenue), 2) if revenue else None,
            int(txns) if txns else None,
        )

    # ---------------------------------------------------------------- zones

    async def zone_metrics(
        self, db: AsyncSession, layout_id: str, start: datetime, end: datetime
    ) -> list[ZoneMetrics]:
        """Per-zone aggregates for every zone on the blueprint.

        Zones with no observations are still returned, with ``observed=False``,
        so the operator can tell a genuinely dead aisle apart from one that
        simply has no camera covering it.
        """
        zones = (
            await db.execute(
                select(StoreZoneModel)
                .where(StoreZoneModel.layout_id == layout_id)
                .order_by(StoreZoneModel.sort_order)
            )
        ).scalars().all()

        agg_rows = (
            await db.execute(
                select(
                    ZoneVisitModel.zone_id,
                    func.count(ZoneVisitModel.id),
                    func.count(func.distinct(ZoneVisitModel.track_id)),
                    func.avg(
                        case((ZoneVisitModel.exited_at.isnot(None), ZoneVisitModel.dwell_seconds))
                    ),
                    func.sum(ZoneVisitModel.dwell_seconds),
                    func.sum(case((ZoneVisitModel.interacted.is_(True), 1), else_=0)),
                )
                .where(
                    and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end,
                         _visit_is_real(), _not_duplicate(ZoneVisitModel.camera_id))
                )
                .group_by(ZoneVisitModel.zone_id)
            )
        ).all()
        agg = {r[0]: r for r in agg_rows}

        occ_rows = (
            await db.execute(
                select(ZoneVisitModel.zone_id, func.count(func.distinct(ZoneVisitModel.track_id)))
                .where(and_(ZoneVisitModel.exited_at.is_(None), _not_duplicate(ZoneVisitModel.camera_id)))
                .group_by(ZoneVisitModel.zone_id)
            )
        ).all()
        occupancy = {r[0]: int(r[1]) for r in occ_rows}

        reach_rows = (
            await db.execute(
                select(ShelfInteractionModel.shelf_zone_id, func.count(ShelfInteractionModel.id))
                .where(and_(ShelfInteractionModel.timestamp >= start, ShelfInteractionModel.timestamp < end,
                            _not_duplicate(ShelfInteractionModel.camera_id)))
                .group_by(ShelfInteractionModel.shelf_zone_id)
            )
        ).all()
        reaches = {r[0]: int(r[1]) for r in reach_rows if r[1]}

        out: list[ZoneMetrics] = []
        for z in zones:
            m = ZoneMetrics(zone_id=z.id, name=z.name, category=z.category,
                            products=list(getattr(z, "products", None) or []))
            m.area_m2, m.shelf_length_m = _zone_dimensions(z.polygon or [], z.category)
            m.reaches = reaches.get(z.id)
            if (row := agg.get(z.id)) is not None:
                _, visits, uniques, avg_dwell, total_dwell, interactions = row
                m.visits = int(visits or 0)
                m.unique_visitors = int(uniques or 0)
                m.avg_dwell_seconds = round(float(avg_dwell), 1) if avg_dwell else None
                m.total_dwell_seconds = float(total_dwell or 0.0)
                m.interactions = int(interactions or 0)
            m.occupancy_now = occupancy.get(z.id, 0)
            out.append(m)
        queues = [z for z in zones if z.category == "CHECKOUT"]
        if queues:
            lengths = self.queue_lengths(queues)
            for m in out:
                m.queue = lengths.get(m.zone_id)
        return out

    @staticmethod
    def queue_lengths(zones: list) -> dict:
        """Queue length now for each zone, from calibrated floor positions only.

        Coverage is the largest share of the zone one calibrated camera sees
        (``restricted_areas.floor_coverage``, cached per camera and zone). The
        people are the live map's persons (confirmed tracks of calibrated cameras).
        """
        from app.services import queue_length
        from app.services.live_analytics_engine import live_engine
        from app.services.restricted_areas import floor_coverage
        from app.services.tracking_service import floor_projector

        try:
            persons = live_engine.live_snapshot().get("persons") or []
        except Exception:  # noqa: BLE001 - the live map must never break the figures
            persons = []
        calibrated = [cid for cid in list(live_engine.runtimes) if floor_projector.has_homography(cid)]
        out = {}
        for z in zones:
            poly = [(float(p["x"]), float(p["y"])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                    for p in (z.polygon or [])]
            covered = 0.0
            for cid in calibrated:
                try:
                    covered = max(covered, float(floor_coverage(cid, poly, None)[0]))
                except Exception as e:  # noqa: BLE001 - one bad matrix must not hide the rest
                    logger.debug(f"queue coverage {cid}/{z.id}: {e}")
            out[z.id] = queue_length.measure(persons, poly, covered or None)
        return out

    # --------------------------------------------------------------- funnel

    async def funnel(
        self, db: AsyncSession, layout_id: str, start: datetime, end: datetime
    ) -> dict:
        """The conversion funnel, from observations only.

        Stages are derived as follows, and any stage without data is reported
        as null rather than interpolated:

          passed    -- distinct people seen anywhere in the store
          dwelled   -- distinct people whose dwell cleared the engagement floor
          engaged   -- distinct people who reached toward a shelf
          converted -- distinct POS transactions in the window

        Conversion therefore compares observed shoppers against real till
        activity. It is null until both signals exist, because a ratio built
        on one of them is meaningless.
        """
        passed = await self.footfall(db, start, end)

        dwelled = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(
                    ZoneVisitModel.entered_at >= start,
                    ZoneVisitModel.entered_at < end,
                    ZoneVisitModel.dwell_seconds > 0,
                    _visit_is_real(),
                    _not_duplicate(ZoneVisitModel.camera_id),
                )
            )
        )
        engaged = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(
                    ZoneVisitModel.entered_at >= start,
                    ZoneVisitModel.entered_at < end,
                    ZoneVisitModel.interacted.is_(True),
                    _visit_is_real(),
                    _not_duplicate(ZoneVisitModel.camera_id),
                )
            )
        )
        revenue, converted = await self.pos_totals(db, start, end)

        def pct(num: Optional[int], den: Optional[int]) -> Optional[float]:
            if not num or not den:
                return None
            return round(num / den * 100.0, 1)

        return {
            "stages": [
                {"stage": "Passed", "count": passed, "observed": passed is not None},
                {"stage": "Dwelled", "count": int(dwelled) if dwelled else None,
                 "observed": bool(dwelled)},
                {"stage": "Engaged", "count": int(engaged) if engaged else None,
                 "observed": bool(engaged)},
                {"stage": "Converted", "count": converted, "observed": converted is not None},
            ],
            "attraction_rate_pct": pct(int(dwelled) if dwelled else None, passed),
            "engagement_rate_pct": pct(int(engaged) if engaged else None,
                                       int(dwelled) if dwelled else None),
            "conversion_rate_pct": pct(converted, passed),
            "revenue": revenue,
            # Friction: shoppers who engaged with product but did not buy.
            "lost_sales_index_pct": (
                round((1 - converted / engaged) * 100.0, 1)
                if converted and engaged and engaged >= converted
                else None
            ),
            "pos_connected": converted is not None,
        }

    # -------------------------------------------------------------- heatmap

    HEATMAP_KINDS = HEATMAP_KINDS
    HEATMAP_DWELL_MAX_GAP_SEC = HEATMAP_DWELL_MAX_GAP_SEC

    async def heatmap(
        self,
        db: AsyncSession,
        layout_id: str,
        start: datetime,
        end: datetime,
        width_m: float,
        height_m: float,
        grid_w: int = 50,
        grid_h: int = 30,
        kind: str = "presence",
        presence_weighting: str = "samples",
    ) -> dict:
        """Floor density built from real observations.

        ``kind``:

        * ``presence``    -- where people were. ``presence_weighting="samples"``
          (default): one count per recorded floor point; ``"tracks"``: each
          track counts once per cell it entered (visitors per cell, as the
          recorded history stores it), so someone standing still for an
          hour is one visitor, not thousands of samples.
        * ``dwell``       -- seconds spent per cell: each trajectory point is
          weighted by the time to the track's next point (gaps over
          ``HEATMAP_DWELL_MAX_GAP_SEC`` are not credited).
        * ``interaction`` -- shelf interactions from the pose pipeline, binned
          at the shopper's floor position when the reach started. Only
          interactions seen by a calibrated camera have a floor position.

        Tracks are selected by ``start_time`` in the window. The binning is
        the shared ``bin_heatmap_paths`` / ``bin_heatmap_points`` also used by
        the recorded history (services/heatmap_history.py).

        The matrix is normalised to 0-1 for rendering; ``peak_value`` and
        ``unit`` give the absolute scale. With nothing observed the matrix is
        omitted entirely rather than returned as a synthetic blob.
        """
        kind = (kind or "presence").lower()
        if kind not in self.HEATMAP_KINDS:
            raise ValueError(f"unknown heatmap kind '{kind}'; expected one of {', '.join(self.HEATMAP_KINDS)}")
        if presence_weighting not in PRESENCE_WEIGHTINGS:
            raise ValueError(f"unknown presence weighting '{presence_weighting}'")
        # Customer heatmaps: staff-only cameras (stockroom role) never contribute.
        # Duplicate cameras (same physical camera as another) are left out too:
        # this is a floor-space merge across cameras.
        staff_cams = sorted(set(await staff_only_camera_ids(db)) | set(duplicate_camera_ids()))

        if kind == "interaction":
            rows = (
                await db.execute(
                    select(ShelfInteractionModel.floor_x, ShelfInteractionModel.floor_y).where(
                        and_(
                            ShelfInteractionModel.timestamp >= start,
                            ShelfInteractionModel.timestamp < end,
                            ShelfInteractionModel.camera_id.notin_(staff_cams),
                        )
                    )
                )
            ).all()
            unplaced = sum(1 for fx, fy in rows if fx is None or fy is None)
            grid, samples = bin_heatmap_points(rows, width_m, height_m, grid_w, grid_h)
            unit = "interactions"
            empty_msg = (
                f"{unplaced} shelf interaction(s) recorded today, but none from a calibrated camera, "
                "so none can be placed on the floor plan."
                if unplaced else
                "No shelf interactions recorded today. Draw product zones on a camera and let the pose pipeline run."
            )
            extra = {"unplaced_interactions": unplaced, "interactions_total": len(rows)}
        else:
            # Raw shopper paths are kept TRACK_POINTS_RETENTION_DAYS (db_maintenance);
            # older hours live in the recorded heatmap history (/heatmaps/history).
            kept_since = paths_retained_since()
            extra = {}
            if kept_since is not None and start < kept_since:
                extra["paths_from"] = kept_since.isoformat() + "Z"
                extra["note"] = ("Shopper paths older than the raw-path retention are kept only as the "
                                 "recorded heatmap history; this map covers the period since then.")
                start = max(start, kept_since)
            rows = (
                await db.execute(
                    select(_raw_points()).where(
                        and_(
                            CustomerTrackModel.start_time >= start,
                            CustomerTrackModel.start_time < end,
                            CustomerTrackModel.camera_id.notin_(staff_cams),
                        )
                    )
                )
            ).scalars().all()
            # JSON decode and binning of possibly months of paths: a worker
            # thread, not the event loop (audit L10).
            grid, samples = await asyncio.to_thread(
                lambda: bin_heatmap_paths(
                    ((_decode_points(raw), True) for raw in rows), kind, width_m, height_m, grid_w, grid_h,
                    presence_weighting=presence_weighting,
                ))
            unit = heatmap_unit(kind, presence_weighting)
            empty_msg = "No trajectories recorded. Calibrate a camera to place people on the floor plan."
        if kind == "presence":
            extra["presence_weighting"] = presence_weighting

        if not samples:
            return {
                "kind": kind,
                "unit": unit,
                "grid_width": grid_w,
                "grid_height": grid_h,
                "density_matrix": None,
                "samples": 0,
                "observed": False,
                "message": empty_msg,
                **extra,
            }

        peak = max(max(row) for row in grid) or 1.0
        cell_m2 = (float(width_m) / grid_w) * (float(height_m) / grid_h) if grid_w and grid_h else None
        return {
            "kind": kind,
            "unit": unit,
            "grid_width": grid_w,
            "grid_height": grid_h,
            "density_matrix": [[round(v / peak, 4) for v in row] for row in grid],
            "samples": samples,
            "peak_count": int(round(peak)),
            "peak_value": round(peak, 2),
            # Real size of one heatmap cell and the peak per square metre, so
            # maps of different grid sizes compare fairly.
            "cell_area_m2": round(cell_m2, 4) if cell_m2 else None,
            "peak_per_m2": round(peak / cell_m2, 3) if cell_m2 else None,
            "observed": True,
            **extra,
        }

    # --------------------------------------------------------------- queues

    async def checkout_queues(
        self, db: AsyncSession, layout_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Queue state per checkout lane, from live occupancy and past dwell.

        Two sources, reported side by side (``source``):

        * ``floor_zone``  -- blueprint CHECKOUT zones, fed by calibrated cameras
          through ``zone_visits``;
        * ``camera_area`` -- Studio checkout / queue areas on a camera (image
          space, no calibration needed), fed through ``queue_visits``.
          ``kind`` is ``checkout`` (time at the lane) or ``queue`` (wait).

        A lane is linked to a POS register through its checkout camera's
        ``pos_register_id``; ``pos_transactions`` counts that register's
        distinct transactions in the window, and ``conversion_pct`` is
        transactions per customer seen at the lane (an estimate: fragmented
        tracks inflate the customer count). Unlinked lanes report null.
        """
        metrics = await self.zone_metrics(db, layout_id, start, end)
        cams = {c.id: c for c in (await db.execute(select(CameraModel))).scalars().all()}
        pos = await self._pos_by_register(db, start, end)
        congested = float(settings.QUEUE_CONGESTED_WAIT_SEC)
        lanes = []
        floor = [m for m in metrics if m.category == "CHECKOUT"]
        zone_cams: dict[str, set] = {}
        if floor:
            rows = (await db.execute(
                select(ZoneVisitModel.zone_id, ZoneVisitModel.camera_id).distinct().where(and_(
                    ZoneVisitModel.zone_id.in_([m.zone_id for m in floor]),
                    ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end))
            )).all()
            for zid, cid in rows:
                zone_cams.setdefault(zid, set()).add(cid)
        for m in floor:
            wait = m.avg_dwell_seconds
            if m.occupancy_now == 0:
                status = "IDLE" if m.visits else "NO DATA"
            elif wait and wait > congested:
                status = "CONGESTED"
            else:
                status = "OPEN"
            registers = sorted({cams[c].pos_register_id for c in zone_cams.get(m.zone_id, ())
                                if c in cams and cams[c].role == "checkout" and cams[c].pos_register_id})
            lanes.append(
                {
                    "zone_id": m.zone_id,
                    "name": m.name,
                    "source": "floor_zone",
                    "kind": "checkout",
                    "camera_ids": sorted(zone_cams.get(m.zone_id, ())),
                    "queue_length_now": m.occupancy_now,
                    # Metres from the first to the last person, None (with the reason) when
                    # no calibrated camera sees the lane: never a guess from the count.
                    "queue_length_m": m.queue.get("length_m") if m.queue else None,
                    "queue_length_reason": m.queue.get("reason") if m.queue else None,
                    "queue_spacing_m": m.queue.get("spacing_m") if m.queue else None,
                    "avg_wait_seconds": wait,
                    "avg_wait_minutes": round(wait / 60.0, 1) if wait else None,
                    "served_today": m.unique_visitors,
                    "status": status,
                    "observed": m.visits > 0,
                    **self._lane_pos(registers, pos, m.unique_visitors),
                }
            )
        lanes.extend(await self._camera_area_lanes(db, cams, pos, start, end, congested))
        return lanes

    @staticmethod
    async def _pos_by_register(db: AsyncSession, start: datetime, end: datetime) -> dict:
        rows = (await db.execute(
            select(POSTransactionModel.register_id,
                   func.count(func.distinct(POSTransactionModel.transaction_id)),
                   func.sum(POSTransactionModel.amount))
            .where(and_(POSTransactionModel.timestamp >= start, POSTransactionModel.timestamp < end))
            .group_by(POSTransactionModel.register_id))).all()
        return {str(r): (int(n or 0), float(a or 0.0)) for r, n, a in rows if r}

    @staticmethod
    def _lane_pos(registers: list, pos: dict, served: Optional[int]) -> dict:
        if not registers:
            return {"register_ids": [], "pos_transactions": None, "pos_revenue": None, "conversion_pct": None}
        txns = sum(pos.get(r, (0, 0.0))[0] for r in registers)
        revenue = sum(pos.get(r, (0, 0.0))[1] for r in registers)
        return {
            "register_ids": registers,
            "pos_transactions": txns if txns else None,
            "pos_revenue": round(revenue, 2) if txns else None,
            "conversion_pct": round(txns / served * 100.0, 1) if txns and served else None,
        }

    async def _camera_area_lanes(self, db: AsyncSession, cams: dict, pos: dict, start: datetime,
                                 end: datetime, congested: float) -> list[dict]:
        from app.models.db_models import QueueVisitModel
        from app.services.ai_zone_service import ai_zone_service
        from app.services.tripwire_engine import tripwire_engine

        try:
            areas = [a for a in ai_zone_service.get_all_zones().get("queue_zones", []) if a.get("enabled", True)]
        except Exception:
            areas = []
        if not areas:
            return []
        agg = {}
        try:
            rows = (await db.execute(
                select(QueueVisitModel.area_id, func.count(QueueVisitModel.id),
                       func.count(func.distinct(QueueVisitModel.track_id)),
                       func.avg(QueueVisitModel.dwell_seconds))
                .where(and_(QueueVisitModel.entered_at >= start, QueueVisitModel.entered_at < end,
                            QueueVisitModel.area_id.in_([a["id"] for a in areas])))
                .group_by(QueueVisitModel.area_id))).all()
            agg = {r[0]: r for r in rows}
        except Exception as e:  # unmigrated DB
            logger.debug(f"queue_visits unavailable: {e}")
        live = tripwire_engine.queue_occupancy()
        out = []
        for a in areas:
            _, visits, uniques, avg = agg.get(a["id"], (a["id"], 0, 0, None))
            visits, uniques = int(visits or 0), int(uniques or 0)
            wait = round(float(avg), 1) if avg else None
            now = live.get(a["id"])
            if now is None:
                status = "NOT RUNNING"
            elif now == 0:
                status = "IDLE"
            elif wait and wait > congested:
                status = "CONGESTED"
            else:
                status = "OPEN"
            cam = cams.get(a.get("camera_id"))
            reg = cam.pos_register_id if cam is not None and cam.role == "checkout" else None
            out.append({
                "zone_id": a["id"],
                "name": a.get("name") or a["id"],
                "source": "camera_area",
                "kind": str(a.get("kind") or "queue"),
                "camera_id": a.get("camera_id"),
                "camera_ids": [a.get("camera_id")] if a.get("camera_id") else [],
                "queue_length_now": now,
                "avg_wait_seconds": wait,
                "avg_wait_minutes": round(wait / 60.0, 1) if wait else None,
                "served_today": uniques,
                "status": status,
                "observed": visits > 0,
                # A queue area measures waiting, not purchases: POS figures go on the lane.
                **self._lane_pos([reg] if reg and str(a.get("kind")) == "checkout" else [], pos, uniques),
            })
        return out

    # ------------------------------------------------------------- coverage

    async def coverage(self, db: AsyncSession) -> dict:
        """How much of the store the system can actually see.

        Calibration matters: an uncalibrated camera detects people but cannot
        place them on the floor, so it contributes no zone or heatmap data.
        Surfacing this stops an operator reading an empty heatmap as "no
        shoppers" when the real cause is "no camera is calibrated".
        """
        from app.services.camera_setup_state import camera_setup_state

        cams = (await db.execute(select(CameraModel).order_by(CameraModel.name))).scalars().all()
        total = len(cams)
        calibrated = sum(1 for c in cams if c.homography_matrix)
        zone_ids = {z for (z,) in (await db.execute(select(StoreZoneModel.id))).all()}
        state = camera_setup_state(cams, zone_ids)
        return {
            "cameras_total": total,
            "cameras_calibrated": calibrated,
            "cameras_uncalibrated": total - calibrated,
            # On the store map (cameras.map_placed). Placed is NOT calibrated.
            "cameras_placed": state["placed"],
            "floor_tracking_possible": calibrated > 0,
            # Not placed / placed but not calibrated / calibrated, with text and next action.
            "camera_setup": state,
        }

    # -------------------------------------------------------------- forecast

    async def _visit_times(self, db: AsyncSession, since: Optional[datetime] = None) -> list[tuple]:
        """(track_id, entered_at) of real visits, entered_at as naive UTC."""
        q = select(ZoneVisitModel.track_id, ZoneVisitModel.entered_at).where(
            and_(_visit_is_real(), _not_duplicate(ZoneVisitModel.camera_id)))
        if since is not None:
            q = q.where(ZoneVisitModel.entered_at >= since)
        return (await db.execute(q)).all()

    async def hourly_history(
        self, db: AsyncSession, days_back: int = 28
    ) -> dict[int, list[int]]:
        """Distinct visitors per store-local clock hour, per local day, over recent history.

        Grouped in Python after converting each stored UTC time to store time,
        so hours stay right across DST and in half-hour time zones.
        """
        since = utcnow() - timedelta(days=days_back)
        buckets: dict[tuple[str, int], set] = {}
        for track_id, entered in await self._visit_times(db, since):
            if entered is None:
                continue
            local = to_local(entered)
            buckets.setdefault((local.date().isoformat(), local.hour), set()).add(track_id)

        by_hour: dict[int, list[int]] = {}
        for (_day, h), tracks in sorted(buckets.items()):
            by_hour.setdefault(h, []).append(len(tracks))
        return by_hour

    async def forecast_hourly(
        self, db: AsyncSession, *, min_days: int = 3
    ) -> dict:
        """Forecast today's hourly footfall from this store's own history.

        This is a per-hour mean with a spread band over observed days -- a
        genuine, if simple, estimator that improves as history accumulates.
        It replaces a fixed table of hand-authored hour weights multiplied by a
        hardcoded daily volume of 3,420, which produced confident-looking
        forecasts for a store that had never been measured.

        Returns ``sufficient_history=False`` until there is enough data, rather
        than extrapolating from one afternoon.
        """
        by_hour = await self.hourly_history(db)
        # Store-local days with at least one real visit (UTC hours folded to local dates).
        utc_hours = (await db.execute(
            select(func.distinct(func.strftime("%Y-%m-%d %H:00", ZoneVisitModel.entered_at)))
            .where(and_(_visit_is_real(), _not_duplicate(ZoneVisitModel.camera_id)))
        )).scalars().all()
        days = len({to_local(datetime.strptime(h, "%Y-%m-%d %H:%M")).date() for h in utc_hours if h})

        if days < min_days or not by_hour:
            return {
                "sufficient_history": False,
                "days_observed": days,
                "days_required": min_days,
                "hourly_forecast": [],
                "message": (
                    f"Forecasting needs at least {min_days} days of recorded footfall; "
                    f"{days} day(s) available so far."
                ),
            }

        out = []
        for h in range(24):
            samples = by_hour.get(h, [])
            if not samples:
                continue
            mean = sum(samples) / len(samples)
            spread = (max(samples) - min(samples)) / 2 if len(samples) > 1 else mean * 0.25
            out.append({
                "hour": f"{h:02d}:00",
                "expected_traffic": int(round(mean)),
                "low": int(max(0, round(mean - spread))),
                "high": int(round(mean + spread)),
                "days_sampled": len(samples),
            })

        peak = max((o["expected_traffic"] for o in out), default=0)
        for o in out:
            o["is_peak_hour"] = peak > 0 and o["expected_traffic"] >= peak * 0.9

        return {
            "sufficient_history": True,
            "days_observed": days,
            "hourly_forecast": out,
            "basis": "Per-hour mean of distinct visitors over observed days at this store.",
        }

    # ------------------------------------------------------- space (per m2)

    async def walking_distance(self, db: AsyncSession, start: datetime, end: datetime) -> dict:
        """Metres walked per tracked path (customer_tracks floor points).

        A path is one camera's sighting of a person, so a shopper crossing
        several cameras' views has several. Only calibrated cameras record
        floor points; staff-only and duplicate cameras are left out. All
        figures are None when no path with two floor points was recorded.
        """
        excluded = sorted(set(await staff_only_camera_ids(db)) | set(duplicate_camera_ids()))
        kept_since = paths_retained_since()
        extra = {}
        if kept_since is not None and start < kept_since:
            extra["paths_from"] = kept_since.isoformat() + "Z"
            start = max(start, kept_since)
        rows = (
            await db.execute(
                select(_raw_points()).where(
                    and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end,
                         _track_is_real(), CustomerTrackModel.camera_id.notin_(excluded))
                )
            )
        ).scalars().all()
        lengths = await asyncio.to_thread(
            lambda: sorted(v for v in (path_length_m(_decode_points(r)) for r in rows) if v is not None))
        if not lengths:
            return {"paths_measured": 0, "avg_path_m": None, "median_path_m": None,
                    "p90_path_m": None, "total_walked_m": None, **extra}
        n = len(lengths)
        return {
            **extra,
            "paths_measured": n,
            "avg_path_m": round(sum(lengths) / n, 1),
            "median_path_m": round(lengths[n // 2] if n % 2 else (lengths[n // 2 - 1] + lengths[n // 2]) / 2, 1),
            "p90_path_m": round(lengths[min(n - 1, int(0.9 * n))], 1),
            "total_walked_m": round(sum(lengths), 1),
        }

    async def space_metrics(self, db: AsyncSession, layout_id: str, start: datetime, end: datetime) -> dict:
        """Store figures normalised by real floor area (the map is in metres).

        * ``sales_floor_m2``: indoor rooms and countable zones (union) minus
          stockroom / excluded zones; ``sales_floor_source`` says which were
          drawn (rooms | zones | rooms+zones); None with neither.
        * ``sales_per_m2``: POS revenue in the window / sales floor (None
          without POS rows).
        * ``footfall_per_100m2``: footfall / sales floor x 100.
        * ``walking``: :meth:`walking_distance`.
        * ``zones``: per-zone visits / dwell / reaches per m^2 (zone_metrics).
        """
        from app.models.db_models import StoreLayoutModel, StoreStructureModel
        from app.services import coverage_geometry as cg

        layout = await db.get(StoreLayoutModel, layout_id)
        structures = (await db.execute(select(StoreStructureModel).where(
            StoreStructureModel.layout_id == layout_id))).scalars().all()
        zones_db = (await db.execute(select(StoreZoneModel).where(
            StoreZoneModel.layout_id == layout_id))).scalars().all()
        area, source = cg.sales_floor_area(
            float(layout.width_m) if layout else 1.0, float(layout.height_m) if layout else 1.0,
            [{"kind": s.kind, "polygon": s.polygon} for s in structures],
            [{"category": z.category, "polygon": z.polygon} for z in zones_db],
        )
        revenue, txns = await self.pos_totals(db, start, end)
        footfall = await self.footfall(db, start, end)
        zones = await self.zone_metrics(db, layout_id, start, end)
        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "sales_floor_m2": area,
            "sales_floor_source": source,
            "revenue": revenue,
            "sales_per_m2": round(revenue / area, 2) if revenue is not None and area else None,
            "transactions": txns,
            "footfall": footfall,
            "footfall_per_100m2": round(footfall / area * 100.0, 1) if footfall and area else None,
            "walking": await self.walking_distance(db, start, end),
            "zones": [
                {k: d[k] for k in ("zone_id", "name", "category", "area_m2", "visits", "visits_per_m2",
                                   "dwell_seconds_per_m2", "people_per_m2_now", "shelf_length_m",
                                   "reaches", "reaches_per_shelf_m", "observed", "queue_length_m")}
                for d in (z.to_dict() for z in zones)
            ],
            # Checkout queues now, in metres (null + reason where no calibrated camera sees them).
            "queues": [
                {"zone_id": z.zone_id, "name": z.name, **(z.queue or {})}
                for z in zones if z.queue is not None
            ],
            # Sales per m2 per zone needs till line items mapped to store areas, which the
            # till data does not carry (SKU only, no department): left out, not estimated.
            "zone_sales_per_m2": None,
            "zone_sales_per_m2_reason": ("The tills send product codes (SKU) only, with no department, and "
                                         "no product list links codes to store areas, so sales cannot be "
                                         "split by area honestly."),
        }

    # -------------------------------------------------------------- overview

    async def overview(self, db: AsyncSession, layout_id: str, store_name: str,
                       day: Optional[datetime] = None) -> dict:
        """Today's figures, or ``day``'s (store-local date; "now" values stay live)."""
        start, end = day_bounds(day)
        footfall = await self.footfall(db, start, end)
        revenue, txns = await self.pos_totals(db, start, end)
        dwell = await self.avg_dwell_seconds(db, start, end)
        active = await self.active_shoppers(db)
        cov = await self.coverage(db)
        zones = await self.zone_metrics(db, layout_id, start, end)
        observed_zones = [z for z in zones if z.visits > 0]

        return {
            "store_name": store_name,
            "timestamp": local_now().isoformat(),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            # None means not observed. The UI must render a dash, never a zero.
            "today_footfall": footfall,
            "footfall_source": await self.footfall_source(db, start, end),
            "active_shoppers_now": active,
            "avg_dwell_seconds": dwell,
            "avg_dwell_minutes": round(dwell / 60.0, 1) if dwell else None,
            "daily_revenue": revenue,
            "transactions": txns,
            "conversion_rate_pct": (
                round(txns / footfall * 100.0, 1) if txns and footfall else None
            ),
            "zones_total": len(zones),
            "zones_with_data": len(observed_zones),
            "top_zones": [z.to_dict() for z in sorted(
                observed_zones, key=lambda z: z.visits, reverse=True
            )[:5]],
            "coverage": cov,
            "has_data": footfall is not None or active > 0,
            "pos_connected": revenue is not None,
        }


retail_metrics_service = RetailMetricsService()
