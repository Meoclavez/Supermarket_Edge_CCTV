"""Product shelf zones and the product-level reach analytics built on them.

Product zones are drawn per camera in **normalised image coordinates**: every
vertex is (x, y) in 0..1 of the camera frame's natural width and height (see
``static/js/studio.js``). They are therefore compared directly against a hand
keypoint divided by the frame size -- no homography is involved.

Each zone carries the product it shows (name, SKU, category, price) and two
attributes market analysis needs:

* **shelf level** (``TOP`` / ``MIDDLE`` / ``BOTTOM``): set by the operator in
  Studio, or -- when left on "auto" -- derived from where the polygon sits
  vertically within its shelf unit (the product zones on the same camera whose
  horizontal extents overlap it). A lone zone with nothing to compare against
  has no derivable level and reports ``None`` rather than a guess.
* **value tier** (``LOW`` / ``STANDARD`` / ``PREMIUM``): operator-set, else
  ``PREMIUM`` when the zone meets the high-value rule used by loss prevention,
  else the price tertile among this store's priced zones (needs 3+ priced
  zones), else ``None``.

The live pose pipeline (``app.services.pose_analytics``) detects reaches and
persists one ``shelf_interactions`` row per reach, with the product attribution
copied onto the row. **Every count reported here is read from that table**, so
figures survive a restart and match what was recorded; there are no in-memory
counters. ``process_person_pose`` backs the manual ``POST
/api/v1/analytics/products/interactions`` diagnostic endpoint only and records
nothing.

A hand position alone cannot tell a pick from a put-back, so neither is
inferred. Conversion is computed only from real POS rows (units sold per SKU
against distinct shoppers who reached for it); without POS data it is
reported as "needs POS data", never estimated.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.services.ai_zone_service import PolygonGeometry

logger = logging.getLogger("ShelfInteractionService")

STORAGE_PATH = settings.STORAGE_DIR / "shelf_products_config.json"
STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

SHELF_LEVELS = ("TOP", "MIDDLE", "BOTTOM")
VALUE_TIERS = ("LOW", "STANDARD", "PREMIUM")
# Legacy ``shelf_tier`` values (the old Studio select) mapped onto a level.
_LEGACY_TIER_LEVEL = {"TOP": "TOP", "EYE_LEVEL": "MIDDLE", "REACH": "MIDDLE", "BOTTOM": "BOTTOM"}
# Two zones belong to the same shelf unit (bay) when their horizontal extents
# overlap by at least this fraction of the narrower one.
SAME_UNIT_X_OVERLAP = 0.5


# ---------------- Data Schemas ----------------

class PointCoord(BaseModel):
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)


class StudyMetricsConfig(BaseModel):
    track_hand_reach: bool = True
    track_dwell_time: bool = True
    track_put_back_friction: bool = True
    track_pos_conversion: bool = True
    ab_test_mode: bool = False


class ProductShelfZone(BaseModel):
    id: str
    camera_id: str
    name: str
    points: List[PointCoord]
    sku_id: str
    category: str
    price: float = Field(0.0, ge=0.0)
    facing_count: int = Field(1, ge=1)
    # Placement ("SHELF" or "ENDCAP"); older builds stored TOP/EYE_LEVEL/REACH/
    # BOTTOM here, which is still read as a level hint for lone zones.
    shelf_tier: Optional[str] = None
    # Operator-set shelf level; None = derive from the polygon's position.
    shelf_level: Optional[Literal["TOP", "MIDDLE", "BOTTOM"]] = None
    # Operator-set value tier; None = derive (see module docstring).
    value_tier: Optional[Literal["LOW", "STANDARD", "PREMIUM"]] = None
    study_metrics: StudyMetricsConfig = Field(default_factory=StudyMetricsConfig)
    enabled: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("shelf_level", "value_tier", "shelf_tier", mode="before")
    @classmethod
    def _upper_or_none(cls, v):
        if v is None:
            return None
        s = str(v).strip().upper()
        return None if s in ("", "AUTO", "NONE", "NULL") else s


class ShelfInteractionEvent(BaseModel):
    event_id: str
    zone_id: str
    sku_id: str
    camera_id: str
    track_id: int
    action_type: str  # "REACH_IN", "INSPECT_DWELL", "REACH_END"
    dwell_duration_sec: float
    confidence: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_put_back: bool = False


# ---------------- Geometry helpers ----------------

def _bbox(z: ProductShelfZone) -> Tuple[float, float, float, float]:
    xs = [p.x for p in z.points]
    ys = [p.y for p in z.points]
    return min(xs), min(ys), max(xs), max(ys)


def _high_value(z: ProductShelfZone) -> bool:
    cats = {p.strip().upper() for p in str(settings.THEFT_HIGH_VALUE_CATEGORIES or "").split(",") if p.strip()}
    min_price = float(settings.THEFT_HIGH_VALUE_MIN_PRICE or 0.0)
    return (z.category or "").upper() in cats or (min_price > 0 and float(z.price or 0.0) >= min_price)


def derive_attributes(zones: List[ProductShelfZone]) -> Dict[str, Dict[str, Any]]:
    """Effective shelf level and value tier for every zone, with their source.

    Returns ``{zone_id: {shelf_level, shelf_level_source, value_tier,
    value_tier_source, high_value}}``; sources are ``operator``, ``derived``,
    ``legacy_tier`` or ``None`` (unknown).
    """
    out: Dict[str, Dict[str, Any]] = {}
    usable = [z for z in zones if len(z.points) >= 3]
    boxes = {z.id: _bbox(z) for z in usable}

    priced = sorted(float(z.price) for z in zones if (z.price or 0) > 0)
    t1 = t2 = None
    if len(priced) >= 3:
        # Tertile cut points by rank: LOW <= t1 < STANDARD <= t2 < PREMIUM.
        t1 = priced[(len(priced) - 1) // 3]
        t2 = priced[(2 * (len(priced) - 1)) // 3]

    for z in zones:
        info: Dict[str, Any] = {"shelf_level": None, "shelf_level_source": None,
                                "value_tier": None, "value_tier_source": None,
                                "high_value": _high_value(z)}
        # ---- shelf level
        if z.shelf_level:
            info["shelf_level"], info["shelf_level_source"] = z.shelf_level, "operator"
        elif z.id in boxes:
            x0, y0, x1, y1 = boxes[z.id]
            unit = []
            for o in usable:
                if o.camera_id != z.camera_id:
                    continue
                ox0, oy0, ox1, oy1 = boxes[o.id]
                overlap = min(x1, ox1) - max(x0, ox0)
                narrower = max(min(x1 - x0, ox1 - ox0), 1e-6)
                if overlap / narrower >= SAME_UNIT_X_OVERLAP:
                    unit.append(boxes[o.id])
            if len(unit) >= 2:
                top = min(b[1] for b in unit)
                bottom = max(b[3] for b in unit)
                span = bottom - top
                if span > 1e-6:
                    r = ((y0 + y1) / 2.0 - top) / span
                    info["shelf_level"] = "TOP" if r < 1 / 3 else ("MIDDLE" if r < 2 / 3 else "BOTTOM")
                    info["shelf_level_source"] = "derived"
            if info["shelf_level"] is None and (z.shelf_tier or "") in _LEGACY_TIER_LEVEL:
                info["shelf_level"] = _LEGACY_TIER_LEVEL[z.shelf_tier]
                info["shelf_level_source"] = "legacy_tier"
        # ---- value tier
        if z.value_tier:
            info["value_tier"], info["value_tier_source"] = z.value_tier, "operator"
        elif info["high_value"]:
            info["value_tier"], info["value_tier_source"] = "PREMIUM", "derived"
        elif t1 is not None and (z.price or 0) > 0:
            p = float(z.price)
            info["value_tier"] = "LOW" if p <= t1 else ("STANDARD" if p <= t2 else "PREMIUM")
            info["value_tier_source"] = "derived"
        out[z.id] = info
    return out


# ---------------- Service Class ----------------

class ShelfInteractionService:
    def __init__(self, config_path: Path = STORAGE_PATH):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.zones: Dict[str, ProductShelfZone] = {}
        self.active_tracks: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # Bumped on every zone change so per-frame consumers can cache geometry.
        self.version = 0
        self._attr_cache: Tuple[int, Dict[str, Dict[str, Any]]] = (-1, {})
        self._load_zones()

    def _load_zones(self):
        with self.lock:
            if self.config_path.exists():
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for z_dict in data.get("zones", []):
                        zone = ProductShelfZone(**z_dict)
                        self.zones[zone.id] = zone
                    logger.info(f"Loaded {len(self.zones)} product shelf zones from {self.config_path}")
                    return
                except Exception as e:
                    logger.error(f"Error reading {self.config_path}: {e}")

            # A fresh install has no product zones. This used to seed four
            # invented SKUs on cameras "cam_02/03/05" and write them to disk.
            self._save_zones()

    def _save_zones(self):
        try:
            payload = {"zones": [z.model_dump() for z in self.zones.values()]}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Saved {len(self.zones)} product shelf zones to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save {self.config_path}: {e}")

    def clear_all(self) -> int:
        """Remove every product shelf zone (store reset)."""
        with self.lock:
            n = len(self.zones)
            self.zones.clear()
            self.active_tracks.clear()
            self.version += 1
            self._save_zones()
            return n

    # ---------------- Zone CRUD ----------------

    def get_zones(self, camera_id: Optional[str] = None) -> List[ProductShelfZone]:
        with self.lock:
            if camera_id and camera_id != "all":
                return [z for z in self.zones.values() if z.camera_id == camera_id]
            return list(self.zones.values())

    def get_zone(self, zone_id: str) -> Optional[ProductShelfZone]:
        with self.lock:
            return self.zones.get(zone_id)

    def save_zone(self, zone: ProductShelfZone) -> ProductShelfZone:
        with self.lock:
            prev = self.zones.get(zone.id)
            if prev is not None and prev.created_at:
                zone = zone.model_copy(update={"created_at": prev.created_at})
            self.zones[zone.id] = zone
            self.version += 1
            self._save_zones()
            return zone

    def delete_zone(self, zone_id: str) -> bool:
        with self.lock:
            if zone_id in self.zones:
                del self.zones[zone_id]
                self.version += 1
                self._save_zones()
                return True
            return False

    # ---------------- Attribution ----------------

    def attributes(self) -> Dict[str, Dict[str, Any]]:
        """Effective level / value tier per zone id (cached per zone version)."""
        with self.lock:
            version, cached = self._attr_cache
            if version == self.version:
                return cached
            zones = list(self.zones.values())
            v = self.version
        attrs = derive_attributes(zones)
        with self.lock:
            self._attr_cache = (v, attrs)
        return attrs

    def zone_dict(self, zone: ProductShelfZone) -> Dict[str, Any]:
        """The stored zone plus its effective level / value tier."""
        d = zone.model_dump()
        a = self.attributes().get(zone.id) or {}
        d["effective_shelf_level"] = a.get("shelf_level")
        d["shelf_level_source"] = a.get("shelf_level_source")
        d["effective_value_tier"] = a.get("value_tier")
        d["value_tier_source"] = a.get("value_tier_source")
        d["high_value"] = bool(a.get("high_value"))
        return d

    # ---------------- Manual diagnostic evaluation ----------------

    def process_person_pose(
        self,
        camera_id: str,
        track_id: int,
        keypoints: List[Any],  # COCO 17 keypoints: index 9 = left_wrist, 10 = right_wrist
        bbox: Tuple[float, float, float, float],
        now_ts: Optional[float] = None
    ) -> List[ShelfInteractionEvent]:
        """Evaluate one posted skeleton's wrists against the camera's product zones.

        Diagnostic only (``POST /api/v1/analytics/products/interactions``):
        nothing is persisted or counted. Keypoints are normalised 0..1. When a
        wrist is inside several zones the deepest (largest bbox-normalised
        margin) wins, matching the live pipeline's one-zone-per-hand rule.
        """
        now = now_ts or time.time()
        track_key = (camera_id, track_id)
        events: List[ShelfInteractionEvent] = []

        zones = [z for z in self.get_zones(camera_id)
                 if z.enabled and z.study_metrics.track_hand_reach and len(z.points) >= 3]
        if not zones or len(keypoints) < 11:
            return events

        def get_kp(idx: int) -> Tuple[float, float, float]:
            kp = keypoints[idx]
            if hasattr(kp, "x") and hasattr(kp, "y"):
                return (kp.x, kp.y, getattr(kp, "confidence", 1.0))
            if isinstance(kp, dict):
                return (float(kp.get("x", 0.0)), float(kp.get("y", 0.0)),
                        float(kp.get("confidence", kp.get("score", kp.get("v", 1.0)))))
            if isinstance(kp, (list, tuple)) and len(kp) >= 2:
                return (kp[0], kp[1], kp[2] if len(kp) > 2 else 1.0)
            return (0.0, 0.0, 0.0)

        wrists = [get_kp(9), get_kp(10)]

        def depth(z: ProductShelfZone, x: float, y: float) -> float:
            poly = [(p.x, p.y) for p in z.points]
            if not PolygonGeometry.is_point_in_polygon(x, y, poly):
                return -1.0
            best = float("inf")
            n = len(poly)
            for i in range(n):
                ax, ay = poly[i]
                bx, by = poly[(i + 1) % n]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy or 1e-12
                t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
                best = min(best, ((x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2) ** 0.5)
            return best

        hit: Optional[ProductShelfZone] = None
        hit_depth, hit_conf = -1.0, 0.0
        for (wx, wy, wc) in wrists:
            if wc <= 0.4:
                continue
            for z in zones:
                d = depth(z, wx, wy)
                if d > hit_depth:
                    hit, hit_depth, hit_conf = z, d, wc

        with self.lock:
            state = self.active_tracks.setdefault(track_key, {
                "active_zone_id": None, "reach_start": 0.0, "last_seen": now, "had_dwell": False,
            })
            state["last_seen"] = now
            active = state["active_zone_id"]
            if active is not None and (hit is None or hit.id != active):
                zone = self.zones.get(active)
                events.append(ShelfInteractionEvent(
                    event_id=f"evt_{int(now * 1000)}_{active}_{track_id}", zone_id=active,
                    sku_id=zone.sku_id if zone else "", camera_id=camera_id, track_id=track_id,
                    action_type="REACH_END", dwell_duration_sec=round(now - state["reach_start"], 2),
                    confidence=round(float(max(w[2] for w in wrists)), 3),
                ))
                state["active_zone_id"] = None
            if hit is not None:
                if state["active_zone_id"] != hit.id:
                    state.update(active_zone_id=hit.id, reach_start=now, had_dwell=False)
                    events.append(ShelfInteractionEvent(
                        event_id=f"evt_{int(now * 1000)}_{hit.id}_{track_id}", zone_id=hit.id,
                        sku_id=hit.sku_id, camera_id=camera_id, track_id=track_id,
                        action_type="REACH_IN", dwell_duration_sec=0.0, confidence=round(hit_conf, 3),
                    ))
                else:
                    dwell = now - state["reach_start"]
                    if dwell >= 1.0 and not state["had_dwell"]:
                        state["had_dwell"] = True
                        events.append(ShelfInteractionEvent(
                            event_id=f"evt_{int(now * 1000)}_{hit.id}_{track_id}", zone_id=hit.id,
                            sku_id=hit.sku_id, camera_id=camera_id, track_id=track_id,
                            action_type="INSPECT_DWELL", dwell_duration_sec=round(dwell, 2),
                            confidence=round(hit_conf, 3),
                        ))
        return events

    # ---------------- Analytics (read from shelf_interactions) ----------------

    async def product_summary(self, db, start: datetime, end: datetime,
                              camera_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-product reach analytics for ``[start, end)`` (naive UTC), from the DB.

        Every figure is an aggregate over persisted ``shelf_interactions`` rows
        (plus ``customer_tracks`` for per-camera shopper traffic and
        ``pos_transactions`` for conversion). Zones with no reaches are listed
        with ``reaches: 0``; rows whose zone has since been deleted are listed
        with ``configured: false`` under the attribution stored on the row.
        """
        from sqlalchemy import and_, func, select
        from app.models.db_models import CustomerTrackModel, POSTransactionModel, ShelfInteractionModel

        zones = {z.id: z for z in self.get_zones(camera_id)}
        attrs = self.attributes()

        stmt = select(
            ShelfInteractionModel.shelf_zone_id, ShelfInteractionModel.camera_id,
            ShelfInteractionModel.person_track_id, ShelfInteractionModel.hand,
            ShelfInteractionModel.duration_sec, ShelfInteractionModel.timestamp,
            ShelfInteractionModel.zone_name, ShelfInteractionModel.sku_id,
            ShelfInteractionModel.product_category, ShelfInteractionModel.shelf_level,
            ShelfInteractionModel.value_tier,
        ).where(and_(ShelfInteractionModel.timestamp >= start, ShelfInteractionModel.timestamp < end))
        if camera_id:
            stmt = stmt.where(ShelfInteractionModel.camera_id == camera_id)
        rows = (await db.execute(stmt)).all()

        # Distinct real shoppers per camera (same "real track" rule as footfall).
        try:
            from app.services.retail_metrics_service import _track_is_real
            real = _track_is_real()
        except Exception:  # pragma: no cover - older metrics module
            real = CustomerTrackModel.end_time.isnot(None)
        cam_rows = (await db.execute(
            select(CustomerTrackModel.camera_id, func.count(func.distinct(CustomerTrackModel.track_id)))
            .where(and_(CustomerTrackModel.start_time >= start, CustomerTrackModel.start_time < end, real))
            .group_by(CustomerTrackModel.camera_id)
        )).all()
        camera_shoppers = {c: int(n) for c, n in cam_rows}

        pos_rows = (await db.execute(
            select(POSTransactionModel.sku_id, func.sum(POSTransactionModel.quantity))
            .where(and_(POSTransactionModel.timestamp >= start, POSTransactionModel.timestamp < end))
            .group_by(POSTransactionModel.sku_id)
        )).all()
        pos_connected = bool(pos_rows)
        units_by_sku = {str(s): int(q or 0) for s, q in pos_rows}

        products: Dict[str, Dict[str, Any]] = {}

        def product_row(zid: str, cam: str, row=None) -> Dict[str, Any]:
            p = products.get(zid)
            if p is not None:
                return p
            z = zones.get(zid)
            a = attrs.get(zid) or {}
            p = {
                "zone_id": zid,
                "camera_id": z.camera_id if z else cam,
                "name": z.name if z else (row.zone_name if row is not None else None) or zid,
                "sku_id": z.sku_id if z else (row.sku_id if row is not None else None),
                "category": z.category if z else (row.product_category if row is not None else None),
                "price": float(z.price) if z else None,
                "shelf_level": a.get("shelf_level") if z else (row.shelf_level if row is not None else None),
                "shelf_level_source": a.get("shelf_level_source") if z else ("recorded" if row is not None and row.shelf_level else None),
                "value_tier": a.get("value_tier") if z else (row.value_tier if row is not None else None),
                "configured": z is not None,
                "enabled": bool(z.enabled) if z else False,
                "reaches": 0,
                "left_hand": 0,
                "right_hand": 0,
                "_shoppers": set(),
                "_durations": [],
                "first_at": None,
                "last_at": None,
            }
            products[zid] = p
            return p

        for z in zones.values():
            product_row(z.id, z.camera_id)

        all_shoppers: set = set()
        for r in rows:
            all_shoppers.add((r.camera_id, r.person_track_id))
            p = product_row(r.shelf_zone_id, r.camera_id, r)
            p["reaches"] += 1
            if r.hand == "left":
                p["left_hand"] += 1
            elif r.hand == "right":
                p["right_hand"] += 1
            p["_shoppers"].add((r.camera_id, r.person_track_id))
            if r.duration_sec is not None:
                p["_durations"].append(float(r.duration_sec))
            ts = r.timestamp
            if ts is not None:
                p["first_at"] = ts if p["first_at"] is None or ts < p["first_at"] else p["first_at"]
                p["last_at"] = ts if p["last_at"] is None or ts > p["last_at"] else p["last_at"]

        # SKU-level conversion (a SKU may be mapped on several zones / cameras).
        sku_shoppers: Dict[str, set] = {}
        for p in products.values():
            if p["sku_id"]:
                sku_shoppers.setdefault(p["sku_id"], set()).update(p["_shoppers"])

        camera_reaches: Dict[str, int] = {}
        for p in products.values():
            camera_reaches[p["camera_id"]] = camera_reaches.get(p["camera_id"], 0) + p["reaches"]

        out_products: List[Dict[str, Any]] = []
        for p in products.values():
            durs = p.pop("_durations")
            shoppers = p.pop("_shoppers")
            p["shoppers"] = len(shoppers)
            p["avg_duration_sec"] = round(sum(durs) / len(durs), 2) if durs else None
            p["total_duration_sec"] = round(sum(durs), 2)
            p["first_at"] = _iso(p["first_at"])
            p["last_at"] = _iso(p["last_at"])
            p["camera_shoppers"] = camera_shoppers.get(p["camera_id"])
            p["camera_reaches"] = camera_reaches.get(p["camera_id"], 0)
            if pos_connected and p["sku_id"]:
                units = units_by_sku.get(p["sku_id"], 0)
                reachers = len(sku_shoppers.get(p["sku_id"], ()))
                p["pos_units_sold"] = units
                p["sku_reaching_shoppers"] = reachers
                p["units_per_reaching_shopper"] = round(units / reachers, 3) if reachers else None
                p["conversion_status"] = "measured" if reachers else "no reaches observed"
            else:
                p["pos_units_sold"] = None
                p["sku_reaching_shoppers"] = len(sku_shoppers.get(p["sku_id"], ())) if p["sku_id"] else None
                p["units_per_reaching_shopper"] = None
                p["conversion_status"] = "needs POS data"
            out_products.append(p)
        out_products.sort(key=lambda p: (-p["reaches"], str(p["name"])))

        # Per shelf level: reaches per configured zone at that level.
        levels: Dict[str, Dict[str, Any]] = {}
        for p in out_products:
            lvl = p["shelf_level"] or "UNKNOWN"
            L = levels.setdefault(lvl, {"level": lvl, "zones": 0, "reaches": 0})
            if p["configured"]:
                L["zones"] += 1
            L["reaches"] += p["reaches"]
        total = sum(p["reaches"] for p in out_products)
        level_rows = []
        for lvl in (*SHELF_LEVELS, "UNKNOWN"):
            if lvl not in levels:
                continue
            L = levels[lvl]
            L["reaches_per_zone"] = round(L["reaches"] / L["zones"], 2) if L["zones"] else None
            L["share_pct"] = round(L["reaches"] / total * 100.0, 1) if total else None
            level_rows.append(L)

        return {
            "window": {"start": _iso(start), "end": _iso(end)},
            "camera_id": camera_id,
            "pos_connected": pos_connected,
            "totals": {
                "reaches": total,
                "shoppers_reaching": len(all_shoppers),
                "products_configured": len(zones),
                "products_reached": sum(1 for p in out_products if p["reaches"] > 0),
            },
            "products": out_products,
            "shelf_levels": level_rows,
            "observed": total > 0,
            "message": None if total else (
                "No shelf reaches recorded in this window." if zones else
                "No product shelf areas are mapped. Draw them on a camera in Studio."
            ),
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Naive-UTC storage value -> ISO 8601 with an explicit Z."""
    if dt is None:
        return None
    return dt.isoformat() + "Z"


shelf_interaction_service = ShelfInteractionService()
