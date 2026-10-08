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
* and when it still differs after the background is scaled by the median
  brightness ratio of the LOCAL_GAIN_WINDOW pixels around it: light through
  a door or window, or from a sign, changes one area of the picture at once;
* pixels that change much of the time, or again and again, are
  "flickering" and do not count: per pixel, the share of the last
  FLICKER_TAU_SEC it was changed (its duty) above FLICKER_DUTY, or at least
  FLICKER_ONSETS separate changes in that time, widened by FLICKER_GROW
  pixels. On the store box (32 D1 cameras, store closed) these were the
  cameras' burnt-in clock (the seconds digits change every second and,
  absorbed at ALPHA_CHANGED, stay changed ~4 s, so they were changed all
  the time: 5-12 thumbnail pixels on the IPC overlay, 2-5 on the
  recorder's), lit signs and a TV cycling their pictures, and glass doors
  and windows lit by the street; a person walking through changes a pixel
  once. The widening covers the clock's minute digits next to its
  flickering seconds. The map is learnt per view (faster in the first
  FLICKER_TAU_SEC) and kept across reset();
* motion = a group (8-neighbour, gaps of one pixel bridged) of at least
  MOTION_GATE_MIN_PIXELS changed, non-flickering pixels outside the camera's
  AI_IGNORE areas on MOTION_GATE_PERSIST frames in a row (one noisy frame, a
  key-frame pulse, scattered single pixels are not motion);
* more than LIGHTING_FRACTION of the picture changing at once (lights, IR
  switch) re-learns the background instead of reporting motion.

