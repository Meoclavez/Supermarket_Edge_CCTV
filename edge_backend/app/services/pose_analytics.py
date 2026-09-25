"""Pose analytics: shelf interactions and loss-prevention rules from live skeletons.

The live engine calls ``pose_analytics.observe()`` once per analysed frame per
camera with the confirmed tracks (each carrying 17 COCO keypoints from the
YOLO26 pose model) and any context objects (bags). This module keeps a small,
bounded state per (camera, track) and turns it into:

* **Shelf interactions** -- a hand inside a product-zone polygon for at least
  ``INTERACTION_MIN_FRAMES`` analysed frames and ``INTERACTION_MIN_SEC``.
  The point tested first is the *hand tip*, extrapolated past the wrist along
  the elbow->wrist direction by ``INTERACTION_HAND_EXTEND_FRAC`` of the forearm
  (fingers enter a top shelf before the wrist), then the wrist. One zone per
  hand: the active zone is kept while the hand is in it, otherwise the deepest
  penetrated one wins. A wrist in the torso "rest" box is ignored unless the
  arm is extended (a foreshortened reach towards the camera); a shopper
  walking faster than ``INTERACTION_MAX_BODY_SPEED`` starts no reach; an
  occluded hand holds a reach open for ``INTERACTION_OCCLUSION_GRACE_SEC``;
  re-entering the same zone within ``INTERACTION_REENTRY_MERGE_SEC`` continues
  the same reach. Newly started interactions are returned to the engine (so it
  can mark the zone visit ``interacted``); each finished one is persisted to
  ``shelf_interactions`` with hand, start/end, duration, contact point, image
  and floor position, and the product attribution (SKU, category, shelf level,
  value tier) captured when the reach started.
* **Loss-prevention incidents** -- the rules in ``theft_detection_service``
  evaluated over that state. Each new incident gets an evidence JPEG, a
  ``theft_incidents`` row and a staff notification.

Coordinate spaces
-----------------
Product zones (``/api/v1/analytics/products/zones``, drawn in studio.js) are
stored per camera in **normalised image coordinates** (0..1 of the frame's
natural size), so a wrist is tested directly as ``(x / width, y / height)``.
This is exact: no projection is involved.

Cameras without any image-space product zone can optionally fall back to the
floor plan's SHELF zones (metres). The wrist pixel is then projected through
the camera's ground-plane homography. That is an approximation: a homography
maps points *on the floor*, and a wrist is ~0.8-1.6 m above it, so the
projected point lands further from the camera than the hand really is (by a
factor of roughly H / (H - h) for camera height H and wrist height h). For a
shelf zone drawn over the shelf footprint this still separates "hand over the
shelf" from "hand in the aisle" for typical ceiling cameras, but boundaries
are soft. Set ``INTERACTION_FLOOR_SHELF_FALLBACK=false`` to disable it.

Threading
---------
``observe`` runs on the camera worker thread and must stay cheap: it only does
arithmetic on a few keypoints per track and appends to bounded deques. All
I/O (JPEG encoding, database writes, notifications) is queued and done by a
background writer thread, in the same buffer-then-drain shape the live
engine uses for zone visits. Notifications are async and must run on the
application event loop, so they are scheduled onto the loop bound by
``pose_analytics.bind_loop()`` (done automatically by ``start()`` and by the
theft / analytics routes on their first request).
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from app.config import settings
from app.services.timeutil import utc_from_ts  # stored times are naive UTC
from app.services import theft_detection_service as rules

logger = logging.getLogger(__name__)

# COCO keypoint indices
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP = 11, 12

SKELETON_EDGES = (
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12),
    (11, 13), (13, 15), (12, 14), (14, 16), (0, 1), (0, 2), (1, 3), (2, 4),
)

HANDS = (("left", L_WRIST), ("right", R_WRIST))
# wrist index -> (shoulder, elbow) of the same arm
ARM = {L_WRIST: (L_SHOULDER, L_ELBOW), R_WRIST: (R_SHOULDER, R_ELBOW)}


def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _parse_names(raw: str) -> set[str]:
    return {p.strip().upper() for p in str(raw or "").split(",") if p.strip()}


def _point_in_poly(x: float, y: float, poly: List[Tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
        j = i
    return inside


def _edge_distance(x: float, y: float, poly: List[Tuple[float, float]]) -> float:
    """Distance from (x, y) to the nearest polygon edge (same units as the polygon)."""
    best = float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy or 1e-12
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / l2))
        best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
    return best


def _box_contains(box: Tuple[float, float, float, float], x: float, y: float) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _boxes_intersect(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


# ============================================================================
# State
# ============================================================================

@dataclass
class ObserveResult:
    # (track_id, product_zone_id) for shelf interactions that started this frame.
    interactions: List[Tuple[str, str]] = field(default_factory=list)
    # Summaries of incidents raised this frame (persisted asynchronously).
    incidents: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ProductZoneGeom:
    id: str
    name: str
    space: str                          # "image" (normalised 0..1) or "floor" (metres)
    polygon: List[Tuple[float, float]]
    bbox: Tuple[float, float, float, float]
    category: str = ""
    price: float = 0.0
    sku_id: Optional[str] = None
    high_value: bool = False
    shelf_level: Optional[str] = None       # TOP | MIDDLE | BOTTOM (operator-set or derived)
    value_tier: Optional[str] = None        # LOW | STANDARD | PREMIUM


@dataclass
class HandState:
    name: str
    kp_index: int
    candidate_zone: Optional[str] = None
    candidate_frames: int = 0
    candidate_since: float = 0.0
    candidate_vis: List[float] = field(default_factory=list)
    candidate_xy: Optional[Tuple[float, float]] = None
    active_zone: Optional[str] = None
    active_started: float = 0.0
    active_last: float = 0.0
    active_frames: int = 0
    active_vis_sum: float = 0.0
    active_missing: int = 0
    active_xy: Optional[Tuple[float, float]] = None
    active_floor: Optional[Tuple[float, float]] = None
    # Which point of the hand entered the zone ("hand_tip" | "wrist") and the
    # zone's product attribution captured when the reach started.
    candidate_contact: Optional[str] = None
    candidate_last: float = 0.0
    active_contact: Optional[str] = None
    active_zone_geom: Optional[ProductZoneGeom] = None
    # A reach that ended less than INTERACTION_REENTRY_MERGE_SEC ago: held
    # back so a hand jittering on the zone boundary continues it instead of
    # starting a second interaction. Persisted once the window passes.
    closing: Optional[Dict[str, Any]] = None
    # Samples since the wrist last left a product zone (concealment evidence).
    post_reach: Optional[List[Dict[str, Any]]] = None
    post_reach_zone: Optional[str] = None
    post_reach_snapshot: Optional[Dict[str, Any]] = None
    trajectory: Deque[Tuple[float, float, float, float]] = field(default_factory=deque)
    # Where the hand touched a zone on the latest frame it was seen in one
    # (pixel point, zone id, time): the input to the cross-track dedupe.
    hit_xy: Optional[Tuple[float, float]] = None
    hit_wrist: Optional[Tuple[float, float]] = None
    hit_zone: Optional[str] = None
    hit_ts: float = -1.0
    # Time _start_reach ran for the active reach (the frame it was reported).
    active_reported_ts: float = -1.0
    # The active reach duplicates another track's reach (the pose model gave
    # one physical hand to two overlapping people): it is never persisted.
    active_suppressed: bool = False


@dataclass
class TrackState:
    track_id: Any
    first_seen: float
    last_seen: float
    hands: Dict[str, HandState]
    skeletons: Deque[Tuple[float, np.ndarray]]
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    keypoints: Optional[np.ndarray] = None
    # Time of the last fresh skeleton (``keypoints`` is from that frame).
    keypoints_ts: float = -1.0
    reaches: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=64))
    interaction_count: int = 0
    interaction_vis_sum: float = 0.0
    first_interaction_t: Optional[float] = None
    # Floor zones entered, in order: {"t", "zone_id", "category"}.
    zone_sequence: List[Dict[str, Any]] = field(default_factory=list)
    current_floor_zone: Optional[str] = None
    frames: int = 0
    frames_with_floor: int = 0
    last_floor: Optional[Tuple[float, float]] = None
    # Head turning.
    head_side: int = 0
    head_turns: Deque[float] = field(default_factory=lambda: deque(maxlen=256))
    head_samples: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=512))
    # High-value zone presence: zone_id -> {"start", "last"}.
    presence: Dict[str, Dict[str, float]] = field(default_factory=dict)
    fired: Dict[str, float] = field(default_factory=dict)
    # (t, x, y) of the hip centre, for the walking-past filter.
    hip_hist: Deque[Tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=32))


@dataclass
class CameraState:
    camera_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    tracks: Dict[Any, TrackState] = field(default_factory=dict)
    frame_size: Optional[Tuple[int, int]] = None      # (width, height)
    zones_version: int = -1
    zones: List[ProductZoneGeom] = field(default_factory=list)
    # Per-camera feature flags (feature_manager), refreshed every frame.
    interactions_on: bool = True
    theft_on: bool = True
    # Camera role (services/camera_roles.py), refreshed every frame from the
    # role cache: scaled theft thresholds, exit-rule gate, zone valuation.
    role: Optional[str] = None
    thresholds: Dict[str, Any] = field(default_factory=dict)
    exit_rule_on: bool = True
    zones_role: Optional[str] = None


# ============================================================================
# Engine
# ============================================================================

class PoseAnalytics:
    def __init__(self) -> None:
        self._cameras: Dict[str, CameraState] = {}
        self._cameras_lock = threading.Lock()
        self._floor_zones_override: Optional[List[Dict[str, Any]]] = None
        # Outgoing work: ("interaction", dict) | ("incident", dict)
        self._pending: List[Tuple[str, Dict[str, Any]]] = []
        self._pending_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._writer: Optional[threading.Thread] = None
        self._writer_stop = threading.Event()
        self._wake = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sync_engine = None
        self._sync_engine_url: Optional[str] = None
        self.stats = {"frames": 0, "interactions": 0, "incidents": 0, "persisted": 0, "write_errors": 0,
                      "deduplicated": 0}

    # ----------------------------------------------------------- lifecycle

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the application event loop for async notifications."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    async def start(self) -> None:
        """Bind the running loop and start the background writer."""
        self.bind_loop()
        self._ensure_writer()

    async def flush_async(self) -> int:
        """Flush queued work without blocking the event loop."""
        return await asyncio.to_thread(self.flush_now)

    def stop(self) -> None:
        self._writer_stop.set()
        self._wake.set()
        self.flush_now()

    def set_floor_zones(self, zones: Optional[List[Dict[str, Any]]]) -> None:
        """Override the floor zones (tests); ``None`` reads the live engine's."""
        self._floor_zones_override = zones

    def reset_camera(self, camera_id: str) -> None:
        """Drop a camera's state, persisting any interaction still in progress."""
        with self._cameras_lock:
            cam = self._cameras.pop(camera_id, None)
        if cam is None:
            return
        with cam.lock:
            for st in list(cam.tracks.values()):
                self._finalize_track(cam, st, st.last_seen)
            cam.tracks.clear()

    def reset_all(self) -> None:
        for cid in list(self._cameras.keys()):
            self.reset_camera(cid)

    # --------------------------------------------------------------- flags

    @staticmethod
    def _flag(camera_id: str, flag: str) -> bool:
        """Per-camera feature switch; schema default when unknown or unreadable."""
        try:
            from app.services.feature_manager import feature_manager
            return bool(feature_manager.is_enabled(camera_id, flag))
        except Exception:
            try:
                from app.models.schemas import CameraFeatureConfig
                return bool(getattr(CameraFeatureConfig(), flag, True))
            except Exception:
                return True

    @staticmethod
    def _refresh_role(cam: CameraState) -> None:
        """Role-derived settings for this frame. No role (or no store) = config defaults."""
        try:
            from app.services import camera_roles as cr

            roles = cr.role_cache.all()
            role = roles.get(cam.camera_id)
            cam.thresholds = cr.scaled_theft_thresholds(cr.theft_sensitivity(role))
            cam.exit_rule_on = cr.exit_rule_allowed(role, roles.values())
        except Exception:
            role = None
            cam.thresholds = {}
            cam.exit_rule_on = True
        cam.role = role

    @staticmethod
    def _thr(cam: CameraState, key: str, default: Any) -> Any:
        return cam.thresholds.get(key, default)

    # --------------------------------------------------------------- zones

    def _camera(self, camera_id: str) -> CameraState:
        cam = self._cameras.get(camera_id)
        if cam is None:
            with self._cameras_lock:
                cam = self._cameras.setdefault(camera_id, CameraState(camera_id=camera_id))
        return cam

    def _refresh_product_zones(self, cam: CameraState) -> None:
        try:
            from app.services.shelf_interaction_service import shelf_interaction_service as sis
        except Exception:  # pragma: no cover - import failure means no zones
            cam.zones = []
            return
        version = getattr(sis, "version", 0)
        if version == cam.zones_version and cam.zones and cam.zones_role == cam.role:
            return
        # On a high-value camera (role) every product zone counts as high value.
        role_hv = False
        try:
            from app.services.camera_roles import preset

            p = preset(cam.role)
            role_hv = bool(p and p.all_zones_high_value)
        except Exception:
            pass
        high_cats = _parse_names(settings.THEFT_HIGH_VALUE_CATEGORIES)
        min_price = float(settings.THEFT_HIGH_VALUE_MIN_PRICE or 0.0)
        try:
            attrs = sis.attributes()
        except Exception:
            attrs = {}
        zones: List[ProductZoneGeom] = []
        for z in sis.get_zones(cam.camera_id):
            if not z.enabled or not z.study_metrics.track_hand_reach or len(z.points) < 3:
                continue
            poly = [(float(p.x), float(p.y)) for p in z.points]
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            cat = (z.category or "").upper()
            zones.append(ProductZoneGeom(
                id=z.id, name=z.name, space="image", polygon=poly,
                bbox=(min(xs), min(ys), max(xs), max(ys)), category=cat,
                price=float(z.price or 0.0), sku_id=z.sku_id,
                high_value=role_hv or (cat in high_cats) or (min_price > 0 and float(z.price or 0.0) >= min_price),
                shelf_level=(attrs.get(z.id) or {}).get("shelf_level"),
                value_tier=(attrs.get(z.id) or {}).get("value_tier"),
            ))
        cam.zones = zones
        cam.zones_version = version
        cam.zones_role = cam.role

    def _floor_zones(self) -> List[Dict[str, Any]]:
        if self._floor_zones_override is not None:
            return self._floor_zones_override
        try:
            from app.services.live_analytics_engine import live_engine
        except Exception:
            try:
                from app.services.live_analytics_engine import live_analytics_engine as live_engine  # type: ignore
            except Exception:
                return []
        lock = getattr(live_engine, "_zones_lock", None)
        zones = getattr(live_engine, "_zones", None) or []
        if lock is not None:
            with lock:
                return list(zones)
        return list(zones)

    @staticmethod
    def _floor_zone_at(zones: List[Dict[str, Any]], x: float, y: float) -> Optional[Dict[str, Any]]:
        for z in zones:
            poly = z.get("polygon") or []
            pts = [(float(p["x"]), float(p["y"])) for p in poly if isinstance(p, dict)]
            if _point_in_poly(x, y, pts):
                return z
        return None

    # ------------------------------------------------------------ geometry

    @staticmethod
    def _track_floor(t: Any) -> Optional[Tuple[float, float]]:
        xy = getattr(t, "floor_xy", None)
        if xy is not None:
            try:
                return float(xy[0]), float(xy[1])
            except (TypeError, IndexError, ValueError):
                pass
        pts = getattr(t, "floor_points", None)
        if pts:
            p = pts[-1]
            try:
                return float(p["x"]), float(p["y"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _body(kps: np.ndarray, vis: float) -> Optional[Dict[str, float]]:
        """Shoulder/hip geometry, or None when the torso is not visible enough."""
        ls, rs, lh, rh = kps[L_SHOULDER], kps[R_SHOULDER], kps[L_HIP], kps[R_HIP]
        if ls[2] < vis or rs[2] < vis:
            return None
        sh_y = (ls[1] + rs[1]) / 2.0
        sh_w = abs(ls[0] - rs[0])
        if lh[2] >= vis and rh[2] >= vis:
            hip_y = (lh[1] + rh[1]) / 2.0
            x_lo = min(ls[0], rs[0], lh[0], rh[0])
            x_hi = max(ls[0], rs[0], lh[0], rh[0])
            hip_vis = (lh[2] + rh[2]) / 2.0
        elif lh[2] >= vis or rh[2] >= vis:
            h = lh if lh[2] >= rh[2] else rh
            hip_y = h[1]
            x_lo, x_hi = min(ls[0], rs[0], h[0]), max(ls[0], rs[0], h[0])
            hip_vis = h[2]
        else:
            return None
        torso = hip_y - sh_y
        if torso <= 1.0:
            return None
        return {
            "sh_y": float(sh_y), "hip_y": float(hip_y), "torso": float(torso),
            "x_lo": float(x_lo), "x_hi": float(x_hi), "sh_w": float(max(sh_w, 1.0)),
            "vis": float((ls[2] + rs[2]) / 2.0 * 0.5 + hip_vis * 0.5),
        }

    @staticmethod
    def _rest_box(body: Dict[str, float]) -> Tuple[float, float, float, float]:
        """Where a relaxed or carrying hand sits: the torso, widened, down to mid-thigh."""
        m = settings.INTERACTION_REST_MARGIN_FRAC * body["sh_w"]
        return (body["x_lo"] - m, body["sh_y"], body["x_hi"] + m, body["hip_y"] + 0.5 * body["torso"])

    @staticmethod
    def _conceal_box(body: Dict[str, float]) -> Tuple[float, float, float, float]:
        """Waistband / pocket band: lower torso to just below the hips."""
        m = 0.1 * body["sh_w"]
        top = body["sh_y"] + settings.THEFT_CONCEAL_REGION_TOP_FRAC * body["torso"]
        bottom = body["hip_y"] + settings.THEFT_CONCEAL_REGION_BOTTOM_FRAC * body["torso"]
        return (body["x_lo"] - m, top, body["x_hi"] + m, bottom)

    # ------------------------------------------------------------- observe

    def observe(self, camera_id: str, ts: float, frame_bgr: Optional[np.ndarray],
                tracks: list, objects: Optional[list] = None) -> ObserveResult:
        result = ObserveResult()
        cam = self._camera(camera_id)
        with cam.lock:
            try:
                self._observe_locked(cam, float(ts), frame_bgr, tracks or [], objects or [], result)
            except Exception as e:  # never break the capture loop
                logger.exception(f"pose_analytics.observe failed for {camera_id}: {e}")
        self.stats["frames"] += 1
        return result

    def _observe_locked(self, cam: CameraState, ts: float, frame: Optional[np.ndarray],
                        tracks: list, objects: list, result: ObserveResult) -> None:
        cam.interactions_on = self._flag(cam.camera_id, "shelf_interaction")
        cam.theft_on = self._flag(cam.camera_id, "theft_detection")
        self._refresh_role(cam)
        if not cam.interactions_on and not cam.theft_on:
            # Both analyses are switched off for this camera: keep no state.
            if cam.tracks:
                for st in list(cam.tracks.values()):
                    self._finalize_track(cam, st, st.last_seen)
                cam.tracks.clear()
            return
        if frame is not None and getattr(frame, "ndim", 0) >= 2:
            cam.frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        self._refresh_product_zones(cam)
        floor_zones = self._floor_zones()

        bag_ids = _parse_ids(settings.THEFT_BAG_CLASS_IDS)
        bags = []
        for o in objects:
            cid = getattr(o, "class_id", None)
            if cid in bag_ids:
                try:
                    bags.append(tuple(float(v) for v in o.bbox))
                except Exception:
                    pass

        seen: set = set()
        for t in tracks:
            if not getattr(t, "confirmed", True):
                continue
            tid = getattr(t, "track_id", None)
            if tid is None:
                continue
            tid = str(tid)   # tracker ids are strings ("trk_..."); keep one type
            seen.add(tid)
            st = cam.tracks.get(tid)
            if st is None:
                st = TrackState(
                    track_id=tid, first_seen=ts, last_seen=ts,
                    hands={name: HandState(name=name, kp_index=idx,
                                           trajectory=deque(maxlen=settings.INTERACTION_HISTORY_FRAMES))
                           for name, idx in HANDS},
                    skeletons=deque(maxlen=settings.INTERACTION_HISTORY_FRAMES),
                )
                cam.tracks[tid] = st
            st.last_seen = ts
            st.frames += 1
            try:
                st.bbox = tuple(float(v) for v in t.bbox)  # type: ignore[assignment]
            except Exception:
                pass

            floor = self._track_floor(t)
            if floor is not None:
                st.frames_with_floor += 1
                st.last_floor = floor
                self._update_floor_zone(cam, st, floor, floor_zones, ts, frame, result)

            kps_raw = getattr(t, "keypoints", None)
            # A coasting track (missed this detection frame) carries its last
            # skeleton; re-using it would invent wrist samples, so its hands
            # only age: the occlusion grace still runs on frame time.
            if kps_raw is None or getattr(t, "keypoints_fresh", True) is False:
                self._age_unseen_hands(cam, st, ts)
                continue
            kps = np.asarray(kps_raw, dtype=np.float32)
            if kps.shape != (17, 3):
                self._age_unseen_hands(cam, st, ts)
                continue
            st.keypoints = kps
            st.keypoints_ts = ts
            st.skeletons.append((ts, kps.copy()))
            self._update_head(st, kps, ts)
            self._update_hands(cam, st, kps, bags, floor, ts, frame, result)
            self._update_presence(cam, st, ts, frame, result)

        # One physical hand given to two overlapping people is one reach.
        self._dedupe_reaches(cam, ts, result)

        # Tracks the engine no longer reports: their open reaches close once
        # the occlusion grace runs out; the state is finalised after the TTL.
        ttl = settings.INTERACTION_TRACK_TTL_SEC
        for tid, st in list(cam.tracks.items()):
            if tid in seen:
                continue
            if ts - st.last_seen > ttl:
                cam.tracks.pop(tid)
                self._finalize_track(cam, st, st.last_seen, frame=None)
            else:
                self._age_unseen_hands(cam, st, ts)

    def _age_unseen_hands(self, cam: CameraState, st: TrackState, ts: float) -> None:
        """Advance a track's hand timers on a frame with no fresh skeleton for it.

        The person stepped out of view, the tracker is coasting, or the pose
        model lost the body. Nothing about the hand is known, so nothing
        advances a reach; but the occlusion grace is wall/frame time, so a
        reach whose hand has not been seen in its zone for
        ``INTERACTION_OCCLUSION_GRACE_SEC`` ends now, dated to the last time
        the hand was seen there (``active_last``). A hand that reappears in the
        same zone within the grace continues the same reach in
        ``_update_hands``.
        """
        grace = settings.INTERACTION_OCCLUSION_GRACE_SEC
        for hand in st.hands.values():
            if hand.active_zone is not None and ts - hand.active_last > grace:
                self._leave_shelf(cam, st, hand, None, ts, None, 0.0, None)
            if hand.closing is not None and ts - hand.closing["last"] > settings.INTERACTION_REENTRY_MERGE_SEC:
                self._emit_reach(cam, st, hand)
            if hand.candidate_zone is not None and ts - hand.candidate_last > grace:
                hand.candidate_zone = None
                hand.candidate_frames = 0
                hand.candidate_vis = []

    # ------------------------------------------------------------- hands

    def _zone_for_wrist(self, cam: CameraState, x: float, y: float) -> Optional[ProductZoneGeom]:
        """Zone containing one pixel point (single-point lookup, deepest zone wins)."""
        hit = self._zone_at_points(cam, [("wrist", x, y)], None)
        return hit[0] if hit else None

    def _image_zones_at(self, cam: CameraState, x: float, y: float) -> List[Tuple[float, ProductZoneGeom]]:
        """(penetration depth in pixels, zone) for every image-space zone containing pixel (x, y)."""
        w, h = cam.frame_size  # type: ignore[misc]
        nx, ny = x / max(w, 1), y / max(h, 1)
        out: List[Tuple[float, ProductZoneGeom]] = []
        for z in cam.zones:
            if z.space != "image":
                continue
            if _box_contains(z.bbox, nx, ny) and _point_in_poly(nx, ny, z.polygon):
                poly_px = [(px * w, py * h) for px, py in z.polygon]
                out.append((_edge_distance(x, y, poly_px), z))
        return out

    def _zone_at_points(self, cam: CameraState, points: List[Tuple[str, float, float]],
                        sticky: Optional[str]) -> Optional[Tuple[ProductZoneGeom, str, float, float]]:
        """Pick exactly one product zone for a hand, or None.

        ``points`` are (label, x, y) pixel points in priority order: the hand
        tip first (where the fingers are), then the wrist. The first point that
        lies in any zone decides. Among the zones containing it -- adjacent or
        overlapping shelf polygons -- the zone the hand is already reaching into
        is kept (hysteresis against flicker on a shared edge); otherwise the
        zone the point penetrates deepest (largest distance to the polygon's
        edge) wins. A hand is never attributed to two zones at once.
        """
        if cam.frame_size is None:
            return None
        if cam.zones:
            for label, x, y in points:
                hits = self._image_zones_at(cam, x, y)
                if not hits:
                    continue
                for _d, z in hits:
                    if sticky is not None and z.id == sticky:
                        return z, label, x, y
                _d, z = max(hits, key=lambda t: t[0])
                return z, label, x, y
            return None
        if not settings.INTERACTION_FLOOR_SHELF_FALLBACK:
            return None
        # Approximate: see module docstring (wrist projected by the ground homography).
        label, x, y = points[-1]
        try:
            from app.services.tracking_service import floor_projector
            p = floor_projector.to_floor(cam.camera_id, float(x), float(y))
        except Exception:
            p = None
        if p is None:
            return None
        for z in self._floor_zones():
            if str(z.get("category", "")).upper() != "SHELF":
                continue
            pts = [(float(q["x"]), float(q["y"])) for q in (z.get("polygon") or []) if isinstance(q, dict)]
            if _point_in_poly(p[0], p[1], pts):
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                return (ProductZoneGeom(id=z["id"], name=z.get("name") or z["id"], space="floor",
                                        polygon=pts, bbox=(min(xs), min(ys), max(xs), max(ys)),
                                        category="SHELF"), label, x, y)
        return None

    def _zone_by_id(self, cam: CameraState, zone_id: Optional[str]) -> Optional[ProductZoneGeom]:
        for z in cam.zones:
            if z.id == zone_id:
                return z
        return None

    @staticmethod
    def _hand_points(kps: np.ndarray, hand: HandState, vis_thr: float) -> List[Tuple[str, float, float]]:
        """Points of a hand to test against zones: extrapolated hand tip, then wrist.

        COCO has no finger keypoints; the hand extends past the wrist along the
        forearm (elbow -> wrist) by roughly 0.3-0.4 x forearm length. At a top
        shelf the fingers cross the zone's lower edge before the wrist does, so
        the tip is tested first. It needs a visible elbow to know the forearm
        direction; without one only the wrist is used.
        """
        wx, wy = float(kps[hand.kp_index][0]), float(kps[hand.kp_index][1])
        pts: List[Tuple[str, float, float]] = []
        frac = float(settings.INTERACTION_HAND_EXTEND_FRAC or 0.0)
        _s, e_idx = ARM[hand.kp_index]
        ex, ey, ev = (float(v) for v in kps[e_idx])
        if frac > 0 and ev >= vis_thr:
            fx, fy = wx - ex, wy - ey
            if math.hypot(fx, fy) >= 1.0:
                pts.append(("hand_tip", wx + frac * fx, wy + frac * fy))
        pts.append(("wrist", wx, wy))
        return pts

    @staticmethod
    def _arm_extended(kps: np.ndarray, hand: HandState, body: Optional[Dict[str, float]], vis_thr: float) -> bool:
        """True for a straight, raised-or-level arm: a reach, even over the torso box.

        A reach straight towards a shelf the camera faces is foreshortened, so
        the wrist projects over the shopper's own torso ("rest" box). Such an
        arm is still nearly straight (a straight 3D segment projects to a
        straight 2D one) and its forearm does not hang down, whereas a relaxed,
        carrying or phone-holding arm is bent or hanging. An arm too
        foreshortened to measure is not called extended.
        """
        if body is None:
            return False
        s_idx, e_idx = ARM[hand.kp_index]
        s, e, w = kps[s_idx], kps[e_idx], kps[hand.kp_index]
        if min(float(s[2]), float(e[2]), float(w[2])) < vis_thr:
            return False
        upper = math.hypot(float(e[0] - s[0]), float(e[1] - s[1]))
        fore = math.hypot(float(w[0] - e[0]), float(w[1] - e[1]))
        if upper + fore < 0.35 * body["torso"]:
            return False
        if float(w[1]) > float(e[1]) + 0.15 * body["torso"]:
            return False                     # forearm hanging down: at rest / carrying
        span = math.hypot(float(w[0] - s[0]), float(w[1] - s[1]))
        return span / (upper + fore) >= settings.INTERACTION_EXTENDED_ARM_RATIO

    @staticmethod
    def _body_speed(st: TrackState, body: Optional[Dict[str, float]], ts: float) -> Optional[float]:
        """Hip-centre speed in torso lengths per second over the last ~0.8 s, or None."""
        if body is None:
            return None
        cx, cy = (body["x_lo"] + body["x_hi"]) / 2.0, body["hip_y"]
        st.hip_hist.append((ts, cx, cy))
        ref = None
        for t, x, y in st.hip_hist:
            if ts - t <= 0.8:
                ref = (t, x, y)
                break
        if ref is None or ts - ref[0] < 0.15:
            return None
        return math.hypot(cx - ref[1], cy - ref[2]) / (ts - ref[0]) / max(body["torso"], 1.0)

    def _update_hands(self, cam: CameraState, st: TrackState, kps: np.ndarray, bags: list,
                      floor: Optional[Tuple[float, float]], ts: float,
                      frame: Optional[np.ndarray], result: ObserveResult) -> None:
        vis_thr = settings.INTERACTION_MIN_KEYPOINT_VIS
        body = self._body(kps, vis_thr)
        rest = self._rest_box(body) if body else None
        conceal = self._conceal_box(body) if body else None
        person_box = st.bbox
        speed = self._body_speed(st, body, ts)
        # Walking past: a hand swinging over a shelf zone is not a reach.
        passing = speed is not None and speed > settings.INTERACTION_MAX_BODY_SPEED

        for hand in st.hands.values():
            wx, wy, wv = (float(v) for v in kps[hand.kp_index])
            hand.trajectory.append((ts, wx, wy, wv))

            # A finished reach whose re-entry window has passed is final.
            if hand.closing is not None and ts - hand.closing["last"] > settings.INTERACTION_REENTRY_MERGE_SEC:
                self._emit_reach(cam, st, hand)

            hit = None
            if wv >= vis_thr:
                resting = bool(rest and _box_contains(rest, wx, wy)) and not self._arm_extended(kps, hand, body, vis_thr)
                if not resting:
                    sticky = hand.active_zone or (hand.closing or {}).get("zone_id")
                    hit = self._zone_at_points(cam, self._hand_points(kps, hand, vis_thr), sticky)
            zone: Optional[ProductZoneGeom] = hit[0] if hit else None
            if hit:
                hand.hit_xy, hand.hit_zone, hand.hit_ts = (hit[2], hit[3]), zone.id, ts
                hand.hit_wrist = (wx, wy)

            # Is the wrist at the waistband/pocket band or inside a bag the
            # person is carrying? (Only matters after a reach, but cheap.)
            target = None
            if wv >= settings.THEFT_CONCEAL_MIN_WRIST_VIS:
                if conceal and _box_contains(conceal, wx, wy):
                    target = "pocket_band"
                for b in bags:
                    if _box_contains(b, wx, wy) and _boxes_intersect(b, person_box):
                        target = "bag"
                        break

            # ---- concealment evidence for a hand that recently left a shelf
            if hand.post_reach is not None:
                hand.post_reach.append({
                    "t": ts, "in_shelf": zone is not None, "in_conceal": target is not None,
                    "vis": wv, "target": target,
                })
                verdict = rules.detect_concealment(
                    hand.post_reach,
                    window_sec=settings.THEFT_CONCEAL_WINDOW_SEC,
                    min_hold_frames=self._thr(cam, "conceal_min_hold_frames", settings.THEFT_CONCEAL_MIN_HOLD_FRAMES),
                    no_return_sec=settings.THEFT_CONCEAL_NO_RETURN_SEC,
                )
                state = verdict.get("state")
                if state in ("holding", "held") and hand.post_reach_snapshot is None and target:
                    hand.post_reach_snapshot = self._snapshot(frame, st, kps, ts)
                if verdict.get("detected"):
                    self._raise(cam, st, rules.RULE_CONCEALMENT, verdict, ts, result,
                                snapshot=hand.post_reach_snapshot or self._snapshot(frame, st, kps, ts),
                                zone_id=hand.post_reach_zone, hand=hand.name,
                                extra_evidence=[f"{hand.name.capitalize()} hand; body region from shoulder/hip keypoints"])
                    self._clear_post_reach(hand)
                elif state in ("returned", "expired"):
                    self._clear_post_reach(hand)
                elif len(hand.post_reach) > settings.INTERACTION_HISTORY_FRAMES:
                    self._clear_post_reach(hand)

            # ---- reach state machine
            # "observed": the hand was seen this frame (well enough to place it).
            # An unobserved hand -- occluded by the shopper's own body or the
            # product -- neither ends nor advances a reach until the occlusion
            # grace runs out.
            observed = wv >= vis_thr or target is not None
            if zone is not None and hand.active_zone == zone.id:
                hand.active_frames += 1
                hand.active_vis_sum += wv
                hand.active_last = ts
                hand.active_missing = 0
                continue
            if zone is not None and hand.closing is not None and hand.closing["zone_id"] == zone.id:
                # Back in the same zone within the merge window: same reach.
                self._resume_reach(hand, ts, wv)
                continue

            if hand.active_zone is not None:
                if observed:
                    hand.active_missing += 1
                    if hand.active_missing > settings.INTERACTION_EXIT_GRACE_FRAMES:
                        self._leave_shelf(cam, st, hand, kps, ts, frame, wv, target)
                elif ts - hand.active_last > settings.INTERACTION_OCCLUSION_GRACE_SEC:
                    self._leave_shelf(cam, st, hand, kps, ts, frame, wv, target)

            if zone is not None and not passing:
                if hand.candidate_zone == zone.id:
                    hand.candidate_frames += 1
                    hand.candidate_vis.append(wv)
                else:
                    hand.candidate_zone = zone.id
                    hand.candidate_frames = 1
                    hand.candidate_since = ts
                    hand.candidate_vis = [wv]
                    hand.candidate_xy = self._norm(cam, hit[2], hit[3])
                    hand.candidate_contact = hit[1]
                hand.candidate_last = ts
                if (hand.candidate_frames >= max(1, settings.INTERACTION_MIN_FRAMES)
                        and ts - hand.candidate_since >= settings.INTERACTION_MIN_SEC - 1e-6):
                    if hand.active_zone is not None:
                        # Moved straight into another zone: that reach ends here.
                        self._end_reach(cam, st, hand, hand.active_last)
                    self._start_reach(cam, st, hand, zone, floor, ts, frame, result)
            elif observed or passing or (hand.candidate_zone is not None
                                          and ts - hand.candidate_last > settings.INTERACTION_OCCLUSION_GRACE_SEC):
                hand.candidate_zone = None
                hand.candidate_frames = 0
                hand.candidate_vis = []

    def _leave_shelf(self, cam: CameraState, st: TrackState, hand: HandState, kps: np.ndarray, ts: float,
                     frame: Optional[np.ndarray], wv: float, target: Optional[str]) -> None:
        """The hand left its zone: end the reach and start collecting concealment evidence.

        ``kps`` is None when the reach ends on a frame without a skeleton
        (occlusion grace expired while the person was out of view).
        """
        left_zone = hand.active_zone
        reach_vis = hand.active_vis_sum / max(hand.active_frames, 1)
        suppressed = hand.active_suppressed
        self._end_reach(cam, st, hand, hand.active_last)
        if suppressed:
            # A duplicate of another person's reach: no concealment evidence
            # is collected for a hand that was never this person's.
            self._clear_post_reach(hand)
            return
        hand.post_reach = [{
            "t": ts, "in_shelf": False, "in_conceal": target is not None, "vis": wv,
            "reach_vis": reach_vis, "target": target,
        }]
        hand.post_reach_zone = left_zone
        hand.post_reach_snapshot = self._snapshot(frame, st, kps, ts) if target else None

    @staticmethod
    def _clear_post_reach(hand: HandState) -> None:
        hand.post_reach = None
        hand.post_reach_zone = None
        hand.post_reach_snapshot = None

    @staticmethod
    def _norm(cam: CameraState, x: float, y: float) -> Optional[Tuple[float, float]]:
        if not cam.frame_size:
            return None
        w, h = cam.frame_size
        return (round(min(max(x / max(w, 1), 0.0), 1.0), 4), round(min(max(y / max(h, 1), 0.0), 1.0), 4))

    def _start_reach(self, cam: CameraState, st: TrackState, hand: HandState, zone: ProductZoneGeom,
                     floor: Optional[Tuple[float, float]], ts: float, frame: Optional[np.ndarray],
                     result: ObserveResult) -> None:
        hand.active_zone = zone.id
        hand.active_zone_geom = zone
        hand.active_contact = hand.candidate_contact
        hand.active_started = hand.candidate_since
        hand.active_last = ts
        hand.active_frames = hand.candidate_frames
        hand.active_vis_sum = float(sum(hand.candidate_vis))
        hand.active_missing = 0
        hand.active_xy = hand.candidate_xy
        hand.active_floor = floor
        hand.active_reported_ts = ts
        hand.active_suppressed = False
        hand.candidate_zone = None
        hand.candidate_frames = 0
        hand.candidate_vis = []
        # A reach back into a shelf cancels pending concealment evidence.
        self._clear_post_reach(hand)

        vis = hand.active_vis_sum / max(hand.active_frames, 1)
        st.interaction_count += 1
        st.interaction_vis_sum += vis
        if st.first_interaction_t is None:
            st.first_interaction_t = hand.active_started
        st.reaches.append({"timestamp": hand.active_started, "zone_id": zone.id, "vis": vis, "hand": hand.name})
        # Reaches are always tracked (the theft rules need them); they are
        # reported and persisted only when shelf_interaction is on.
        if cam.interactions_on:
            result.interactions.append((str(st.track_id), zone.id))
            self.stats["interactions"] += 1

        # Shelf sweeping: evaluate on every new reach.
        verdict = rules.detect_shelf_sweeping(
            [r for r in st.reaches if r["zone_id"] == zone.id],
            window_sec=settings.THEFT_SWEEP_WINDOW_SEC,
            min_reaches=self._thr(cam, "sweep_min_reaches", settings.THEFT_SWEEP_MIN_REACHES),
        )
        if verdict.get("detected"):
            self._raise(cam, st, rules.RULE_SHELF_SWEEPING, verdict, ts, result,
                        snapshot=self._snapshot(frame, st, st.keypoints, ts),
                        zone_id=zone.id, hand=hand.name,
                        loss_multiplier=int(verdict.get("count", 1)))

    def _resume_reach(self, hand: HandState, ts: float, wv: float) -> None:
        """Continue the reach held in ``closing`` (the hand came straight back)."""
        c = hand.closing
        hand.closing = None
        hand.active_zone = c["zone_id"]
        hand.active_zone_geom = c["zone"]
        hand.active_contact = c["contact"]
        hand.active_started = c["started"]
        hand.active_frames = c["frames"] + 1
        hand.active_vis_sum = c["vis_sum"] + wv
        hand.active_last = ts
        hand.active_missing = 0
        hand.active_xy = c["xy"]
        hand.active_floor = c["floor"]
        hand.active_suppressed = bool(c.get("suppressed"))
        hand.candidate_zone = None
        hand.candidate_frames = 0
        hand.candidate_vis = []
        self._clear_post_reach(hand)

    def _end_reach(self, cam: CameraState, st: TrackState, hand: HandState, end_ts: float) -> None:
        """Close the active reach; it is persisted once the re-entry window passes."""
        if hand.active_zone is None:
            return
        if hand.closing is not None:
            self._emit_reach(cam, st, hand)
        hand.closing = {
            "zone_id": hand.active_zone,
            "zone": hand.active_zone_geom or self._zone_by_id(cam, hand.active_zone),
            "contact": hand.active_contact,
            "started": hand.active_started,
            "last": end_ts,
            "frames": hand.active_frames,
            "vis_sum": hand.active_vis_sum,
            "xy": hand.active_xy,
            "floor": hand.active_floor,
            "suppressed": hand.active_suppressed,
        }
        hand.active_zone = None
        hand.active_suppressed = False
        hand.active_zone_geom = None
        hand.active_contact = None
        hand.active_frames = 0
        hand.active_vis_sum = 0.0
        hand.active_missing = 0

    def _emit_reach(self, cam: CameraState, st: TrackState, hand: HandState) -> None:
        """Persist the reach held in ``closing`` as one shelf_interactions row."""
        c = hand.closing
        hand.closing = None
        if c is None:
            return
        if c.get("suppressed"):
            self.stats["deduplicated"] = self.stats.get("deduplicated", 0) + 1
            return
        zone: Optional[ProductZoneGeom] = c["zone"]
        duration = max(c["last"] - c["started"], 0.0)
        vis = c["vis_sum"] / max(c["frames"], 1)
        rec = {
            "id": f"si_{uuid.uuid4().hex[:14]}",
            "camera_id": cam.camera_id,
            "zone_id": c["zone_id"],
            "zone_name": zone.name if zone else None,
            "zone_space": zone.space if zone else "floor",
            "sku_id": zone.sku_id if zone else None,
            "product_category": (zone.category or None) if zone else None,
            "shelf_level": zone.shelf_level if zone else None,
            "value_tier": zone.value_tier if zone else None,
            "contact_point": c["contact"],
            "track_id": str(st.track_id),
            "hand": hand.name,
            "started_at": c["started"],
            "ended_at": c["last"],
            "duration_sec": round(duration, 3),
            "frames": c["frames"],
            "confidence": round(vis, 3),
            "image_xy": c["xy"],
            "floor_xy": c["floor"],
        }
        if cam.interactions_on:
            self._enqueue("interaction", rec)

    # ------------------------------------------------- cross-track dedupe

    def _dedupe_reaches(self, cam: CameraState, ts: float, result: ObserveResult) -> None:
        """Keep one reach when two people's skeletons claim the same hand.

        With two shoppers overlapping in the image, the pose model can give the
        front person's hand to the person behind as well, so one physical reach
        would be recorded twice, on two tracks, at the same spot. On every
        frame, active reaches of *different* tracks in the same zone whose
        contact points -- or wrists, since a borrowed wrist hangs off a
        different elbow and so extrapolates to a different hand tip -- are
        closer (this frame) than ``INTERACTION_DEDUPE_DIST_FRAC`` of the frame
        diagonal are one reach:
        the hand whose arm chain is most plausible for its own body keeps it
        (``_arm_plausibility``), ties going to the track whose shoulder is
        nearer the hand; the other is suppressed (never persisted, removed from
        the theft rules' reach history). Two people reaching side by side touch
        the zone at different points and are left alone.
        """
        if not cam.frame_size or len(cam.tracks) < 2:
            return
        frac = float(settings.INTERACTION_DEDUPE_DIST_FRAC or 0.0)
        if frac <= 0:
            return
        w, h = cam.frame_size
        max_d = frac * math.hypot(w, h)
        live: List[Tuple[TrackState, HandState]] = [
            (st, hand)
            for st in cam.tracks.values() if st.keypoints_ts == ts and st.keypoints is not None
            for hand in st.hands.values()
            if hand.active_zone is not None and not hand.active_suppressed
            and hand.hit_ts == ts and hand.hit_zone == hand.active_zone and hand.hit_xy is not None
        ]
        if len(live) < 2:
            return
        for i in range(len(live)):
            st_a, hand_a = live[i]
            for j in range(i + 1, len(live)):
                st_b, hand_b = live[j]
                if st_a is st_b or hand_a.active_suppressed or hand_b.active_suppressed:
                    continue
                if hand_a.active_zone != hand_b.active_zone:
                    continue
                (ax, ay), (bx, by) = hand_a.hit_xy, hand_b.hit_xy  # type: ignore[misc]
                d = math.hypot(ax - bx, ay - by)
                if hand_a.hit_wrist is not None and hand_b.hit_wrist is not None:
                    (awx, awy), (bwx, bwy) = hand_a.hit_wrist, hand_b.hit_wrist
                    d = min(d, math.hypot(awx - bwx, awy - bwy))
                if d > max_d:
                    continue
                keep_a = self._prefer_first(st_a, hand_a, st_b, hand_b)
                loser_st, loser = (st_b, hand_b) if keep_a else (st_a, hand_a)
                self._suppress_reach(cam, loser_st, loser, ts, result)

    def _prefer_first(self, st_a: TrackState, hand_a: HandState, st_b: TrackState, hand_b: HandState) -> bool:
        """True when track A's claim on the shared hand beats track B's."""
        pa = self._arm_plausibility(st_a, hand_a, st_b)
        pb = self._arm_plausibility(st_b, hand_b, st_a)
        if abs(pa - pb) > 0.1:
            return pa > pb
        return self._shoulder_distance(st_a, hand_a) <= self._shoulder_distance(st_b, hand_b)

    @staticmethod
    def _shoulder_distance(st: TrackState, hand: HandState) -> float:
        """Pixels from the hand's own shoulder (else the nearer visible one) to its wrist."""
        kps = st.keypoints
        if kps is None:
            return float("inf")
        vis_thr = settings.INTERACTION_MIN_KEYPOINT_VIS
        wx, wy = float(kps[hand.kp_index][0]), float(kps[hand.kp_index][1])
        s_idx, _e = ARM[hand.kp_index]
        order = [s_idx] + [i for i in (L_SHOULDER, R_SHOULDER) if i != s_idx]
        for i in order:
            if float(kps[i][2]) >= vis_thr:
                return math.hypot(float(kps[i][0]) - wx, float(kps[i][1]) - wy)
        return float("inf")

    def _arm_plausibility(self, st: TrackState, hand: HandState, other: TrackState) -> float:
        """0..1: how believable it is that this wrist belongs to this body.

        A wrist the pose model lent from someone else hangs off an arm chain
        that does not fit: the forearm is far longer (or shorter) than the
        upper arm, the arm is too long for the body's torso, the forearm folds
        straight back over the upper arm, or the "hand" sits inside the other
        person's torso. Each misfit scales the score down; a missing shoulder
        or elbow leaves the chain unverifiable, which also scores lower.
        """
        kps = st.keypoints
        if kps is None:
            return 0.0
        vis_thr = settings.INTERACTION_MIN_KEYPOINT_VIS
        s_idx, e_idx = ARM[hand.kp_index]
        s, e, wr = kps[s_idx], kps[e_idx], kps[hand.kp_index]
        score = 1.0
        if float(s[2]) < vis_thr:
            score *= 0.2
        elif float(e[2]) < vis_thr:
            score *= 0.5
        else:
            ux, uy = float(e[0] - s[0]), float(e[1] - s[1])
            fx, fy = float(wr[0] - e[0]), float(wr[1] - e[1])
            upper, fore = math.hypot(ux, uy), math.hypot(fx, fy)
            if upper < 1.0 or fore < 1.0:
                score *= 0.4
            else:
                ratio = fore / upper
                lo = settings.INTERACTION_DEDUPE_FOREARM_RATIO_MIN
                hi = settings.INTERACTION_DEDUPE_FOREARM_RATIO_MAX
                if ratio < lo:
                    score *= ratio / lo
                elif ratio > hi:
                    score *= hi / ratio
                if (ux * fx + uy * fy) / (upper * fore) < -0.85:
                    score *= 0.5            # forearm folded back onto the upper arm
                body = self._body(kps, vis_thr)
                if body is not None:
                    max_arm = settings.INTERACTION_DEDUPE_MAX_ARM_TORSOS * body["torso"]
                    if upper + fore > max_arm:
                        score *= max_arm / (upper + fore)
        if other.keypoints is not None:
            ob = self._body(other.keypoints, vis_thr)
            if ob is not None and _box_contains((ob["x_lo"], ob["sh_y"], ob["x_hi"], ob["hip_y"]),
                                                float(wr[0]), float(wr[1])):
                score *= 0.3
        return score

    def _suppress_reach(self, cam: CameraState, st: TrackState, hand: HandState, ts: float,
                        result: ObserveResult) -> None:
        """Mark ``hand``'s active reach a duplicate and undo what its start recorded."""
        hand.active_suppressed = True
        started, zone_id = hand.active_started, hand.active_zone
        for r in list(st.reaches):
            if r["hand"] == hand.name and r["zone_id"] == zone_id and r["timestamp"] == started:
                st.reaches.remove(r)
                st.interaction_count = max(0, st.interaction_count - 1)
                st.interaction_vis_sum = max(0.0, st.interaction_vis_sum - float(r["vis"]))
                break
        if st.first_interaction_t == started:
            st.first_interaction_t = min((r["timestamp"] for r in st.reaches), default=None)
        if hand.active_reported_ts == ts and cam.interactions_on:
            key = (str(st.track_id), zone_id)
            if key in result.interactions:
                result.interactions.remove(key)
                self.stats["interactions"] = max(0, self.stats["interactions"] - 1)
        logger.debug(f"pose_analytics: reach by {st.track_id} ({hand.name}) in {zone_id} "
                     f"on {cam.camera_id} duplicates another track's; suppressed")

    # ------------------------------------------------------------- head

    def _update_head(self, st: TrackState, kps: np.ndarray, ts: float) -> None:
        vis_thr = settings.INTERACTION_MIN_KEYPOINT_VIS
        yaw = rules.estimate_head_yaw(kps, vis_thr)
        if yaw is None:
            return
        head_vis = float(np.mean([kps[NOSE][2], max(kps[L_EAR][2], kps[R_EAR][2])]))
        st.head_samples.append((ts, head_vis))
        thr = settings.THEFT_HEAD_TURN_YAW
        side = 1 if yaw > thr else (-1 if yaw < -thr else 0)
        if side != 0:
            if st.head_side != 0 and side != st.head_side:
                st.head_turns.append(ts)
            st.head_side = side

    # --------------------------------------------------------- presence

    def _update_presence(self, cam: CameraState, st: TrackState, ts: float,
                         frame: Optional[np.ndarray], result: ObserveResult) -> None:
        if not cam.frame_size:
            return
        hv = [z for z in cam.zones if z.high_value]
        if not hv:
            return
        w, h = cam.frame_size
        x1, y1, x2, y2 = st.bbox
        nb = (x1 / w, y1 / h, x2 / w, y2 / h)
        body = self._body(st.keypoints, settings.INTERACTION_MIN_KEYPOINT_VIS) if st.keypoints is not None else None
        gap = settings.THEFT_LOITER_GAP_SEC
        for z in hv:
            # "Near" = the zone is within arm's reach of the shoulders
            # (THEFT_LOITER_REACH_TORSOS torso lengths), or overlaps the person.
            near = _boxes_intersect(nb, z.bbox)
            if not near and body is not None:
                cx = (body["x_lo"] + body["x_hi"]) / 2.0
                zx1, zy1, zx2, zy2 = z.bbox[0] * w, z.bbox[1] * h, z.bbox[2] * w, z.bbox[3] * h
                dx = max(zx1 - cx, 0.0, cx - zx2)
                dy = max(zy1 - body["sh_y"], 0.0, body["sh_y"] - zy2)
                near = math.hypot(dx, dy) <= settings.THEFT_LOITER_REACH_TORSOS * body["torso"]
            if not near:
                continue
            p = st.presence.get(z.id)
            if p is None or ts - p["last"] > gap:
                p = {"start": ts, "last": ts}
                st.presence[z.id] = p
            p["last"] = ts
            dwell = p["last"] - p["start"]
            min_dwell = self._thr(cam, "loiter_min_dwell_sec", settings.THEFT_LOITER_MIN_DWELL_SEC)
            if dwell < min_dwell:
                continue
            reaches = sum(1 for r in st.reaches if r["zone_id"] == z.id and r["timestamp"] >= p["start"])
            turns = sum(1 for t in st.head_turns if t >= p["start"])
            samples = [v for (t, v) in st.head_samples if t >= p["start"]]
            vis = float(np.mean(samples)) if samples else 0.0
            verdict = rules.detect_suspicious_loitering(
                dwell_sec=dwell, reaches=reaches, head_turns=turns, head_samples=len(samples),
                visibility=vis,
                min_dwell_sec=min_dwell,
                min_reaches=self._thr(cam, "loiter_min_reaches", settings.THEFT_LOITER_MIN_REACHES),
                min_head_turns=self._thr(cam, "loiter_min_head_turns", settings.THEFT_LOITER_MIN_HEAD_TURNS),
                zone_label=z.name,
            )
            if verdict.get("detected"):
                self._raise(cam, st, rules.RULE_SUSPICIOUS_LOITERING, verdict, ts, result,
                            snapshot=self._snapshot(frame, st, st.keypoints, ts), zone_id=z.id)

    # ------------------------------------------------------- floor zones

    def _update_floor_zone(self, cam: CameraState, st: TrackState, floor: Tuple[float, float],
                           floor_zones: List[Dict[str, Any]], ts: float,
                           frame: Optional[np.ndarray], result: ObserveResult) -> None:
        z = self._floor_zone_at(floor_zones, floor[0], floor[1])
        zid = z["id"] if z else None
        if zid == st.current_floor_zone:
            return
        st.current_floor_zone = zid
        if z is None:
            return
        cat = str(z.get("category") or "").upper()
        st.zone_sequence.append({"t": ts, "zone_id": zid, "category": cat})
        if len(st.zone_sequence) > 200:
            del st.zone_sequence[:-200]
        if not settings.THEFT_EXIT_RULE_ENABLED or cat not in rules.EXIT_CATEGORIES:
            return
        if not cam.exit_rule_on:
            # Camera role: a product-area camera in a store with dedicated
            # checkout cameras never sees the checkout visit (single-camera
            # rule), and stockroom cameras watch staff.
            return
        verdict = rules.detect_exit_without_checkout(
            st.zone_sequence,
            first_interaction_t=st.first_interaction_t,
            interaction_count=st.interaction_count,
            interaction_vis=st.interaction_vis_sum / max(st.interaction_count, 1),
            floor_coverage=st.frames_with_floor / max(st.frames, 1),
        )
        if verdict.get("detected"):
            self._raise(cam, st, rules.RULE_EXIT_WITHOUT_CHECKOUT, verdict, ts, result,
                        snapshot=self._snapshot(frame, st, st.keypoints, ts), zone_id=zid)

    # --------------------------------------------------------- incidents

    def _snapshot(self, frame: Optional[np.ndarray], st: TrackState, kps: Optional[np.ndarray],
                  ts: float) -> Optional[Dict[str, Any]]:
        if frame is None:
            return None
        return {"frame": frame.copy(), "bbox": st.bbox,
                "keypoints": None if kps is None else np.array(kps, copy=True), "t": ts}

    def _raise(self, cam: CameraState, st: TrackState, rule: str, verdict: Dict[str, Any], ts: float,
               result: ObserveResult, *, snapshot: Optional[Dict[str, Any]], zone_id: Optional[str] = None,
               hand: Optional[str] = None, extra_evidence: Optional[List[str]] = None,
               loss_multiplier: int = 1) -> None:
        if not cam.theft_on:
            return
        confidence = float(verdict.get("confidence") or 0.0)
        if confidence < self._thr(cam, "min_confidence", settings.THEFT_MIN_CONFIDENCE):
            return
        last = st.fired.get(rule)
        if last is not None and ts - last < settings.THEFT_INCIDENT_COOLDOWN_SEC:
            return
        st.fired[rule] = ts

        zone = self._zone_by_id(cam, zone_id)
        evidence = list(verdict.get("evidence") or [])
        if zone is not None:
            evidence.insert(0, f"Product zone: {zone.name}" + (f" ({zone.category})" if zone.category else ""))
        evidence.extend(extra_evidence or [])
        trajectory = []
        if hand and hand in st.hands:
            trajectory = [{"t": round(t, 2), "x": round(x, 1), "y": round(y, 1), "v": round(v, 2)}
                          for (t, x, y, v) in list(st.hands[hand].trajectory)[-40:]]
        items = []
        loss = 0.0
        if zone is not None:
            items.append({"zone_id": zone.id, "name": zone.name, "sku": zone.sku_id,
                          "category": zone.category, "price": zone.price})
            if rule in (rules.RULE_CONCEALMENT, rules.RULE_SHELF_SWEEPING) and zone.price > 0:
                loss = round(zone.price * max(1, loss_multiplier), 2)

        incident = {
            "id": f"theft_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
            "rule": rule,
            "camera_id": cam.camera_id,
            "track_id": str(st.track_id),
            "ts": ts,
            "confidence": round(confidence, 3),
            "severity": self._severity(cam, confidence),
            "zone_id": zone_id,
            "zone_name": zone.name if zone else None,
            "evidence": evidence,
            "bbox": st.bbox,
            "wrist_trajectory": trajectory,
            "items": items,
            "estimated_loss_value": loss,
            "snapshot": snapshot,
        }
        self._enqueue("incident", incident)
        self.stats["incidents"] += 1
        result.incidents.append({k: v for k, v in incident.items() if k != "snapshot"})
        logger.info(f"Suspicious behaviour for review: {rule} on {cam.camera_id} track {st.track_id} "
                    f"(confidence {confidence:.2f})")

    @staticmethod
    def _severity(cam: CameraState, confidence: float) -> str:
        sev = "HIGH" if confidence >= 0.75 else ("MEDIUM" if confidence >= 0.5 else "LOW")
        try:
            from app.services.camera_roles import preset

            p = preset(cam.role)
        except Exception:
            p = None
        order = ("LOW", "MEDIUM", "HIGH")
        if p is not None and p.alert_severity_floor in order and order.index(sev) < order.index(p.alert_severity_floor):
            sev = p.alert_severity_floor
        return sev

    def _finalize_track(self, cam: CameraState, st: TrackState, ts: float,
                        frame: Optional[np.ndarray] = None) -> None:
        for hand in st.hands.values():
            if hand.post_reach:
                verdict = rules.detect_concealment(
                    hand.post_reach,
                    window_sec=settings.THEFT_CONCEAL_WINDOW_SEC,
                    min_hold_frames=self._thr(cam, "conceal_min_hold_frames", settings.THEFT_CONCEAL_MIN_HOLD_FRAMES),
                    no_return_sec=settings.THEFT_CONCEAL_NO_RETURN_SEC,
                    track_ended=True,
                )
                if verdict.get("detected"):
                    self._raise(cam, st, rules.RULE_CONCEALMENT, verdict, ts, ObserveResult(),
                                snapshot=hand.post_reach_snapshot, zone_id=hand.post_reach_zone, hand=hand.name)
                self._clear_post_reach(hand)
            if hand.active_zone is not None:
                self._end_reach(cam, st, hand, hand.active_last or ts)
            if hand.closing is not None:
                self._emit_reach(cam, st, hand)

    # ------------------------------------------------------------ writer

    def _enqueue(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._pending_lock:
            self._pending.append((kind, payload))
            # Bound the queue if the database is unreachable for a long time.
            if len(self._pending) > 5000:
                dropped = len(self._pending) - 5000
                del self._pending[:dropped]
                logger.warning(f"pose_analytics queue full; dropped {dropped} oldest item(s)")
        self._ensure_writer()
        self._wake.set()

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer_stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="pose-analytics-writer", daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        interval = max(0.2, float(settings.INTERACTION_FLUSH_INTERVAL_SEC))
        while not self._writer_stop.is_set():
            self._wake.wait(interval)
            self._wake.clear()
            # Batch: give the capture threads a moment to add related items.
            time.sleep(0.05)
            try:
                self.flush_now()
            except Exception as e:
                logger.error(f"pose_analytics writer error: {e}")

    def _engine(self):
        from sqlalchemy import create_engine, event

        url = f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}"
        if self._sync_engine is None or self._sync_engine_url != url:
            eng = create_engine(url, connect_args={"timeout": 15.0, "check_same_thread": False})

            @event.listens_for(eng, "connect")
            def _pragma(dbapi_conn, _rec):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.close()

            self._sync_engine, self._sync_engine_url = eng, url
        return self._sync_engine

    def flush_now(self) -> int:
        """Write every queued interaction and incident. Returns rows written."""
        with self._flush_lock:
            with self._pending_lock:
                batch, self._pending = self._pending, []
            if not batch:
                return 0
            from sqlalchemy.orm import Session
            from app.models.db_models import CameraModel, ShelfInteractionModel, TheftIncidentModel

            written = 0
            notifications: List[Tuple[str, str, Dict[str, Any]]] = []
            try:
                with Session(self._engine()) as db:
                    cams: Dict[str, Any] = {}
                    for kind, p in batch:
                        if kind == "interaction":
                            db.add(ShelfInteractionModel(
                                id=p["id"],
                                camera_id=p["camera_id"],
                                shelf_zone_id=p["zone_id"],
                                timestamp=utc_from_ts(p["started_at"]),
                                person_track_id=p["track_id"],
                                action_type="REACH",
                                duration_sec=p["duration_sec"],
                                hand=p["hand"],
                                started_at=utc_from_ts(p["started_at"]),
                                ended_at=utc_from_ts(p["ended_at"]),
                                confidence=p["confidence"],
                                zone_name=p.get("zone_name"),
                                zone_space=p.get("zone_space"),
                                image_x=(p["image_xy"] or (None, None))[0],
                                image_y=(p["image_xy"] or (None, None))[1],
                                floor_x=(p["floor_xy"] or (None, None))[0],
                                floor_y=(p["floor_xy"] or (None, None))[1],
                                sku_id=p.get("sku_id"),
                                product_category=p.get("product_category"),
                                shelf_level=p.get("shelf_level"),
                                value_tier=p.get("value_tier"),
                                contact_point=p.get("contact_point"),
                            ))
                            written += 1
                        elif kind == "incident":
                            cid = p["camera_id"]
                            if cid not in cams:
                                cams[cid] = db.get(CameraModel, cid)
                            cam = cams[cid]
                            snapshot_path = self._write_evidence(p)
                            row = self._incident_row(p, cam, snapshot_path)
                            db.add(TheftIncidentModel(**row))
                            written += 1
                            notifications.append(self._notification(p, row))
                    db.commit()
            except Exception as e:
                self.stats["write_errors"] += 1
                logger.error(f"pose_analytics flush failed ({len(batch)} item(s)): {e}")
                # Re-queue for a few attempts so a transient lock does not
                # lose observations; a persistent failure drops them.
                retry = []
                for k, v in batch:
                    v["_attempts"] = int(v.get("_attempts", 0)) + 1
                    if v["_attempts"] < 3:
                        retry.append((k, v))
                with self._pending_lock:
                    self._pending[:0] = retry
                return 0

            self.stats["persisted"] += written
            for title, body, data in notifications:
                self._dispatch_notification(title, body, data)
            return written

    @staticmethod
    def evidence_dir() -> Path:
        d = Path(settings.THEFT_EVIDENCE_DIR) if settings.THEFT_EVIDENCE_DIR else Path(settings.STORAGE_DIR) / "theft_evidence"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_evidence(self, p: Dict[str, Any]) -> Optional[str]:
        snap = p.get("snapshot")
        if not snap or snap.get("frame") is None:
            return None
        try:
            import cv2
        except Exception:
            return None
        try:
            img = render_evidence(snap["frame"], snap.get("bbox"), snap.get("keypoints"),
                                  f"SUSPICIOUS BEHAVIOUR FOR REVIEW: {p['rule']}",
                                  f"{p['camera_id']}  track {p['track_id']}  confidence {p['confidence']:.2f}  "
                                  f"{datetime.fromtimestamp(p['ts']).strftime('%Y-%m-%d %H:%M:%S')}")
            path = self.evidence_dir() / f"{p['id']}.jpg"
            ok = cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.THEFT_EVIDENCE_JPEG_QUALITY)])
            if ok:
                from app.services.evidence_storage import note_evidence_written

                note_evidence_written(path)  # the evidence limit (oldest first) runs if now exceeded
            return str(path) if ok else None
        except Exception as e:
            logger.error(f"Could not write evidence image for {p.get('id')}: {e}")
            return None

    @staticmethod
    def _incident_row(p: Dict[str, Any], cam: Any, snapshot_path: Optional[str]) -> Dict[str, Any]:
        label = rules.RULE_LABELS.get(p["rule"], p["rule"])
        bbox = p.get("bbox") or (0, 0, 0, 0)
        summary = f"Suspicious behaviour for staff review: {label}. " + "; ".join(p["evidence"][:3])
        return dict(
            id=p["id"],
            timestamp=utc_from_ts(p["ts"]),  # naive UTC, like created_at
            camera_id=p["camera_id"],
            camera_name=getattr(cam, "name", None) or p["camera_id"],
            department=getattr(cam, "department", None) or "GENERAL",
            shelf_zone_id=p.get("zone_id") if p["rule"] != rules.RULE_EXIT_WITHOUT_CHECKOUT else None,
            zone_id=p.get("zone_id"),
            theft_type=p["rule"],
            rule=p["rule"],
            severity=p["severity"],
            confidence=p["confidence"],
            person_track_id=p["track_id"],
            evidence_summary=summary[:1000],
            evidence=p["evidence"],
            status="ACTIVE",
            snapshot_path=snapshot_path,
            evidence_snapshot_url=f"/api/v1/theft/incidents/{p['id']}/evidence" if snapshot_path else None,
            bounding_box={"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]},
            wrist_trajectory=p.get("wrist_trajectory") or [],
            items_involved=p.get("items") or [],
            estimated_loss_value=p.get("estimated_loss_value") or 0.0,
        )

    @staticmethod
    def _notification(p: Dict[str, Any], row: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
        event_type = {
            rules.RULE_CONCEALMENT: "CONCEALMENT",
            rules.RULE_SHELF_SWEEPING: "SHELF_SWEEP",
            rules.RULE_EXIT_WITHOUT_CHECKOUT: "EXIT_WITHOUT_CHECKOUT",
            rules.RULE_SUSPICIOUS_LOITERING: "LOITERING",
        }.get(p["rule"], "THEFT_SUSPECTED")
        conf = p["confidence"]
        title = f"Review: {rules.RULE_LABELS.get(p['rule'], p['rule'])} - {row['camera_name']}"
        body = "Suspicious behaviour for staff review. " + "; ".join(p["evidence"][:2])
        data = {
            "camera_id": p["camera_id"],
            "event_type": event_type,
            "severity": "HIGH" if conf >= 0.75 else ("WARNING" if conf >= 0.5 else "INFO"),
            "confidence": conf,
            "incident_id": p["id"],
            "rule": p["rule"],
            "evidence_snapshot_url": row.get("evidence_snapshot_url") or "",
            "snapshot_url": row.get("evidence_snapshot_url") or "",
            "zone_name": p.get("zone_name") or "",
        }
        return title, body, data

    def _dispatch_notification(self, title: str, body: str, data: Dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning(f"Loss-prevention notification for {data.get('incident_id')} not sent: "
                           "no application event loop bound (call pose_analytics.start()).")
            return
        try:
            from app.services.notification_service import notification_service
            notify = getattr(notification_service, "notify_loss_prevention")
        except Exception as e:
            logger.error(f"notify_loss_prevention unavailable: {e}")
            return

        async def _send():
            try:
                await asyncio.wait_for(notify(title, body, data), timeout=15.0)
            except Exception as e:
                logger.error(f"notify_loss_prevention failed for {data.get('incident_id')}: {e}")

        try:
            asyncio.run_coroutine_threadsafe(_send(), loop)
        except Exception as e:
            logger.error(f"Could not schedule loss-prevention notification: {e}")


def render_evidence(frame: np.ndarray, bbox: Any, keypoints: Optional[np.ndarray],
                    title: str, subtitle: str) -> np.ndarray:
    """Full frame with skeleton and box drawn, the person crop beside it, and a caption band."""
    import cv2

    full = frame.copy()
    h, w = full.shape[:2]
    scale = max(1, int(round(max(w, h) / 640)))
    if bbox is not None:
        x1, y1, x2, y2 = (int(round(v)) for v in bbox)
        cv2.rectangle(full, (x1, y1), (x2, y2), (0, 165, 255), 2 * scale)
    if keypoints is not None:
        kp = np.asarray(keypoints)
        vis_thr = settings.INTERACTION_MIN_KEYPOINT_VIS * 0.5
        for a, b in SKELETON_EDGES:
            if kp[a][2] >= vis_thr and kp[b][2] >= vis_thr:
                cv2.line(full, (int(kp[a][0]), int(kp[a][1])), (int(kp[b][0]), int(kp[b][1])), (0, 255, 0), 2 * scale)
        for i, (x, y, v) in enumerate(kp):
            if v >= vis_thr:
                color = (0, 0, 255) if i in (L_WRIST, R_WRIST) else (255, 255, 0)
                cv2.circle(full, (int(x), int(y)), 3 * scale + (2 if i in (L_WRIST, R_WRIST) else 0), color, -1)

    # Person crop from the annotated frame, padded, scaled to the frame height.
    crop = None
    if bbox is not None:
        x1, y1, x2, y2 = (float(v) for v in bbox)
        pad_x, pad_y = 0.15 * (x2 - x1), 0.08 * (y2 - y1)
        cx1, cy1 = max(0, int(x1 - pad_x)), max(0, int(y1 - pad_y))
        cx2, cy2 = min(w, int(x2 + pad_x)), min(h, int(y2 + pad_y))
        if cx2 - cx1 > 4 and cy2 - cy1 > 4:
            crop = full[cy1:cy2, cx1:cx2]
            ratio = h / crop.shape[0]
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * ratio)), h))
    panel = full if crop is None else np.hstack([full, np.full((h, 6, 3), 40, np.uint8), crop])

    # Cap the output width to keep evidence files small.
    max_w = 1920
    if panel.shape[1] > max_w:
        r = max_w / panel.shape[1]
        panel = cv2.resize(panel, (max_w, int(panel.shape[0] * r)))
    band_h = 56
    band = np.full((band_h, panel.shape[1], 3), 24, np.uint8)
    cv2.putText(band, title, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2, cv2.LINE_AA)
    cv2.putText(band, subtitle, (10, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1, cv2.LINE_AA)
    return np.vstack([band, panel])


pose_analytics = PoseAnalytics()
