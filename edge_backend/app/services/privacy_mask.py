"""Privacy masks and analysis-exclusion regions for camera frames.

Masks are the per-camera polygons stored by ``ai_zone_service`` as
``exclusion_masks`` (drawn in Studio, served by ``routes/zones.py``). Points
are normalised 0..1 against the camera's frame, so they fit any delivered
size. A legacy polygon whose coordinates exceed 1 is in pixels: of
``frame_width`` x ``frame_height`` when the mask records them, else of the
camera's native stream (``frame_geometry``), and is scaled to the frame.

Semantics, decided by ``mask_mode``:

* ``BLUR`` / ``MOSAIC`` / ``BLACKOUT`` / ``COLOR`` -- **privacy masks**. They
  are burned into every frame the system shows or saves: the MJPEG stream,
  the overlay and calibration feeds, ``/snapshot`` (plain and annotated),
  saved snapshot actions, the clip pre-event buffer and the evidence frames
  handed to pose_analytics. The detector still sees the unmasked frame, so a
  shopper walking through a masked area is still counted; only their image
  is hidden. An unknown mode is treated as ``BLACKOUT`` (fail closed).
* ``AI_IGNORE`` -- an **analysis exclusion** ("ignore area": a poster, a
  mannequin, a screen showing people). The picture is left alone, but any
  person or object whose foot point (bottom-centre of the box) lies inside
  the polygon, OR at least ``ignore_box_fraction`` (default
  ``settings.IGNORE_BOX_FRACTION``, 0.6) of whose box area lies inside it, is
  dropped before tracking, so it never reaches counts, zone visits, shelf
  interactions or theft rules. The area rule catches a wall poster whose
  painted feet are below the drawn area; a shopper walking past in front of
  the area has most of the box outside it and is kept.

If a mask cannot be applied (malformed polygon, OpenCV error) the frame is
blacked out entirely rather than shown unmasked.

Direct live video (go2rtc/WebRTC) does not pass through this process; it
burns the same masks in with an ffmpeg filter instead, and refuses to show a
masked camera if that cannot be done (services/privacy_video.py).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

PRIVACY_MODES = ("BLUR", "MOSAIC", "BLACKOUT", "COLOR")
IGNORE_MODE = "AI_IGNORE"

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


def camera_masks(camera_id: str) -> list[dict]:
    """Enabled mask records for this camera (read live, so edits apply at once)."""
    try:
        from app.services.ai_zone_service import ai_zone_service

        masks = ai_zone_service.get_all_zones(camera_id).get("exclusion_masks", [])
    except Exception as e:  # zone store unreadable: no masks known
        _warn_once("load", f"privacy masks could not be read: {e}")
        return []
    return [m for m in masks if m.get("enabled", True) and len(m.get("points") or []) >= 3]


def _mode(mask: dict) -> str:
    return str(mask.get("mask_mode") or "BLUR").upper()


def _pixel_space(mask: dict) -> Optional[tuple[int, int]]:
    """The frame size a legacy pixel polygon was drawn on, if known."""
    try:
        w, h = int(mask.get("frame_width") or 0), int(mask.get("frame_height") or 0)
        if w > 0 and h > 0:
            return w, h
    except (TypeError, ValueError):
        pass
    cam = mask.get("camera_id")
    if cam:
        from app.services.frame_geometry import frame_sizes

        return frame_sizes.native(str(cam))
    return None


def polygon_pixels(mask: dict, width: int, height: int) -> np.ndarray:
    """Polygon as int32 pixel coordinates in a ``width`` x ``height`` frame."""
    pts = np.array([[float(p["x"]), float(p["y"])] for p in mask["points"]], dtype=np.float64)
    if pts.size and pts.max() <= 1.0:
        pts[:, 0] *= width
        pts[:, 1] *= height
    elif pts.size:
        drawn = _pixel_space(mask)
        if drawn is not None and drawn != (width, height):
            pts[:, 0] *= width / float(drawn[0])
            pts[:, 1] *= height / float(drawn[1])
    pts[:, 0] = np.clip(pts[:, 0], 0, width - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, height - 1)
    return np.round(pts).astype(np.int32)


def _apply_one(out: np.ndarray, mask: dict, mode: str) -> None:
    import cv2

    h, w = out.shape[:2]
    poly = polygon_pixels(mask, w, h)
    if mode in ("BLACKOUT", "COLOR") or mode not in PRIVACY_MODES:
        colour = tuple(int(c) for c in (mask.get("mask_color_bgr") or (0, 0, 0))) if mode == "COLOR" else (0, 0, 0)
        cv2.fillPoly(out, [poly], colour)
        return

    x, y, bw, bh = cv2.boundingRect(poly)
    if bw <= 0 or bh <= 0:
        return
    roi = out[y: y + bh, x: x + bw]
    if mode == "BLUR":
        # Scale the kernel with the region so a large face is not merely soft.
        k = max(int(mask.get("blur_kernel_size") or 51), (min(bw, bh) // 4) | 1)
        k = k if k % 2 == 1 else k + 1
        filtered = _strong_blur(roi, k)
    else:  # MOSAIC
        scale = max(2, int(mask.get("mosaic_scale") or 16))
        small = cv2.resize(roi, (max(1, bw // scale), max(1, bh // scale)), interpolation=cv2.INTER_LINEAR)
        filtered = cv2.resize(small, (bw, bh), interpolation=cv2.INTER_NEAREST)
    region = np.zeros((bh, bw), dtype=np.uint8)
    cv2.fillPoly(region, [poly - np.array([x, y], dtype=np.int32)], 255)
    roi[region > 0] = filtered[region > 0]


# Blur kernels from this size up are applied to a 1/4-size copy of the region
# (kernel / 4) and scaled back: the same "nothing recognisable" result at a
# fraction of the cost (a 1280x720 frame's 512x288 region with its 71 px kernel
# took ~9 ms in one thread, ~1 ms this way), which matters because the mask is
# burned into every snapshot, stream frame and clip frame.
_BLUR_DOWNSCALE_MIN_KERNEL = 15


def _strong_blur(roi: np.ndarray, k: int) -> np.ndarray:
    import cv2

    bh, bw = roi.shape[:2]
    if k < _BLUR_DOWNSCALE_MIN_KERNEL or min(bw, bh) < 16:
        return cv2.GaussianBlur(roi, (k, k), 0)
    small = cv2.resize(roi, (max(1, bw // 4), max(1, bh // 4)), interpolation=cv2.INTER_AREA)
    ks = max(3, (k // 4) | 1)
    small = cv2.GaussianBlur(small, (ks, ks), 0)
    return cv2.resize(small, (bw, bh), interpolation=cv2.INTER_LINEAR)


def apply_privacy_masks(frame: Optional[np.ndarray], camera_id: str, masks: Optional[list[dict]] = None,
                        inplace: bool = False):
    """Return ``frame`` with this camera's privacy masks burned in.

    Returns the input unchanged (same object) when there is nothing to mask;
    otherwise a masked copy, so the caller's frame is never modified
    (``inplace=True``: masked in place, for a caller that owns the array).
    """
    if frame is None:
        return None
    masks = camera_masks(camera_id) if masks is None else masks
    privacy = [m for m in masks if _mode(m) != IGNORE_MODE]
    if not privacy:
        return frame
    out = frame if inplace else frame.copy()
    for m in privacy:
        try:
            _apply_one(out, m, _mode(m))
        except Exception as e:
            _warn_once(f"apply:{camera_id}:{m.get('id')}",
                       f"privacy mask {m.get('id')} on {camera_id} failed ({e}); blacking out the frame")
            out[:] = 0
            return out
    return out


class DeferredMaskedFrame:
    """A frame whose privacy masks are burned in only when a copy is taken.

    The analysis hands every analysed frame to pose_analytics and
    tripwire_engine as their evidence frame, but they keep a copy only when
    something happens (a reach, a crossing, an incident). Masking eagerly
    cost a full copy plus the blur on every analysed frame; this defers both
    to ``copy()``, which returns the masked array exactly as before. Only
    ``shape`` / ``ndim`` / ``dtype`` are readable otherwise.
    """

    __slots__ = ("_frame", "_camera_id", "_masks")

    def __init__(self, frame: np.ndarray, camera_id: str, masks: list[dict]):
        self._frame, self._camera_id, self._masks = frame, camera_id, masks

    @property
    def shape(self):
        return self._frame.shape

    @property
    def ndim(self) -> int:
        return self._frame.ndim

    @property
    def dtype(self):
        return self._frame.dtype

    def copy(self) -> np.ndarray:
        out = apply_privacy_masks(self._frame, self._camera_id, self._masks)
        return out.copy() if out is self._frame else out

    def __array__(self, dtype=None, copy=None):
        out = self.copy()
        return out if dtype is None else out.astype(dtype)


def deferred_privacy_masks(frame: np.ndarray, camera_id: str, masks: Optional[list[dict]] = None):
    """``frame`` itself when the camera has no privacy masks, else a DeferredMaskedFrame."""
    masks = camera_masks(camera_id) if masks is None else masks
    if not any(_mode(m) != IGNORE_MODE for m in masks):
        return frame
    return DeferredMaskedFrame(frame, camera_id, masks)


def ignore_fraction(mask: dict) -> float:
    """The share of a box that must lie inside this AI_IGNORE area to drop it (0.1..1.0)."""
    from app.config import settings

    v = mask.get("ignore_box_fraction")
    try:
        v = float(v) if v is not None else float(settings.IGNORE_BOX_FRACTION)
    except (TypeError, ValueError):
        v = float(settings.IGNORE_BOX_FRACTION)
    if v != v:  # NaN
        v = float(settings.IGNORE_BOX_FRACTION)
    return min(1.0, max(0.1, v))


class IgnoreRegion(np.ndarray):
    """An AI_IGNORE polygon: a float32 (N, 2) pixel array that also carries its rule.

    It is an ndarray (OpenCV, shadow_trial and older callers use it as the
    polygon itself) with two attributes: ``box_fraction`` (drop a box with at
    least this share of its area inside) and ``mask_id``.
    """

    def __new__(cls, pts, box_fraction: Optional[float] = None, mask_id=None):
        obj = np.asarray(pts, dtype=np.float32).view(cls)
        obj.box_fraction = box_fraction
        obj.mask_id = mask_id
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        self.box_fraction = getattr(obj, "box_fraction", None)
        self.mask_id = getattr(obj, "mask_id", None)


def ignore_polygons(camera_id: str, width: int, height: int, masks: Optional[list[dict]] = None) -> list[np.ndarray]:
    """This camera's AI_IGNORE polygons in pixels of a ``width`` x ``height`` frame (IgnoreRegion)."""
    masks = camera_masks(camera_id) if masks is None else masks
    out = []
    for m in masks:
        if _mode(m) == IGNORE_MODE:
            try:
                out.append(IgnoreRegion(polygon_pixels(m, width, height), ignore_fraction(m), m.get("id")))
            except Exception as e:
                _warn_once(f"ignore:{m.get('id')}", f"AI_IGNORE mask {m.get('id')} unusable: {e}")
    return out


