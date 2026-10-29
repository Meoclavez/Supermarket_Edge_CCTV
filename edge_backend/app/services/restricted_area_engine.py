"""Restricted areas, evaluated live on every analysed frame (high security).

Called by each camera worker right after the tracker (``live_analytics_engine
CameraWorker._analyse``) with *all* live tracks of that camera, not only the
"human" ones, because inside an armed area the static-figure filter may not hide
a real person (see below). Plain arithmetic on bounded per-track state; I/O
(evidence still, alert dispatch, clip, audit) is handed to a writer thread that
is woken at once, so the camera loop never blocks.

Positions
---------
* **Calibrated** (the camera has a homography): the foot point in floor
  metres. Floor-zone areas are tested there; speed is m/s, distances metres.
* **Uncalibrated**: the foot point in image pixels; distances and speeds are
  divided by the person's box height ("body heights"), which is clearly less
  accurate (perspective) and labelled so.
* A camera *linked* to the area's store-map zone without calibration counts its
  whole view as inside the area.

Triggers (services/restricted_areas.TRIGGERS) while the area is **armed**:

* ``unannounced_entry`` -- inside for ``min_presence_sec`` (brief exits under
  ``EXIT_GRACE_SEC`` keep the clock) on at least 2 observations, nobody
  announced. A person who was inside during an announcement is covered until
  its grace ends; still inside after it is ``announced_overstay``.
* ``dwell`` / ``speed`` / ``pacing`` / ``erratic`` / ``crouch`` / ``reach_up`` /
  ``poi_approach`` / ``boundary_linger`` / ``people_count`` / ``wrong_door`` /
  ``tamper``: see the pure functions below.

Each alert carries the trigger's tier (``alert_tier``) and goes through
``alert_dispatcher.dispatch`` (tier routing of theft alerts, phone roster,
cooldown bypass, quiet-hours bypass) as event type ``RESTRICTED_AREA``. Every
trigger fires at most once per (area, track) and then waits its own cooldown
per area; tamper per (area, camera).

Static figures. Outside guarded time (disarmed, or someone announced) a track
the tracker flags static inside the area is *learnt* as a still figure of that
area (bbox + pose, ``STORAGE_DIR/restricted_areas/still_figures.json``). While
guarded, a static or pending-static track is suppressed only when it matches a
figure learnt that way; any other still "person" counts. The tracker's own
static memory is kept untouched.

Latency: the alert is queued on the worker thread the moment the trigger is
satisfied and the writer dispatches it at once; ``stats["latency_ms"]`` keeps
the time from the triggering frame to the end of the dispatch (log, websocket
broadcast, phone push).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import numpy as np

from app.config import settings

logger = logging.getLogger("edge.restricted_area_engine")

EXIT_GRACE_SEC = 1.0
MIN_OBSERVATIONS = 2
HISTORY_SEC = 130.0
STATE_TTL_SEC = 20.0
OCCUPANCY_MAX_AGE = 3.0
PEOPLE_COUNT_HOLD_SEC = 1.0
HEALTH_EVERY_SEC = 1.0
COVERED_STD = 6.0
REFERENCE_MIN_STD = 12.0
MOVED_ZNCC = 0.35
STILL_FIGURE_TTL_SEC = 30 * 24 * 3600
MAX_CLIPS = 2

# COCO keypoints
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SH, R_SH, L_EL, R_EL, L_WR, R_WR = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP, L_KNEE, R_KNEE = 11, 12, 13, 14
KP_VIS = 0.3


# =========================================================================
# Pure functions (unit-tested with synthetic tracks)
# =========================================================================

def net_speed(samples: List[Tuple[float, float, float]], window: float,
              now: float) -> Optional[float]:
    """Sustained speed over the last ``window`` seconds (units of the samples per second).

    The smaller of the net displacement / time and the median step speed.
    ``samples`` are (t, x, y) in metres (or body heights). None when they span
    less than 80 % of the window or there are fewer than 3.
    """
    pts = [s for s in samples if s[0] >= now - window - 1e-6]
    if len(pts) < 3:
        return None
    dt = pts[-1][0] - pts[0][0]
    if dt < 0.8 * window or dt <= 0:
        return None
    net = math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2]) / dt
    # Sustained, not one jump: the median step speed must agree (a track that
    # jumps to another person or a projection glitch is one fast step).
    steps = sorted(math.hypot(b[1] - a[1], b[2] - a[2]) / max(1e-3, b[0] - a[0]) for a, b in zip(pts, pts[1:]))
    return min(net, steps[len(steps) // 2])


def count_reversals(points: List[Tuple[float, float]], min_leg: float) -> int:
    """Back-and-forth reversals along the path's main direction (hysteresis ``min_leg``)."""
    if len(points) < 3:
        return 0
    a = np.asarray(points, dtype=np.float64)
    c = a - a.mean(axis=0)
    try:
        _u, _s, vt = np.linalg.svd(c, full_matrices=False)
    except np.linalg.LinAlgError:
        return 0
    s = c @ vt[0]
    direction, extreme, n = 0, s[0], 0
    for v in s[1:]:
        if direction == 0:
            if abs(v - extreme) >= min_leg:
                direction = 1 if v > extreme else -1
                extreme = v
        elif direction > 0:
            if v > extreme:
                extreme = v
            elif extreme - v >= min_leg:
                n, direction, extreme = n + 1, -1, v
        else:
            if v < extreme:
                extreme = v
            elif v - extreme >= min_leg:
                n, direction, extreme = n + 1, 1, v
    return n


