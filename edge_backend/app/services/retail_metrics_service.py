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

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Float, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.timeutil import local_day_bounds_utc, to_local, utcnow

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
            "observed": self.visits > 0,
        }


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
        """
        entries = await self._tripwire_entries(db, start, end)
        if entries is not None:
            return entries
        count = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end,
                     _visit_is_real())
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
                     _track_is_real())
            )
        )
        return int(track_count) if track_count else None

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
        if await db.scalar(select(func.count(ZoneVisitModel.id)).where(
                and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end, _visit_is_real()))):
            return "zone_visits"
        if await db.scalar(select(func.count(CustomerTrackModel.id)).where(
                and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end,
                     _track_is_real()))):
            return "tracks"
        return None

    async def active_shoppers(self, db: AsyncSession) -> int:
        """People currently inside a zone, from visits with no exit time yet."""
        count = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                ZoneVisitModel.exited_at.is_(None)
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
                         _visit_is_real())
                )
                .group_by(ZoneVisitModel.zone_id)
            )
        ).all()
        agg = {r[0]: r for r in agg_rows}

        occ_rows = (
            await db.execute(
                select(ZoneVisitModel.zone_id, func.count(func.distinct(ZoneVisitModel.track_id)))
                .where(ZoneVisitModel.exited_at.is_(None))
                .group_by(ZoneVisitModel.zone_id)
            )
        ).all()
        occupancy = {r[0]: int(r[1]) for r in occ_rows}

        out: list[ZoneMetrics] = []
        for z in zones:
            m = ZoneMetrics(zone_id=z.id, name=z.name, category=z.category)
            if (row := agg.get(z.id)) is not None:
                _, visits, uniques, avg_dwell, total_dwell, interactions = row
                m.visits = int(visits or 0)
                m.unique_visitors = int(uniques or 0)
                m.avg_dwell_seconds = round(float(avg_dwell), 1) if avg_dwell else None
                m.total_dwell_seconds = float(total_dwell or 0.0)
                m.interactions = int(interactions or 0)
            m.occupancy_now = occupancy.get(z.id, 0)
            out.append(m)
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

    HEATMAP_KINDS = ("presence", "dwell", "interaction")
    # Gaps longer than this between two trajectory points of one track are
    # not credited as dwell: the person was not observed in between.
    HEATMAP_DWELL_MAX_GAP_SEC = 5.0

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
    ) -> dict:
        """Floor density built from real observations.

        ``kind``:

        * ``presence``    -- one count per recorded floor point (where people were).
        * ``dwell``       -- seconds spent per cell: each trajectory point is
          weighted by the time to the track's next point (gaps over
          ``HEATMAP_DWELL_MAX_GAP_SEC`` are not credited).
        * ``interaction`` -- shelf interactions from the pose pipeline, binned
          at the shopper's floor position when the reach started. Only
          interactions seen by a calibrated camera have a floor position.

        The matrix is normalised to 0-1 for rendering; ``peak_value`` and
        ``unit`` give the absolute scale. With nothing observed the matrix is
        omitted entirely rather than returned as a synthetic blob.
        """
        kind = (kind or "presence").lower()
        if kind not in self.HEATMAP_KINDS:
            raise ValueError(f"unknown heatmap kind '{kind}'; expected one of {', '.join(self.HEATMAP_KINDS)}")

        grid = [[0.0] * grid_w for _ in range(grid_h)]
        samples = 0

        def drop(x: float, y: float, weight: float) -> bool:
            if not (0 <= x <= width_m and 0 <= y <= height_m):
                return False
            gx = min(int(x / max(width_m, 1e-6) * grid_w), grid_w - 1)
            gy = min(int(y / max(height_m, 1e-6) * grid_h), grid_h - 1)
            grid[gy][gx] += weight
            return True

        if kind == "interaction":
            rows = (
                await db.execute(
                    select(ShelfInteractionModel.floor_x, ShelfInteractionModel.floor_y).where(
                        and_(
                            ShelfInteractionModel.timestamp >= start,
                            ShelfInteractionModel.timestamp < end,
                        )
                    )
                )
            ).all()
            unplaced = 0
            for fx, fy in rows:
                if fx is None or fy is None:
                    unplaced += 1
                    continue
                if drop(float(fx), float(fy), 1.0):
                    samples += 1
            unit = "interactions"
            empty_msg = (
                f"{unplaced} shelf interaction(s) recorded today, but none from a calibrated camera, "
                "so none can be placed on the floor plan."
                if unplaced else
                "No shelf interactions recorded today. Draw product zones on a camera and let the pose pipeline run."
            )
            extra = {"unplaced_interactions": unplaced, "interactions_total": len(rows)}
        else:
            rows = (
                await db.execute(
                    select(CustomerTrackModel.trajectory_points).where(
                        and_(
                            CustomerTrackModel.start_time >= start,
                            CustomerTrackModel.start_time < end,
                        )
                    )
                )
            ).scalars().all()
            for points in rows:
                pts = []
                for p in points or []:
                    try:
                        pts.append((float(p["x"]), float(p["y"]), p.get("t")))
                    except (KeyError, TypeError, ValueError, AttributeError):
                        continue
                for i, (x, y, t) in enumerate(pts):
                    if kind == "dwell":
                        if i + 1 >= len(pts) or t is None or pts[i + 1][2] is None:
                            continue
                        dt = float(pts[i + 1][2]) - float(t)
                        if dt <= 0 or dt > self.HEATMAP_DWELL_MAX_GAP_SEC:
                            continue
                        weight = dt
                    else:
                        weight = 1.0
                    if drop(x, y, weight):
                        samples += 1
            unit = "seconds" if kind == "dwell" else "observations"
            empty_msg = "No trajectories recorded. Calibrate a camera to place people on the floor plan."
            extra = {}

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
        return {
            "kind": kind,
            "unit": unit,
            "grid_width": grid_w,
            "grid_height": grid_h,
            "density_matrix": [[round(v / peak, 4) for v in row] for row in grid],
            "samples": samples,
            "peak_count": int(round(peak)),
            "peak_value": round(peak, 2),
            "observed": True,
            **extra,
        }

    # --------------------------------------------------------------- queues

    async def checkout_queues(
        self, db: AsyncSession, layout_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Queue state per checkout zone, from live occupancy and past dwell."""
        metrics = await self.zone_metrics(db, layout_id, start, end)
        lanes = []
        for m in metrics:
            if m.category != "CHECKOUT":
                continue
            wait = m.avg_dwell_seconds
            if m.occupancy_now == 0:
                status = "IDLE" if m.visits else "NO DATA"
            elif wait and wait > 270:
                status = "CONGESTED"
            else:
                status = "OPEN"
            lanes.append(
                {
                    "zone_id": m.zone_id,
                    "name": m.name,
                    "queue_length_now": m.occupancy_now,
                    "avg_wait_seconds": wait,
                    "avg_wait_minutes": round(wait / 60.0, 1) if wait else None,
                    "served_today": m.unique_visitors,
                    "status": status,
                    "observed": m.visits > 0,
                }
            )
        return lanes

    # ------------------------------------------------------------- coverage

    async def coverage(self, db: AsyncSession) -> dict:
        """How much of the store the system can actually see.

        Calibration matters: an uncalibrated camera detects people but cannot
        place them on the floor, so it contributes no zone or heatmap data.
        Surfacing this stops an operator reading an empty heatmap as "no
        shoppers" when the real cause is "no camera is calibrated".
        """
        cams = (await db.execute(select(CameraModel))).scalars().all()
        total = len(cams)
        calibrated = sum(1 for c in cams if c.homography_matrix)
        return {
            "cameras_total": total,
            "cameras_calibrated": calibrated,
            "cameras_uncalibrated": total - calibrated,
            "floor_tracking_possible": calibrated > 0,
        }

    # -------------------------------------------------------------- forecast

    async def _visit_times(self, db: AsyncSession, since: Optional[datetime] = None) -> list[tuple]:
        """(track_id, entered_at) of real visits, entered_at as naive UTC."""
        q = select(ZoneVisitModel.track_id, ZoneVisitModel.entered_at).where(_visit_is_real())
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
            .where(_visit_is_real())
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

    # -------------------------------------------------------------- overview

    async def overview(self, db: AsyncSession, layout_id: str, store_name: str) -> dict:
        start, end = day_bounds()
        footfall = await self.footfall(db, start, end)
        revenue, txns = await self.pos_totals(db, start, end)
        dwell = await self.avg_dwell_seconds(db, start, end)
        active = await self.active_shoppers(db)
        cov = await self.coverage(db)
        zones = await self.zone_metrics(db, layout_id, start, end)
        observed_zones = [z for z in zones if z.visits > 0]

        return {
            "store_name": store_name,
            "timestamp": datetime.now().isoformat(),
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