Changed pixels learn the background slowly and still ones fast, so a walking
person stays "moving" while a parked trolley is absorbed within seconds.
Replayed at 10 frames/s on those cameras' own night frames
(tests/test_motion_gate_store.py), the first deploy's rules kept them
active 83-98 % of the time (1.5-2.5 wakes a minute); these 0-3 % (0-0.5),
and a person walking in still wakes the camera within 0.1-0.3 s.
Measured cost per 704 x 576 frame (tests/test_analysis_rate_settings.py,
dev box): ~95 us from NV12 (the GPU path), ~120-140 us from BGR, i.e. 32
cameras x 10 fps cost ~3-4 % of one core.
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
# Local brightness: a pixel also has to differ from the background scaled by
# the median brightness ratio of the LOCAL_GAIN_WINDOW x LOCAL_GAIN_WINDOW
# pixels around it (LOCAL_GAIN_FLOOR grey levels added to both, so near-black
# pixels do not give huge ratios). Light from the street through a glass door
# dimmed half of one store camera's picture by 10-40 %, smoothly, every ~35 s.
LOCAL_GAIN_WINDOW = 5
LOCAL_GAIN_FLOOR = 8.0
# Flicker model: per pixel, the share of time it was changed (exponential
# average over FLICKER_TAU_SEC of real time, a running mean while the gate
# has seen less than that); above FLICKER_DUTY it is flickering and ignored,
# with FLICKER_GROW pixels around it (horizontally, FLICKER_GROW_ROWS
# vertically: a clock's digits sit in a row). A person crossing a pixel keeps
# it changed ~1-5 s, i.e. 0.3-2 % of 5 minutes; the clock's seconds 100 %,
# its tens of seconds ~40 %, a sign cycling its pictures or a door lit by
# passing cars 10-60 %.
FLICKER_TAU_SEC = 300.0
FLICKER_MIN_TAU_SEC = 10.0
FLICKER_DUTY = 0.08
# A pixel that changes briefly but again and again is flickering too: at
# least FLICKER_ONSETS separate changes (each after FLICKER_REARM_SEC
# unchanged) in about the last FLICKER_TAU_SEC (decaying count). The street
# light through the store's glass changed a thin light edge for ~1 s every
# ~35 s (a duty of only 2-3 %); a clock's minute digit changes once a
# minute; a person walking by changes a pixel once or twice.
FLICKER_ONSETS = 6.0
FLICKER_REARM_SEC = 1.0
FLICKER_GROW = 2
FLICKER_GROW_ROWS = 1
# Changed pixels at most one pixel apart (gap) form one group.
_GROUP_KERNEL = np.ones((3, 3), np.uint8)
# Longest real time one frame stands for in the duty average (a stalled feed).
FLICKER_MAX_DT_SEC = 1.0


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
        # Flicker model (FLICKER_*): per-pixel share of time changed, the time
        # of the last frame folded in, and how long it has been learning.
        self._duty: Optional[np.ndarray] = None
        self._duty_t: Optional[float] = None
        self._duty_age = 0.0
        # ... and per pixel the (decaying) count of separate changes, and when
        # it was last changed.
        self._onsets: Optional[np.ndarray] = None
        self._last_on: Optional[np.ndarray] = None
        self.changed_pixels = 0
        self.flicker_pixels = 0
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
        """Re-learn the background (reconnect, gate switched on).

        The flicker map is kept: the same view's clock and signs flicker on.
        """
        self._bg = None
        self._streak = 0
        self._duty_t = None

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

    def _flicker(self, changed: np.ndarray, now: float) -> np.ndarray:
        """Fold this frame's changed pixels into the duty map; the flickering pixels (grown)."""
        import cv2

        if self._duty is None or self._duty.shape != changed.shape:
            self._duty, self._duty_age = np.zeros(changed.shape, np.float32), 0.0
            self._onsets = np.zeros(changed.shape, np.float32)
            self._last_on = np.full(changed.shape, -np.inf)
        dt = 0.0 if self._duty_t is None else min(max(now - self._duty_t, 0.0), FLICKER_MAX_DT_SEC)
        self._duty_t = now
        if dt > 0.0:
            self._duty_age += dt
            tau = min(FLICKER_TAU_SEC, max(FLICKER_MIN_TAU_SEC, self._duty_age))
            a = 1.0 - float(np.exp(-dt / tau))
            cv2.accumulateWeighted(changed.astype(np.float32), self._duty, a)
            self._onsets *= float(np.exp(-dt / FLICKER_TAU_SEC))
        if changed.any():
            # A new change: the pixel was not changed for FLICKER_REARM_SEC
            # (a change flickering at the threshold is one change).
            onset = changed & (now - self._last_on > FLICKER_REARM_SEC)
            self._last_on[changed] = now
            self._onsets[onset] = np.minimum(self._onsets[onset] + 1.0, 2.0 * FLICKER_ONSETS)
        return self.flicker_mask()

    def flicker_mask(self) -> np.ndarray:
        """The pixels currently treated as flickering (grown), as a HEIGHT x WIDTH bool array."""
        import cv2

        if self._duty is None:
            return np.zeros((HEIGHT, WIDTH), bool)
        flicker = ((self._duty > FLICKER_DUTY) | (self._onsets >= FLICKER_ONSETS)).astype(np.uint8)
        if flicker.any() and (FLICKER_GROW > 0 or FLICKER_GROW_ROWS > 0):
            flicker = cv2.dilate(flicker, np.ones((2 * FLICKER_GROW_ROWS + 1, 2 * FLICKER_GROW + 1), np.uint8))
        return flicker.astype(bool)

    def _local_change(self, g: np.ndarray, expected: np.ndarray) -> np.ndarray:
        """Pixels still changed after this area's own brightness change is removed.

        The brightness ratio of thumbnail to (fitted) background, its median
        over LOCAL_GAIN_WINDOW x LOCAL_GAIN_WINDOW pixels: light through a
        window or door, a sign lighting part of the shop, scales a whole area
        smoothly, while a person changes a few pixels of it.
        """
        import cv2

        ratio = (g + LOCAL_GAIN_FLOOR) / (np.maximum(expected, 0.0) + LOCAL_GAIN_FLOOR)
        code = np.clip(np.round(128.0 + 64.0 * np.log2(ratio)), 0, 255).astype(np.uint8)
        local = np.exp2((cv2.medianBlur(code, LOCAL_GAIN_WINDOW).astype(np.float32) - 128.0) / 64.0)
        resid = g - ((np.maximum(expected, 0.0) + LOCAL_GAIN_FLOOR) * local - LOCAL_GAIN_FLOOR)
        return np.abs(resid) > self.threshold

    def _largest_group(self, counted: np.ndarray) -> int:
        """Pixels of ``counted`` in its largest group (8-connected, gaps of one pixel bridged)."""
        import cv2

        n = int(np.count_nonzero(counted))
        if n < 2:
            return n
        # Pixels one apart belong together: a small or low-contrast person
        # changes a few pixels with gaps between them.
        c = counted.astype(np.uint8)
        k, labels = cv2.connectedComponents(cv2.dilate(c, _GROUP_KERNEL), connectivity=8)
        if k <= 2:
            return n
        return int(np.bincount(labels[counted], minlength=k)[1:].max())

    def update(self, frame, now: Optional[float] = None) -> bool:
        """Feed one decoded frame; True when motion is confirmed on it.

        ``now`` (seconds) times the flicker average; without it the monotonic clock.
        """
        import cv2

        t0 = time.perf_counter()
        clock = time.monotonic() if now is None else float(now)
        try:
            g = _luma_small(frame).astype(np.float32)
            if self._bg is None or self._bg.shape != g.shape:
                self._bg, self._streak = g, 0
                self._duty_t = clock
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
                self._duty_t = clock
                self.lighting_events += 1
                self.changed_pixels = 0
                return False
            if changed.any() and LOCAL_GAIN_WINDOW > 1:
                # Light changing over one area only (a door, a window, a sign).
                changed &= self._local_change(g, g - diff + shift)
            # Clock digits, signs, screens, lit glass: changed much of the time.
            flicker = self._flicker(changed, clock)
            self.flicker_pixels = int(np.count_nonzero(flicker))
            counted = changed & ~flicker
            if self._valid is not None:
                counted &= self._valid
            self.changed_pixels = n = self._largest_group(counted)
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
            "flicker_pixels": self.flicker_pixels,
            "lighting_events": self.lighting_events,
            "last_motion": self.last_motion,
        }
