"""AI Zone Service: PolygonGeometry, Virtual Tripwires, Intrusion Zones, Privacy Masks, and Persistence."""

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from ..config import settings
from ..models.schemas import Point2D, TripwireDirection, ZoneConfig, ZoneType, MaskMode

logger = logging.getLogger("AIZoneService")
ZONES_CONFIG_FILE = Path("/home/meoclavezz/Projects-1/Edge_AI_CCTV/storage/zones_config.json")
ZONES_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)


class LineCrossingResult(str):
    """Result object for check_line_crossing that acts as Enum/str, unpacked (crossed, dir) tuple, and boolean."""
    def __new__(cls, val: str = "", crossed: bool = True):
        obj = str.__new__(cls, val)
        obj.crossed = crossed
        return obj

    def __bool__(self) -> bool:
        return self.crossed

    def __iter__(self):
        yield self.crossed
        yield str(self) if self.crossed else None

    def __eq__(self, other: Any) -> bool:
        if other is None:
            return not self.crossed
        if isinstance(other, tuple):
            return (self.crossed, str(self) if self.crossed else None) == other
        if hasattr(other, "value"):
            return self.crossed and str(self) == str(other.value)
        return self.crossed and str(self) == str(other)

    def __hash__(self):
        return super().__hash__()


class PolygonGeometry:
    @staticmethod
    def is_point_in_polygon(x: float, y: float, polygon: Any) -> bool:
        if len(polygon) < 3:
            return False
        inside = False
        j = len(polygon) - 1
        for i in range(len(polygon)):
            pt_i = polygon[i]
            pt_j = polygon[j]
            xi = pt_i.x if hasattr(pt_i, "x") else pt_i[0]
            yi = pt_i.y if hasattr(pt_i, "y") else pt_i[1]
            xj = pt_j.x if hasattr(pt_j, "x") else pt_j[0]
            yj = pt_j.y if hasattr(pt_j, "y") else pt_j[1]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
                inside = not inside
            j = i
        return inside

    @classmethod
    def point_in_polygon_raycasting(cls, *args, **kwargs) -> bool:
        if len(args) == 2:
            pt, polygon = args
            x = pt.x if hasattr(pt, "x") else pt[0]
            y = pt.y if hasattr(pt, "y") else pt[1]
            return cls.is_point_in_polygon(x, y, polygon)
        elif len(args) == 3:
            x, y, polygon = args
            return cls.is_point_in_polygon(x, y, polygon)
        if "point" in kwargs and "polygon" in kwargs:
            pt = kwargs["point"]
            x = pt.x if hasattr(pt, "x") else pt[0]
            y = pt.y if hasattr(pt, "y") else pt[1]
            return cls.is_point_in_polygon(x, y, kwargs["polygon"])
        if "x" in kwargs and "y" in kwargs and "polygon" in kwargs:
            return cls.is_point_in_polygon(kwargs["x"], kwargs["y"], kwargs["polygon"])
        raise ValueError("Invalid arguments to point_in_polygon_raycasting")

    @staticmethod
    def check_line_crossing(p1: Any, p2: Any, q1: Any, q2: Any) -> LineCrossingResult:
        def get_xy(p):
            return (p.x, p.y) if hasattr(p, "x") else (p[0], p[1])
        
        x_p1, y_p1 = get_xy(p1)
        x_p2, y_p2 = get_xy(p2)
        x_q1, y_q1 = get_xy(q1)
        x_q2, y_q2 = get_xy(q2)

        def ccw(ax, ay, bx, by, cx, cy):
            return (cy - ay) * (bx - ax) - (by - ay) * (cx - ax)

        d1 = ccw(x_p1, y_p1, x_p2, y_p2, x_q1, y_q1)
        d2 = ccw(x_p1, y_p1, x_p2, y_p2, x_q2, y_q2)
        d3 = ccw(x_q1, y_q1, x_q2, y_q2, x_p1, y_p1)
        d4 = ccw(x_q1, y_q1, x_q2, y_q2, x_p2, y_p2)

        if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
           ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
            direction = "A_TO_B" if d1 < 0 else "B_TO_A"
            return LineCrossingResult(direction, crossed=True)
        return LineCrossingResult("", crossed=False)


