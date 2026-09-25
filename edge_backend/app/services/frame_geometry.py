"""Frame sizes per camera, so pixel geometry survives a change of delivered size.

A camera's frames reach the pipeline at its *delivered* size: the stream's
native size, or smaller when the GPU scales it down (DECODE_MAX_WIDTH, or the
camera's own ``decode_max_width``). Almost every piece of per-camera geometry
is stored normalised 0..1 (zones, tripwires, privacy masks, product zones,
heatmap paths) and needs nothing. A few things are in pixels of the frame
they were authored on and must be scaled to the delivered frame:

* the calibration homography (image pixels -> floor metres), authored at the
  size recorded in ``calibration_points.frame_width/height``;
* legacy privacy masks stored in native pixels (coordinates above 1);
* the detector's minimum person-box size (``PERSON_MIN_BOX_PIXELS``), which is
  meant in native pixels so a downscaled stream keeps the people it had.

The camera worker records both sizes here after its first frame; readers
treat an unknown size as "not scaled" (the behaviour before downscaling).
"""

from __future__ import annotations

import threading
from typing import Optional

Size = tuple[int, int]


def _valid(size) -> Optional[Size]:
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def scale_point(x: float, y: float, from_size: Optional[Size], to_size: Optional[Size]) -> tuple[float, float]:
    """(x, y) in pixels of ``from_size`` expressed in pixels of ``to_size`` (unchanged if either is unknown)."""
    f, t = _valid(from_size) if from_size else None, _valid(to_size) if to_size else None
    if f is None or t is None or f == t:
        return float(x), float(y)
    return float(x) * t[0] / f[0], float(y) * t[1] / f[1]


def calibration_frame_size(calibration_points) -> Optional[Size]:
    """The (width, height) a stored calibration was authored at, or None if not recorded."""
    if not isinstance(calibration_points, dict):
        return None
    return _valid((calibration_points.get("frame_width"), calibration_points.get("frame_height")))


class FrameSizes:
    """Delivered and native frame size per camera, set by its worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._delivered: dict[str, Size] = {}
        self._native: dict[str, Size] = {}

    def set(self, camera_id: str, delivered: Optional[Size], native: Optional[Size] = None) -> None:
        d, n = _valid(delivered) if delivered else None, _valid(native) if native else None
        with self._lock:
            if d is not None:
                self._delivered[camera_id] = d
            if n is not None:
                self._native[camera_id] = n
            elif d is not None:
                self._native.setdefault(camera_id, d)

    def delivered(self, camera_id: str) -> Optional[Size]:
        with self._lock:
            return self._delivered.get(camera_id)

    def native(self, camera_id: str) -> Optional[Size]:
        with self._lock:
            return self._native.get(camera_id)

    def downscale(self, camera_id: Optional[str]) -> float:
        """Delivered width / native width (<= 1; 1.0 when unknown or not scaled)."""
        if not camera_id:
            return 1.0
        with self._lock:
            d, n = self._delivered.get(camera_id), self._native.get(camera_id)
        if d is None or n is None or n[0] <= 0:
            return 1.0
        return min(1.0, d[0] / float(n[0]))

    def forget(self, camera_id: str) -> None:
        with self._lock:
            self._delivered.pop(camera_id, None)
            self._native.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._delivered.clear()
            self._native.clear()


frame_sizes = FrameSizes()
