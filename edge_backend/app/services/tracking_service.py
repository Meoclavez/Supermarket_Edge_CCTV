"""Multi-object tracking and floor projection.

Detections alone cannot answer any retail question. "How many people came in"
needs identity across frames, and "how long did they spend at the bakery" needs
those identities placed on the store floor rather than in camera pixels.

This module supplies both:

* ``CentroidTracker`` -- IoU-based association with a short memory, which is
  enough to hold an identity through the brief occlusions typical of aisle
  footage without the cost of a full appearance model.
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
from app.services.inference_backend import Detection

logger = logging.getLogger(__name__)


@dataclass
class Track:
    """One person followed across frames of a single camera."""

    track_id: str
    camera_id: str
    bbox: tuple[float, float, float, float]
    confidence: float
    first_seen: float
    last_seen: float
    hits: int = 1
    misses: int = 0
    # Trajectory in floor metres, appended only while a homography exists.
    floor_points: list[dict] = field(default_factory=list)
    # Zone the track is currently inside, and when it entered.
    current_zone_id: Optional[str] = None
    zone_entered_at: Optional[float] = None
    # Set once this track's dwell in the current zone has been published.
    open_visit_id: Optional[str] = None
    interacted: bool = False

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


class CentroidTracker:
    """Greedy IoU association tracker, one instance per camera."""

    def __init__(self, camera_id: str, iou_threshold: float = 0.3):
        self.camera_id = camera_id
        self.iou_threshold = iou_threshold
        self.tracks: dict[str, Track] = {}
        self._finished: list[Track] = []

    def update(self, detections: list[Detection], now: Optional[float] = None) -> list[Track]:
        """Associate detections to tracks. Returns the currently live tracks."""
        now = now or time.time()

        if not self.tracks:
            for d in detections:
                self._spawn(d, now)
            return list(self.tracks.values())

        track_ids = list(self.tracks.keys())
        # Score every (track, detection) pair, then take matches greedily by
        # descending IoU so the strongest overlap claims its detection first.
        pairs: list[tuple[float, str, int]] = []
        for tid in track_ids:
            for di, d in enumerate(detections):
                score = _iou(self.tracks[tid].bbox, (d.x1, d.y1, d.x2, d.y2))
                if score >= self.iou_threshold:
                    pairs.append((score, tid, di))
        pairs.sort(reverse=True)

        claimed_tracks: set[str] = set()
        claimed_dets: set[int] = set()
        for score, tid, di in pairs:
            if tid in claimed_tracks or di in claimed_dets:
                continue
            claimed_tracks.add(tid)
            claimed_dets.add(di)
            d = detections[di]
            t = self.tracks[tid]
            t.bbox = (d.x1, d.y1, d.x2, d.y2)
            t.confidence = d.confidence
            t.last_seen = now
            t.hits += 1
            t.misses = 0

        for tid in track_ids:
            if tid not in claimed_tracks:
                self.tracks[tid].misses += 1

        for di, d in enumerate(detections):
            if di not in claimed_dets:
                self._spawn(d, now)

        # Retire tracks that have gone missing for too long.
        for tid in list(self.tracks.keys()):
            if self.tracks[tid].misses > settings.TRACK_MAX_AGE_FRAMES:
                t = self.tracks.pop(tid)
                if t.confirmed:
                    self._finished.append(t)

        return list(self.tracks.values())

    def _spawn(self, d: Detection, now: float) -> None:
        tid = f"trk_{uuid.uuid4().hex[:10]}"
        self.tracks[tid] = Track(
            track_id=tid,
            camera_id=self.camera_id,
            bbox=(d.x1, d.y1, d.x2, d.y2),
            confidence=d.confidence,
            first_seen=now,
            last_seen=now,
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


class FloorProjector:
    """Projects image points into store metres via a per-camera homography.

    The homography is a 3x3 matrix mapping image pixels to floor metres,
    established by the operator clicking four known floor points in the camera
    view and their positions on the blueprint. Without it, a camera's pixels
    have no defined relationship to the store, so this returns None rather
    than fabricating a location.
    """

    def __init__(self):
        self._matrices: dict[str, np.ndarray] = {}

    def set_homography(self, camera_id: str, matrix: Optional[list]) -> None:
        if not matrix:
            self._matrices.pop(camera_id, None)
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

    def has_homography(self, camera_id: str) -> bool:
        return camera_id in self._matrices

    def to_floor(self, camera_id: str, x: float, y: float) -> Optional[tuple[float, float]]:
        m = self._matrices.get(camera_id)
        if m is None:
            return None
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
