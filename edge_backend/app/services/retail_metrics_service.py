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

from sqlalchemy import Float, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import (
    CameraModel,
    CustomerTrackModel,
    POSTransactionModel,
    StoreZoneModel,
    ZoneVisitModel,
)

logger = logging.getLogger(__name__)


def day_bounds(day: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Midnight-to-midnight window for a trading day."""
    d = day or datetime.now()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


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
        """Distinct people observed in the window.

        Counted as distinct track ids rather than visit rows, so one shopper
        walking through six aisles is one person, not six.
        """
        count = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end)
            )
        )
        if count:
            return int(count)
        # Fall back to raw tracks: a store with no zones drawn yet still sees
        # people, and reporting nothing at all would hide a working camera.
        track_count = await db.scalar(
            select(func.count(func.distinct(CustomerTrackModel.track_id))).where(
                and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end)
            )
        )
        return int(track_count) if track_count else None

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
                    and_(ZoneVisitModel.entered_at >= start, ZoneVisitModel.entered_at < end)
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
                )
            )
        )
        engaged = await db.scalar(
            select(func.count(func.distinct(ZoneVisitModel.track_id))).where(
                and_(
                    ZoneVisitModel.entered_at >= start,
                    ZoneVisitModel.entered_at < end,
                    ZoneVisitModel.interacted.is_(True),
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
    ) -> dict:
        """Occupancy density built from real trajectories.

        Each recorded floor point drops into a grid cell. The result is a count
        of observations per cell, normalised to 0-1 for rendering. With no
        trajectories the matrix is omitted entirely rather than returned as a
        smooth synthetic blob around four hand-picked hotspots.
        """
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

        grid = [[0.0] * grid_w for _ in range(grid_h)]
        samples = 0
        for points in rows:
            for p in points or []:
                try:
                    x, y = float(p["x"]), float(p["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (0 <= x <= width_m and 0 <= y <= height_m):
                    continue
                gx = min(int(x / max(width_m, 1e-6) * grid_w), grid_w - 1)
                gy = min(int(y / max(height_m, 1e-6) * grid_h), grid_h - 1)
                grid[gy][gx] += 1.0
                samples += 1

        if not samples:
            return {
                "grid_width": grid_w,
                "grid_height": grid_h,
                "density_matrix": None,
                "samples": 0,
                "observed": False,
                "message": "No trajectories recorded. Calibrate a camera to place people on the floor plan.",
            }

        peak = max(max(row) for row in grid) or 1.0
        return {
            "grid_width": grid_w,
            "grid_height": grid_h,
            "density_matrix": [[round(v / peak, 4) for v in row] for row in grid],
            "samples": samples,
            "peak_count": int(peak),
            "observed": True,
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

    async def hourly_history(
        self, db: AsyncSession, days_back: int = 28
    ) -> dict[int, list[int]]:
        """Distinct visitors per clock hour, per day, over recent history."""
        since = datetime.now() - timedelta(days=days_back)
        rows = (
            await db.execute(
                select(
                    func.strftime("%Y-%m-%d", ZoneVisitModel.entered_at),
                    func.strftime("%H", ZoneVisitModel.entered_at),
                    func.count(func.distinct(ZoneVisitModel.track_id)),
                )
                .where(ZoneVisitModel.entered_at >= since)
                .group_by(
                    func.strftime("%Y-%m-%d", ZoneVisitModel.entered_at),
                    func.strftime("%H", ZoneVisitModel.entered_at),
                )
            )
        ).all()

        by_hour: dict[int, list[int]] = {}
        for _day, hour, count in rows:
            try:
                h = int(hour)
            except (TypeError, ValueError):
                continue
            by_hour.setdefault(h, []).append(int(count or 0))
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
        distinct_days = await db.scalar(
            select(func.count(func.distinct(func.strftime("%Y-%m-%d", ZoneVisitModel.entered_at))))
        )
        days = int(distinct_days or 0)

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
