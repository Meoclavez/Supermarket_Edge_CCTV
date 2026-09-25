"""Measured lighting per camera, and an optional local-contrast enhancement.

What the measurements say. On COCO persons degraded to the site's conditions
(352x288 and 704x576; -2/-3/-5 EV with sensor noise and JPEG; half-frame
shade, vignette, spotlight pools; IR-like grayscale with near-field glare),
every enhancement tried LOWERED yolo26n-pose recall at the same threshold:
CLAHE (clip 2 and 3), global gamma and a local-mean adaptive gamma, e.g. -3 EV
0.36 -> 0.26, shade (dark region) 0.41 -> 0.33, IR 0.40 -> 0.34, -5 EV
0.25 -> 0.09. Brightening amplifies sensor noise and JPEG blocking that the
model (trained with brightness augmentation) otherwise ignores. So the
enhancement is off by default (``LOW_LIGHT_ENHANCE``); what does help is the
per-box threshold in ``inference_backend.person_threshold``. The lighting
state is still measured and reported, and ``enhance_for_detection`` stays
available for a viewer or a night-watch feature that wants it.

Why local, not global, if it is used. A store camera often sees a well-lit aisle and a
shaded corner in the same frame. A global gamma or brightness lift washes out
the lit part to recover the dark one; CLAHE on the luminance channel
(contrast-limited adaptive histogram equalisation over a tile grid) lifts
each region by its own histogram and leaves colour (chroma) untouched.

Decided from the picture, never from the clock. Every inferred frame is
measured (a strided sample of a few thousand pixels, ~0.1 ms): mean and
percentiles of luminance, the share of dark pixels and the mean colour
saturation. A Dahua camera in IR night mode sends a near-grayscale picture
(saturation close to zero), which is how ``ir`` is recognised. States:

    day        normal exposure; the frame goes to the model unchanged
    mixed      normally exposed overall but a large dark region (shade,
               unlit aisle, vignetting): local contrast on the luminance
    low_light  the whole frame is dim (dusk, lights off, under-exposed)
    ir         IR night mode: grayscale picture

Switching needs the new state to be measured on ``LOW_LIGHT_HYSTERESIS_FRAMES``
consecutive frames, and the thresholds have a dead band, so a camera does
not flicker between states as people walk past or lights flicker.

Cost. The enhancement runs on the image the model sees (already resized to
the network input, e.g. 640x524 or 960x540), so it costs the same for a
352x288 sub-stream and a 2560x1440 main stream, and only on frames admitted
for inference. Measured single-threaded on a Ryzen 7 7435HS: ~2-4 ms.

Nothing is invented here: the enhancement only redistributes the contrast of
pixels the camera captured. It is reusable (``enhance_for_detection``) by any
caller that wants the same treatment, for example a motion-gated night watch.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.config import settings

STATES = ("day", "mixed", "low_light", "ir")


@dataclass(frozen=True)
class LightStats:
    """Luminance statistics of one frame (0..255 scale)."""

    mean: float
    p10: float
    p50: float
    p90: float
    dark_fraction: float      # share of pixels with luma < LOW_LIGHT_DARK_LEVEL
    saturation: float         # mean HSV-style saturation, 0..255

    def to_dict(self) -> dict:
        return {k: round(float(getattr(self, k)), 3) for k in
                ("mean", "p10", "p50", "p90", "dark_fraction", "saturation")}


def measure(img: np.ndarray, samples: int = 4096) -> LightStats:
    """Luminance / saturation statistics from a strided pixel sample.

    Cheap regardless of the frame size: at most ~``samples`` pixels are read.
    """
    h, w = img.shape[:2]
    step = max(1, int(np.sqrt(h * w / float(samples))))
    s = img[step // 2::step, step // 2::step].reshape(-1, img.shape[2] if img.ndim == 3 else 1)
    s = s.astype(np.float32)
    if s.shape[1] >= 3:
        b, g, r = s[:, 0], s[:, 1], s[:, 2]
        y = 0.114 * b + 0.587 * g + 0.299 * r
        mx = np.maximum(np.maximum(b, g), r)
        mn = np.minimum(np.minimum(b, g), r)
        sat = float(np.mean((mx - mn) / np.maximum(mx, 1.0) * 255.0))
    else:
        y = s[:, 0]
        sat = 0.0
    p10, p50, p90 = (float(v) for v in np.percentile(y, (10, 50, 90)))
    dark = float(np.mean(y < float(settings.LOW_LIGHT_DARK_LEVEL)))
    return LightStats(float(y.mean()), p10, p50, p90, dark, sat)


def classify(stats: LightStats, previous: Optional[str] = None) -> str:
    """The lighting state these statistics indicate.

    ``previous`` moves each threshold by ``LOW_LIGHT_DEAD_BAND`` (relative)
    in the direction that keeps the current state, so a frame near a
    boundary does not switch it.
    """
    band = max(0.0, float(settings.LOW_LIGHT_DEAD_BAND))

    def widen(state: str) -> float:
        return 1.0 + band if previous == state else 1.0

    # IR night mode: no colour at all. Checked first: a grayscale picture is
    # enhanced whatever its mean (IR glare next to a dark far field).
    if stats.saturation < float(settings.LOW_LIGHT_IR_SATURATION) * widen("ir"):
        return "ir"
    if stats.mean < float(settings.LOW_LIGHT_MEAN_LEVEL) * widen("low_light"):
        return "low_light"
    if stats.dark_fraction > float(settings.LOW_LIGHT_MIXED_DARK_FRACTION) / widen("mixed"):
        return "mixed"
    return "day"


def _clahe(clip: float, tiles: int):
    import cv2

    return cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(int(tiles), int(tiles)))


# One CLAHE object per thread and setting: camera workers call in parallel.
_tls = threading.local()


def _thread_clahe(key: tuple[float, int]):
    cache = getattr(_tls, "clahe", None)
    if cache is None:
        cache = _tls.clahe = {}
    c = cache.get(key)
    if c is None:
        c = cache[key] = _clahe(*key)
    return c


def enhance_for_detection(img: np.ndarray, state: str) -> np.ndarray:
    """Local-contrast enhancement for ``state`` (a new array), or ``img`` itself for ``day``.

    CLAHE on the luminance channel only (YCrCb), so shaded regions are lifted
    by their own histogram and lit regions keep their contrast; chroma is not
    touched. An IR (grayscale) frame is equalised the same way.
    """
    import cv2

    if state not in ("mixed", "low_light", "ir") or img is None or img.size == 0:
        return img
    clip = float(settings.LOW_LIGHT_CLAHE_CLIP)
    tiles = max(1, int(settings.LOW_LIGHT_CLAHE_TILES))
    clahe = _thread_clahe((round(clip, 3), tiles))
    if img.ndim == 2:
        return clahe.apply(img)
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    ycc[..., 0] = clahe.apply(np.ascontiguousarray(ycc[..., 0]))
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)


def mode() -> str:
    m = (settings.LOW_LIGHT_ENHANCE or "off").strip().lower()
    return m if m in ("auto", "always") else "off"


@dataclass
class _CameraLight:
    state: str = "day"
    candidate: Optional[str] = None
    streak: int = 0
    since: float = field(default_factory=time.time)
    stats: Optional[LightStats] = None
    frames: int = 0
    enhanced: int = 0


class LightingMonitor:
    """Per-camera lighting state with hysteresis, and what was done about it.

    ``observe()`` is called with the image the detector is about to infer; it
    returns the state to act on. A call without a camera id is classified on
    its own (no hysteresis) and not recorded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cams: dict[str, _CameraLight] = {}

    def observe(self, img: np.ndarray, camera_id: Optional[str] = None) -> tuple[str, LightStats]:
        stats = measure(img)
        if camera_id is None:
            return classify(stats), stats
        need = max(1, int(settings.LOW_LIGHT_HYSTERESIS_FRAMES))
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                # First frame of a camera: take its state at once.
                c = self._cams[camera_id] = _CameraLight(state=classify(stats))
            else:
                seen = classify(stats, previous=c.state)
                if seen == c.state:
                    c.candidate, c.streak = None, 0
                elif seen == c.candidate:
                    c.streak += 1
                else:
                    c.candidate, c.streak = seen, 1
                if c.candidate is not None and c.streak >= need:
                    c.state, c.candidate, c.streak, c.since = c.candidate, None, 0, time.time()
            c.stats = stats
            c.frames += 1
            return c.state, stats

    def mark_enhanced(self, camera_id: Optional[str]) -> None:
        if camera_id is None:
            return
        with self._lock:
            c = self._cams.get(camera_id)
            if c is not None:
                c.enhanced += 1

    def camera_status(self, camera_id: str) -> Optional[dict]:
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                return None
            return {
                "lighting": c.state,
                "since": c.since,
                "enhancing": mode() == "always" or (mode() == "auto" and c.state != "day"),
                "frames_measured": c.frames,
                "frames_enhanced": c.enhanced,
                "stats": c.stats.to_dict() if c.stats else None,
            }

    def status(self) -> dict:
        with self._lock:
            ids = list(self._cams)
        cams = {cid: self.camera_status(cid) for cid in ids}
        counts = {s: 0 for s in STATES}
        for v in cams.values():
            if v:
                counts[v["lighting"]] += 1
        return {"enhance": mode(),
                "method": f"CLAHE on luminance (clip {settings.LOW_LIGHT_CLAHE_CLIP}, "
                          f"{settings.LOW_LIGHT_CLAHE_TILES}x{settings.LOW_LIGHT_CLAHE_TILES} tiles)",
                "cameras_by_state": counts, "cameras": cams}

    def forget(self, camera_id: str) -> None:
        with self._lock:
            self._cams.pop(camera_id, None)


lighting_monitor = LightingMonitor()
