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


class ByteTracker:
    """Two-stage (ByteTrack-style) association tracker, one instance per camera."""

    def __init__(self, camera_id: str, iou_threshold: Optional[float] = None):
        self.camera_id = camera_id
        self.iou_threshold = settings.TRACK_IOU_THRESHOLD if iou_threshold is None else iou_threshold
        self.low_iou_threshold = settings.TRACK_LOW_IOU_THRESHOLD
        self.tracks: dict[str, Track] = {}
        self._finished: list[Track] = []

    @property
    def high_threshold(self) -> float:
        return settings.PERSON_CONF_THRESHOLD

    def update(self, detections: list[Detection], now: Optional[float] = None) -> list[Track]:
        """Associate detections to tracks. Returns the currently live tracks."""
        now = time.time() if now is None else now

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
