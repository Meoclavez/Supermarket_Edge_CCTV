"""Multi-object tracking and floor projection.

Detections alone cannot answer any retail question. "How many people came in"
needs identity across frames, and "how long did they spend at the bakery" needs
those identities placed on the store floor rather than in camera pixels.

This module supplies both:

* ``ByteTracker`` (also exported as ``CentroidTracker``) -- ByteTrack-style
  two-stage association. Every track's box is predicted forward with a
  constant-velocity Kalman filter; high-confidence detections are matched
  first, then the leftover tracks get a second chance against low-confidence
  detections, which is what holds an identity through partial occlusion by a
  shelf end or another shopper. Low-confidence boxes can only extend a track,
  never start one, so they cannot create false shoppers. Assignment is the
  Hungarian algorithm when scipy is importable, greedy IoU otherwise.
  Each track carries the latest pose keypoints, smoothed only against
  jitter (fast limb motion passes through unsmoothed).
  Each track is also classified "pending" / "moving" / "static" from joint
  micro-motion normalised by torso length: a poster or mannequin the pose
  model keeps recognising becomes "static" (``Track.is_human`` False) and is
  remembered per camera, so its next track id is recognised within seconds.
* ``FloorProjector`` -- maps a detection's foot point into store metres using
  the camera's homography when one has been calibrated, and declines to guess
  when one has not.

Nothing here invents a position. A camera without a homography contributes
detections and dwell time but no floor coordinates, and the analytics layer
reports it as uncalibrated rather than placing its shoppers at the origin.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.config import settings
from app.services.inference_backend import Detection, person_threshold

logger = logging.getLogger(__name__)

try:  # optional: optimal assignment when available
    from scipy.optimize import linear_sum_assignment as _hungarian
except Exception:  # pragma: no cover - depends on the environment
    _hungarian = None

ASSIGNMENT_METHOD = "hungarian" if _hungarian is not None else "greedy"


# ------------------------------------------------------------ Kalman filter


class _KalmanXYWH:
    """Constant-velocity Kalman filter on (cx, cy, w, h), one step per update.

    Noise is scaled by the box size, as in SORT/ByteTrack, so a near person
    and a far person get proportionate uncertainty.
    """

    _W_POS = 1.0 / 20.0
    _W_VEL = 1.0 / 160.0

    def __init__(self):
        self._F = np.eye(8)
        self._F[:4, 4:] = np.eye(4)
        self._H = np.eye(4, 8)

    def initiate(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[z, np.zeros(4)]
        w, h = z[2], z[3]
        p, v = self._W_POS, self._W_VEL
        std = [2 * p * w, 2 * p * h, 2 * p * w, 2 * p * h, 10 * v * w, 10 * v * h, 10 * v * w, 10 * v * h]
        return mean, np.diag(np.square(std))

    def predict(self, mean: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        w, h = max(mean[2], 1.0), max(mean[3], 1.0)
        p, v = self._W_POS, self._W_VEL
        q = np.diag(np.square([p * w, p * h, p * w, p * h, v * w, v * h, v * w, v * h]))
        mean = self._F @ mean
        cov = self._F @ cov @ self._F.T + q
        return mean, cov

    def update(self, mean: np.ndarray, cov: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        w, h = max(mean[2], 1.0), max(mean[3], 1.0)
        p = self._W_POS
        r = np.diag(np.square([p * w, p * h, p * w, p * h]))
        s = self._H @ cov @ self._H.T + r
        k = np.linalg.solve(s, (cov @ self._H.T).T).T
        mean = mean + k @ (z - self._H @ mean)
        cov = cov - k @ s @ k.T
        return mean, cov


_KF = _KalmanXYWH()


def _xyxy_to_xywh(b) -> np.ndarray:
    x1, y1, x2, y2 = b
    return np.array([(x1 + x2) / 2.0, (y1 + y2) / 2.0, max(x2 - x1, 1.0), max(y2 - y1, 1.0)], dtype=np.float64)


def _xywh_to_xyxy(m) -> tuple[float, float, float, float]:
    cx, cy, w, h = (float(v) for v in m[:4])
    w, h = max(w, 1.0), max(h, 1.0)
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


# -------------------------------------------------------------------- track


@dataclass
class Track:
    """One person followed across frames of a single camera."""

    track_id: str
    camera_id: str
    bbox: tuple[float, float, float, float]      # last measured box (pixels)
    confidence: float
    first_seen: float
    last_seen: float
    hits: int = 1
    misses: int = 0
    # Detection frames since the track was born.
    age: int = 0
    # Latest (17, 3) keypoints (x_px, y_px, visibility), EMA-smoothed on
    # points visible in consecutive observations. None for a detect-only model.
    keypoints: Optional[np.ndarray] = field(default=None, repr=False)
    # Latest floor position in metres, set by the engine when calibrated.
    floor_xy: Optional[tuple[float, float]] = None
    # Trajectory in floor metres, appended only while a homography exists.
    floor_points: list[dict] = field(default_factory=list)
    # Persisted path (customer_tracks.trajectory_points), sampled every
    # TRAJECTORY_SAMPLE_SEC while people_counting is on: normalised image foot
    # point {"u", "v", "t"} for every camera, plus floor {"x", "y"} when
    # calibrated. Feeds the image- and floor-space heatmaps.
    path_points: list[dict] = field(default_factory=list)
    # Zone the track is currently inside, and when it entered.
    current_zone_id: Optional[str] = None
    zone_entered_at: Optional[float] = None
    # Set once this track's dwell in the current zone has been published.
    open_visit_id: Optional[str] = None
    interacted: bool = False
    # Static-figure filter (ByteTracker._observe_motion): "pending" until the
    # track has either moved (-> "moving") or stayed motionless for the
    # camera's static time (-> "static": a poster, mannequin or cut-out).
    # A static track goes back to "moving" as soon as real motion resumes.
    motion_state: str = "pending"
    static_since: Optional[float] = None
    # Anchor pose the motion is measured against, and since when the track
    # has shown no motion beyond detector jitter.
    still_since: Optional[float] = None
    _anchor_kpts: Optional[np.ndarray] = field(default=None, repr=False)
    _anchor_centre: Optional[tuple[float, float]] = field(default=None, repr=False)
    _anchor_height: float = field(default=0.0, repr=False)
    _anchor_scale: float = field(default=0.0, repr=False)
    _anchor_n: int = field(default=0, repr=False)
    _motion_streak: int = field(default=0, repr=False)
    _motion_obs_at: Optional[float] = field(default=None, repr=False)
    # Recent poses (ByteTracker._observe_motion): per-joint jitter floor and
    # the 3-frame median pose compared against the anchor.
    _pose_hist: deque = field(default_factory=lambda: deque(maxlen=MOTION_JITTER_WINDOW + 1), repr=False)
    # Kalman state (cx, cy, w, h, vx, vy, vw, vh) and covariance.
    _mean: Optional[np.ndarray] = field(default=None, repr=False)
    _cov: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def confirmed(self) -> bool:
        """A track is only real once it has been seen repeatedly.

        Single-frame blips are false positives; counting them would inflate
        footfall by a large factor in busy scenes.
        """
        return self.hits >= settings.TRACK_MIN_HITS

    @property
    def is_static(self) -> bool:
        """A figure that has not moved beyond detector jitter (poster, mannequin)."""
        return self.motion_state == "static"

    @property
    def is_human(self) -> bool:
        """Confirmed and not a static figure: what counting and analytics may use.

        A "pending" track (not enough history yet) counts as human, so the
        filter fails open rather than hiding a real person.
        """
        return self.confirmed and not self.is_static

    @property
    def age_seconds(self) -> float:
        return self.last_seen - self.first_seen

    @property
    def foot_point(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2)

    @property
    def keypoints_fresh(self) -> bool:
        """True when ``keypoints`` came from this detection frame."""
        return self.keypoints is not None and self.misses == 0

    @property
    def predicted_bbox(self) -> tuple[float, float, float, float]:
        return _xywh_to_xyxy(self._mean) if self._mean is not None else self.bbox

    @property
    def velocity_px(self) -> tuple[float, float]:
        """Predicted centre motion, pixels per detection frame."""
        if self._mean is None:
            return (0.0, 0.0)
        return (float(self._mean[4]), float(self._mean[5]))


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    return inter / max(area_a + area_b - inter, 1e-9)


def _iou_matrix(tracks: list[Track], dets: list[Detection]) -> np.ndarray:
    if not tracks or not dets:
        return np.zeros((len(tracks), len(dets)))
    a = np.array([t.predicted_bbox for t in tracks], dtype=np.float64)
    b = np.array([(d.x1, d.y1, d.x2, d.y2) for d in dets], dtype=np.float64)
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
    iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
    iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = np.clip(a[:, 2] - a[:, 0], 0, None) * np.clip(a[:, 3] - a[:, 1], 0, None)
    area_b = np.clip(b[:, 2] - b[:, 0], 0, None) * np.clip(b[:, 3] - b[:, 1], 0, None)
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def linear_assignment(iou: np.ndarray, min_iou: float) -> list[tuple[int, int]]:
    """Pairs (row, col) maximising total IoU, each with IoU >= ``min_iou``."""
    if iou.size == 0:
        return []
    if _hungarian is not None:
        rows, cols = _hungarian(1.0 - iou)
        return [(int(r), int(c)) for r, c in zip(rows, cols) if iou[r, c] >= min_iou]
    pairs: list[tuple[int, int]] = []
    used_r: set[int] = set()
    used_c: set[int] = set()
    flat = np.argsort(-iou, axis=None)
    for idx in flat:
        r, c = divmod(int(idx), iou.shape[1])
        if iou[r, c] < min_iou:
            break
        if r in used_r or c in used_c:
            continue
        used_r.add(r)
        used_c.add(c)
        pairs.append((r, c))
    return pairs


# Elbows and wrists: the joints a reach is measured on.
ARM_JOINTS = (7, 8, 9, 10)


def smooth_keypoints(
    prev: Optional[np.ndarray],
    new: Optional[np.ndarray],
    alpha: float,
    scale: Optional[float] = None,
    arm_alpha: Optional[float] = None,
) -> Optional[np.ndarray]:
    """Velocity-aware EMA on x/y of points visible in both observations.

    ``alpha`` is the weight of the new observation for a point that barely
    moved (detector jitter). A point that moved further than
    ``TRACK_KEYPOINT_JITTER_FRAC`` of ``scale`` (the person's box height) gets
    progressively less smoothing, and none at all beyond
    ``TRACK_KEYPOINT_FAST_FRAC``: a plain EMA made a wrist lifted to a top
    shelf arrive one or two detection frames late (at 5 detections/s that is
    a quick grab missed entirely). ``arm_alpha`` overrides ``alpha`` for
    elbows and wrists. Points not visible now take the new observation (with
    its low visibility): a stale position is never carried forward.
    """
    if new is None:
        return None
    new = np.asarray(new, dtype=np.float32)
    if prev is None or prev.shape != new.shape or new.shape[1] < 3:
        return new.copy()
    thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
    both = (new[:, 2] >= thr) & (prev[:, 2] >= thr)
    out = new.copy()
    if not both.any():
        return out
    a = np.full(new.shape[0], float(alpha), dtype=np.float32)
    if arm_alpha is not None and new.shape[0] > max(ARM_JOINTS):
        a[list(ARM_JOINTS)] = float(arm_alpha)
    if scale is not None and scale > 0:
        moved = np.hypot(new[:, 0] - prev[:, 0], new[:, 1] - prev[:, 1]) / float(scale)
        lo = float(settings.TRACK_KEYPOINT_JITTER_FRAC)
        hi = max(float(settings.TRACK_KEYPOINT_FAST_FRAC), lo + 1e-6)
        a = a + (1.0 - a) * np.clip((moved - lo) / (hi - lo), 0.0, 1.0)
    a = np.clip(a, 0.0, 1.0)[both, None]
    out[both, :2] = a * new[both, :2] + (1.0 - a) * prev[both, :2]
    return out


# ------------------------------------------------------ static-figure filter

# Detection frames in a row whose pose differs from the anchor by more than
# STATIC_FIGURE_MOTION_FRAC before that counts as motion: a single-frame
# keypoint glitch on a poster (a wrist flipping for one frame) is not motion,
# while a real movement holds or continues into the next frame.
MOTION_CONFIRM_FRAMES = 2
# The anchor pose is the mean of the first this-many still observations
# after (re-)anchoring: averaging N samples cuts the anchor's own jitter by
# sqrt(N), so the per-frame comparison sees ~1.1x the detector jitter
# instead of ~1.4x (two single noisy samples).
MOTION_ANCHOR_FRAMES = 5
# A gap longer than this between two observations of a track restarts its
# stillness clock: time nobody looked at the figure is not time it was seen
# standing still.
MOTION_MAX_OBS_GAP_SEC = 10.0
# Per-joint jitter floor. On small or low-resolution figures RTMO's wrists
# (and sometimes elbows) scatter widely from frame to frame although the
# image is unchanged: on a 150 px poster figure a wrist's frame-to-frame
# step had a median of 0.12-0.17 and a 95th percentile of 0.3-0.45 torso,
# so the plain 0.15 torso threshold saw "motion" every few frames and the
# poster never went static. A joint must therefore also move more than
# MOTION_JITTER_K times the MOTION_JITTER_PERCENTILE-th percentile of its own
# frame-to-frame step over the last MOTION_JITTER_WINDOW observations (once
# MOTION_JITTER_MIN_STEPS steps are known), and the pose compared with the
# anchor (and averaged into it) is the per-joint median of the last 3
# observations, so a single-frame flip never counts. The 75th percentile
# rather than the median: with the median (K 3) the 150 px poster still
# had 7 motion events in 90 s and never went static. A still person's
# joints step only by detector jitter, so for them the floor stays below
# STATIC_FIGURE_MOTION_FRAC; the box centre and height are not floored, so
# whole-body motion is seen as before. Chosen on real RTMO detections (see config.py STATIC_FIGURE_*):
# K 2 or a 30-observation window still let a poster wrist's jitter tail
# through about once per 90 s; K 3 began to miss 0.06-0.12 torso head/body
# moves at 2 detections/s that K 2.5 still catches.
MOTION_JITTER_K = 2.5
MOTION_JITTER_PERCENTILE = 75.0
MOTION_JITTER_WINDOW = 50
MOTION_JITTER_MIN_STEPS = 8
_POSE_MEDIAN_FRAMES = 3
# At most this many remembered static boxes per camera (oldest-seen dropped).
STATIC_MEMORY_MAX = 32
# Joints seen in both poses needed to compare two poses (memory match).
_POSE_MATCH_MIN_JOINTS = 4
_SHOULDERS, _HIPS = (5, 6), (11, 12)


def body_scale(kpts: Optional[np.ndarray], bbox: tuple[float, float, float, float]) -> float:
    """Torso length in pixels (shoulder midpoint to hip midpoint).

    Falls back to 0.3 x the box height (a standing adult's torso share) when
    a shoulder or a hip is not visible, e.g. for a detect-only model.
    """
    box_h = max(float(bbox[3] - bbox[1]), 1.0)
    if kpts is not None and len(kpts) > max(_HIPS) and kpts.shape[1] >= 3:
        thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
        sh = [kpts[i, :2] for i in _SHOULDERS if kpts[i, 2] >= thr]
        hp = [kpts[i, :2] for i in _HIPS if kpts[i, 2] >= thr]
        if sh and hp:
            torso = float(np.linalg.norm(np.mean(sh, axis=0) - np.mean(hp, axis=0)))
            if torso >= 0.1 * box_h:
                return torso
    return max(0.3 * box_h, 1.0)


def _joint_displacements(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> np.ndarray:
    """Pixel displacement of each joint visible in both poses (empty if none)."""
    if a is None or b is None or a.shape != b.shape or a.shape[1] < 3:
        return np.zeros(0, dtype=np.float32)
    thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
    both = (a[:, 2] >= thr) & (b[:, 2] >= thr)
    if not both.any():
        return np.zeros(0, dtype=np.float32)
    return np.hypot(a[both, 0] - b[both, 0], a[both, 1] - b[both, 1])


def recent_pose(hist) -> Optional[np.ndarray]:
    """The newest pose in ``hist`` with x/y the per-joint median of the last 3.

    A keypoint that flips for a single frame does not move this pose.
    Visibility is the newest observation's.
    """
    if not hist:
        return None
    cur = hist[-1]
    poses = [p for p in list(hist)[-_POSE_MEDIAN_FRAMES:] if p.shape == cur.shape]
    out = cur.copy()
    if len(poses) > 1 and cur.ndim == 2 and cur.shape[1] >= 3:
        out[:, :2] = np.median(np.stack(poses)[:, :, :2], axis=0)
    return out


def joint_motion_over_floor(hist, anchor: Optional[np.ndarray], ref: float, frac: float) -> bool:
    """Whether some joint moved away from ``anchor`` beyond threshold and its own jitter.

    ``hist`` holds the track's recent (17, 3) poses, newest last. A joint
    counts when its recent_pose position is further than
    max(``frac``, MOTION_JITTER_K x its frame-to-frame step percentile) x ``ref``
    from the anchor (see MOTION_JITTER_K). The joint must be visible now and
    in the anchor.
    """
    pos = recent_pose(hist)
    if pos is None or anchor is None or pos.shape != anchor.shape or pos.ndim != 2 or pos.shape[1] < 3:
        return False
    thr_vis = settings.KEYPOINT_VISIBILITY_THRESHOLD
    both = (pos[:, 2] >= thr_vis) & (anchor[:, 2] >= thr_vis)
    if not both.any():
        return False
    ref = max(float(ref), 1.0)
    dist = np.hypot(pos[:, 0] - anchor[:, 0], pos[:, 1] - anchor[:, 1]) / ref
    floor = np.full(pos.shape[0], float(frac), dtype=np.float64)
    poses = [p for p in hist if p.shape == pos.shape]
    if len(poses) > MOTION_JITTER_MIN_STEPS:
        h = np.stack(poses)
        seen = (h[1:, :, 2] >= thr_vis) & (h[:-1, :, 2] >= thr_vis)
        step = np.hypot(h[1:, :, 0] - h[:-1, :, 0], h[1:, :, 1] - h[:-1, :, 1]) / ref
        for j in range(pos.shape[0]):
            s = step[seen[:, j], j]
            if s.size >= MOTION_JITTER_MIN_STEPS:
                floor[j] = max(floor[j], MOTION_JITTER_K * float(np.percentile(s, MOTION_JITTER_PERCENTILE)))
    return bool((dist[both] > floor[both]).any())


def poses_match(a: Optional[np.ndarray], b: Optional[np.ndarray], scale: float) -> Optional[bool]:
    """Whether two poses are the same figure in the same posture.

    The median joint displacement must stay within STATIC_FIGURE_MOTION_FRAC
    of ``scale`` (the median ignores one or two glitching joints). None when
    too few joints are visible in both to tell.
    """
    d = _joint_displacements(a, b)
    if d.size < _POSE_MATCH_MIN_JOINTS:
        return None
    return float(np.median(d)) <= float(settings.STATIC_FIGURE_MOTION_FRAC) * max(scale, 1.0)


@dataclass
class StaticMemoryEntry:
    """A box where a static figure was seen, kept so its next track id is caught fast."""

    bbox: tuple[float, float, float, float]
    keypoints: Optional[np.ndarray]
    scale: float
    first_seen: float
    last_seen: float


class ByteTracker:
    """Two-stage (ByteTrack-style) association tracker, one instance per camera."""

    def __init__(self, camera_id: str, iou_threshold: Optional[float] = None):
        self.camera_id = camera_id
        self.iou_threshold = settings.TRACK_IOU_THRESHOLD if iou_threshold is None else iou_threshold
        self.low_iou_threshold = settings.TRACK_LOW_IOU_THRESHOLD
        self.tracks: dict[str, Track] = {}
        self._finished: list[Track] = []
        # Static-figure filter, per camera (configure_static); remembered
        # static boxes survive flush_all (camera offline, night watch) and
        # expire after STATIC_MEMORY_TTL_SEC unseen.
        self.static_filter = True
        self.static_seconds: Optional[float] = None
        self.static_memory: list[StaticMemoryEntry] = []

    # ------------------------------------------------- static-figure filter

    def configure_static(self, enabled: bool = True, seconds: Optional[float] = None) -> None:
        """Per-camera settings: filter on/off and seconds before static (None = server default)."""
        self.static_filter = bool(enabled)
        self.static_seconds = None if seconds is None else float(seconds)

    @property
    def static_after(self) -> float:
        s = self.static_seconds if self.static_seconds is not None else settings.STATIC_FIGURE_SECONDS
        return max(float(s), 0.0)

    def _memory_hit(self, bbox, keypoints, scale: float) -> Optional[StaticMemoryEntry]:
        """The remembered static figure at this box in this pose, if any."""
        best, best_iou = None, float(settings.STATIC_MEMORY_IOU)
        for e in self.static_memory:
            iou = _iou(tuple(bbox), e.bbox)
            if iou < best_iou:
                continue
            # Same box is not enough: a real person standing where a poster
            # hangs has a different pose. Unknown (too few joints) -> box only.
            if poses_match(keypoints, e.keypoints, max(scale, e.scale)) is False:
                continue
            best, best_iou = e, iou
        return best

    def matches_static_memory(self, bbox, keypoints=None, now: Optional[float] = None,
                              refresh: bool = True) -> bool:
        """True when ``bbox`` (with ``keypoints``) is a remembered static figure.

        Used where no tracking runs (night-watch confirmation). A hit with
        ``refresh`` keeps the entry from expiring while the figure is still there.
        """
        if not self.static_filter or not self.static_memory:
            return False
        now = time.time() if now is None else now
        self._expire_memory(now)
        kp = None if keypoints is None else np.asarray(keypoints, dtype=np.float32)
        hit = self._memory_hit(tuple(float(v) for v in bbox), kp, body_scale(kp, tuple(bbox)))
        if hit is not None and refresh:
            hit.last_seen = now
        return hit is not None

    def _expire_memory(self, now: float) -> None:
        ttl = float(settings.STATIC_MEMORY_TTL_SEC)
        self.static_memory = [e for e in self.static_memory if now - e.last_seen <= ttl]

    def _remember_static(self, t: Track, now: float) -> None:
        scale = body_scale(t.keypoints, t.bbox)
        hit = self._memory_hit(t.bbox, t.keypoints, scale)
        if hit is not None:
            hit.bbox, hit.last_seen = t.bbox, now
            return
        kp = None if t.keypoints is None else t.keypoints.copy()
        self.static_memory.append(StaticMemoryEntry(t.bbox, kp, scale, now, now))
        if len(self.static_memory) > STATIC_MEMORY_MAX:
            self.static_memory.sort(key=lambda e: e.last_seen)
            del self.static_memory[: len(self.static_memory) - STATIC_MEMORY_MAX]
        logger.info(f"Camera {self.camera_id}: figure at {tuple(round(v) for v in t.bbox)} has not moved "
                    f"for {self.static_after:.0f}s; treating it as static (poster/mannequin)")

    def _observe_motion(self, t: Track, now: float) -> None:
        """Update the track's motion state from this detection frame.

        Motion = some joint (or the box centre/height) moved more than
        STATIC_FIGURE_MOTION_FRAC of the torso length away from the anchor
        pose on MOTION_CONFIRM_FRAMES frames in a row. The anchor is reset on
        motion, so slow drift accumulates and counts, and jitter around a
        fixed point never does. The track's jitter-smoothed pose is used
        (smooth_keypoints): it lowers poster jitter while letting real limb
        motion through unsmoothed. A joint must also clear its own jitter
        floor (MOTION_JITTER_K) using the median of its last 3 positions, so
        a wrist the model keeps flipping on a small poster is not motion.
        """
        kp = t.keypoints
        if kp is not None and (kp.ndim != 2 or kp.shape[1] < 3):
            kp = None
        scale = body_scale(kp, t.bbox)
        x1, y1, x2, y2 = t.bbox
        centre, height = ((x1 + x2) / 2.0, (y1 + y2) / 2.0), max(y2 - y1, 1.0)
        gap = None if t._motion_obs_at is None else now - t._motion_obs_at
        t._motion_obs_at = now

        def reanchor() -> None:
            t._anchor_kpts = None if kp is None else kp.copy()
            t._anchor_centre, t._anchor_height, t._anchor_scale = centre, height, scale
            t._anchor_n = 1
            t._motion_streak = 0
            t.still_since = now

        if gap is not None and gap > MOTION_MAX_OBS_GAP_SEC:
            t._pose_hist.clear()
        if kp is not None:
            t._pose_hist.append(kp.copy())
            # The anchor is built from the same 3-frame median pose the
            # comparison uses, so a pose held after a move is not averaged
            # half-way back by the frame that is still in the median window.
            kp = recent_pose(t._pose_hist)
        if t._anchor_centre is None or (gap is not None and gap > MOTION_MAX_OBS_GAP_SEC):
            reanchor()
        else:
            ref = max(t._anchor_scale, 1.0)
            frac = float(settings.STATIC_FIGURE_MOTION_FRAC)
            moved = float(np.hypot(centre[0] - t._anchor_centre[0], centre[1] - t._anchor_centre[1]))
            moved = max(moved, abs(height - t._anchor_height))
            # Box motion as is; joints above their own jitter floor (MOTION_JITTER_K).
            if moved > frac * ref or (kp is not None
                                      and joint_motion_over_floor(t._pose_hist, t._anchor_kpts, ref, frac)):
                t._motion_streak += 1
            else:
                t._motion_streak = 0
            if t._motion_streak >= MOTION_CONFIRM_FRAMES:
                reanchor()
                t.motion_state, t.static_since = "moving", None
            elif t._motion_streak == 0 and t._anchor_n < MOTION_ANCHOR_FRAMES:
                self._average_anchor(t, kp, centre, height, scale)

        if not self.static_filter:
            if t.motion_state == "static":
                t.motion_state, t.static_since = "pending", None
            return
        if t.motion_state == "static":
            self._remember_static(t, now)
            return
        still = now - (t.still_since if t.still_since is not None else now)
        need = self.static_after
        if still < need and still >= float(settings.STATIC_FIGURE_GRACE_SEC) \
                and self._memory_hit(t.bbox, kp, scale) is not None:
            need = float(settings.STATIC_FIGURE_GRACE_SEC)
        if still >= need and t.hits >= settings.TRACK_MIN_HITS:
            t.motion_state, t.static_since = "static", now
            self._remember_static(t, now)

    @staticmethod
    def _average_anchor(t: Track, kp, centre, height: float, scale: float) -> None:
        """Fold a still observation into the anchor (running mean of the first few)."""
        n = t._anchor_n
        f = 1.0 / (n + 1)
        cx, cy = t._anchor_centre
        t._anchor_centre = (cx + (centre[0] - cx) * f, cy + (centre[1] - cy) * f)
        t._anchor_height += (height - t._anchor_height) * f
        t._anchor_scale += (scale - t._anchor_scale) * f
        a = t._anchor_kpts
        if kp is not None and a is not None and a.shape == kp.shape:
            thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
            both = (a[:, 2] >= thr) & (kp[:, 2] >= thr)
            a[both, :2] += (kp[both, :2] - a[both, :2]) * f
            # A joint that only now became visible starts from this sample.
            new = (a[:, 2] < thr) & (kp[:, 2] >= thr)
            a[new] = kp[new]
        elif a is None and kp is not None:
            t._anchor_kpts = kp.copy()
        t._anchor_n = n + 1

    @property
    def high_threshold(self) -> float:
        return settings.PERSON_CONF_THRESHOLD

    def update(self, detections: list[Detection], now: Optional[float] = None) -> list[Track]:
        """Associate detections to tracks. Returns the currently live tracks."""
        now = time.time() if now is None else now
        self._expire_memory(now)

        for t in self.tracks.values():
            if t._mean is not None:
                t._mean, t._cov = _KF.predict(t._mean, t._cov)
            t.age += 1

        # A box in shade needs less confidence than one in light
        # (inference_backend.person_threshold).
        high = [d for d in detections if d.confidence >= person_threshold(d, self.high_threshold)]
        low = [d for d in detections if d.confidence < person_threshold(d, self.high_threshold)]

        # Stage 1: every live track against the confident detections.
        pool = list(self.tracks.values())
        matched_tracks: set[str] = set()
        m1 = linear_assignment(_iou_matrix(pool, high), self.iou_threshold)
        used_high = set()
        for r, c in m1:
            self._apply(pool[r], high[c], now)
            matched_tracks.add(pool[r].track_id)
            used_high.add(c)

        # Stage 2: tracks seen on the previous frame get a second chance
        # against low-confidence boxes (occlusion), with a stricter IoU.
        recent = [t for t in pool if t.track_id not in matched_tracks and t.misses == 0]
        for r, c in linear_assignment(_iou_matrix(recent, low), self.low_iou_threshold):
            self._apply(recent[r], low[c], now)
            matched_tracks.add(recent[r].track_id)

        for t in pool:
            if t.track_id not in matched_tracks:
                t.misses += 1

        # Only confident, unexplained detections may start a new identity.
        for ci, d in enumerate(high):
            if ci not in used_high and d.confidence >= person_threshold(d, settings.TRACK_NEW_TRACK_THRESHOLD):
                self._spawn(d, now)

        # Retire tracks that have gone missing for too long.
        for tid in list(self.tracks.keys()):
            t = self.tracks[tid]
            if t.confirmed and t.misses > settings.TRACK_MAX_AGE_FRAMES:
                self._finished.append(self.tracks.pop(tid))
            elif not t.confirmed and t.misses > settings.TRACK_TENTATIVE_MAX_MISSES:
                self.tracks.pop(tid)

        return list(self.tracks.values())

    def _apply(self, t: Track, d: Detection, now: float) -> None:
        z = _xyxy_to_xywh((d.x1, d.y1, d.x2, d.y2))
        if t._mean is None:
            t._mean, t._cov = _KF.initiate(z)
        else:
            t._mean, t._cov = _KF.update(t._mean, t._cov, z)
        t.bbox = (d.x1, d.y1, d.x2, d.y2)
        t.confidence = d.confidence
        t.keypoints = smooth_keypoints(
            t.keypoints, d.keypoints, settings.TRACK_KEYPOINT_EMA,
            scale=max(d.y2 - d.y1, 1.0), arm_alpha=settings.TRACK_KEYPOINT_EMA_ARMS,
        )
        t.last_seen = now
        t.hits += 1
        t.misses = 0
        self._observe_motion(t, now)

    def _spawn(self, d: Detection, now: float) -> None:
        tid = f"trk_{uuid.uuid4().hex[:10]}"
        mean, cov = _KF.initiate(_xyxy_to_xywh((d.x1, d.y1, d.x2, d.y2)))
        self.tracks[tid] = Track(
            track_id=tid,
            camera_id=self.camera_id,
            bbox=(d.x1, d.y1, d.x2, d.y2),
            confidence=d.confidence,
            first_seen=now,
            last_seen=now,
            keypoints=None if d.keypoints is None else np.asarray(d.keypoints, dtype=np.float32).copy(),
            _mean=mean,
            _cov=cov,
        )
        self._observe_motion(self.tracks[tid], now)

    def drain_finished(self) -> list[Track]:
        """Take the tracks that have ended, for persistence."""
        out, self._finished = self._finished, []
        return out

    def flush_all(self) -> list[Track]:
        """End every live track, e.g. when a camera goes offline."""
        out = [t for t in self.tracks.values() if t.confirmed]
        self.tracks.clear()
        out.extend(self.drain_finished())
        return out


# Name kept for existing callers.
CentroidTracker = ByteTracker


class FloorProjector:
    """Projects image points into store metres via a per-camera homography.

    The homography is a 3x3 matrix mapping image pixels to floor metres,
    established by the operator clicking four known floor points in the camera
    view and their positions on the blueprint. Without it, a camera's pixels
    have no defined relationship to the store, so this returns None rather
    than fabricating a location.

    The matrix is in pixels of the frame it was authored on
    (``calibration_points.frame_width/height``). Points are given in pixels
    of the frame the camera delivers now (``frame_geometry.frame_sizes``, or
    an explicit ``frame_size``) and are scaled to the authored size first, so
    a calibration stays valid when the delivered size changes (a GPU
    downscale, a different stream). Without a known authored or delivered
    size nothing is scaled, as before.
    """

    def __init__(self):
        self._matrices: dict[str, np.ndarray] = {}
        self._authored: dict[str, tuple[int, int]] = {}

    def set_homography(self, camera_id: str, matrix: Optional[list],
                       frame_size: Optional[tuple] = None) -> None:
        """Install (or with no matrix, remove) a camera's homography.

        ``frame_size`` is the (width, height) the image points were authored
        at; None keeps a size recorded earlier for the same camera.
        """
        if not matrix:
            self._matrices.pop(camera_id, None)
            self._authored.pop(camera_id, None)
            return
        try:
            m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
            if not np.isfinite(m).all() or abs(np.linalg.det(m)) < 1e-12:
                logger.warning(f"Camera {camera_id} has a degenerate homography; ignoring it")
                self._matrices.pop(camera_id, None)
                return
            self._matrices[camera_id] = m
        except Exception as e:
            logger.warning(f"Camera {camera_id} homography rejected: {e}")
            self._matrices.pop(camera_id, None)
            return
        if frame_size is not None:
            self.set_authored_size(camera_id, *frame_size)

    def set_authored_size(self, camera_id: str, width, height) -> None:
        try:
            w, h = int(width or 0), int(height or 0)
        except (TypeError, ValueError):
            w = h = 0
        if w > 0 and h > 0:
            self._authored[camera_id] = (w, h)
        else:
            self._authored.pop(camera_id, None)

    def authored_size(self, camera_id: str) -> Optional[tuple[int, int]]:
        return self._authored.get(camera_id)

    def has_homography(self, camera_id: str) -> bool:
        return camera_id in self._matrices

    def to_floor(self, camera_id: str, x: float, y: float,
                 frame_size: Optional[tuple] = None) -> Optional[tuple[float, float]]:
        """Floor metres of pixel (x, y) of the delivered frame (or of ``frame_size``)."""
        m = self._matrices.get(camera_id)
        if m is None:
            return None
        authored = self._authored.get(camera_id)
        if authored is not None:
            from app.services.frame_geometry import frame_sizes, scale_point

            x, y = scale_point(x, y, frame_size or frame_sizes.delivered(camera_id), authored)
        vec = m @ np.array([x, y, 1.0], dtype=np.float64)
        w = vec[2]
        if abs(w) < 1e-9:
            return None
        fx, fy = float(vec[0] / w), float(vec[1] / w)
        if not (math.isfinite(fx) and math.isfinite(fy)):
            return None
        return fx, fy

    @staticmethod
    def estimate_homography(
        image_points: list[tuple[float, float]],
        floor_points: list[tuple[float, float]],
    ) -> Optional[list[list[float]]]:
        """Solve for H from >=4 point correspondences using the DLT.

        Returns a row-major 3x3 suitable for storing on the camera record.
        """
        if len(image_points) < 4 or len(image_points) != len(floor_points):
            return None
        A = []
        for (u, v), (X, Y) in zip(image_points, floor_points):
            A.append([-u, -v, -1, 0, 0, 0, u * X, v * X, X])
            A.append([0, 0, 0, -u, -v, -1, u * Y, v * Y, Y])
        try:
            _, _, Vh = np.linalg.svd(np.asarray(A, dtype=np.float64))
            h = Vh[-1]
            if abs(h[8]) < 1e-12:
                return None
            H = (h / h[8]).reshape(3, 3)
            if not np.isfinite(H).all():
                return None
            return H.tolist()
        except np.linalg.LinAlgError:
            return None


floor_projector = FloorProjector()
