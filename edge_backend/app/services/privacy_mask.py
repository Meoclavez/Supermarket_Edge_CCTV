"""Privacy masks and analysis-exclusion regions for camera frames.

Masks are the per-camera polygons stored by ``ai_zone_service`` as
``exclusion_masks`` (drawn in Studio, served by ``routes/zones.py``). Points
are normalised 0..1 against the camera's native frame; a polygon whose
coordinates exceed 1 is taken as native pixels.

Semantics, decided by ``mask_mode``:

* ``BLUR`` / ``MOSAIC`` / ``BLACKOUT`` / ``COLOR`` -- **privacy masks**. They
  are burned into every frame the system shows or saves: the MJPEG stream,
  the overlay and calibration feeds, ``/snapshot`` (plain and annotated),
  saved snapshot actions, the clip pre-event buffer and the evidence frames
  handed to pose_analytics. The detector still sees the unmasked frame, so a
  shopper walking through a masked area is still counted; only their image
  is hidden. An unknown mode is treated as ``BLACKOUT`` (fail closed).
* ``AI_IGNORE`` -- an **analysis exclusion**. The picture is left alone, but
  any person or object whose foot point (bottom-centre of the box) lies
  inside the polygon is dropped before tracking, so it never reaches counts,
  zone visits, shelf interactions or theft rules.

If a mask cannot be applied (malformed polygon, OpenCV error) the frame is
blacked out entirely rather than shown unmasked.

Not covered: video that bypasses this process, e.g. a browser playing the
camera's RTSP stream directly through go2rtc/WebRTC.
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


def polygon_pixels(mask: dict, width: int, height: int) -> np.ndarray:
    """Polygon as int32 pixel coordinates in a ``width`` x ``height`` frame."""
    pts = np.array([[float(p["x"]), float(p["y"])] for p in mask["points"]], dtype=np.float64)
    if pts.size and pts.max() <= 1.0:
        pts[:, 0] *= width
        pts[:, 1] *= height
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
        filtered = cv2.GaussianBlur(roi, (k, k), 0)
    else:  # MOSAIC
        scale = max(2, int(mask.get("mosaic_scale") or 16))
        small = cv2.resize(roi, (max(1, bw // scale), max(1, bh // scale)), interpolation=cv2.INTER_LINEAR)
        filtered = cv2.resize(small, (bw, bh), interpolation=cv2.INTER_NEAREST)
    region = np.zeros((bh, bw), dtype=np.uint8)
    cv2.fillPoly(region, [poly - np.array([x, y], dtype=np.int32)], 255)
    roi[region > 0] = filtered[region > 0]


def apply_privacy_masks(frame: Optional[np.ndarray], camera_id: str, masks: Optional[list[dict]] = None):
    """Return ``frame`` with this camera's privacy masks burned in.

    Returns the input unchanged (same object) when there is nothing to mask;
    otherwise a masked copy, so the caller's frame is never modified.
    """
    if frame is None:
        return None
    masks = camera_masks(camera_id) if masks is None else masks
    privacy = [m for m in masks if _mode(m) != IGNORE_MODE]
    if not privacy:
        return frame
    out = frame.copy()
    for m in privacy:
        try:
            _apply_one(out, m, _mode(m))
        except Exception as e:
            _warn_once(f"apply:{camera_id}:{m.get('id')}",
                       f"privacy mask {m.get('id')} on {camera_id} failed ({e}); blacking out the frame")
            out[:] = 0
            return out
    return out


def ignore_polygons(camera_id: str, width: int, height: int, masks: Optional[list[dict]] = None) -> list[np.ndarray]:
    masks = camera_masks(camera_id) if masks is None else masks
    out = []
    for m in masks:
        if _mode(m) == IGNORE_MODE:
            try:
                out.append(polygon_pixels(m, width, height).astype(np.float32))
            except Exception as e:
                _warn_once(f"ignore:{m.get('id')}", f"AI_IGNORE mask {m.get('id')} unusable: {e}")
    return out


def outside_ignore_regions(items: Iterable, polys: list[np.ndarray], foot=lambda d: d.foot_point) -> list:
    """Drop items whose foot point lies inside any AI_IGNORE polygon."""
    items = list(items)
    if not polys:
        return items
    import cv2

    kept = []
    for it in items:
        fx, fy = foot(it)
        if any(cv2.pointPolygonTest(p, (float(fx), float(fy)), False) >= 0 for p in polys):
            continue
        kept.append(it)
    return kept