def zigzag_turns(points: List[Tuple[float, float]], step: float, turn_deg: float) -> int:
    """Sharp turns of alternating sign along the path resampled every ``step``."""
    if len(points) < 3 or step <= 0:
        return 0
    kept = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - kept[-1][0], p[1] - kept[-1][1]) >= step:
            kept.append(p)
    if len(kept) < 3:
        return 0
    heads = [math.atan2(b[1] - a[1], b[0] - a[0]) for a, b in zip(kept, kept[1:])]
    n, last_sign = 0, 0
    for h1, h2 in zip(heads, heads[1:]):
        d = math.degrees((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        if abs(d) >= turn_deg:
            sign = 1 if d > 0 else -1
            if sign != last_sign:
                n += 1
                last_sign = sign
    return n


def _vis(kp, i) -> bool:
    return kp is not None and len(kp) > i and float(kp[i][2]) >= KP_VIS


def _mid_y(kp, a, b) -> Optional[float]:
    ys = [float(kp[i][1]) for i in (a, b) if _vis(kp, i)]
    return sum(ys) / len(ys) if ys else None


def low_posture(kp) -> Optional[bool]:
    """Crouching / kneeling / bending low: hips close to knee height.

    The hip-to-knee vertical drop is below 40 % of the shoulder-to-hip length.
    None when shoulders, hips or knees are not visible.
    """
    if kp is None:
        return None
    sh, hip, knee = _mid_y(kp, L_SH, R_SH), _mid_y(kp, L_HIP, R_HIP), _mid_y(kp, L_KNEE, R_KNEE)
    if sh is None or hip is None or knee is None:
        return None
    torso = hip - sh
    if torso <= 1.0:
        return None
    return (knee - hip) < 0.40 * torso


def reaching_up(kp) -> Optional[bool]:
    """A wrist above the top of the head with its elbow above the shoulder."""
    if kp is None:
        return None
    heads = [float(kp[i][1]) for i in (NOSE, L_EYE, R_EYE, L_EAR, R_EAR) if _vis(kp, i)]
    sh = _mid_y(kp, L_SH, R_SH)
    hip = _mid_y(kp, L_HIP, R_HIP)
    if not heads or sh is None:
        return None
    torso = (hip - sh) if hip is not None and hip > sh else max(1.0, (sh - min(heads)) * 1.6)
    top = min(heads)
    known = False
    for wr, el, s in ((L_WR, L_EL, L_SH), (R_WR, R_EL, R_SH)):
        if _vis(kp, wr) and _vis(kp, el) and _vis(kp, s):
            known = True
            if float(kp[wr][1]) < top - 0.15 * torso and float(kp[el][1]) < float(kp[s][1]):
                return True
    return False if known else None


def segment_distance(px: float, py: float, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def edge_distance(px: float, py: float, poly) -> float:
    n = len(poly)
    return min(segment_distance(px, py, poly[i], poly[(i + 1) % n]) for i in range(n)) if n >= 2 else float("inf")


def segments_cross(p1, p2, a, b) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = orient(a, b, p1), orient(a, b, p2)
    d3, d4 = orient(p1, p2, a), orient(p1, p2, b)
    if d1 == 0 and d2 == 0:
        return False          # along the line, not across it
    # Touching the line counts (a foot point exactly on it is half a crossing).
    return (d1 * d2 <= 0) and (d3 * d4 <= 0)


def zncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    a -= a.mean()
    b -= b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 1e-6 else 0.0


def _iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


# =========================================================================
# State
# =========================================================================

@dataclass
class TrackHistory:
    # (t, image x, image y, box height, floor x or None, floor y or None)
    samples: Deque[tuple] = field(default_factory=lambda: deque(maxlen=900))
    seen_at: float = 0.0


@dataclass
class AreaTrack:
    first_seen: float = 0.0
    seen_outside: bool = False
    inside_since: Optional[float] = None
    last_inside: Optional[float] = None
    inside_obs: int = 0
    first_inside_ever: Optional[float] = None
    alerted: set = field(default_factory=set)
    crouch_since: Optional[float] = None
    reach_since: Optional[float] = None
    poi_since: Dict[str, float] = field(default_factory=dict)
    edge_since: Optional[float] = None
    entry_checked: bool = False
    seen_at: float = 0.0


@dataclass
class TamperState:
    ref: Optional[np.ndarray] = None
    ref_std: float = 0.0
    warm: int = 0
    covered_since: Optional[float] = None
    moved_since: Optional[float] = None
    last_check: float = 0.0
    last_frame: Optional[float] = None
    offline_alerted: bool = False


@dataclass
class AreaRuntime:
    last_alert: Dict[str, float] = field(default_factory=dict)
    occupancy: Dict[str, Tuple[int, float]] = field(default_factory=dict)
    over_since: Optional[float] = None
    last_events: Deque[dict] = field(default_factory=lambda: deque(maxlen=10))


@dataclass
class PendingAlert:
    alert_id: str
    area_id: str
    area_name: str
    trigger: str
    tier: str
    camera_id: str
    title: str
    body: str
    data: dict
    detected_at: float
    frame: Optional[np.ndarray] = None
    bbox: Optional[tuple] = None
    keypoints: Any = None
    caption: str = ""
    clip: bool = True


# =========================================================================
# Engine
# =========================================================================

class RestrictedAreaEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hist: Dict[Tuple[str, str], TrackHistory] = {}
        self._area_tracks: Dict[Tuple[str, str, str], AreaTrack] = {}
        self._areas: Dict[str, AreaRuntime] = {}
        self._tamper: Dict[Tuple[str, str], TamperState] = {}
        self._frames: Dict[str, float] = {}
        self._figures: Dict[str, List[dict]] = {}
        self._figures_loaded = False
        self._figures_dirty = 0.0
        self._alerts: List[PendingAlert] = []
        self._alerts_lock = threading.Lock()
        self._health_gate: Dict[str, float] = {}
        self._wake = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clips = threading.Semaphore(MAX_CLIPS)
        self._secured_cache: Dict[str, Tuple[float, bool]] = {}
        self._started_at = time.time()
        self._last_prune = 0.0
        self.clock: Callable[[], float] = time.time
        # Tests: deliver on the calling thread with process_pending_alerts().
        self.synchronous = False
        self.dispatcher_override = None
        self.clip_recorder_override = None
        self.record_clips = True
        self.stats: Dict[str, Any] = {"alerts": 0, "delivered": 0, "errors": 0, "suppressed_static": 0,
                                      "boosted": 0, "latency_ms": None, "latency_max_ms": None,
                                      "clips": 0, "evaluations": 0}
        self.last_delivery: Optional[dict] = None

    # ------------------------------------------------------------ plumbing
    @staticmethod
    def _service():
        from app.services.restricted_areas import restricted_areas

        return restricted_areas

    def _snapshot(self):
        return self._service().snapshot

    @staticmethod
    def _plan_on() -> bool:
        from app.services.entitlements import feature_safe

        return feature_safe("restricted_areas")

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    def reset(self) -> None:
        with self._lock:
            self._hist.clear()
            self._area_tracks.clear()
            self._areas.clear()
            self._tamper.clear()
            self._frames.clear()
            self._secured_cache.clear()
        with self._alerts_lock:
            self._alerts.clear()
        self.stats.update(alerts=0, delivered=0, errors=0, suppressed_static=0, boosted=0, latency_ms=None,
                          latency_max_ms=None, clips=0, evaluations=0)

    def _rt(self, area_id: str) -> AreaRuntime:
        rt = self._areas.get(area_id)
        if rt is None:
            rt = self._areas[area_id] = AreaRuntime()
        return rt

    # ------------------------------------------------------------ questions from the pipeline
    def handles_intrusion(self, intrusion_zone_id: str) -> bool:
        """True when a zone-store restricted area is evaluated here (tripwire_engine skips it)."""
        if not self._plan_on():
            return False
        return intrusion_zone_id in self._snapshot().linked_intrusion_ids

    def camera_secured(self, camera_id: str, now: Optional[float] = None) -> bool:
        """Does this camera see an armed restricted area right now? (cached 0.5 s)"""
        now = self.clock() if now is None else now
        hit = self._secured_cache.get(camera_id)
        if hit is not None and abs(now - hit[0]) < 0.5:
            return hit[1]
        on = False
        if self._plan_on():
            for a in self._snapshot().by_camera.get(camera_id, []):
                if a.armed_state(now)[0]:
                    on = True
                    break
        self._secured_cache[camera_id] = (now, on)
        return on

    def covers(self, camera_id: str) -> bool:
        """Does any switched-on area use this camera (armed or not: occupancy, still-figure learning)?"""
        return self._plan_on() and bool(self._snapshot().by_camera.get(camera_id))

    def wants_clip_ring(self, camera_id: str) -> bool:
        return self.record_clips and self.camera_secured(camera_id)

    # ------------------------------------------------------------ membership
    @staticmethod
    def _membership(area, camera_id: str, tr, fw: int, fh: int):
        """(inside, mode, position, edge distance, scale) for one track, or None if not covered.

        mode: "region" (image, body heights), "floor" (metres) or "view" (linked camera).
        position is in the units of ``mode`` for kinematics.
        """
        x1, y1, x2, y2 = (float(v) for v in tr.bbox)
        bh = max(1.0, y2 - y1)
        fx, fy = (x1 + x2) / 2.0, y2
        regions = area.regions.get(camera_id)
        if regions:
            from app.services.tripwire_engine import point_in_polygon

            nx, ny = fx / fw, fy / fh
            inside = any(point_in_polygon(nx, ny, poly) for poly in regions)
            dmin = min(edge_distance(fx, fy, [(px * fw, py * fh) for px, py in poly]) for poly in regions)
            return inside, "region", (fx / bh, fy / bh), dmin / bh, bh
        if area.floor_polygon is not None and camera_id in area.floor_cameras:
            floor = getattr(tr, "floor_xy", None)
            if floor is None:
                try:
                    from app.services.tracking_service import floor_projector

                    floor = floor_projector.to_floor(camera_id, fx, fy, frame_size=(fw, fh))
                except Exception:  # noqa: BLE001
                    floor = None
            if floor is None:
                return None
            from app.services.tripwire_engine import point_in_polygon

            inside = point_in_polygon(floor[0], floor[1], area.floor_polygon)
            return inside, "floor", (float(floor[0]), float(floor[1])), edge_distance(floor[0], floor[1], area.floor_polygon), bh
        if camera_id in area.linked_cameras:
            return True, "view", (fx / bh, fy / bh), None, bh
        return None

    # ------------------------------------------------------------ still figures
    def _figures_path(self) -> Path:
        return Path(settings.STORAGE_DIR) / "restricted_areas" / "still_figures.json"

    def _load_figures(self) -> None:
        if self._figures_loaded:
            return
        self._figures_loaded = True
        try:
            raw = json.loads(self._figures_path().read_text())
            now = time.time()
            for cam, items in (raw or {}).items():
                self._figures[cam] = [i for i in items if now - float(i.get("last_seen", 0)) <= STILL_FIGURE_TTL_SEC]
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Restricted area still figures not read (%s); starting empty", exc)

    def _figures_to_save(self, force: bool = False) -> Optional[str]:
        """Under the lock: the JSON to write, or None when nothing is due."""
        now = time.time()
        if not force and (not self._figures_dirty or now - self._figures_dirty < 10):
            return None
        self._figures_dirty = 0.0
        try:
            return json.dumps(self._figures)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Restricted area still figures not saved: %s", exc)
            return None

    def _write_figures(self, data: Optional[str]) -> None:
        """Outside the lock: the file write (camera threads wait on the lock)."""
        if data is None:
            return
        try:
            p = self._figures_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(data)
            tmp.replace(p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Restricted area still figures not saved: %s", exc)

    def _save_figures(self, force: bool = False) -> None:
        self._write_figures(self._figures_to_save(force))

    @staticmethod
    def _kp_list(kp):
        if kp is None:
            return None
        try:
            return [[round(float(a), 1), round(float(b), 1), round(float(c), 2)] for a, b, c in np.asarray(kp)[:17]]
        except Exception:  # noqa: BLE001
            return None

    def _figure_match(self, camera_id: str, area_id: str, tr) -> Optional[dict]:
        from app.services.tracking_service import body_scale, poses_match

        box = tuple(float(v) for v in tr.bbox)
        kp = getattr(tr, "keypoints", None)
        kp_arr = None if kp is None else np.asarray(kp, dtype=np.float32)
        for f in self._figures.get(camera_id, []):
            if f.get("area_id") != area_id:
                continue
            if _iou(box, tuple(f["bbox"])) < float(settings.STATIC_MEMORY_IOU):
                continue
            fk = None if f.get("kp") is None else np.asarray(f["kp"], dtype=np.float32)
            if poses_match(kp_arr, fk, max(body_scale(kp_arr, box), 1.0)) is False:
                continue
            return f
        return None

    def _learn_figure(self, camera_id: str, area_id: str, tr, now: float) -> None:
        f = self._figure_match(camera_id, area_id, tr)
        if f is not None:
            f["last_seen"] = now
            f["bbox"] = [round(float(v), 1) for v in tr.bbox]
        else:
            self._figures.setdefault(camera_id, []).append({
                "area_id": area_id, "bbox": [round(float(v), 1) for v in tr.bbox],
                "kp": self._kp_list(getattr(tr, "keypoints", None)), "first_seen": now, "last_seen": now})
            del self._figures[camera_id][:-50]
        self._figures_dirty = self._figures_dirty or time.time()

    def forget_figures(self, area_id: Optional[str] = None) -> int:
        with self._lock:
            self._load_figures()
            n = 0
            for cam in list(self._figures):
                keep = [f for f in self._figures[cam] if area_id is not None and f.get("area_id") != area_id]
                n += len(self._figures[cam]) - len(keep)
                self._figures[cam] = keep
            self._figures_dirty = time.time()
            self._save_figures(force=True)
            return n

    def figure_count(self, area_id: str) -> int:
        with self._lock:
            self._load_figures()
            return sum(1 for items in self._figures.values() for f in items if f.get("area_id") == area_id)

    # ------------------------------------------------------------ detection sensitivity
    def boost_detections(self, camera_id: str, detections: list, fw: int, fh: int,
                         now: Optional[float] = None) -> int:
        """Lower the person threshold inside armed areas by the area's ``confidence_delta``.

        A detection whose foot point is inside an armed area and whose
        confidence is within ``confidence_delta`` below the threshold a new
        track needs is raised to that threshold, so it is tracked. Called after
        AI_IGNORE areas removed what privacy hides. Returns how many were raised.
        """
        if not detections or not self.camera_secured(camera_id, now):
            return 0
        now = self.clock() if now is None else now
        from app.services.inference_backend import person_threshold

        base = max(float(settings.PERSON_CONF_THRESHOLD), float(settings.TRACK_NEW_TRACK_THRESHOLD))
        n = 0
        areas = [a for a in self._snapshot().by_camera.get(camera_id, []) if a.armed_state(now)[0]]
        for d in detections:
            thr = person_threshold(d, base)
            for a in areas:
                delta = float((a.security or {}).get("confidence_delta", 0.05) or 0.0)
                if delta <= 0 or not (thr - delta <= float(d.confidence) < thr):
                    continue
                view = SimpleNamespace(bbox=(d.x1, d.y1, d.x2, d.y2), floor_xy=None)
                m = self._membership(a, camera_id, view, fw, fh)
                if m is not None and m[0]:
                    d.confidence = thr + 1e-6
                    n += 1
                    break
        self.stats["boosted"] += n
        return n

    # ------------------------------------------------------------ evaluation
    def evaluate(self, camera_id: str, tracks: Iterable[Any], fw: int, fh: int, now: float,
                 frame=None, camera_name: Optional[str] = None) -> dict:
        """Evaluate this camera's live tracks against the restricted areas it sees.

        ``frame`` must already carry the privacy masks (evidence only).
        Returns ``{"alerts": [alert ids], "occupancy": {area_id: n}}``.
        """
        out: dict = {"alerts": [], "occupancy": {}}
        if not fw or not fh or not self._plan_on():
            return out
        areas = self._snapshot().by_camera.get(camera_id) or []
        if not areas:
            return out
        tracks = [t for t in tracks if int(getattr(t, "hits", 3)) >= 2]
        with self._lock:
            self._load_figures()
            self.stats["evaluations"] += 1
            for tr in tracks:
                tid = str(tr.track_id)
                h = self._hist.get((camera_id, tid))
                if h is None:
                    h = self._hist[(camera_id, tid)] = TrackHistory()
                if getattr(tr, "misses", 0) == 0 and (not h.samples or h.samples[-1][0] < now):
                    x1, y1, x2, y2 = (float(v) for v in tr.bbox)
                    fl = getattr(tr, "floor_xy", None)
                    h.samples.append((now, (x1 + x2) / 2.0, y2, max(1.0, y2 - y1),
                                      None if fl is None else float(fl[0]), None if fl is None else float(fl[1])))
                    while h.samples and h.samples[0][0] < now - HISTORY_SEC:
                        h.samples.popleft()
                h.seen_at = now
            for area in areas:
                self._eval_area(area, camera_id, camera_name, tracks, fw, fh, now, frame, out)
            self._prune(now)
            figures = self._figures_to_save()
        self._write_figures(figures)
        return out

    def _prune(self, now: float) -> None:
        if abs(now - self._last_prune) < 5.0 and len(self._hist) <= 256:
            return
        self._last_prune = now
        cutoff = now - STATE_TTL_SEC
        self._hist = {k: v for k, v in self._hist.items() if v.seen_at >= cutoff}
        self._area_tracks = {k: v for k, v in self._area_tracks.items() if v.seen_at >= cutoff}

    def _eval_area(self, area, cam, cam_name, tracks, fw, fh, now, frame, out) -> None:
        rt = self._rt(area.id)
        armed, source = area.armed_state(now)
        active = area.active_announcements(now)
        announced = bool(active)
        trig = area.triggers
        grace_min = float(trig["announced_overstay"].get("grace_min", 2.0))
        inside_tracks = []
        for tr in tracks:
            tid = str(tr.track_id)
            m = self._membership(area, cam, tr, fw, fh)
            if m is None:
                continue
            inside, mode, pos, edge, bh = m
            key = (area.id, cam, tid)
            st = self._area_tracks.get(key)
            if st is None:
                st = self._area_tracks[key] = AreaTrack(first_seen=now)
            st.seen_at = now
            static = bool(getattr(tr, "is_static", False) or getattr(tr, "is_pending_static", False))
            if static:
                guarded = armed and not announced
                if not guarded:
                    if inside and getattr(tr, "is_static", False):
                        self._learn_figure(cam, area.id, tr, now)
                    continue
                if self._figure_match(cam, area.id, tr) is not None:
                    self.stats["suppressed_static"] += 1
                    continue
            # Inside / outside bookkeeping (brief exits keep the clock).
            if inside:
                if st.inside_since is None or (st.last_inside is not None and now - st.last_inside > EXIT_GRACE_SEC):
                    st.inside_since = now
                    st.inside_obs = 0
                    st.entry_checked = False
                st.last_inside = now
                st.inside_obs += 1
                if st.first_inside_ever is None:
                    st.first_inside_ever = now
                inside_tracks.append((tr, st, mode, pos, bh))
            else:
                st.seen_outside = True
                if st.last_inside is not None and now - st.last_inside > EXIT_GRACE_SEC:
                    st.inside_since = None
                    st.inside_obs = 0
                    st.crouch_since = st.reach_since = None
                    st.poi_since.clear()
                if armed:
                    self._edge(area, rt, cam, cam_name, tr, st, mode, edge, now, frame, announced, out)
                else:
                    st.edge_since = None
        rt.occupancy[cam] = (len(inside_tracks), now)
        out["occupancy"][area.id] = len(inside_tracks)
        if not armed:
            rt.over_since = None
            for tr, st, *_ in inside_tracks:
                st.crouch_since = st.reach_since = None
                st.poi_since.clear()
            return
        for tr, st, mode, pos, bh in inside_tracks:
            self._inside_triggers(area, rt, cam, cam_name, tr, st, mode, pos, bh, now, frame, announced,
                                  source, grace_min, fw, fh, out)
        self._people_count(area, rt, cam, cam_name, inside_tracks, now, frame, announced, out)

    # ------------------------------------------------------------ triggers
    def _allowed(self, area, tid: str, announced: bool) -> Optional[dict]:
        cfg = area.triggers.get(tid) or {}
        if not cfg.get("enabled"):
            return None
        if announced and not cfg.get("while_announced"):
            return None
        return cfg

    def _cooled(self, rt: AreaRuntime, key: str, cfg: dict, now: float) -> bool:
        last = rt.last_alert.get(key)
        return last is None or now - last >= float(cfg.get("cooldown_sec", 60))

    def _covering_announcement(self, area, st: AreaTrack, now: float, grace_min: float):
        """('covered' | 'overstay' | None, announcement) for a track inside an armed area."""
        first = st.first_inside_ever if st.first_inside_ever is not None else st.inside_since
        best = None
        for a in area.announcements:
            if a.active(now):
                continue
            if first is not None and a.started_at - 60.0 <= first < a.end and now - a.end < 3600:
                if best is None or a.end > best.end:
                    best = a
        if best is None:
            return None, None
        return ("covered" if now < best.grace_until(grace_min) else "overstay"), best

    def _inside_triggers(self, area, rt, cam, cam_name, tr, st, mode, pos, bh, now, frame, announced,
                         source, grace_min, fw, fh, out) -> None:
        tid = str(tr.track_id)
        trig = area.triggers
        calibrated = mode == "floor"
        hist = self._hist.get((cam, tid))
        samples = list(hist.samples) if hist else []
        since = st.inside_since or now
        cover, ann = (None, None) if announced else self._covering_announcement(area, st, now, grace_min)
        guarded_here = not announced and cover != "covered"

        # Entry without notice / stayed longer than announced.
        if guarded_here and st.inside_obs >= MIN_OBSERVATIONS:
            if cover == "overstay" and trig["announced_overstay"].get("enabled"):
                cfg = trig["announced_overstay"]
                if "announced_overstay" not in st.alerted and self._cooled(rt, "announced_overstay", cfg, now):
                    self._fire(area, rt, "announced_overstay", cfg, cam, cam_name, tr, st, now, frame, out,
                               f"Stayed longer than announced: {area.name}",
                               f"{ann.user_name} announced entry to {area.name} until "
                               f"{_hhmm(ann.until)}; someone is still inside.",
                               {"announced_by": ann.user_name, "announced_until": _iso(ann.until)})
                st.alerted.add("unannounced_entry")
            else:
                cfg = trig["unannounced_entry"]
                if cfg.get("enabled") and "unannounced_entry" not in st.alerted \
                        and now - since >= float(cfg.get("min_presence_sec", 0.5)) \
                        and self._cooled(rt, "unannounced_entry", cfg, now):
                    why = {"manual": "armed by hand", "always": "always armed", "schedule": "armed by its schedule"}[source]
                    self._fire(area, rt, "unannounced_entry", cfg, cam, cam_name, tr, st, now, frame, out,
                               f"Entry without notice: {area.name}",
                               f"Someone is inside {area.name} ({why}) and nobody announced entry.",
                               {"armed_by": source, "inside_sec": round(now - since, 2)})

        # Entry not through the door (checked once per entry, when known).
        cfg = self._allowed(area, "wrong_door", announced)
        if cfg and not st.entry_checked and st.seen_outside and area.door_lines.get(cam):
            st.entry_checked = True
            window = float(cfg.get("window_sec", 4.0))
            pts = [(s[1] / fw, s[2] / fh) for s in samples if s[0] >= since - window]
            crossed = any(segments_cross(p, q, a, b) for p, q in zip(pts, pts[1:])
                          for a, b in area.door_lines[cam].values())
            if not crossed and "wrong_door" not in st.alerted and self._cooled(rt, "wrong_door", cfg, now):
                self._fire(area, rt, "wrong_door", cfg, cam, cam_name, tr, st, now, frame, out,
                           f"Entry not through the door: {area.name}",
                           f"Someone came into {area.name} without crossing a door line.", {})

        cfg = self._allowed(area, "dwell", announced)
        if cfg and now - since > float(cfg.get("max_seconds", 60)) and "dwell" not in st.alerted \
                and self._cooled(rt, "dwell", cfg, now):
            self._fire(area, rt, "dwell", cfg, cam, cam_name, tr, st, now, frame, out,
                       f"Inside too long: {area.name}",
                       f"Someone has been inside {area.name} for {now - since:.0f} s.",
                       {"inside_sec": round(now - since, 1)})

        # Kinematics on samples while inside.
        if calibrated:
            path = [(s[0], s[4], s[5]) for s in samples if s[0] >= since - 0.5 and s[4] is not None]
        else:
            path = [(s[0], s[1] / s[3], s[2] / s[3]) for s in samples if s[0] >= since - 0.5]
        cfg = self._allowed(area, "speed", announced)
        if cfg and "speed" not in st.alerted:
            v = net_speed(path, float(cfg.get("min_duration_sec", 0.6)), now)
            limit = float(cfg.get("max_mps" if calibrated else "max_bh_per_sec", 2.5))
            if v is not None and v > limit and self._cooled(rt, "speed", cfg, now):
                unit = "m/s" if calibrated else "body heights/s (estimate, camera not calibrated)"
                self._fire(area, rt, "speed", cfg, cam, cam_name, tr, st, now, frame, out,
                           f"Moving fast: {area.name}",
                           f"Someone is moving at about {v:.1f} {unit} inside {area.name}.",
                           {"speed": round(v, 2), "speed_unit": "m/s" if calibrated else "bh/s",
                            "calibrated": calibrated})
        cfg = self._allowed(area, "pacing", announced)
        if cfg and "pacing" not in st.alerted:
            w = float(cfg.get("window_sec", 30))
            leg = float(cfg.get("min_leg_m" if calibrated else "min_leg_bh", 0.8))
            pts = [(p[1], p[2]) for p in path if p[0] >= now - w]
            n = count_reversals(pts, leg)
            if n >= int(cfg.get("reversals", 3)) and self._cooled(rt, "pacing", cfg, now):
                self._fire(area, rt, "pacing", cfg, cam, cam_name, tr, st, now, frame, out,
                           f"Pacing back and forth: {area.name}",
                           f"Someone reversed direction {n} times within {w:.0f} s inside {area.name}.",
                           {"reversals": n, "calibrated": calibrated})
        cfg = self._allowed(area, "erratic", announced)
        if cfg and "erratic" not in st.alerted:
            w = float(cfg.get("window_sec", 12))
            pts = [(p[1], p[2]) for p in path if p[0] >= now - w]
            n = zigzag_turns(pts, 0.4 if calibrated else 0.3, float(cfg.get("turn_deg", 60)))
            if n >= int(cfg.get("turns", 4)) and self._cooled(rt, "erratic", cfg, now):
                self._fire(area, rt, "erratic", cfg, cam, cam_name, tr, st, now, frame, out,
                           f"Zig-zag movement: {area.name}",
                           f"Someone made {n} sharp alternating turns within {w:.0f} s inside {area.name}.",
                           {"turns": n, "calibrated": calibrated})

        kp = getattr(tr, "keypoints", None) if getattr(tr, "keypoints_fresh", True) else None
        cfg = self._allowed(area, "crouch", announced)
        if cfg:
            low = low_posture(kp)
            if low:
                st.crouch_since = st.crouch_since or now
                if now - st.crouch_since >= float(cfg.get("min_seconds", 2.0)) and "crouch" not in st.alerted \
                        and self._cooled(rt, "crouch", cfg, now):
                    self._fire(area, rt, "crouch", cfg, cam, cam_name, tr, st, now, frame, out,
                               f"Crouching: {area.name}",
                               f"Someone is crouching or kneeling inside {area.name}.",
                               {"seconds": round(now - st.crouch_since, 1)})
            elif low is False:
                st.crouch_since = None
        cfg = self._allowed(area, "reach_up", announced)
        if cfg:
            up = reaching_up(kp)
            if up:
                st.reach_since = st.reach_since or now
                if now - st.reach_since >= float(cfg.get("min_seconds", 1.0)) and "reach_up" not in st.alerted \
                        and self._cooled(rt, "reach_up", cfg, now):
                    self._fire(area, rt, "reach_up", cfg, cam, cam_name, tr, st, now, frame, out,
                               f"Reaching up high: {area.name}",
                               f"Someone is reaching above their head inside {area.name}.",
                               {"seconds": round(now - st.reach_since, 1)})
            elif up is False:
                st.reach_since = None
        cfg = self._allowed(area, "poi_approach", announced)
        if cfg and area.pois:
            x1, y1, x2, y2 = (float(v) for v in tr.bbox)
            for poi in area.pois:
                d = None
                if poi.get("camera_id"):
                    if poi["camera_id"] == cam:
                        fx, fy = (x1 + x2) / 2.0, y2
                        d = math.hypot(fx - poi["u"] * fw, fy - poi["v"] * fh) / bh
                        limit = float(cfg.get("distance_bh", 0.6))
                elif calibrated and "x" in poi:
                    d = math.hypot(pos[0] - float(poi["x"]), pos[1] - float(poi["y"]))
                    limit = float(cfg.get("distance_m", 1.0))
                if d is None:
                    continue
                if d <= limit:
                    st.poi_since.setdefault(poi["id"], now)
                    key = f"poi_approach:{poi['id']}"
                    if now - st.poi_since[poi["id"]] >= float(cfg.get("min_seconds", 1.0)) and key not in st.alerted \
                            and self._cooled(rt, key, cfg, now):
                        st.alerted.add(key)
                        self._fire(area, rt, "poi_approach", cfg, cam, cam_name, tr, st, now, frame, out,
                                   f"Near {poi['name']}: {area.name}",
                                   f"Someone is close to {poi['name']} in {area.name}.",
                                   {"poi_id": poi["id"], "poi_name": poi["name"],
                                    "distance": round(d, 2), "distance_unit": "m" if calibrated or not poi.get("camera_id") else "bh"},
                                   cooldown_key=key, once_key=key)
                else:
                    st.poi_since.pop(poi["id"], None)

    def _edge(self, area, rt, cam, cam_name, tr, st, mode, edge, now, frame, announced, out) -> None:
        cfg = self._allowed(area, "boundary_linger", announced)
        if not cfg or edge is None or mode == "view":
            st.edge_since = None
            return
        limit = float(cfg.get("distance_m" if mode == "floor" else "distance_bh", 1.0))
        if edge <= limit:
            st.edge_since = st.edge_since or now
            if now - st.edge_since >= float(cfg.get("min_seconds", 8.0)) and "boundary_linger" not in st.alerted \
                    and self._cooled(rt, "boundary_linger", cfg, now):
                self._fire(area, rt, "boundary_linger", cfg, cam, cam_name, tr, st, now, frame, out,
                           f"Lingering at the edge: {area.name}",
                           f"Someone has waited at the edge of {area.name} for {now - st.edge_since:.0f} s.",
                           {"seconds": round(now - st.edge_since, 1)})
        else:
            st.edge_since = None

    def _people_count(self, area, rt, cam, cam_name, inside_tracks, now, frame, announced, out) -> None:
        cfg = area.triggers.get("people_count") or {}
        if not cfg.get("enabled") or (announced and not cfg.get("while_announced")):
            rt.over_since = None
            return
        counts = [n for c, (n, at) in rt.occupancy.items() if now - at <= OCCUPANCY_MAX_AGE]
        people = max(counts) if counts else 0
        allowed = int(cfg.get("max_people", 1))
        if announced:
            allowed = max(allowed, area.announced_people(now))
        if people <= allowed:
            rt.over_since = None
            return
        rt.over_since = rt.over_since or now
        if now - rt.over_since < PEOPLE_COUNT_HOLD_SEC or not self._cooled(rt, "people_count", cfg, now):
            return
        tr = inside_tracks[0][0] if inside_tracks else None
        if tr is None:
            return
        self._fire(area, rt, "people_count", cfg, cam, cam_name, tr, None, now, frame, out,
                   f"Too many people inside: {area.name}",
                   f"{people} people are inside {area.name}; {allowed} allowed.",
                   {"people": people, "allowed": allowed})

    # ------------------------------------------------------------ tamper
    def frame_health(self, camera_id: str, frame, now: Optional[float] = None) -> Optional[str]:
        """Every decoded frame of a camera: tamper checks (1 Hz) for cameras of armed areas.

        Returns the tamper kind raised, if any (tests).
        """
        now = self.clock() if now is None else now
        self._frames[camera_id] = now
        # Every decoded frame comes here; the checks run at most once per
        # HEALTH_EVERY_SEC per camera, so skip the plan and schedule work on
        # the frames in between (audit optimisation 9).
        last = self._health_gate.get(camera_id)
        if last is not None and 0.0 <= now - last < HEALTH_EVERY_SEC:
            return None
        self._health_gate[camera_id] = now
        if not self._plan_on():
            return None
        areas = [a for a in self._snapshot().by_camera.get(camera_id, [])
                 if a.armed_state(now)[0] and (a.triggers.get("tamper") or {}).get("enabled")]
        if not areas:
            return None
        key0 = (areas[0].id, camera_id)
        ts0 = self._tamper.get(key0)
        if ts0 is not None and now - ts0.last_check < HEALTH_EVERY_SEC:
            return None
        try:
            from app.services.motion_gate import _luma_small

            thumb = _luma_small(frame).astype(np.float32)
        except Exception:  # noqa: BLE001
            try:
                thumb = np.asarray(frame, dtype=np.float32)
                if thumb.ndim == 3:
                    thumb = thumb.mean(axis=2)
            except Exception:  # noqa: BLE001
                return None
        raised = None
        with self._lock:
            for area in areas:
                cfg = area.triggers["tamper"]
                st = self._tamper.setdefault((area.id, camera_id), TamperState())
                st.last_check = now
                st.last_frame = now
                st.offline_alerted = False
                std = float(thumb.std())
                if st.ref is None or st.warm < 5:
                    if std >= REFERENCE_MIN_STD:
                        st.ref = thumb if st.ref is None else 0.7 * st.ref + 0.3 * thumb
                        st.ref_std = float(st.ref.std())
                        st.warm += 1
                    continue
                corr = zncc(thumb, st.ref)
                if std < COVERED_STD and st.ref_std >= REFERENCE_MIN_STD:
                    st.covered_since = st.covered_since or now
                    st.moved_since = None
                    if now - st.covered_since >= float(cfg.get("covered_sec", 10)):
                        if self._tamper_alert(area, camera_id, "covered", cfg, now):
                            raised = "covered"
                    continue
                st.covered_since = None
                if corr < MOVED_ZNCC:
                    st.moved_since = st.moved_since or now
                    if now - st.moved_since >= float(cfg.get("moved_sec", 15)):
                        if self._tamper_alert(area, camera_id, "moved", cfg, now):
                            raised = "moved"
                        st.ref, st.warm, st.moved_since = thumb, 5, None   # one alert per move
                        st.ref_std = std
                    continue
                st.moved_since = None
                if corr > 0.8:
                    st.ref = 0.98 * st.ref + 0.02 * thumb
        return raised

    def _tamper_alert(self, area, camera_id: str, kind: str, cfg: dict, now: float, camera_name=None) -> bool:
        rt = self._rt(area.id)
        key = f"tamper:{camera_id}"
        if not self._cooled(rt, key, cfg, now):
            return False
        rt.last_alert[key] = now
        words = {"covered": "is covered or blacked out", "moved": "has been turned to look elsewhere",
                 "offline": "stopped sending pictures"}[kind]
        cam = camera_name or camera_id
        self._queue(area, "tamper", cfg, camera_id, None, now,
                    f"Camera tampered with: {area.name}",
                    f"The camera {cam} that watches {area.name} {words} while the area is armed.",
                    {"tamper": kind}, frame=None, tr=None, clip=False)
        return True

    async def tick(self, now: Optional[float] = None) -> int:
        """Once a second: offline cameras of armed areas (tamper)."""
        now = self.clock() if now is None else now
        if not self._plan_on():
            return 0
        n = 0
        names = {}
        try:
            from app.services.live_analytics_engine import live_engine

            names = {cid: getattr(rt, "name", cid) for cid, rt in live_engine.runtimes.items()}
        except Exception:  # noqa: BLE001
            pass
        # The engine lock is shared with the camera threads' per-frame evaluation:
        # waiting for it here blocked the event loop (audit LOW). Only the
        # selection runs under it; alerts are raised after it is released.
        due = await asyncio.to_thread(self._offline_due, now)
        for area, cam, cfg in due:
            if self._tamper_alert(area, cam, "offline", cfg, now, camera_name=names.get(cam)):
                n += 1
        return n

    def _offline_due(self, now: float) -> list:
        due = []
        with self._lock:
            for area in self._snapshot().areas:
                if not area.enabled or not area.armed_state(now)[0]:
                    continue
                cfg = area.triggers.get("tamper") or {}
                if not cfg.get("enabled"):
                    continue
                limit = float(cfg.get("offline_sec", 20))
                for cam in area.cameras():
                    last = self._frames.get(cam)
                    ref = last if last is not None else self._started_at + 30.0
                    st = self._tamper.setdefault((area.id, cam), TamperState())
                    if now - ref > limit and not st.offline_alerted:
                        st.offline_alerted = True
                        due.append((area, cam, cfg))
        return due

    # ------------------------------------------------------------ alerts
    def _fire(self, area, rt, trigger, cfg, cam, cam_name, tr, st, now, frame, out, title, body, extra,
              cooldown_key=None, once_key=None) -> None:
        rt.last_alert[cooldown_key or trigger] = now
        if st is not None:
            st.alerted.add(once_key or trigger)
        alert = self._queue(area, trigger, cfg, cam, cam_name, now, title, body, extra, frame=frame, tr=tr)
        out["alerts"].append(alert.alert_id)

    def _queue(self, area, trigger, cfg, cam, cam_name, now, title, body, extra, frame=None, tr=None,
               clip=True, test=False, actor=None) -> PendingAlert:
        from app.services import theft_alert_policy as tp
        from app.services.restricted_areas import TRIGGERS

        tier = cfg.get("tier") or TRIGGERS[trigger]["tier"]
        if tier not in tp.TIERS:
            tier = "alert"
        alert_id = f"ra_{uuid.uuid4().hex[:12]}"
        occ = self.occupancy(area.id, now)
        prefix = tp.TIER_TITLE.get(tier, "Alert")
        label = TRIGGERS[trigger]["label"]
        data = {
            "camera_id": cam, "area_id": area.id, "area_name": area.name, "zone_id": area.id,
            "zone_name": area.name, "ra_area_id": area.id, "ra_trigger": trigger, "ra_trigger_label": label,
            "ra_requires_outcome": not test, "ra_bypass_quiet": True, "ra_all_phones": tier == "critical",
            "alert_tier": tier, "alert_id": alert_id, "ra_detected_at": _iso(now),
            "timestamp": _iso(now), "people_inside": occ.get("people"),
            "track_id": None if tr is None else str(tr.track_id), "test": bool(test),
        }
        data.update(extra or {})
        if test:
            data["ra_test_by"] = (actor or {}).get("name")
        x = None if tr is None else tuple(float(v) for v in tr.bbox)
        alert = PendingAlert(
            alert_id=alert_id, area_id=area.id, area_name=area.name, trigger=trigger, tier=tier, camera_id=cam,
            title=f"{'TEST ' if test else ''}{prefix}: {title}"[:120], body=body, data=data, detected_at=now,
            frame=None if frame is None else frame.copy(), bbox=x,
            keypoints=None if tr is None else getattr(tr, "keypoints", None),
            caption=f"RESTRICTED AREA: {label.upper()}: {area.name}", clip=clip and frame is not None)
        rt = self._rt(area.id)
        rt.last_events.appendleft({"id": alert_id, "trigger": trigger, "label": label, "tier": tier,
                                   "at": _iso(now), "camera_id": cam, "test": bool(test)})
        self.stats["alerts"] += 1
        with self._alerts_lock:
            self._alerts.append(alert)
        if not self.synchronous:
            self._ensure_writer()
            self._wake.set()
        return alert

    def raise_test(self, area, actor: Optional[dict] = None, now: Optional[float] = None) -> PendingAlert:
        """A clearly labelled test alert through the real path (the entry trigger's level)."""
        now = self.clock() if now is None else now
        cfg = dict(area.triggers.get("unannounced_entry") or {})
        cams = area.cameras()
        cam = cams[0] if cams else ""
        who = (actor or {}).get("name") or "an administrator"
        return self._queue(area, "unannounced_entry", cfg, cam, None, now,
                           f"Entry without notice: {area.name}",
                           f"This is a test of the restricted area alert for {area.name}, started by {who}. "
                           "No one needs to go there.", {}, clip=False, test=True, actor=actor)

    def occupancy(self, area_id: str, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        rt = self._areas.get(area_id)
        if rt is None:
            return {"people": None, "by_camera": {}, "at": None}
        fresh = {c: n for c, (n, at) in rt.occupancy.items() if now - at <= OCCUPANCY_MAX_AGE}
        at = max((at for _c, (_n, at) in rt.occupancy.items()), default=None)
        return {"people": max(fresh.values()) if fresh else None, "by_camera": fresh, "at": _iso(at) if at else None}

    def last_events(self, area_id: str) -> List[dict]:
        rt = self._areas.get(area_id)
        return list(rt.last_events) if rt else []

    def camera_heartbeat(self, camera_id: str) -> Optional[float]:
        return self._frames.get(camera_id)

    # ------------------------------------------------------------ writer
    @staticmethod
    def evidence_dir() -> Path:
        from app.services.tripwire_engine import TripwireEngine

        return TripwireEngine.evidence_dir()

    def _write_snapshot(self, alert: PendingAlert) -> Optional[str]:
        if alert.frame is None:
            return None
        try:
            import cv2

            from app.services.pose_analytics import render_evidence

            kps = alert.keypoints if isinstance(alert.keypoints, np.ndarray) else None
            img = render_evidence(alert.frame, alert.bbox, kps, alert.caption,
                                  f"{alert.camera_id}  {alert.data.get('timestamp', '')[:19].replace('T', ' ')} UTC")
            path = self.evidence_dir() / f"{alert.alert_id}.jpg"
            ok = cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.THEFT_EVIDENCE_JPEG_QUALITY)])
            if ok:
                from app.services.evidence_storage import note_evidence_written

                note_evidence_written(path)
                return str(path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Restricted area snapshot {alert.alert_id} not written: {e}")
        return None

    def _dispatcher(self):
        if self.dispatcher_override is not None:
            return self.dispatcher_override
        from app.services.alert_dispatcher import alert_dispatcher

        async def _d(event_type, severity, title, body, data):
            return await alert_dispatcher.dispatch(event_type, severity, title, body, data, bypass_cooldown=True)

        return _d

    def _deliver(self, alert: PendingAlert) -> None:
        from app.services import theft_alert_policy as tp

        if not self._plan_on():
            alert.frame = None
            return
        if self._write_snapshot(alert):
            url = f"/api/v1/restricted-areas/events/{alert.alert_id}/snapshot"
            alert.data["snapshot_url"] = url
            alert.data["evidence_snapshot_url"] = url
        frame_for_clip = alert.clip
        alert.frame = None
        dispatch = self._dispatcher()
        severity = tp.TIER_EVENT_SEVERITY.get(alert.tier, "HIGH")

        async def _send():
            try:
                result = await asyncio.wait_for(
                    dispatch("RESTRICTED_AREA", severity, alert.title, alert.body, dict(alert.data)), timeout=15.0)
                ms = round((self.clock() - alert.detected_at) * 1000.0, 1)
                self.stats["delivered"] += 1
                self.stats["latency_ms"] = ms
                self.stats["latency_max_ms"] = max(ms, self.stats["latency_max_ms"] or 0.0)
                self.last_delivery = {"alert_id": alert.alert_id, "latency_ms": ms, "result": result}
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                self.last_delivery = {"alert_id": alert.alert_id, "error": str(e)}
                logger.error(f"Restricted area alert {alert.alert_id} not delivered: {e}")
                return
            # The alert is out: a failed audit write is an audit problem, not an
            # undelivered alert (it used to be counted as one).
            try:
                await self._service().audit_now(
                    "test_alert" if alert.data.get("test") else "alert", SimpleNamespace(id=alert.area_id, name=alert.area_name),
                    {"name": alert.data.get("ra_test_by") or "System", "role": None}, event_id=alert.alert_id,
                    detail={"trigger": alert.trigger, "tier": alert.tier, "camera_id": alert.camera_id,
                            "latency_ms": ms, "title": alert.title,
                            "measured": {k: v for k, v in alert.data.items()
                                         if k in ("speed", "speed_unit", "reversals", "turns", "people", "allowed",
                                                  "inside_sec", "seconds", "poi_name", "distance", "tamper",
                                                  "calibrated")}})
            except Exception as e:  # noqa: BLE001
                self.stats["audit_errors"] = self.stats.get("audit_errors", 0) + 1
                logger.error(f"Restricted area alert {alert.alert_id} delivered, but not written to the audit: {e}")

        loop = self._loop
        if loop is None:
            try:
                from app.services.pose_analytics import pose_analytics

                loop = getattr(pose_analytics, "_loop", None)
            except Exception:  # noqa: BLE001
                loop = None
        if loop is not None and not loop.is_closed() and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_send(), loop)
            if self.synchronous:
                try:
                    fut.result(timeout=20)
                except Exception:  # noqa: BLE001
                    pass
        elif self.synchronous or self.dispatcher_override is not None:
            asyncio.run(_send())
        else:
            self.stats["errors"] += 1
            logger.error(f"Restricted area alert {alert.alert_id} not sent: no application event loop bound")
            return
        if frame_for_clip and self.record_clips:
            threading.Thread(target=self._record_clip, args=(alert,), name="ra-clip", daemon=True).start()

    def _record_clip(self, alert: PendingAlert) -> None:
        if not self._clips.acquire(blocking=False):
            logger.info(f"No clip for restricted area alert {alert.alert_id}: {MAX_CLIPS} clips already recording")
            return
        try:
            rec = self.clip_recorder_override
            if rec is None:
                from app.services.clip_recorder import clip_recorder_service as rec
            path = rec.record_incident_clip(alert.alert_id, alert.camera_id, self.evidence_dir(),
                                            event_ts=alert.detected_at)
            if path is None:
                return
            self.stats["clips"] += 1
            import sqlite3

            url = f"/api/v1/restricted-areas/events/{alert.alert_id}/clip"
            for _ in range(10):
                try:
                    with sqlite3.connect(str(settings.DATABASE_PATH), timeout=10) as conn:
                        cur = conn.execute("UPDATE security_events SET clip_url = ? WHERE id = ?", (url, alert.alert_id))
                        if cur.rowcount:
                            return
                except sqlite3.Error as e:
                    logger.warning(f"clip link for {alert.alert_id} not saved yet: {e}")
                time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Restricted area clip {alert.alert_id} failed: {e}")
        finally:
            self._clips.release()

    def process_pending_alerts(self) -> int:
        with self._alerts_lock:
            batch, self._alerts = self._alerts, []
        for a in batch:
            try:
                self._deliver(a)
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.error(f"restricted area alert {a.alert_id} failed: {e}")
        return len(batch)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer = threading.Thread(target=self._writer_loop, name="restricted-area-writer", daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            self.process_pending_alerts()

    # ------------------------------------------------------------ synthetic feed (tests only)
    def qa_feed(self, camera_id: str, frames: List[dict], fw: int, fh: int, camera_name: Optional[str] = None,
                now: Optional[float] = None) -> dict:
        """Feed synthetic tracks: frames ``[{dt, tracks: [{id, bbox, keypoints?, floor?, static?}]}]``.

        ``dt`` is seconds before ``now`` of each frame (the last one is usually 0).
        Called by the tests only; no route exposes it.
        """
        now = self.clock() if now is None else now
        out = {"alerts": [], "occupancy": {}}
        for f in frames:
            t = now - float(f.get("dt", 0.0))
            tracks = []
            for x in f.get("tracks") or []:
                kp = x.get("keypoints")
                tracks.append(SimpleNamespace(
                    track_id=str(x["id"]), bbox=tuple(float(v) for v in x["bbox"]), hits=int(x.get("hits", 5)),
                    misses=0, keypoints=None if kp is None else np.asarray(kp, dtype=np.float32),
                    keypoints_fresh=True, floor_xy=tuple(x["floor"]) if x.get("floor") else None,
                    is_static=bool(x.get("static")), is_pending_static=False))
            r = self.evaluate(camera_id, tracks, fw, fh, t, frame=None, camera_name=camera_name)
            out["alerts"] += r["alerts"]
            out["occupancy"].update(r["occupancy"])
        return out

    def status(self) -> dict:
        with self._lock:
            return {"stats": dict(self.stats), "last_delivery": self.last_delivery,
                    "tracked": len(self._area_tracks), "cameras_reporting": len(self._frames)}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _hhmm(ts: float) -> str:
    try:
        from app.services.timeutil import site_tz

        tz = site_tz()
        d = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
        return d.strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return "--:--"


restricted_area_engine = RestrictedAreaEngine()
