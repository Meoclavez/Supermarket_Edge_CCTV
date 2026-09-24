"""Business management analysis over observed retail metrics.

Two layers, deliberately separated:

1. **Deterministic rules.** Threshold checks over real zone metrics produce the
   findings. These are reproducible, explainable and cite the numbers that
   triggered them, which is what an operator needs in order to act.
2. **Local language model.** Ollama turns the findings into an executive
   narrative. It never invents a finding and never supplies a number; if the
   model is unreachable the findings still stand on their own.

Recommendations are persisted to ``ai_decision_recommendations`` so their
status survives a restart. Previously that table was populated with five
hand-written findings seeded on first boot, and the genuine rule engine that
should have filled it had no caller anywhere in the application.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import AIDecisionRecommendationModel
from app.services.retail_metrics_service import ZoneMetrics, day_bounds, retail_metrics_service

logger = logging.getLogger(__name__)

# Thresholds that define an exception worth an operator's attention. They are
# explicit and tunable rather than buried in the rule bodies.
DEAD_ZONE_TRAFFIC_RATIO = 0.25      # below a quarter of store-average traffic
HIGH_DWELL_LOW_ENGAGE_SEC = 45.0    # people linger this long...
LOW_ENGAGEMENT_PCT = 10.0           # ...but this few reach for product
QUEUE_WAIT_WARN_SECONDS = 270.0     # 4.5 minutes at a checkout
MIN_VISITS_FOR_CONFIDENCE = 5       # below this, the sample proves nothing

# Only zones where a shopper is expected to handle product can be judged on
# engagement. Dwelling at an entrance or a checkout without reaching for
# anything is normal behaviour, not a merchandising failure.
MERCHANDISING_CATEGORIES = ("AISLE", "DEPARTMENT", "SHELF")

# Product-level (shelf reach) rules, over shelf_interactions rows.
# A shelf is "dead" only when the same camera demonstrably recorded reaches
# elsewhere (so the pose pipeline was working) and saw enough shoppers.
DEAD_SHELF_MIN_CAMERA_REACHES = 10
DEAD_SHELF_MIN_CAMERA_SHOPPERS = 10
# Shelf-level comparison needs this many reaches in total, spread over at
# least one configured zone per compared level.
SHELF_LEVEL_MIN_REACHES = 20
SHELF_LEVEL_GAP_RATIO = 0.5          # top/bottom below half the eye-level rate
# High reach, low sale: only with POS rows for the day.
HIGH_REACH_MIN_SHOPPERS = 10
LOW_UNITS_PER_REACHING_SHOPPER = 0.2


class Finding:
    """One rule hit, with the evidence that produced it."""

    def __init__(
        self,
        *,
        category: str,
        severity: str,
        zone: str,
        finding: str,
        root_cause: str,
        action_item: str,
        evidence: dict[str, Any],
    ):
        self.category = category
        self.severity = severity
        self.zone = zone
        self.finding = finding
        self.root_cause = root_cause
        self.action_item = action_item
        self.evidence = evidence

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "zone": self.zone,
            "finding": self.finding,
            "root_cause": self.root_cause,
            "action_item": self.action_item,
            "evidence": self.evidence,
        }


def _detect(zones: list[ZoneMetrics], funnel: dict) -> list[Finding]:
    """Run every rule over the observed metrics.

    Only zones with enough visits to be meaningful are assessed; a zone with
    two recorded visits cannot support a claim about shopper behaviour.
    """
    findings: list[Finding] = []
    assessable = [z for z in zones if z.visits >= MIN_VISITS_FOR_CONFIDENCE]
    if not assessable:
        return findings

    # Engagement is only measurable once something reports shelf interactions.
    # Without that signal every zone reads as 0% engagement, which would make
    # the merchandising rule fire everywhere and mean nothing. Suppress the
    # rule entirely rather than emit a store full of false findings.
    engagement_tracked = any(z.interactions > 0 for z in zones)

    avg_visits = sum(z.visits for z in assessable) / len(assessable)

    for z in assessable:
        # Shoppers stop but do not engage: a merchandising or pricing problem.
        if (
            engagement_tracked
            and z.category in MERCHANDISING_CATEGORIES
            and z.avg_dwell_seconds
            and z.avg_dwell_seconds >= HIGH_DWELL_LOW_ENGAGE_SEC
            and z.engagement_rate is not None
            and z.engagement_rate < LOW_ENGAGEMENT_PCT
        ):
            findings.append(Finding(
                category="MERCHANDISING",
                severity="HIGH",
                zone=z.name,
                finding=(
                    f"Shoppers dwell {z.avg_dwell_seconds:.0f}s in {z.name} but only "
                    f"{z.engagement_rate:.1f}% reach for product."
                ),
                root_cause=(
                    "High attention with low pick-up usually indicates unclear pricing, "
                    "an out-of-stock facing, or a confusing planogram."
                ),
                action_item=(
                    f"Audit {z.name} facings and shelf-edge pricing; verify stock against the planogram."
                ),
                evidence={
                    "visits": z.visits,
                    "avg_dwell_seconds": z.avg_dwell_seconds,
                    "engagement_rate_pct": z.engagement_rate,
                },
            ))

        # Chronically bypassed aisle.
        if z.category in ("AISLE", "DEPARTMENT") and z.visits < avg_visits * DEAD_ZONE_TRAFFIC_RATIO:
            findings.append(Finding(
                category="STORE_LAYOUT",
                severity="MEDIUM",
                zone=z.name,
                finding=(
                    f"{z.name} saw {z.visits} visits against a store average of {avg_visits:.0f}."
                ),
                root_cause=(
                    "Traffic well below the store average suggests the aisle sits off the "
                    "dominant circulation path or its category signage is not visible."
                ),
                action_item=(
                    f"Reposition a destination category into {z.name} or add an aisle-end "
                    "signpost to draw the main traffic flow through it."
                ),
                evidence={"visits": z.visits, "store_avg_visits": round(avg_visits, 1)},
            ))

        # Checkout congestion.
        if (
            z.category == "CHECKOUT"
            and z.avg_dwell_seconds
            and z.avg_dwell_seconds > QUEUE_WAIT_WARN_SECONDS
        ):
            findings.append(Finding(
                category="STAFFING",
                severity="CRITICAL",
                zone=z.name,
                finding=(
                    f"Average wait at {z.name} is {z.avg_dwell_seconds / 60:.1f} minutes."
                ),
                root_cause="Lane throughput is below arrival rate for the observed period.",
                action_item=f"Open an additional lane covering {z.name} during this period.",
                evidence={
                    "avg_wait_seconds": z.avg_dwell_seconds,
                    "queue_now": z.occupancy_now,
                },
            ))

    # Store-wide conversion friction, only when POS is actually connected.
    lost = funnel.get("lost_sales_index_pct")
    if funnel.get("pos_connected") and lost is not None and lost >= 75.0:
        findings.append(Finding(
            category="LOSS_PREVENTION",
            severity="HIGH",
            zone="Store-wide",
            finding=f"{lost:.0f}% of shoppers who handled product did not complete a purchase.",
            root_cause=(
                "A large gap between engagement and transactions points to price resistance, "
                "checkout friction, or abandonment at the queue."
            ),
            action_item="Review pricing on high-engagement lines and checkout wait times together.",
            evidence={k: funnel.get(k) for k in
                      ("conversion_rate_pct", "engagement_rate_pct", "lost_sales_index_pct")},
        ))

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return findings


def _detect_products(summary: Optional[dict]) -> tuple[list[Finding], list[str]]:
    """Merchandising rules over per-product shelf reaches. Returns (findings, suppressed)."""
    findings: list[Finding] = []
    suppressed: list[str] = []
    if not summary:
        return findings, ["product_reach: product summary unavailable"]
    products = [p for p in summary.get("products") or [] if p.get("configured") and p.get("enabled", True)]
    if not products:
        return findings, ["product_reach: no product shelf areas are mapped in Studio"]

    # 1. Dead shelves: no reach at all while the camera was demonstrably working.
    for p in products:
        cam_reaches = int(p.get("camera_reaches") or 0)
        cam_shoppers = p.get("camera_shoppers")
        if (
            p["reaches"] == 0
            and cam_reaches >= DEAD_SHELF_MIN_CAMERA_REACHES
            and cam_shoppers is not None and cam_shoppers >= DEAD_SHELF_MIN_CAMERA_SHOPPERS
        ):
            findings.append(Finding(
                category="MERCHANDISING",
                severity="MEDIUM",
                zone=p["name"],
                finding=(
                    f"No shopper reached for {p['name']} (SKU {p['sku_id']}) today, while the same camera "
                    f"recorded {cam_reaches} reaches at other products and {cam_shoppers} shoppers."
                ),
                root_cause="A shelf nobody touches is usually out of stock, hidden, mispriced or mis-faced.",
                action_item=f"Check stock, facing and shelf-edge label for {p['name']}.",
                evidence={"reaches": 0, "camera_reaches": cam_reaches, "camera_shoppers": cam_shoppers,
                          "shelf_level": p.get("shelf_level")},
            ))
    if not any(int(p.get("camera_reaches") or 0) >= DEAD_SHELF_MIN_CAMERA_REACHES for p in products):
        suppressed.append(
            f"dead_shelf: needs a camera with at least {DEAD_SHELF_MIN_CAMERA_REACHES} recorded reaches today"
        )

    # 2. Top / bottom shelf vs eye level (MIDDLE), reaches per configured zone.
    levels = {row["level"]: row for row in summary.get("shelf_levels") or []}
    mid = levels.get("MIDDLE")
    total = sum(p["reaches"] for p in products)
    compared = False
    if mid and mid.get("zones") and total >= SHELF_LEVEL_MIN_REACHES and mid.get("reaches_per_zone"):
        for lvl, label in (("TOP", "top-shelf"), ("BOTTOM", "bottom-shelf")):
            row = levels.get(lvl)
            if not row or not row.get("zones") or row.get("reaches_per_zone") is None:
                continue
            compared = True
            ratio = row["reaches_per_zone"] / mid["reaches_per_zone"]
            if ratio < SHELF_LEVEL_GAP_RATIO:
                findings.append(Finding(
                    category="MERCHANDISING",
                    severity="LOW",
                    zone=f"{label.capitalize()} products",
                    finding=(
                        f"{label.capitalize()} products drew {row['reaches_per_zone']:.1f} reaches per product "
                        f"today against {mid['reaches_per_zone']:.1f} at eye level ({ratio * 100:.0f}%)."
                    ),
                    root_cause="Shoppers reach most at eye level; products placed high or low are handled less.",
                    action_item=(
                        f"Keep high-margin or promoted lines at eye level; review which {label} products "
                        "would benefit from moving."
                    ),
                    evidence={"level": lvl, "reaches": row["reaches"], "zones": row["zones"],
                              "reaches_per_zone": row["reaches_per_zone"],
                              "eye_level_reaches_per_zone": mid["reaches_per_zone"]},
                ))
    if not compared:
        suppressed.append(
            f"shelf_level_reach: needs {SHELF_LEVEL_MIN_REACHES}+ reaches and products at eye level "
            "and at another level (set shelf levels in Studio)"
        )

    # 3. High reach, low conversion -- only when POS data exists.
    if not summary.get("pos_connected"):
        suppressed.append("product_conversion: needs POS data (no POS rows for this day)")
    else:
        seen_skus: set = set()
        for p in products:
            sku = p.get("sku_id")
            ratio = p.get("units_per_reaching_shopper")
            reachers = p.get("sku_reaching_shoppers") or 0
            if not sku or sku in seen_skus or ratio is None or reachers < HIGH_REACH_MIN_SHOPPERS:
                continue
            seen_skus.add(sku)
            if ratio < LOW_UNITS_PER_REACHING_SHOPPER:
                findings.append(Finding(
                    category="MERCHANDISING",
                    severity="HIGH",
                    zone=p["name"],
                    finding=(
                        f"{reachers} shoppers reached for {p['name']} (SKU {sku}) but POS recorded "
                        f"{p['pos_units_sold']} unit(s) sold ({ratio:.2f} per reaching shopper)."
                    ),
                    root_cause="Handled but not bought: price resistance, unclear label, or damaged/short-dated stock.",
                    action_item=f"Check the price label, pack condition and dates on {p['name']}.",
                    evidence={"sku_id": sku, "reaching_shoppers": reachers,
                              "pos_units_sold": p["pos_units_sold"], "units_per_reaching_shopper": ratio},
                ))
    return findings, suppressed


# ------------------------------------------------------------ heatmap trends
# Rules over the recorded heatmap history (services/heatmap_history.py).
# Each finding cites the snapshot ids and periods it used; each rule reports
# "not enough recorded history" instead of firing on thin data. Thresholds
# are HEATMAP_* in config.py.

_DEAD_SPACE_CATEGORIES = ("AISLE", "DEPARTMENT", "SHELF")
_CONGESTION_CATEGORIES = ("CHECKOUT", "ENTRANCE", "EXIT")


def _congestion_roles() -> tuple:
    """Camera roles whose whole view is a lane or a door (camera_roles presets)."""
    from app.services.camera_roles import PRIMARY_FOOTFALL_ROLES

    return ("checkout",) + tuple(PRIMARY_FOOTFALL_ROLES)


def _period(days: list) -> dict:
    return {"from": days[0].isoformat(), "to": days[-1].isoformat(), "days": len(days)} if days else {}


def _zone_at(zones: list, x: float, y: float) -> Optional[str]:
    from app.services.store_layout_service import point_in_polygon

    for z in zones:
        if point_in_polygon(x, y, z.polygon or []):
            return z.name
    return None


async def heatmap_trends(db: AsyncSession, layout_id: Optional[str] = None, *,
                         now: Optional[datetime] = None, zones: Optional[list] = None,
                         cameras: Optional[list] = None, products: Optional[list] = None) -> dict:
    """Heatmap-trend findings, suppressed-rule reasons and a compact summary for the narration.

    Uses complete store-local days only (today excluded):

    * dead space -- a covered AISLE/DEPARTMENT/SHELF zone whose presence
      (visitors per cell) stays under HEATMAP_DEAD_SPACE_RATIO x the median
      covered zone on HEATMAP_DEAD_SPACE_DAY_SHARE of the recorded days;
    * hot-spot shift -- the centroid of the busiest 5% of presence cells moved
      at least HEATMAP_SHIFT_MIN_M week over week;
    * congestion -- people standing (dwell / 3600) in a CHECKOUT/ENTRANCE/EXIT
      zone at its peak store-local hour exceeds its quietest recorded hour by
      HEATMAP_CONGESTION_MIN_PEOPLE;
    * congestion by camera role -- the same peak-vs-quietest test on the
      whole image of each checkout / entrance / exit camera (image-space
      dwell), so an uncalibrated lane camera is still assessed;
    * browsed, not touched -- a Studio product zone on an aisle or
      high-value camera (or one with no role) with at least
      HEATMAP_BROWSE_MIN_SHOPPER_MIN shopper-minutes in front of it (image
      dwell) but under HEATMAP_BROWSE_MAX_REACHES_PER_MIN reaches per minute.
    """
    import numpy as np

    from app.models.db_models import CameraModel
    from app.services import heatmap_history as hh
    from app.services.store_layout_service import store_layout_service
    from app.services.timeutil import local_midnight_utc, to_local, utcnow

    now = now or utcnow()
    findings: list[Finding] = []
    suppressed: list[str] = []
    n_days = max(int(settings.HEATMAP_TREND_DAYS), 1)
    min_days = max(int(settings.HEATMAP_TREND_MIN_DAYS), 1)
    today = to_local(now).date()
    days = [today - timedelta(days=i) for i in range(n_days, 0, -1)]
    win_start, win_end = local_midnight_utc(days[0]), local_midnight_utc(today)

    layout = await store_layout_service.get_active_layout(db)
    if layout is not None and layout_id and layout.id != layout_id:
        from app.models.db_models import StoreLayoutModel

        layout = await db.get(StoreLayoutModel, layout_id) or layout
    # zones / cameras / products default to the blueprint, the cameras table
    # and the Studio product zones (tests inject them).
    if zones is None:
        zones = await store_layout_service.list_zones(db, layout.id) if layout is not None else []
    if cameras is None:
        cameras = list((await db.execute(select(CameraModel))).scalars().all())
    from app.services.camera_roles import PRODUCT_AREA_ROLES, ROLE_PRESETS, preset

    roles = {c.id: (getattr(c, "role", None) if getattr(c, "role", None) in ROLE_PRESETS else None)
             for c in cameras}
    names = {c.id: (getattr(c, "name", None) or c.id) for c in cameras}

    presence = [r for r in await hh.load_rows(db, "floor", None, "presence", win_start, win_end, hh.DAY_MINUTES)
                if r.total_samples > 0]
    summary: dict[str, Any] = {"window": _period(days), "days_with_floor_history": len(presence),
                               "days_required": min_days}

    def not_enough(rule: str, have: int, need: int, what: str = "days with floor presence recorded") -> str:
        return f"{rule}: not enough recorded history ({have} of {need} {what})"

    # ---- shared floor geometry
    ref = max(presence, key=lambda r: r.bucket_start) if presence else None
    zone_masks: dict[str, Any] = {}
    covered_ids: set = set()
    if ref is not None and layout is not None:
        shape = (ref.grid_w, ref.grid_h, ref.width_m, ref.height_m)
        presence = [r for r in presence if (r.grid_w, r.grid_h, r.width_m, r.height_m) == shape]
        cover = hh.floor_coverage_mask(cameras, ref.width_m, ref.height_m, ref.grid_w, ref.grid_h)
        for z in zones:
            m = hh.polygon_cells(z.polygon or [], ref.width_m, ref.height_m, ref.grid_w, ref.grid_h)
            if not m.any():
                continue
            zone_masks[z.id] = m
            if (cover & m).sum() / m.sum() >= settings.HEATMAP_MIN_ZONE_COVERAGE:
                covered_ids.add(z.id)
        summary["zones_in_camera_view"] = len(covered_ids)

    # ---- 1. dead space
    if len(presence) < min_days:
        suppressed.append(not_enough("heatmap_dead_space", len(presence), min_days))
    else:
        cand = [z for z in zones if z.category in _DEAD_SPACE_CATEGORIES and z.id in zone_masks]
        covered = [z for z in cand if z.id in covered_ids]
        uncovered = [z.name for z in cand if z.id not in covered_ids]
        if uncovered:
            suppressed.append("heatmap_dead_space: not judged, no calibrated camera covers "
                              + ", ".join(uncovered[:5]))
        if len(covered) < 3:
            suppressed.append("heatmap_dead_space: needs at least 3 aisle/department/shelf zones "
                              f"in view of a calibrated camera ({len(covered)} now)")
        else:
            per_day = []   # (row, {zone_id: visitors per cell}, median)
            for r in presence:
                g = hh.row_grid(r)
                dens = {z.id: float(g[zone_masks[z.id]].mean()) for z in covered}
                per_day.append((r, dens, float(np.median(list(dens.values())))))
            need = math.ceil(settings.HEATMAP_DEAD_SPACE_DAY_SHARE * len(per_day))
            for z in covered:
                cold = [(r, d[z.id], med) for (r, d, med) in per_day
                        if med > 0 and d[z.id] < settings.HEATMAP_DEAD_SPACE_RATIO * med]
                if len(cold) < need:
                    continue
                mean_z = sum(d[z.id] for (_r, d, _m) in per_day) / len(per_day)
                mean_med = sum(m for (_r, _d, m) in per_day) / len(per_day)
                used_days = sorted(hh.day_of(r.bucket_start) for (r, _d, _m) in per_day)
                findings.append(Finding(
                    category="HEATMAP_DEAD_SPACE", severity="MEDIUM", zone=z.name,
                    finding=(f"{z.name} stayed cold on {len(cold)} of {len(per_day)} recorded days: "
                             f"{mean_z:.1f} visitors per cell per day against a median of {mean_med:.1f} "
                             "across the zones cameras cover."),
                    root_cause=("Shoppers are not routing through this area: it may be off the main path, "
                                "blocked, poorly signed or holding low-draw categories."),
                    action_item=(f"Walk {z.name}: check sightlines and signage, and consider moving a "
                                 "destination or promoted category into it."),
                    evidence={"snapshot_ids": [r.id for (r, _d, _m) in per_day], "period": _period(used_days),
                              "cold_days": len(cold), "recorded_days": len(per_day),
                              "zone_visitors_per_cell_per_day": round(mean_z, 2),
                              "median_zone_visitors_per_cell_per_day": round(mean_med, 2),
                              "ratio_threshold": settings.HEATMAP_DEAD_SPACE_RATIO},
                ))

    # ---- 2. hot-spot shift, week over week
    if layout is not None:
        this_days = [today - timedelta(days=i) for i in range(7, 0, -1)]
        prev_days = [today - timedelta(days=i) for i in range(14, 7, -1)]
        weeks = []
        for ds in (prev_days, this_days):
            rows = [r for r in await hh.load_rows(db, "floor", None, "presence", local_midnight_utc(ds[0]),
                                                  local_midnight_utc(ds[-1] + timedelta(days=1)), hh.DAY_MINUTES)
                    if r.total_samples > 0]
            weeks.append((ds, rows))
        need = settings.HEATMAP_SHIFT_MIN_DAYS
        have = min(len(weeks[0][1]), len(weeks[1][1]))
        if have < need:
            suppressed.append(not_enough("heatmap_hotspot_shift", have, need,
                                         "days recorded in each of the last two weeks"))
        else:
            s_prev, s_now = hh.sum_rows(weeks[0][1]), hh.sum_rows(weeks[1][1])
            r_now = s_now["ref"]
            if (s_prev["ref"].grid_w, s_prev["ref"].grid_h, s_prev["ref"].width_m, s_prev["ref"].height_m) != \
                    (r_now.grid_w, r_now.grid_h, r_now.width_m, r_now.height_m):
                suppressed.append("heatmap_hotspot_shift: the blueprint was resized between the two weeks")
            elif min(s_prev["grid"].sum(), s_now["grid"].sum()) < settings.HEATMAP_SHIFT_MIN_PASSES:
                suppressed.append(f"heatmap_hotspot_shift: not enough recorded history (fewer than "
                                  f"{settings.HEATMAP_SHIFT_MIN_PASSES} visitor-cell passes in a week)")
            else:
                # Only cells seen in both weeks, so a camera added or lost is not read as a shift.
                both = (s_prev["grid"] > 0) & (s_now["grid"] > 0)
                a = np.where(both, s_prev["grid"] / len(s_prev["used"]), 0.0)
                b = np.where(both, s_now["grid"] / len(s_now["used"]), 0.0)
                ca, cb = hh.weighted_centroid(a), hh.weighted_centroid(b)
                if ca and cb:
                    sx, sy = r_now.width_m / r_now.grid_w, r_now.height_m / r_now.grid_h
                    pa = (ca[0] * sx, ca[1] * sy)
                    pb = (cb[0] * sx, cb[1] * sy)
                    dist = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
                    summary["hotspot_shift_m"] = round(dist, 1)
                    if dist >= settings.HEATMAP_SHIFT_MIN_M:
                        za = _zone_at(zones, *pa) or f"the area at ({pa[0]:.1f} m, {pa[1]:.1f} m)"
                        zb = _zone_at(zones, *pb) or f"the area at ({pb[0]:.1f} m, {pb[1]:.1f} m)"
                        d_prev = sorted(hh.day_of(r.bucket_start) for r in s_prev["used"])
                        d_now = sorted(hh.day_of(r.bucket_start) for r in s_now["used"])
                        findings.append(Finding(
                            category="HEATMAP_HOTSPOT_SHIFT", severity="LOW", zone=zb,
                            finding=(f"The busiest part of the floor moved {dist:.1f} m week over week, "
                                     f"from {za} to {zb}."),
                            root_cause=("A change in layout, promotion, stock position or an obstruction "
                                        "has redirected shopper traffic."),
                            action_item=(f"Check what changed near {zb} and {za} this week, and whether "
                                         "staffing and replenishment followed the traffic."),
                            evidence={"snapshot_ids": [r.id for r in s_now["used"]],
                                      "previous_snapshot_ids": [r.id for r in s_prev["used"]],
                                      "period": _period(d_now), "previous_period": _period(d_prev),
                                      "centroid_previous_m": [round(pa[0], 2), round(pa[1], 2)],
                                      "centroid_now_m": [round(pb[0], 2), round(pb[1], 2)],
                                      "distance_m": round(dist, 2)},
                        ))

    # ---- 3. congestion near checkout / entrance at peak hours
    targets = [z for z in zones if z.category in _CONGESTION_CATEGORIES and z.id in zone_masks]
    dwell_rows = await hh.load_rows(db, "floor", None, "dwell", win_start, win_end, hh.HOUR_MINUTES)
    if ref is not None:
        dwell_rows = [r for r in dwell_rows
                      if (r.grid_w, r.grid_h, r.width_m, r.height_m) == (ref.grid_w, ref.grid_h, ref.width_m, ref.height_m)]
    if not targets:
        suppressed.append("heatmap_congestion: needs a checkout, entrance or exit zone on the blueprint")
    else:
        by_hour: dict[int, list] = {}
        for r in dwell_rows:
            by_hour.setdefault(to_local(r.bucket_start).hour, []).append((r, hh.row_grid(r)))
        need = settings.HEATMAP_CONGESTION_MIN_DAYS_PER_HOUR
        eligible = {h: rs for h, rs in by_hour.items() if len(rs) >= need}
        judged = [z for z in targets if z.id in covered_ids]
        for z in targets:
            if z.id not in covered_ids:
                suppressed.append(f"heatmap_congestion: {z.name} is not in view of a calibrated camera")
        if not eligible:
            best = max((len(rs) for rs in by_hour.values()), default=0)
            suppressed.append(not_enough("heatmap_congestion", best, need,
                                         "days recorded for any single hour of day"))
        else:
            for z in judged:
                mask = zone_masks[z.id]
                means = {h: sum(float(g[mask].sum()) / (r.bucket_minutes * 60.0) for (r, g) in rs) / len(rs)
                         for h, rs in eligible.items()}
                peak_h = max(means, key=means.get)
                base_h = min(means, key=means.get)
                excess = means[peak_h] - means[base_h]
                if excess < settings.HEATMAP_CONGESTION_MIN_PEOPLE:
                    continue
                used = eligible[peak_h]
                findings.append(Finding(
                    category="HEATMAP_CONGESTION", severity="HIGH", zone=z.name,
                    finding=(f"Around {peak_h:02d}:00 an average of {means[peak_h]:.1f} people stand in {z.name} "
                             f"({len(used)} recorded days), {excess:.1f} more than at its quietest hour "
                             f"({base_h:02d}:00, {means[base_h]:.1f})."),
                    root_cause=("Arrivals outpace service or the doorway at this hour; the figure includes staff, "
                                "who are part of the quiet-hour baseline."),
                    action_item=f"Add a lane or door staff in {z.name} from about {peak_h:02d}:00.",
                    evidence={"snapshot_ids": [r.id for (r, _g) in used],
                              "baseline_snapshot_ids": [r.id for (r, _g) in eligible[base_h]],
                              "period": _period(sorted(hh.day_of(r.bucket_start) for (r, _g) in used)),
                              "peak_hour_local": f"{peak_h:02d}:00", "avg_people_at_peak": round(means[peak_h], 2),
                              "quietest_hour_local": f"{base_h:02d}:00",
                              "avg_people_at_quietest": round(means[base_h], 2)},
                ))

    # ---- 3b. congestion on checkout / entrance / exit cameras (by role, image space)
    need = settings.HEATMAP_CONGESTION_MIN_DAYS_PER_HOUR
    for cam, role in roles.items():
        if role not in _congestion_roles():
            continue
        rows = [r for r in await hh.load_rows(db, "image", cam, "dwell", win_start, win_end, hh.HOUR_MINUTES)]
        by_hour_c: dict[int, list] = {}
        for r in rows:
            by_hour_c.setdefault(to_local(r.bucket_start).hour, []).append(r)
        eligible_c = {h: rs for h, rs in by_hour_c.items() if len(rs) >= need}
        label = f"{names[cam]} ({preset(role).label})"
        if not eligible_c:
            best = max((len(rs) for rs in by_hour_c.values()), default=0)
            suppressed.append(not_enough(f"heatmap_congestion[{cam}]", best, need,
                                         "days recorded for any single hour of day"))
            continue
        means = {h: sum(r.total_value / (r.bucket_minutes * 60.0) for r in rs) / len(rs)
                 for h, rs in eligible_c.items()}
        peak_h, base_h = max(means, key=means.get), min(means, key=means.get)
        excess = means[peak_h] - means[base_h]
        if excess < settings.HEATMAP_CONGESTION_MIN_PEOPLE:
            continue
        used = eligible_c[peak_h]
        findings.append(Finding(
            category="HEATMAP_CONGESTION", severity="HIGH", zone=label,
            finding=(f"Around {peak_h:02d}:00 an average of {means[peak_h]:.1f} people stand in view of "
                     f"{label} ({len(used)} recorded days), {excess:.1f} more than at its quietest hour "
                     f"({base_h:02d}:00, {means[base_h]:.1f})."),
            root_cause=("Arrivals outpace service or the doorway at this hour; the figure includes staff "
                        "in view, who are part of the quiet-hour baseline."),
            action_item=(f"Open another lane or add door staff near {names[cam]} from about {peak_h:02d}:00."
                         if role == "checkout" else
                         f"Keep the doorway at {names[cam]} clear and staffed from about {peak_h:02d}:00."),
            evidence={"snapshot_ids": [r.id for r in used],
                      "baseline_snapshot_ids": [r.id for r in eligible_c[base_h]],
                      "camera_id": cam, "camera_role": role,
                      "period": _period(sorted(hh.day_of(r.bucket_start) for r in used)),
                      "peak_hour_local": f"{peak_h:02d}:00", "avg_people_at_peak": round(means[peak_h], 2),
                      "quietest_hour_local": f"{base_h:02d}:00",
                      "avg_people_at_quietest": round(means[base_h], 2)},
        ))

    # ---- 4. browsed but not touched (image space, Studio product zones)
    if products is None:
        try:
            from app.services.shelf_interaction_service import shelf_interaction_service

            products = shelf_interaction_service.get_zones()
        except Exception as e:  # never let product config break the trends
            logger.warning(f"product zones unavailable for heatmap trends: {e}")
            products = []
    products = [p for p in products if getattr(p, "enabled", True)]
    if not products:
        suppressed.append("heatmap_browse_no_touch: no product shelf areas are mapped in Studio")
    else:
        by_cam: dict[str, list] = {}
        for p in products:
            by_cam.setdefault(p.camera_id, []).append(p)
        for cam, prods in by_cam.items():
            role = roles.get(cam)
            if role is not None and role not in PRODUCT_AREA_ROLES:
                # Standing at a checkout rack or a door is queuing or transit, not browsing.
                suppressed.append(f"heatmap_browse_no_touch[{cam}]: not applied, camera role is "
                                  f"{preset(role).label}; it applies to aisle and high-value cameras")
                continue
            severity = (preset(role).alert_severity_floor if role else None) or "MEDIUM"
            dwell = {hh.day_of(r.bucket_start): r for r in await hh.load_rows(
                db, "image", cam, "dwell", win_start, win_end, hh.DAY_MINUTES) if r.total_samples > 0}
            inter = {hh.day_of(r.bucket_start): r for r in await hh.load_rows(
                db, "image", cam, "interaction", win_start, win_end, hh.DAY_MINUTES)}
            used_days = sorted(d for d in dwell if d in inter)
            if len(used_days) < min_days:
                suppressed.append(not_enough(f"heatmap_browse_no_touch[{cam}]", len(used_days), min_days,
                                             "days with both dwell and shelf-interaction recording"))
                continue
            d_rows = [dwell[d] for d in used_days]
            i_rows = [inter[d] for d in used_days]
            s_d, s_i = hh.sum_rows(d_rows), hh.sum_rows(i_rows)
            gd, gi = s_d["grid"], s_i["grid"]
            gh, gw = gd.shape
            for p in prods:
                xs = [pt.x for pt in p.points]
                ys = [pt.y for pt in p.points]
                if len(xs) < 3:
                    continue
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                # Where the shopper's feet are while facing this shelf (approximate):
                # the shelf's columns, from its top edge down by its own height.
                ay1 = min(1.0, y1 + max(y1 - y0, 0.1))
                cols = np.arange(gw)
                rows_ = np.arange(gh)
                cx, cy = (cols + 0.5) / gw, (rows_ + 0.5) / gh
                approach = np.outer((cy >= y0) & (cy <= ay1), (cx >= x0) & (cx <= x1))
                hx, hy = 0.5 / gw, 0.5 / gh
                reach = np.outer((cy >= y0 - hy) & (cy <= y1 + hy), (cx >= x0 - hx) & (cx <= x1 + hx))
                shopper_min = float(gd[approach].sum()) / 60.0
                reaches = float(gi[reach].sum())
                if shopper_min < settings.HEATMAP_BROWSE_MIN_SHOPPER_MIN:
                    continue
                rate = reaches / shopper_min
                if rate >= settings.HEATMAP_BROWSE_MAX_REACHES_PER_MIN:
                    continue
                findings.append(Finding(
                    category="HEATMAP_BROWSE_NO_TOUCH", severity=severity, zone=p.name,
                    finding=(f"Shoppers spent {shopper_min:.0f} minutes in front of {p.name} over "
                             f"{len(used_days)} recorded days but reached for it {int(reaches)} time(s) "
                             f"({rate:.3f} reaches per shopper-minute)."),
                    root_cause=("Browsed but not touched: the product is seen but not picked up -- price, "
                                "facing, label clarity or stock condition."),
                    action_item=f"Check price label, facings and stock condition of {p.name} (SKU {p.sku_id}).",
                    evidence={"snapshot_ids": [r.id for r in d_rows], "interaction_snapshot_ids": [r.id for r in i_rows],
                              "camera_id": cam, "camera_role": role, "period": _period(used_days),
                              "shopper_minutes": round(shopper_min, 1), "reaches": int(reaches),
                              "reaches_per_shopper_minute": round(rate, 4),
                              "note": "area in front of the shelf is estimated in the camera image"},
                ))

    # ---- compact summary for the narration (numbers from snapshots only)
    if len(presence) < min_days:
        summary["status"] = "not enough recorded history"
    else:
        summary["status"] = "ok"
        # Hourly floor dwell of the window (already filtered to the presence grid shape).
        s = hh.sum_rows([r for r in dwell_rows if r.total_samples > 0])
        if s["grid"] is not None and covered_ids:
            tot = float(s["grid"].sum())
            shares = sorted(((z.name, float(s["grid"][zone_masks[z.id]].sum()) / tot * 100.0)
                             for z in zones if z.id in covered_ids), key=lambda t: -t[1]) if tot > 0 else []
            summary["busiest_zones_by_dwell"] = [{"zone": n, "share_pct": round(v, 1)} for n, v in shares[:3]]
            summary["quietest_zones_by_dwell"] = [{"zone": n, "share_pct": round(v, 1)} for n, v in shares[-2:]]
        hourly_p = await hh.load_rows(db, "floor", None, "presence", win_start, win_end, hh.HOUR_MINUTES)
        prof = [p for p in hh.hour_profile_from_rows(hourly_p) if p["mean_value"] is not None]
        if prof:
            summary["peak_hour_local"] = max(prof, key=lambda p: p["mean_value"])["label"]
    summary["findings"] = [{"category": f.category, "zone": f.zone} for f in findings]
    return {"findings": findings, "suppressed": suppressed, "summary": summary}


class BusinessAnalysisService:
    """Generates, narrates and persists store recommendations."""

    # ------------------------------------------------------------------ LLM

    def _ollama_models(self) -> list[dict]:
        try:
            req = urllib.request.Request(f"{settings.OLLAMA_BASE_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=2.5) as r:
                return json.loads(r.read().decode()).get("models", [])
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return []

    def select_model(self) -> Optional[str]:
        """Pick a generative model, preferring ones that answer quickly.

        Reasoning-heavy models are deprioritised here: a model that needs more
        than a minute to write three sentences makes the analysis feel broken,
        and the findings it narrates are already complete without it.
        """
        models = [m.get("name", "") for m in self._ollama_models()]
        generative = [
            n for n in models
            if not any(t in n.lower() for t in ("embed", "bert", "bge", "nomic-embed"))
        ]
        if not generative:
            return None
        for preference in ("lfm", "qwen", "llama", "mistral", "phi", "gemma", "ornith"):
            for n in generative:
                if preference in n.lower():
                    return n
        return generative[0]

    def narrate(self, findings: list[Finding], overview: dict, timeout: float = 90.0,
                heatmap_summary: Optional[dict] = None) -> dict:
        """Ask the local model for an executive summary of the findings.

        The generation budget is generous because this runs on demand, not on
        the request path of a dashboard poll. The previous implementation
        allowed six seconds, which every locally-hosted model exceeded, so it
        silently served a templated string while reporting the model as active.
        """
        model = self.select_model()
        if not model:
            return {
                "summary": None,
                "model_used": None,
                "ollama_active": False,
                "reason": "No generative model is available from Ollama.",
            }

        if not findings:
            return {
                "summary": None,
                "model_used": None,
                "ollama_active": True,
                "reason": "No findings to summarise.",
            }

        facts = [
            {"zone": f.zone, "severity": f.severity, "finding": f.finding, "action": f.action_item}
            for f in findings[:6]
        ]
        prompt = (
            "You are a retail operations analyst. Below are findings measured by an "
            "in-store camera analytics system, with the store's headline numbers.\n\n"
            f"Store metrics: {json.dumps({k: overview.get(k) for k in ('today_footfall', 'active_shoppers_now', 'avg_dwell_minutes', 'conversion_rate_pct', 'daily_revenue')})}\n"
            f"Findings: {json.dumps(facts)}\n"
            # Compact recorded-heatmap summary (heatmap_trends); numbers from snapshots only.
            + (f"Recorded heatmap history: {json.dumps(heatmap_summary, default=str)}\n"
               if heatmap_summary else "")
            + "\n"
            "Write a 3-sentence executive summary for the store manager. State what is "
            "happening, why it matters commercially, and what to do first. "
            "Use only the numbers given above. Do not invent figures. Do not use markdown."
        )

        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            # Several local models interleave a <think> block before their
            # answer. Ollama honours this flag on models that support it, and
            # ignores it on those that do not; _strip_reasoning handles the rest.
            "think": False,
            # Generous enough that a reasoning model can finish its block and
            # still produce the answer. Truncating mid-thought yields output
            # that is all scaffolding and no summary.
            "options": {"temperature": 0.2, "num_predict": 700},
        }).encode()

        started = datetime.now()
        try:
            req = urllib.request.Request(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # An honest failure. The findings above are unaffected.
            return {
                "summary": None,
                "model_used": model,
                "ollama_active": False,
                "reason": f"Model '{model}' did not respond within {timeout:.0f}s ({e}).",
            }
        except json.JSONDecodeError as e:
            return {"summary": None, "model_used": model, "ollama_active": False,
                    "reason": f"Malformed response from Ollama: {e}"}

        text = self._strip_reasoning(body.get("response") or "")
        if not text:
            return {
                "summary": None,
                "model_used": model,
                "ollama_active": True,
                "reason": (
                    "Model produced only reasoning, no answer. Increase the token "
                    "budget or choose a non-reasoning model."
                ),
            }

        return {
            "summary": text,
            "model_used": model,
            "ollama_active": True,
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
            "reason": None,
        }

    @staticmethod
    def _strip_reasoning(raw: str) -> str:
        """Remove chain-of-thought scaffolding and return only the answer.

        Reasoning models wrap their deliberation in <think> tags. When the
        token budget runs out mid-thought the closing tag never arrives, so a
        naive paired-tag regex leaves the entire block intact -- which is how
        raw reasoning would otherwise reach the manager's dashboard.
        """
        import re

        text = (raw or "").strip()
        text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        # An unterminated block means everything after it is deliberation.
        if (m := re.search(r"<think>", text, re.IGNORECASE)) is not None:
            text = text[: m.start()]
        text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    # -------------------------------------------------------------- analysis

    async def analyse(
        self, db: AsyncSession, layout_id: str, *, persist: bool = True, narrate: bool = False
    ) -> dict:
        """Produce findings from today's observations, optionally narrated."""
        start, end = day_bounds()
        zones = await retail_metrics_service.zone_metrics(db, layout_id, start, end)
        funnel = await retail_metrics_service.funnel(db, layout_id, start, end)
        overview = await retail_metrics_service.overview(db, layout_id, settings.STORE_NAME)

        assessable = sum(1 for z in zones if z.visits >= MIN_VISITS_FOR_CONFIDENCE)
        findings = _detect(zones, funnel)

        # Product-level shelf reach rules (from shelf_interactions rows).
        product_summary = None
        try:
            from app.services.shelf_interaction_service import shelf_interaction_service
            product_summary = await shelf_interaction_service.product_summary(db, start, end)
        except Exception as e:  # never let product rules break the zone analysis
            logger.warning(f"Product reach summary unavailable: {e}")
        product_findings, product_suppressed = _detect_products(product_summary)
        findings.extend(product_findings)

        # Trends over the recorded heatmap history (heatmap_trends).
        heatmap = None
        try:
            heatmap = await heatmap_trends(db, layout_id)
            findings.extend(heatmap["findings"])
            product_suppressed = product_suppressed + heatmap["suppressed"]
        except Exception as e:  # never let the history rules break the analysis
            logger.warning(f"Heatmap trend rules unavailable: {e}")
        _order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        findings.sort(key=lambda f: _order.get(f.severity, 9))

        if persist and findings:
            await self._persist(db, findings)

        engagement_tracked = any(z.interactions > 0 for z in zones)
        result = {
            "generated_at": datetime.now().isoformat(),
            "findings": [f.to_dict() for f in findings],
            "findings_count": len(findings),
            "zones_assessed": assessable,
            "zones_total": len(zones),
            "engagement_tracking_active": engagement_tracked,
            "suppressed_rules": (
                [] if engagement_tracked
                else ["merchandising_engagement: no shelf-interaction source is reporting"]
            ) + product_suppressed,
            "product_reach": (
                {
                    "reaches": product_summary["totals"]["reaches"],
                    "products_reached": product_summary["totals"]["products_reached"],
                    "products_configured": product_summary["totals"]["products_configured"],
                    "shelf_levels": product_summary["shelf_levels"],
                    "pos_connected": product_summary["pos_connected"],
                }
                if product_summary else None
            ),
            "heatmap_trends": (
                {"summary": heatmap["summary"], "findings": len(heatmap["findings"])} if heatmap else None
            ),
            # Say plainly why an empty result is empty.
            "sufficient_data": assessable > 0 or bool(product_findings),
            "message": (
                None if (assessable or product_findings)
                else (
                    f"Not enough observations yet. A zone needs at least "
                    f"{MIN_VISITS_FOR_CONFIDENCE} recorded visits before it can be assessed."
                )
            ),
        }
        if narrate:
            result["narrative"] = self.narrate(findings, overview,
                                               heatmap_summary=heatmap["summary"] if heatmap else None)
        return result

    async def _persist(self, db: AsyncSession, findings: list[Finding]) -> None:
        """Store today's findings, replacing any earlier run for the same day.

        Re-running the analysis should refresh the day's recommendations rather
        than accumulate near-duplicates, but an operator's status changes on a
        finding that still applies are preserved.
        """
        today = date.today().isoformat()
        existing = (
            await db.execute(
                select(AIDecisionRecommendationModel).where(
                    AIDecisionRecommendationModel.date == today
                )
            )
        ).scalars().all()
        by_key = {(r.zone, r.category): r for r in existing}

        seen: set[tuple[str, str]] = set()
        for f in findings:
            key = (f.zone, f.category)
            seen.add(key)
            if (row := by_key.get(key)) is not None:
                row.severity = f.severity
                row.finding = f.finding[:512]
                row.root_cause = f.root_cause[:512]
                row.action_item = f.action_item[:512]
                row.updated_at = datetime.utcnow()
                continue
            db.add(AIDecisionRecommendationModel(
                id=f"rec_{uuid.uuid4().hex[:12]}",
                date=today,
                category=f.category,
                severity=f.severity,
                zone=f.zone[:64],
                finding=f.finding[:512],
                root_cause=f.root_cause[:512],
                action_item=f.action_item[:512],
                status="PENDING",
            ))

        # Drop stale findings from earlier today that no longer hold, unless
        # someone has already acted on them.
        for key, row in by_key.items():
            if key not in seen and row.status == "PENDING":
                await db.delete(row)

        await db.commit()


business_analysis_service = BusinessAnalysisService()
