"""Product Shelf Mapping & Hand-to-Shelf Tracking Service.

Tracks customer hand/wrist keypoints entering designated product shelf zones,
evaluates dwell inspection, detects grab vs. put-back (friction), and calculates
real-time shelf conversion analytics.
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from app.services.ai_zone_service import PolygonGeometry
from app.config import settings

logger = logging.getLogger("ShelfInteractionService")

STORAGE_PATH = settings.STORAGE_DIR / "shelf_products_config.json"
STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)


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
    shelf_tier: str = "EYE_LEVEL"  # TOP, EYE_LEVEL, REACH, BOTTOM, ENDCAP
    study_metrics: StudyMetricsConfig = Field(default_factory=StudyMetricsConfig)
    enabled: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ShelfInteractionEvent(BaseModel):
    event_id: str
    zone_id: str
    sku_id: str
    camera_id: str
    track_id: int
    action_type: str  # "APPROACH", "REACH_IN", "INSPECT_DWELL", "ITEM_PICK", "ITEM_PUT_BACK"
    dwell_duration_sec: float
    confidence: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_put_back: bool = False


# ---------------- Service Class ----------------

class ShelfInteractionService:
    def __init__(self, config_path: Path = STORAGE_PATH):
        self.config_path = config_path
        self.lock = threading.Lock()
        self.zones: Dict[str, ProductShelfZone] = {}
        self.active_tracks: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.zone_stats: Dict[str, Dict[str, Any]] = {}
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
                        self._init_stats(zone.id)
                    logger.info(f"Loaded {len(self.zones)} product shelf zones from {self.config_path}")
                    return
                except Exception as e:
                    logger.error(f"Error reading {self.config_path}: {e}")

            # A fresh install has no product zones. This used to seed four
            # invented SKUs on cameras "cam_02/03/05" and write them to disk.
            self._save_zones()

    def _init_stats(self, zone_id: str):
        """Counters start at zero; every increment corresponds to an observed event."""
        if zone_id not in self.zone_stats:
            self.zone_stats[zone_id] = {
                "impressions": 0,
                "touches": 0,
                "dwell_seconds_total": 0.0,
                "picks": 0,
                "put_backs": 0,
                "pos_sales": 0,
            }

    def _save_zones(self):
        try:
            payload = {"zones": [z.model_dump() for z in self.zones.values()]}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Saved {len(self.zones)} product shelf zones to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save {self.config_path}: {e}")

    def clear_all(self) -> int:
        """Remove every product shelf zone and its counters (store reset)."""
        with self.lock:
            n = len(self.zones)
            self.zones.clear()
            for attr in ("stats", "_stats", "zone_stats", "interactions", "_interactions"):
                store = getattr(self, attr, None)
                if isinstance(store, dict):
                    store.clear()
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
            self.zones[zone.id] = zone
            self._init_stats(zone.id)
            self._save_zones()
            return zone

    def delete_zone(self, zone_id: str) -> bool:
        with self.lock:
            if zone_id in self.zones:
                del self.zones[zone_id]
                self._save_zones()
                return True
            return False

    # ---------------- Hand & Reach Tracking Engine ----------------

    def process_person_pose(
        self,
        camera_id: str,
        track_id: int,
        keypoints: List[Any],  # COCO 17 keypoints: index 9 = left_wrist, 10 = right_wrist
        bbox: Tuple[float, float, float, float],  # (x_min, y_min, x_max, y_max)
        now_ts: Optional[float] = None
    ) -> List[ShelfInteractionEvent]:
        """Evaluates hand reach coordinates into active product shelf zones.

        Keypoint COCO standard:
          id 9: left_wrist (x, y, conf)
          id 10: right_wrist (x, y, conf)
        """
        now = now_ts or time.time()
        track_key = (camera_id, track_id)
        events: List[ShelfInteractionEvent] = []

        relevant_zones = self.get_zones(camera_id)
        if not relevant_zones or len(keypoints) < 11:
            return events

        # Extract wrists
        def get_kp(idx: int) -> Tuple[float, float, float]:
            kp = keypoints[idx]
            if hasattr(kp, "x") and hasattr(kp, "y"):
                return (kp.x, kp.y, getattr(kp, "confidence", 1.0))
            elif isinstance(kp, (list, tuple)) and len(kp) >= 2:
                conf = kp[2] if len(kp) > 2 else 1.0
                return (kp[0], kp[1], conf)
            return (0.0, 0.0, 0.0)

        lw_x, lw_y, lw_conf = get_kp(9)   # left_wrist
        rw_x, rw_y, rw_conf = get_kp(10)  # right_wrist

        with self.lock:
            state = self.active_tracks.setdefault(track_key, {
                "active_zone_id": None,
                "reach_start": 0.0,
                "last_seen": now,
                "had_grab": False
            })
            elapsed = max(0.0, now - state["last_seen"])
            state["last_seen"] = now

            for zone in relevant_zones:
                if not zone.enabled or not zone.study_metrics.track_hand_reach:
                    continue

                poly = [(p.x, p.y) for p in zone.points]

                # Check if either left or right wrist is inside product polygon
                left_inside = (lw_conf > 0.4 and PolygonGeometry.is_point_in_polygon(lw_x, lw_y, poly))
                right_inside = (rw_conf > 0.4 and PolygonGeometry.is_point_in_polygon(rw_x, rw_y, poly))
                hand_inside = left_inside or right_inside

                # State Machine Transition
                if hand_inside:
                    if state["active_zone_id"] != zone.id:
                        # 1. New REACH_IN
                        state["active_zone_id"] = zone.id
                        state["reach_start"] = now
                        state["had_grab"] = False
                        self.zone_stats[zone.id]["touches"] += 1

                        evt = ShelfInteractionEvent(
                            event_id=f"evt_{int(now*1000)}_{zone.id}_{track_id}",
                            zone_id=zone.id,
                            sku_id=zone.sku_id,
                            camera_id=camera_id,
                            track_id=track_id,
                            action_type="REACH_IN",
                            dwell_duration_sec=0.0,
                            confidence=max(lw_conf if left_inside else 0.0, rw_conf if right_inside else 0.0),
                            is_put_back=False
                        )
                        events.append(evt)
                    else:
                        # 2. Dwell inspection
                        dwell = now - state["reach_start"]
                        self.zone_stats[zone.id]["dwell_seconds_total"] += elapsed
                        if dwell >= 1.0 and not state["had_grab"]:
                            state["had_grab"] = True
                            evt = ShelfInteractionEvent(
                                event_id=f"evt_{int(now*1000)}_{zone.id}_{track_id}",
                                zone_id=zone.id,
                                sku_id=zone.sku_id,
                                camera_id=camera_id,
                                track_id=track_id,
                                action_type="INSPECT_DWELL",
                                dwell_duration_sec=round(dwell, 2),
                                confidence=0.9,
                                is_put_back=False
                            )
                            events.append(evt)
                else:
                    # If was inside this zone and now left
                    if state["active_zone_id"] == zone.id:
                        dwell = now - state["reach_start"]
                        # Determine if ITEM_PICK or ITEM_PUT_BACK
                        # If dwell was brief (< 1.2s) or hand hovered and retreated -> PUT_BACK
                        is_put_back = (dwell < 1.2 or not state["had_grab"])
                        action = "ITEM_PUT_BACK" if is_put_back else "ITEM_PICK"

                        if is_put_back:
                            self.zone_stats[zone.id]["put_backs"] += 1
                        else:
                            self.zone_stats[zone.id]["picks"] += 1

                        evt = ShelfInteractionEvent(
                            event_id=f"evt_{int(now*1000)}_{zone.id}_{track_id}",
                            zone_id=zone.id,
                            sku_id=zone.sku_id,
                            camera_id=camera_id,
                            track_id=track_id,
                            action_type=action,
                            dwell_duration_sec=round(dwell, 2),
                            confidence=0.85,
                            is_put_back=is_put_back
                        )
                        events.append(evt)
                        state["active_zone_id"] = None

        return events

    def get_zone_stats(self, zone_id: str) -> Dict[str, Any]:
        with self.lock:
            zone = self.zones.get(zone_id)
            if not zone:
                return {}
            self._init_stats(zone_id)
            s = self.zone_stats[zone_id]

            touches = int(s.get("touches", 0))
            picks = int(s.get("picks", 0))
            put_backs = int(s.get("put_backs", 0))
            sales = int(s.get("pos_sales", 0))
            impressions = int(s.get("impressions", 0))

            def ratio(num, den):
                return round((num / den) * 100.0, 2) if den > 0 else None

            # Rates exist only when their denominator was observed; nothing
            # is divided by an assumed floor of 1.
            attraction = ratio(touches, impressions)
            friction_idx = ratio(put_backs, touches)
            conversion = ratio(sales, touches)
            avg_dwell = round(s.get("dwell_seconds_total", 0.0) / touches, 1) if touches > 0 else None

            return {
                "zone_id": zone.id,
                "camera_id": zone.camera_id,
                "product_name": zone.name,
                "sku_id": zone.sku_id,
                "category": zone.category,
                "price": zone.price,
                "shelf_tier": zone.shelf_tier,
                "impressions": impressions,
                "touches": touches,
                "picks": picks,
                "put_backs": put_backs,
                "pos_sales": sales,
                "avg_dwell_sec": avg_dwell,
                "attraction_rate": attraction,
                "friction_index": friction_idx,
                "conversion_rate": conversion,
                "ab_test_mode": zone.study_metrics.ab_test_mode
            }


shelf_interaction_service = ShelfInteractionService()
