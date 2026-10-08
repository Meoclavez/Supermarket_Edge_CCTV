"""Motion gate: did anything move on this camera since the last frames?

The inference scheduler gives a camera with no people and no motion only
ANALYTICS_IDLE_DETECT_FPS. Someone walking into such a view must not wait up
to 1 / idle rate for the pose model to see them, so every decoded frame goes
through this gate first, and motion wakes the camera to its full share
(``inference_scheduler.motion``) with its next frame due at once.

It has to cost next to nothing on 32 cameras at 10 fps, so it never touches a
full frame: a GPU frame's luma plane (NV12, no colour conversion) or a BGR
frame is sampled on a 192 x 144 grid (nearest pixel, so only those pixels are
read) and averaged 3 x 3 down to a 64 x 48 grey thumbnail (which also averages
away sensor noise).
The thumbnail is compared with a slowly learnt background:

* a thumbnail pixel changed when it differs from the background by more than
  MOTION_GATE_THRESHOLD grey levels, after fitting the background's
  brightness to the thumbnail (gain and offset, least squares) and removing
  the median change (lights and auto exposure scale and shift every pixel at
  once);
* motion = at least MOTION_GATE_MIN_PIXELS changed pixels outside the
  camera's AI_IGNORE areas on MOTION_GATE_PERSIST frames in a row (one noisy
  frame, a key-frame pulse, is not motion);
* more than LIGHTING_FRACTION of the picture changing at once (lights, IR
  switch) re-learns the background instead of reporting motion.

Changed pixels learn the background slowly and still ones fast, so a walking
person stays "moving" while a parked trolley is absorbed within seconds.
Measured cost per 704 x 576 frame (tests/test_analysis_rate_settings.py,
dev box, other tests running): ~55-75 us from NV12 (the GPU path), ~80-105 us
from BGR, i.e. 32 cameras x 10 fps cost ~2-3 % of one core.
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from app.config import settings

WIDTH, HEIGHT = 64, 48
# Pixels averaged per thumbnail pixel, per axis (sensor noise / sqrt(9)).
SAMPLE = 3
# More than this share of the thumbnail changing at once is a lighting event.
LIGHTING_FRACTION = 0.45
# Background learning rate per frame: still pixels, changed pixels.
ALPHA_STILL = 0.10
ALPHA_CHANGED = 0.03


def _luma_small(frame) -> np.ndarray:
    """A 64 x 48 uint8 grey thumbnail of a Frame (NV12 luma or BGR) or an array."""
    import cv2

    src = frame
    if hasattr(frame, "gray") and hasattr(frame, "converted"):
        # capture_backends.Frame: the NV12 luma plane is a free view; a frame
        # already converted to BGR is read as BGR (no extra conversion).
        src = frame.bgr() if frame.converted else frame.gray()
    src = np.asarray(src)
    # Pick a SAMPLE x SAMPLE grid of pixels per thumbnail pixel (nearest:
    # reads only those), then average them (INTER_AREA). Averaging the whole
    # frame cost ~100 us at 704x576 and a strided BGR view ~170 us; this
    # reads 1/27 of a D1 frame's pixels.
    grid = cv2.resize(src, (WIDTH * SAMPLE, HEIGHT * SAMPLE), interpolation=cv2.INTER_NEAREST)
    if grid.ndim == 3:
        grid = cv2.cvtColor(grid, cv2.COLOR_BGR2GRAY)
    return cv2.resize(grid, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)


class MotionGate:
    """Per-camera frame-difference motion check on a 64 x 48 thumbnail."""

    def __init__(self, threshold: Optional[float] = None, min_pixels: Optional[int] = None,
                 persist: Optional[int] = None) -> None:
        self._threshold = threshold
        self._min_pixels = min_pixels
        self._persist = persist
        self._bg: Optional[np.ndarray] = None
        self._streak = 0
        self._valid: Optional[np.ndarray] = None
        self._valid_key = None
        self.changed_pixels = 0
        self.last_motion: Optional[float] = None
        self.lighting_events = 0
        self.frames = 0
        self.busy_s = 0.0

    @property
    def threshold(self) -> float:
        return float(self._threshold if self._threshold is not None else settings.MOTION_GATE_THRESHOLD)

    @property
    def min_pixels(self) -> int:
        return max(1, int(self._min_pixels if self._min_pixels is not None else settings.MOTION_GATE_MIN_PIXELS))

    @property
    def persist(self) -> int:
        return max(1, int(self._persist if self._persist is not None else settings.MOTION_GATE_PERSIST))

    def reset(self) -> None:
        self._bg = None
        self._streak = 0

    def set_ignore(self, polygons) -> None:
        """AI_IGNORE polygons in thumbnail pixels (``ignore_polygons(cam, WIDTH, HEIGHT)``).

        Motion inside them does not count. An empty list removes the mask.
        """
        polys = [np.asarray(p, dtype=np.int32).reshape(-1, 2) for p in (polygons or []) if len(p) >= 3]
        key = tuple(p.tobytes() for p in polys)
        if key == self._valid_key:
            return
        self._valid_key = key
        if not polys:
            self._valid = None
            return
        import cv2

        valid = np.ones((HEIGHT, WIDTH), np.uint8)
        cv2.fillPoly(valid, polys, 0)
        self._valid = valid.astype(bool)

    def _fit(self, g: np.ndarray, where: Optional[np.ndarray]) -> np.ndarray:
        """The background scaled and shifted to match ``g`` on the ``where`` pixels (gain 0.5-2)."""
        bg = self._bg
        b, x = (bg, g) if where is None or where.sum() < 64 else (bg[where], g[where])
        mb, mg = float(b.mean()), float(x.mean())
        cb = b - mb
        var = float(np.dot(cb.ravel(), cb.ravel()))
        gain = float(np.dot(cb.ravel(), (x - mg).ravel())) / var if var > cb.size else 1.0
        gain = min(2.0, max(0.5, gain))
        return (bg - mb) * gain + mg

    def update(self, frame, now: Optional[float] = None) -> bool:
        """Feed one decoded frame; True when motion is confirmed on it."""
        import cv2

        t0 = time.perf_counter()
        try:
            g = _luma_small(frame).astype(np.float32)
            if self._bg is None or self._bg.shape != g.shape:
                self._bg, self._streak = g, 0
                return False
            # Lights and exposure scale and shift every pixel: compare with the
            # background fitted to this thumbnail (gain and offset, least
            # squares outside the ignore areas, refitted without the pixels
            # that moved), then remove the median change.
            diff = g - self._fit(g, self._valid)
            moved = np.abs(diff) > 2.0 * self.threshold
            if moved.any():
                keep = ~moved if self._valid is None else (~moved & self._valid)
                diff = g - self._fit(g, keep)
            sample = diff[::2, ::2].ravel()
            shift = float(np.partition(sample, sample.size // 2)[sample.size // 2])   # median
            changed = np.abs(diff - shift) > self.threshold
            if changed.mean() > LIGHTING_FRACTION:
                # Lights, IR switch, a camera re-exposing: not motion.
                self._bg, self._streak = g, 0
                self.lighting_events += 1
                self.changed_pixels = 0
                return False
            counted = changed & self._valid if self._valid is not None else changed
            self.changed_pixels = n = int(np.count_nonzero(counted))
            self._streak = self._streak + 1 if n >= self.min_pixels else 0
            mask = changed.astype(np.uint8)
            cv2.accumulateWeighted(g, self._bg, ALPHA_STILL, mask=1 - mask)
            cv2.accumulateWeighted(g, self._bg, ALPHA_CHANGED, mask=mask)
            moved = self._streak >= self.persist
            if moved:
                self.last_motion = time.time() if now is None else now
            return moved
        finally:
            self.frames += 1
            self.busy_s += time.perf_counter() - t0

    def stats(self) -> dict:
        return {
            "frames": self.frames,
            "us_per_frame": round(self.busy_s / self.frames * 1e6, 1) if self.frames else None,
            "changed_pixels": self.changed_pixels,
            "lighting_events": self.lighting_events,
            "last_motion": self.last_motion,
        }
