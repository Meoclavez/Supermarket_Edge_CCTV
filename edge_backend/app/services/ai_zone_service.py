"""Per-camera overlay store: tripwires, restricted areas, queue areas and privacy masks.

All geometry is in image coordinates normalised 0..1 to the camera's own
frame (Camera Studio / mobile zone editor). Privacy masks are applied by
services/privacy_mask.py; tripwires, restricted areas ("intrusion_zones") and
checkout / queue areas ("queue_zones") are evaluated live by
services/tripwire_engine.py.
"""

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
ZONES_CONFIG_FILE = settings.STORAGE_DIR / "zones_config.json"
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
        self.queue_zones: Dict[str, Dict[str, Any]] = {}
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
                    self.queue_zones = {qz["id"]: qz for qz in data.get("queue_zones", [])}
                    logger.info(f"Loaded {len(self.tripwires)} tripwires, {len(self.intrusion_zones)} intrusion zones, {len(self.exclusion_masks)} masks.")
                    return
                except Exception as e:
                    logger.error(f"Error loading zones_config.json: {e}")
            self._init_default_zones()
            self._save_persistent_zones()

    def _init_default_zones(self):
        # A fresh install has no overlays: the operator draws them in Studio.
        self.tripwires = {}
        self.intrusion_zones = {}
        self.exclusion_masks = {}
        self.queue_zones = {}

    def _save_persistent_zones(self):
        try:
            payload = {
                "tripwires": list(self.tripwires.values()),
                "intrusion_zones": list(self.intrusion_zones.values()),
                "exclusion_masks": list(self.exclusion_masks.values()),
                "queue_zones": list(self.queue_zones.values()),
            }
            with open(ZONES_CONFIG_FILE, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"Error writing zones_config.json: {e}")

    def add_tripwire(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            tw_id = data.get("id") or f"tw_{int(time.time()*1000)}"
            data["id"] = tw_id
            # Counts are not stored on the line: crossings are rows in
            # tripwire_events (services/tripwire_engine.py). Drop the legacy
            # always-zero counters so nothing presents them as observations.
            data.pop("in_count", None)
            data.pop("out_count", None)
            self.tripwires[tw_id] = data
            self._save_persistent_zones()
            return data

    def get_tripwire(self, tw_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            tw = self.tripwires.get(tw_id)
            return dict(tw) if tw is not None else None

    def update_tripwire(self, tw_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace a tripwire's stored record (already validated); None if missing."""
        with self.lock:
            if tw_id not in self.tripwires:
                return None
            record = dict(record)
            record["id"] = tw_id
            self.tripwires[tw_id] = record
            self._save_persistent_zones()
            return dict(record)

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

    def get_intrusion(self, iz_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            iz = self.intrusion_zones.get(iz_id)
            return dict(iz) if iz is not None else None

    def update_intrusion(self, iz_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace a restricted area's stored record (already validated); None if missing."""
        with self.lock:
            if iz_id not in self.intrusion_zones:
                return None
            record = dict(record)
            record["id"] = iz_id
            self.intrusion_zones[iz_id] = record
            self._save_persistent_zones()
            return dict(record)

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

    def update_exclusion(self, ex_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge ``changes`` into an existing mask; None if it does not exist."""
        with self.lock:
            mask = self.exclusion_masks.get(ex_id)
            if mask is None:
                return None
            mask.update(changes)
            self._save_persistent_zones()
            return dict(mask)

    def delete_exclusion(self, ex_id: str) -> bool:
        with self.lock:
            if ex_id in self.exclusion_masks:
                del self.exclusion_masks[ex_id]
                self._save_persistent_zones()
                return True
            return False

    # Checkout / queue areas (camera roles, m0012): polygons whose dwell is
    # recorded in queue_visits by tripwire_engine.
    def add_queue_zone(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            qz_id = data.get("id") or f"qa_{int(time.time()*1000)}"
            data["id"] = qz_id
            self.queue_zones[qz_id] = data
            self._save_persistent_zones()
            return dict(data)

    def get_queue_zone(self, qz_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            qz = self.queue_zones.get(qz_id)
            return dict(qz) if qz is not None else None

    def update_queue_zone(self, qz_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace a queue area's stored record (already validated); None if missing."""
        with self.lock:
            if qz_id not in self.queue_zones:
                return None
            record = dict(record)
            record["id"] = qz_id
            self.queue_zones[qz_id] = record
            self._save_persistent_zones()
            return dict(record)

    def delete_queue_zone(self, qz_id: str) -> bool:
        with self.lock:
            if qz_id in self.queue_zones:
                del self.queue_zones[qz_id]
                self._save_persistent_zones()
                return True
            return False

    def clear_all(self, kinds: Optional[List[str]] = None) -> int:
        """Delete every overlay of the given kinds (default: all kinds).

        kinds: any of "tripwires", "intrusion_zones", "exclusion_masks", "queue_zones".
        """
        stores = {
            "tripwires": self.tripwires,
            "intrusion_zones": self.intrusion_zones,
            "exclusion_masks": self.exclusion_masks,
            "queue_zones": self.queue_zones,
        }
        with self.lock:
            n = 0
            for kind, store in stores.items():
                if kinds is None or kind in kinds:
                    n += len(store)
                    store.clear()
            self._save_persistent_zones()
            return n

    def get_all_zones(self, camera_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if camera_id:
                tws = [tw for tw in self.tripwires.values() if tw.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                izs = [iz for iz in self.intrusion_zones.values() if iz.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                exs = [ex for ex in self.exclusion_masks.values() if ex.get("camera_id", "cam_main") == camera_id or camera_id == "all"]
                qzs = [qz for qz in self.queue_zones.values() if qz.get("camera_id") == camera_id or camera_id == "all"]
                return {"tripwires": tws, "intrusion_zones": izs, "exclusion_masks": exs, "queue_zones": qzs}
            return {
                "tripwires": list(self.tripwires.values()),
                "intrusion_zones": list(self.intrusion_zones.values()),
                "exclusion_masks": list(self.exclusion_masks.values()),
                "queue_zones": list(self.queue_zones.values()),
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