def _region_fraction(poly) -> float:
    from app.config import settings

    f = getattr(poly, "box_fraction", None)
    return float(settings.IGNORE_BOX_FRACTION) if f is None else float(f)


def _clip_area(poly: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> float:
    """Area of ``poly`` (pixels, any simple polygon) inside the box (Sutherland-Hodgman).

    Clipping a concave polygon against a convex window can leave zero-width
    "bridges", which add no area, so the shoelace area of the result is the
    exact overlap. ~10-30 us in Python for a box and a 4-12 point polygon.
    """
    pts = [(float(p[0]), float(p[1])) for p in np.asarray(poly).reshape(-1, 2)]
    for axis, bound, keep_greater in ((0, x1, True), (0, x2, False), (1, y1, True), (1, y2, False)):
        if not pts:
            return 0.0
        out = []
        prev = pts[-1]
        prev_in = (prev[axis] >= bound) if keep_greater else (prev[axis] <= bound)
        for cur in pts:
            cur_in = (cur[axis] >= bound) if keep_greater else (cur[axis] <= bound)
            if cur_in != prev_in:
                d = cur[axis] - prev[axis]
                t = (bound - prev[axis]) / d if d else 0.0
                out.append((prev[0] + (cur[0] - prev[0]) * t, prev[1] + (cur[1] - prev[1]) * t))
            if cur_in:
                out.append(cur)
            prev, prev_in = cur, cur_in
        pts = out
    if len(pts) < 3:
        return 0.0
    a = 0.0
    for i in range(len(pts)):
        xa, ya = pts[i - 1]
        xb, yb = pts[i]
        a += xa * yb - xb * ya
    return abs(a) / 2.0


def box_coverage(poly: np.ndarray, bbox) -> float:
    """Share (0..1) of ``bbox`` (x1, y1, x2, y2 pixels) that lies inside ``poly``."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    area = max(x2 - x1, 0.0) * max(y2 - y1, 0.0)
    if area <= 0.0:
        return 0.0
    p = np.asarray(poly).reshape(-1, 2)
    # Disjoint bounding boxes: no overlap, no clipping.
    if p[:, 0].max() <= x1 or p[:, 0].min() >= x2 or p[:, 1].max() <= y1 or p[:, 1].min() >= y2:
        return 0.0
    return min(1.0, _clip_area(p, x1, y1, x2, y2) / area)


def max_ignore_coverage(polys: list[np.ndarray], bbox) -> float:
    """The largest share of ``bbox`` inside any one AI_IGNORE polygon (0 when there are none)."""
    return max((box_coverage(p, bbox) for p in polys), default=0.0)


def in_ignore_region(poly: np.ndarray, foot_xy, bbox) -> bool:
    """The AI_IGNORE rule for one polygon: foot point inside, or enough of the box inside."""
    import cv2

    fx, fy = foot_xy
    if cv2.pointPolygonTest(np.asarray(poly, dtype=np.float32).reshape(-1, 1, 2),
                            (float(fx), float(fy)), False) >= 0:
        return True
    return bbox is not None and box_coverage(poly, bbox) >= _region_fraction(poly) - 1e-9


def _bbox_of(it):
    b = getattr(it, "bbox", None)
    if b is not None:
        return b
    try:
        return (it.x1, it.y1, it.x2, it.y2)
    except AttributeError:
        return None


def outside_ignore_regions(items: Iterable, polys: list[np.ndarray], foot=lambda d: d.foot_point,
                           box=_bbox_of) -> list:
    """Drop items inside any AI_IGNORE polygon (see ``in_ignore_region``).

    ``polys`` are ``ignore_polygons`` (each with its own ``box_fraction``);
    a plain array uses ``settings.IGNORE_BOX_FRACTION``. ``box`` gives an
    item's (x1, y1, x2, y2), or None to test the foot point only.
    """
    items = list(items)
    if not polys:
        return items
    kept = []
    for it in items:
        b = box(it) if box is not None else None
        if any(in_ignore_region(p, foot(it), b) for p in polys):
            continue
        kept.append(it)
    return kept
