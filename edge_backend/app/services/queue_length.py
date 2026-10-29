"""Queue length in floor metres, only where it is really measured.

A queue's length is measured from the floor positions of the people standing
in a CHECKOUT zone, which exist only for tracks of CALIBRATED cameras
(``live_engine.live_snapshot()["persons"]``). The people are projected onto
the zone's long axis (the long side of its minimum-area rectangle): the
length is the spread from the first to the last person along that axis, and
the spacing is that spread divided by the gaps between them.

Nothing is invented:

* no calibrated camera sees enough of the zone (``MIN_COVERED_FRACTION``):
  ``length_m`` is None with a plain reason, even when an uncalibrated camera
  counts people there;
* nobody in the zone: 0 m (the zone is in view and empty);
* one person: 0 m spread, no spacing;
* two or more: spread and mean spacing; ``length_estimate_m`` = people x
  spacing (each person takes one spacing), labelled as an estimate.

Pure (lists in, dict out) so tests can pin it.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

Point = Tuple[float, float]

# Share of the queue zone calibrated cameras must see before a length is given.
MIN_COVERED_FRACTION = 0.5


def _xy(p) -> Point:
    if isinstance(p, dict):
        return float(p.get("x", p.get("x_m"))), float(p.get("y", p.get("y_m")))
    return float(p[0]), float(p[1])


def _hull(points: Sequence[Point]) -> list[Point]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def long_axis(polygon: Iterable) -> Optional[dict]:
    """Long axis of a zone: the long side of its minimum-area enclosing rectangle.

    Returns ``{direction, length_m, width_m}`` (direction a unit vector)
    or None for a degenerate polygon.
    """
    hull = _hull([_xy(p) for p in polygon])
    if len(hull) < 3:
        return None
    best = None
    for i in range(len(hull)):
        (x1, y1), (x2, y2) = hull[i], hull[(i + 1) % len(hull)]
        ex, ey = x2 - x1, y2 - y1
        n = math.hypot(ex, ey)
        if n < 1e-9:
            continue
        ux, uy = ex / n, ey / n
        us = [px * ux + py * uy for px, py in hull]
        vs = [-px * uy + py * ux for px, py in hull]
        a, b = max(us) - min(us), max(vs) - min(vs)
        if best is None or a * b < best[0] - 1e-12:
            best = (a * b, ux, uy, a, b, min(us), min(vs))
    if best is None:
        return None
    _, ux, uy, a, b, umin, vmin = best
    if b > a:  # the long side runs along the rectangle's other axis
        ux, uy, a, b = -uy, ux, b, a
    return {"direction": (ux, uy), "length_m": round(a, 3), "width_m": round(b, 3)}


def point_in_polygon(x: float, y: float, polygon: Sequence[Point]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def measure(people: Iterable, polygon: Iterable, covered_fraction: Optional[float]) -> dict:
    """Length of the queue in ``polygon`` from calibrated floor points ``people``.

    ``people``: floor points in metres ({x_m, y_m} / {x, y} / (x, y)); points
    outside the polygon are ignored. ``covered_fraction``: share of the zone the
    calibrated cameras see (None or 0: none).
    """
    poly = [_xy(p) for p in polygon]
    axis = long_axis(poly)
    out = {"length_m": None, "length_estimate_m": None, "spacing_m": None, "people": None,
           "zone_length_m": axis["length_m"] if axis else None,
           "covered_fraction": None if covered_fraction is None else round(float(covered_fraction), 3),
           "method": "calibrated floor positions along the zone's long side", "reason": None}
    if axis is None:
        out["reason"] = "The zone has no usable outline."
        return out
    if not covered_fraction:
        out["reason"] = ("No calibrated camera sees this queue area, so its length cannot be measured "
                         "in metres. Calibrate a camera that looks at it.")
        return out
    if covered_fraction < MIN_COVERED_FRACTION:
        out["reason"] = (f"Calibrated cameras see only {round(covered_fraction * 100)}% of this queue area, "
                         "too little to measure its length.")
        return out
    ux, uy = axis["direction"]
    pts = [_xy(p) for p in people]
    inside = [p for p in pts if point_in_polygon(p[0], p[1], poly)]
    along = sorted(px * ux + py * uy for px, py in inside)
    out["people"] = len(along)
    if not along:
        out["length_m"] = 0.0
        return out
    spread = along[-1] - along[0]
    out["length_m"] = round(spread, 1)
    if len(along) >= 2:
        spacing = spread / (len(along) - 1)
        out["spacing_m"] = round(spacing, 2)
        out["length_estimate_m"] = round(spacing * len(along), 1)
    return out