class AIZoneService:
    def __init__(self):
        self.lock = threading.Lock()
        self.tripwires: Dict[str, Dict[str, Any]] = {}
        self.intrusion_zones: Dict[str, Dict[str, Any]] = {}
        self.exclusion_masks: Dict[str, Dict[str, Any]] = {}
        self.zone_trackers: Dict[str, Any] = {}
        self._load_persistent_zones()

    def _load_persistent_zones(self):
        with self.lock:
            if ZONES_CONFIG_FILE.exists():
                try:
                    with open(ZONES_CONFIG_FILE, "r") as f:
                        data = json.load(f)
                    self.tripwires = {tw["id"]: tw for tw in data.get("tripwires", [])}
                    self.intrusion_zones = {iz["id"]: iz for iz in data.get("intrusion_zones", [])}
                    self.exclusion_masks = {ex["id"]: ex for ex in data.get("exclusion_masks", [])}
                    logger.info(f"Loaded {len(self.tripwires)} tripwires, {len(self.intrusion_zones)} intrusion zones, {len(self.exclusion_masks)} masks.")
                    return
                except Exception as e:
                    logger.error(f"Error loading zones_config.json: {e}")
            self._init_default_zones()
            self._save_persistent_zones()

    def _init_default_zones(self):
        self.tripwires = {
            "tw_main_entry": {
                "id": "tw_main_entry",
                "name": "Main Entrance Perimeter",
                "camera_id": "cam_living_room",
                "x1": 0.15, "y1": 0.65, "x2": 0.85, "y2": 0.65,
                "direction": "BIDIRECTIONAL",
                "allowed_classes": ["person", "vehicle"],
                "enabled": True,
                "in_count": 0,
                "out_count": 0
            }
        }
        self.intrusion_zones = {
            "iz_porch_restricted": {
                "id": "iz_porch_restricted",
                "name": "Porch / Restricted Zone",
                "camera_id": "cam_front_door",
                "points": [{"x": 0.20, "y": 0.50}, {"x": 0.80, "y": 0.50}, {"x": 0.85, "y": 0.90}, {"x": 0.15, "y": 0.90}],
                "allowed_classes": ["person"],
                "dwell_time_seconds": 0.5,
                "enabled": True
            }
        }
        self.exclusion_masks = {
            "ex_street_mask": {
                "id": "ex_street_mask",
                "name": "Street & Tree Foliage Mask",
                "camera_id": "cam_backyard",
                "points": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 0.25}, {"x": 0.0, "y": 0.25}],
                "mask_mode": "BLUR",
                "enabled": True
            }
        }

    def _save_persistent_zones(self):
        try:
            payload = {
                "tripwires": list(self.tripwires.values()),
                "intrusion_zones": list(self.intrusion_zones.values()),
                "exclusion_masks": list(self.exclusion_masks.values())
            }
            with open(ZONES_CONFIG_FILE, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"Error writing zones_config.json: {e}")

    def add_tripwire(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            tw_id = data.get("id") or f"tw_{int(time.time()*1000)}"
            data["id"] = tw_id
            data["in_count"] = data.get("in_count", 0)
            data["out_count"] = data.get("out_count", 0)
            self.tripwires[tw_id] = data
            self._save_persistent_zones()
            return data

    def delete_tripwire(self, tw_id: str) -> bool:
        with self.lock:
            if tw_id in self.tripwires:
                del self.tripwires[tw_id]
                self._save_persistent_zones()
                return True
            return False

    def add_intrusion(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            iz_id = data.get("id") or f"iz_{int(time.time()*1000)}"
            data["id"] = iz_id
            self.intrusion_zones[iz_id] = data
            self._save_persistent_zones()
            return data

    def delete_intrusion(self, iz_id: str) -> bool:
        with self.lock:
            if iz_id in self.intrusion_zones:
                del self.intrusion_zones[iz_id]
                self._save_persistent_zones()
                return True
            return False

    def add_exclusion(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            ex_id = data.get("id") or f"ex_{int(time.time()*1000)}"
            data["id"] = ex_id
            self.exclusion_masks[ex_id] = data
            self._save_persistent_zones()
            return data

    def delete_exclusion(self, ex_id: str) -> bool:
        with self.lock:
            if ex_id in self.exclusion_masks:
                del self.exclusion_masks[ex_id]
                self._save_persistent_zones()
                return True
            return False

    def clear_all(self):
        with self.lock:
            self.tripwires.clear()
            self.intrusion_zones.clear()
            self.exclusion_masks.clear()
            self._save_persistent_zones()

    def get_all_zones(self, camera_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if camera_id:
                tws = [tw for tw in self.tripwires.values() if tw.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                izs = [iz for iz in self.intrusion_zones.values() if iz.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                exs = [ex for ex in self.exclusion_masks.values() if ex.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                return {"tripwires": tws, "intrusion_zones": izs, "exclusion_masks": exs}
            return {
                "tripwires": list(self.tripwires.values()),
                "intrusion_zones": list(self.intrusion_zones.values()),
                "exclusion_masks": list(self.exclusion_masks.values())
            }

    def is_bbox_in_exclusion(self, x_center: float, y_center: float, camera_id: str = "cam_main") -> bool:
        with self.lock:
            for mask in self.exclusion_masks.values():
                if not mask.get("enabled", True):
                    continue
                pts_raw = mask.get("points", [])
                if len(pts_raw) >= 3:
                    pts = [Point2D(x=p["x"], y=p["y"]) for p in pts_raw]
                    if PolygonGeometry.is_point_in_polygon(x_center, y_center, pts):
                        return True
        return False

ai_zone_service = AIZoneService()
