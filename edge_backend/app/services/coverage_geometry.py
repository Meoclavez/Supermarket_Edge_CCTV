"""What each camera really sees on the store map, in metres.

Pure geometry over the persisted blueprint (store_structures, store_zones,
cameras): no I/O, no database, so every function here is unit-testable. The
store map (``static/js/floorplan.js``) runs the same algorithm in the browser
for live feedback while the operator drags a camera; this module is the source
of the numbers the API, the analytics and the business insights use.

Coordinate frame: metres, origin top-left, x right, y down (the map's frame).
A camera's ``azimuth_deg`` is its bearing on the map: 0 = up (-y), 90 = right
(+x), clockwise, which is how the map draws it.

Coverage of one camera
----------------------
* **Manual cone**: a sector of the camera's field of view (``fov_deg``) and
  reach (``range_m``; when not set, :func:`default_range_m` from the lens
  angle) around its bearing.
* **Calibrated footprint**: the camera's homography maps image pixels to floor
  metres, so the floor the picture really shows can be computed instead of
  guessed (:func:`homography_footprint`): the image rectangle is clipped to the
  part below the horizon that is trustworthy (no further than
  ``FOOTPRINT_DEPTH_FACTOR`` times the depth of the farthest calibration point)
  and projected to the floor. Its nearest and farthest distance from the
  camera are the real near and far limits.
* **Line of sight**: the chosen shape is clipped by what blocks the view
  (walls, room outlines, shelves, obstacles; never doors or outdoor areas, see
  :func:`blocking_segments`) by casting rays from the camera's map position
  (:func:`visibility_polygon`) and keeping the part both shapes agree on.

Store-wide numbers come from a raster of the site (cell centres, cell size
:func:`grid_cell_m`): floor coverage %, overlap, uncovered gaps and the share
of every zone each camera sees. Shelf faces are sampled along their long
sides, so "camera X sees 4.5 m of this shelf's front" is a measured length.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

Point = tuple[float, float]
Segment = tuple[float, float, float, float]

# ------------------------------------------------------------------ settings

DEFAULT_RANGE_M = 10.0          # reach of a normal 85 degree lens
DEFAULT_FOV_DEG = 85.0
MIN_RANGE_M = 0.5
MAX_RANGE_M = 100.0
COVERAGE_MODES = ("auto", "calibrated", "manual")

# A ray ignores anything closer than this to the camera: cameras hang on walls
# (or a few centimetres outside a room outline after snapping), and that wall
# must not hide the whole view.
MIN_HIT_M = 0.15
# Fixtures lower than this (a checkout counter, a low display) do not hide the
# shoppers behind them from a ceiling camera, so they do not block the view.
LOW_FIXTURE_M = 1.2
# Kinds that block the view unless the operator says otherwise
# (properties.blocks_view); doors and outdoor areas never do.
BLOCKING_KINDS = {"WALL": True, "ROOM": True, "SHELF": True, "OBSTACLE": True, "COUNTER": False}
NEVER_BLOCKING = ("DOOR", "OUTDOOR")
# A door cuts the wall / room edge it lies on: both its ends within this
# distance of the wall line (or its own thickness, if larger).
DOOR_SNAP_M = 0.3
DOOR_MAX_ANGLE_DEG = 15.0

# Calibrated footprint: points deeper than this factor times the farthest
# calibration point are not trusted (perspective error grows towards the
# horizon), and nothing beyond FOOTPRINT_MAX_M from the camera is kept.
FOOTPRINT_DEPTH_FACTOR = 3.0
FOOTPRINT_MAX_M = 60.0

# Rays: at least one per degree of the field of view, plus one each side of
# every wall end, capped so a huge map stays fast.
MAX_RAYS = 2400
SHELF_SAMPLE_M = 0.25
SHELF_FACE_OFFSET_M = 0.03
GAP_MIN_M2 = 1.0
GRID_MAX_CELLS = 200_000
GRID_MIN_CELL_M = 0.25


# --------------------------------------------------------------- primitives

def _pts(poly: Any) -> list[Point]:
    """``[{x, y}]`` or ``[[x, y]]`` -> ``[(x, y)]`` (invalid points dropped)."""
    out: list[Point] = []
    for p in poly or []:
        try:
            if isinstance(p, dict):
                x, y = float(p["x"]), float(p["y"])
            else:
                x, y = float(p[0]), float(p[1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            out.append((x, y))
    return out


def polygon_area(poly: Sequence[Point]) -> float:
    """Shoelace area (m^2), always >= 0."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(x: float, y: float, poly: Sequence[Point]) -> bool:
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _signed_area(poly: Sequence[Point]) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def clip_polygon_convex(subject: Sequence[Point], clip: Sequence[Point]) -> list[Point]:
    """Sutherland-Hodgman: ``subject`` (any simple polygon) inside the convex ``clip``.

    The result's area is exact for a convex clip; a concave subject may come
    back with zero-width bridges along the clip edge, which add no area.
    """
    out = list(subject)
    if len(clip) < 3 or len(out) < 3:
        return []
    ccw = _signed_area(clip) > 0
    n = len(clip)
    for i in range(n):
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % n]

        def inside(p: Point) -> bool:
            c = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
            return c >= -1e-12 if ccw else c <= 1e-12

        def cross(p: Point, q: Point) -> Point:
            dx, dy = q[0] - p[0], q[1] - p[1]
            ex, ey = bx - ax, by - ay
            den = dx * ey - dy * ex
            if abs(den) < 1e-15:
                return q
            t = ((ax - p[0]) * ey - (ay - p[1]) * ex) / den
            return (p[0] + t * dx, p[1] + t * dy)

        inp, out = out, []
        if not inp:
            break
        prev = inp[-1]
        for cur in inp:
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
            prev = cur
    return out


def clip_polygon_halfplane(poly: Sequence[Point], a: float, b: float, c: float) -> list[Point]:
    """Keep the part of ``poly`` where ``a*x + b*y + c >= 0``."""
    out: list[Point] = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp, fq = a * p[0] + b * p[1] + c, a * q[0] + b * q[1] + c
        if fp >= 0:
            out.append(p)
        if (fp >= 0) != (fq >= 0):
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def oriented_box(poly: Sequence[Point]) -> tuple[float, float, float]:
    """(long side m, short side m, angle of the long side in radians) of the
    minimum-area box aligned with one of the polygon's edges."""
    best = None
    n = len(poly)
    if n < 2:
        return 0.0, 0.0, 0.0
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        ang = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(ang), math.sin(ang)
        us = [x * c + y * s for x, y in poly]
        vs = [-x * s + y * c for x, y in poly]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0] - 1e-9:
            best = (w * h, w, h, ang)
    _, w, h, ang = best
    if h > w:
        return h, w, ang + math.pi / 2
    return w, h, ang


# ------------------------------------------------------------ range defaults

def default_range_m(fov_deg: Optional[float]) -> float:
    """Reach used when the operator has not set one, from the lens angle.

    10 m for a normal 85 degree lens; a narrower lens magnifies and sees
    further (same pixels per metre), a wider one less:
    ``10 * tan(42.5 deg) / tan(fov / 2)``, kept within 3..30 m. 180 degrees or
    more (fisheye, 360) uses 6 m.
    """
    try:
        fov = float(fov_deg) if fov_deg is not None else DEFAULT_FOV_DEG
    except (TypeError, ValueError):
        fov = DEFAULT_FOV_DEG
    if not math.isfinite(fov) or fov <= 0:
        fov = DEFAULT_FOV_DEG
    if fov >= 179.0:
        return 6.0
    r = DEFAULT_RANGE_M * math.tan(math.radians(DEFAULT_FOV_DEG / 2)) / math.tan(math.radians(fov / 2))
    return round(min(30.0, max(3.0, r)), 1)


def validate_range_m(value: Any) -> Optional[float]:
    """A stored reach: None (use the default) or a number within limits."""
    if value is None:
        return None
    v = float(value)
    if not math.isfinite(v) or v < MIN_RANGE_M or v > MAX_RANGE_M:
        raise ValueError(f"range must be between {MIN_RANGE_M:g} and {MAX_RANGE_M:g} metres")
    return round(v, 2)


def validate_coverage_mode(value: Any) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in COVERAGE_MODES:
        raise ValueError("coverage mode must be auto, calibrated or manual")
    return None if v == "auto" else v


def bearing_vector(azimuth_deg: float) -> Point:
    """Unit vector of a map bearing (0 = up, clockwise)."""
    a = math.radians(float(azimuth_deg or 0.0))
    return math.sin(a), -math.cos(a)


def _bearing_to_angle(azimuth_deg: float) -> float:
    """Map bearing -> screen angle (radians from +x, y down)."""
    return math.radians(float(azimuth_deg or 0.0) - 90.0)


# ---------------------------------------------------------------- blockers

@dataclass
class Blocker:
    structure_id: Optional[str]
    kind: str
    segments: list[Segment]


def structure_blocks_view(kind: str, properties: Optional[dict]) -> bool:
    """Whether a structure hides what is behind it from a camera.

    ``properties.blocks_view`` (true/false) set by the operator wins. Doors and
    outdoor areas never block. A fixture with ``properties.height_m`` below
    ``LOW_FIXTURE_M`` does not block (shoppers stay visible over it); walls
    and room outlines block whatever their height.
    """
    kind = str(kind or "").upper()
    if kind in NEVER_BLOCKING:
        return False
    props = properties or {}
    bv = props.get("blocks_view")
    if isinstance(bv, bool):
        return bv
    default = BLOCKING_KINDS.get(kind, False)
    if not default:
        return False
    if kind not in ("WALL", "ROOM"):
        h = props.get("height_m")
        if isinstance(h, (int, float)) and math.isfinite(h) and 0 < h < LOW_FIXTURE_M:
            return False
    return True


def _poly_edges(pts: Sequence[Point], closed: bool) -> list[Segment]:
    n = len(pts)
    out = []
    for i in range(n if closed else n - 1):
        a, b = pts[i], pts[(i + 1) % n]
        if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9:
            out.append((a[0], a[1], b[0], b[1]))
    return out


def _cut_doors(seg: Segment, doors: list[tuple[Segment, float]]) -> list[Segment]:
    """Remove the openings of doors lying along this wall segment."""
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return []
    ux, uy = dx / L, dy / L
    cuts: list[tuple[float, float]] = []
    for (a, b, c, d), thick in doors:
        tol = max(DOOR_SNAP_M, thick)
        ddx, ddy = c - a, d - b
        dl = math.hypot(ddx, ddy)
        if dl < 1e-9:
            continue
        cosang = abs((ddx * ux + ddy * uy) / dl)
        if cosang < math.cos(math.radians(DOOR_MAX_ANGLE_DEG)):
            continue
        # perpendicular distance of both door ends to the wall line
        d1 = abs((a - x1) * uy - (b - y1) * ux)
        d2 = abs((c - x1) * uy - (d - y1) * ux)
        if d1 > tol or d2 > tol:
            continue
        t1 = ((a - x1) * ux + (b - y1) * uy) / L
        t2 = ((c - x1) * ux + (d - y1) * uy) / L
        lo, hi = min(t1, t2), max(t1, t2)
        if hi <= 0 or lo >= 1:
            continue
        cuts.append((max(0.0, lo), min(1.0, hi)))
    if not cuts:
        return [seg]
    cuts.sort()
    out: list[Segment] = []
    t = 0.0
    for lo, hi in cuts:
        if lo > t + 1e-9:
            out.append((x1 + dx * t, y1 + dy * t, x1 + dx * lo, y1 + dy * lo))
        t = max(t, hi)
    if t < 1 - 1e-9:
        out.append((x1 + dx * t, y1 + dy * t, x2, y2))
    return out


def blocking_segments(structures: Iterable[dict]) -> list[Blocker]:
    """Every structure that blocks the view, as line segments in metres.

    Walls are their centre line, polygons (rooms, shelves, obstacles) their
    outline. Doors cut the wall or room edge they sit on, so a camera sees
    through an open doorway. Outdoor areas, doors and low fixtures add nothing.
    """
    structures = list(structures or [])
    doors: list[tuple[Segment, float]] = []
    for s in structures:
        if str(s.get("kind", "")).upper() == "DOOR":
            pts = _pts(s.get("polygon"))
            for seg in _poly_edges(pts, closed=False):
                doors.append((seg, float(s.get("thickness_m") or 0.0)))
    out: list[Blocker] = []
    for s in structures:
        kind = str(s.get("kind", "")).upper()
        if not structure_blocks_view(kind, s.get("properties")):
            continue
        pts = _pts(s.get("polygon"))
        closed = kind != "WALL"
        if len(pts) < (3 if closed else 2):
            continue
        segs = _poly_edges(pts, closed)
        if kind in ("WALL", "ROOM") and doors:
            segs = [piece for seg in segs for piece in _cut_doors(seg, doors)]
        if segs:
            out.append(Blocker(structure_id=s.get("id"), kind=kind, segments=segs))
    return out


def _segments_for(origin: Point, blockers: Sequence[Blocker]) -> np.ndarray:
    """All blocking segments as an (N, 4) array, minus fixtures the camera sits in
    (a camera mounted above a shelf is not hidden by that shelf)."""
    rows: list[Segment] = []
    for b in blockers:
        if b.kind not in ("WALL", "ROOM"):
            poly = [(s[0], s[1]) for s in b.segments]
            if len(poly) >= 3 and point_in_polygon(origin[0], origin[1], poly):
                continue
        rows.extend(b.segments)
    return np.asarray(rows, dtype=float).reshape(-1, 4)


# ---------------------------------------------------------------- visibility

def _cast(origin: Point, angles: np.ndarray, max_range: float, segs: np.ndarray) -> np.ndarray:
    """Distance along each ray to the first blocker (capped at max_range)."""
    ox, oy = origin
    dx, dy = np.cos(angles), np.sin(angles)
    if segs.size == 0:
        return np.full(angles.shape, float(max_range))
    ax, ay, bx, by = segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3]
    ex, ey = bx - ax, by - ay                        # (S,)
    # Solve origin + t*d = a + u*e  for t >= 0, 0 <= u <= 1
    den = dx[:, None] * ey[None, :] - dy[:, None] * ex[None, :]      # (R, S)
    wx, wy = ax - ox, ay - oy
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (wx[None, :] * ey[None, :] - wy[None, :] * ex[None, :]) / den
        u = (wx[None, :] * dy[:, None] - wy[None, :] * dx[:, None]) / den
    ok = (np.abs(den) > 1e-12) & (t >= MIN_HIT_M) & (u >= -1e-9) & (u <= 1 + 1e-9)
    t = np.where(ok, t, np.inf)
    return np.minimum(t.min(axis=1), float(max_range))


def visibility_polygon(origin: Point, start_angle: float, end_angle: float, max_range: float,
                       blockers: Sequence[Blocker]) -> list[Point]:
    """Floor a camera at ``origin`` sees between two screen angles (radians,
    ``end >= start``), up to ``max_range`` metres, with blockers in the way.

    A sector narrower than a full turn starts and ends at the camera, so the
    polygon is the clipped cone; a full turn is the ring of hit points.
    """
    span = end_angle - start_angle
    full = span >= 2 * math.pi - 1e-9
    if full:
        start_angle, end_angle, span = -math.pi, math.pi, 2 * math.pi
    segs = _segments_for(origin, blockers)
    n = max(32, int(math.ceil(math.degrees(span))))
    base = np.linspace(start_angle, end_angle, n + 1)
    extra: list[float] = []
    if segs.size:
        ends = np.concatenate([segs[:, 0:2], segs[:, 2:4]])
        dist = np.hypot(ends[:, 0] - origin[0], ends[:, 1] - origin[1])
        ends = ends[dist <= max_range + 1e-6]
        ang = np.arctan2(ends[:, 1] - origin[1], ends[:, 0] - origin[0])
        for a in ang:
            # bring into [start, start + 2pi)
            k = start_angle + ((a - start_angle) % (2 * math.pi))
            for e in (k - 1e-4, k + 1e-4):
                if start_angle <= e <= end_angle:
                    extra.append(e)
    angles = np.unique(np.concatenate([base, np.asarray(extra[: MAX_RAYS - len(base)], dtype=float)]))
    d = _cast(origin, angles, max_range, segs)
    ox, oy = origin
    ring = [(ox + float(r) * math.cos(a), oy + float(r) * math.sin(a)) for a, r in zip(angles, d)]
    if full:
        return ring[:-1] if len(ring) > 1 else ring
    return [origin] + ring


def sector_angles(azimuth_deg: float, fov_deg: float) -> tuple[float, float]:
    centre = _bearing_to_angle(azimuth_deg)
    half = math.radians(min(360.0, max(1.0, float(fov_deg or DEFAULT_FOV_DEG)))) / 2
    return centre - half, centre + half


# --------------------------------------------------- calibrated footprint

def _h(matrix: Any) -> Optional[np.ndarray]:
    try:
        H = np.asarray(matrix, dtype=float).reshape(3, 3)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(H)) or abs(np.linalg.det(H)) < 1e-15:
        return None
    return H


def homography_footprint(matrix: Any, frame_w: Optional[float], frame_h: Optional[float],
                         image_points: Optional[Sequence[Any]] = None,
                         camera_xy: Optional[Point] = None) -> Optional[dict]:
    """The floor area the camera's picture shows, from its calibration.

    ``matrix`` maps image pixels (in the ``frame_w`` x ``frame_h`` frame the
    calibration was clicked in) to floor metres. The image rectangle is cut
    to the part whose projective depth ``w = h31*u + h32*v + h33`` is at least
    the farthest calibration point's depth / ``FOOTPRINT_DEPTH_FACTOR``: the
    horizon (w = 0) and the sky above it fall away, and so does the band near
    the horizon where a pixel covers metres. The rest is projected (a
    homography keeps straight lines straight, so projecting the corners is
    exact). With ``camera_xy`` the result is also limited to
    ``FOOTPRINT_MAX_M`` around the camera and reports near/far limits.

    Returns ``{"polygon": [{x, y}], "near_m", "far_m", "area_m2"}`` or None
    when the calibration cannot give a usable floor area.
    """
    H = _h(matrix)
    if H is None or not frame_w or not frame_h:
        return None
    W, Hh = float(frame_w), float(frame_h)
    if W <= 0 or Hh <= 0:
        return None
    pts = _pts(image_points)
    ws_ref = [H[2, 0] * u + H[2, 1] * v + H[2, 2] for u, v in pts]
    if not ws_ref:
        # No stored clicks: the bottom centre of the picture is floor near the camera.
        ws_ref = [H[2, 0] * W / 2 + H[2, 1] * Hh + H[2, 2]]
    if np.mean(ws_ref) < 0:
        H = -H
        ws_ref = [-w for w in ws_ref]
    w_far = min(w for w in ws_ref if w > 0) if any(w > 0 for w in ws_ref) else None
    if w_far is None:
        return None
    w_min = w_far / FOOTPRINT_DEPTH_FACTOR
    rect = [(0.0, 0.0), (W, 0.0), (W, Hh), (0.0, Hh)]
    img = clip_polygon_halfplane(rect, H[2, 0], H[2, 1], H[2, 2] - w_min)
    if len(img) < 3:
        return None
    floor: list[Point] = []
    for u, v in img:
        x, y, w = H @ np.array([u, v, 1.0])
        if w <= 0:
            return None
        floor.append((float(x / w), float(y / w)))
    if _signed_area(floor) < 0:
        floor.reverse()
    if camera_xy is not None:
        cx, cy = camera_xy
        circle = [(cx + FOOTPRINT_MAX_M * math.cos(a), cy + FOOTPRINT_MAX_M * math.sin(a))
                  for a in np.linspace(0, 2 * math.pi, 48, endpoint=False)]
        floor = clip_polygon_convex(floor, circle)
    area = polygon_area(floor)
    if len(floor) < 3 or area < 0.05:
        return None
    out = {"polygon": [{"x": round(x, 3), "y": round(y, 3)} for x, y in floor], "area_m2": round(area, 2),
           "near_m": None, "far_m": None}
    if camera_xy is not None:
        out["near_m"], out["far_m"] = near_far(camera_xy, floor)
    return out


def _seg_point_dist(px: float, py: float, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / l2))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def near_far(origin: Point, poly: Sequence[Point]) -> tuple[float, float]:
    """Nearest and farthest floor distance (m) of a polygon from a point."""
    ox, oy = origin
    far = max(math.hypot(x - ox, y - oy) for x, y in poly)
    if point_in_polygon(ox, oy, poly):
        near = 0.0
    else:
        near = min(_seg_point_dist(ox, oy, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))
    return round(near, 2), round(far, 2)


# ------------------------------------------------------- one camera's view

@dataclass
class CameraView:
    camera_id: str
    name: str
    mode: str                         # "calibrated" | "manual"
    polygon: list[Point]
    range_m: float                    # reach used for the cone (manual) or far limit (calibrated)
    range_is_default: bool
    near_m: Optional[float] = None
    far_m: Optional[float] = None
    footprint_available: bool = False
    occlusion_applied: bool = True
    counted: bool = True              # enabled and placed: part of the store numbers
    enabled: bool = True
    footprint_polygon: Optional[list[Point]] = None
    calibrated: bool = False
    watch_zone_id: Optional[str] = None
    area_m2: float = 0.0


def camera_range(cam: dict) -> tuple[float, bool]:
    """(reach in m, is it the default)."""
    r = cam.get("range_m")
    if isinstance(r, (int, float)) and math.isfinite(r) and r > 0:
        return float(r), False
    return default_range_m(cam.get("fov_deg")), True


def camera_footprint(cam: dict) -> Optional[dict]:
    """Calibrated footprint of a serialised camera (``homography_matrix`` +
    ``calibration_points`` with the frame size), or None."""
    H = cam.get("homography_matrix")
    if not H:
        return None
    cp = cam.get("calibration_points") or {}
    fw = cp.get("frame_width") or cam.get("frame_width")
    fh = cp.get("frame_height") or cam.get("frame_height")
    placed = cam.get("map_placed") is not False
    xy = (float(cam.get("floor_x") or 0.0), float(cam.get("floor_y") or 0.0)) if placed else None
    return homography_footprint(H, fw, fh, cp.get("image_points"), xy)


def camera_view(cam: dict, blockers: Sequence[Blocker], footprint: Optional[dict] = None,
                site: Optional[tuple[float, float]] = None) -> CameraView:
    """The floor one camera covers, after line of sight, inside the site
    (``site`` = (width_m, height_m): nothing beyond the map's edge counts).

    ``cam`` is a serialised camera (store_layout_service.serialize_cameras plus
    ``homography_matrix``). ``coverage_mode`` "manual" always uses the cone;
    "calibrated" / auto use the calibrated footprint when there is one.
    """
    rng, is_default = camera_range(cam)
    origin = (float(cam.get("floor_x") or 0.0), float(cam.get("floor_y") or 0.0))
    placed = cam.get("map_placed") is not False
    enabled = cam.get("is_ai_enabled") is not False and cam.get("status") != "DISABLED"
    if footprint is None and cam.get("homography_matrix"):
        footprint = camera_footprint(cam)
    mode_pref = (cam.get("coverage_mode") or "auto").lower()
    use_cal = footprint is not None and mode_pref != "manual"
    view = CameraView(
        camera_id=str(cam.get("camera_id") or cam.get("id")), name=str(cam.get("name") or ""),
        mode="calibrated" if use_cal else "manual", polygon=[], range_m=rng, range_is_default=is_default,
        footprint_available=footprint is not None, counted=bool(placed and enabled), enabled=bool(enabled),
        footprint_polygon=_pts(footprint["polygon"]) if footprint else None,
        calibrated=bool(cam.get("has_homography") or cam.get("homography_matrix")),
        watch_zone_id=cam.get("watch_zone_id"),
    )
    if use_cal:
        fp = _pts(footprint["polygon"])
        if not placed:
            # Without a spot on the map the line of sight is unknown.
            view.polygon, view.occlusion_applied = fp, False
        else:
            angs = [math.atan2(y - origin[1], x - origin[0]) for x, y in fp]
            far = max(math.hypot(x - origin[0], y - origin[1]) for x, y in fp) + 0.5
            if point_in_polygon(origin[0], origin[1], fp):
                lo, hi = -math.pi, math.pi
            else:
                # Convex footprint not containing the camera: its angular span is < 180 deg.
                ref = angs[0]
                rel = [((a - ref + math.pi) % (2 * math.pi)) - math.pi for a in angs]
                lo, hi = ref + min(rel) - 1e-3, ref + max(rel) + 1e-3
            vis = visibility_polygon(origin, lo, hi, far, blockers)
            view.polygon = clip_polygon_convex(vis, fp)
            view.near_m, view.far_m = footprint.get("near_m"), footprint.get("far_m")
        view.range_m = float(footprint.get("far_m") or rng)
    else:
        lo, hi = sector_angles(cam.get("azimuth_deg") or 0.0, cam.get("fov_deg") or DEFAULT_FOV_DEG)
        view.polygon = visibility_polygon(origin, lo, hi, rng, blockers)
        view.near_m, view.far_m = 0.0, rng
    if site is not None and len(view.polygon) >= 3:
        sw, sh = float(site[0]), float(site[1])
        box = [(0.0, 0.0), (sw, 0.0), (sw, sh), (0.0, sh)]
        view.polygon = clip_polygon_convex(view.polygon, box)
        if use_cal and placed:
            # Near / far limits of the footprint part that lies on the map.
            on_map = clip_polygon_convex(_pts(footprint["polygon"]), box)
            if len(on_map) >= 3:
                view.near_m, view.far_m = near_far(origin, on_map)
                view.range_m = float(view.far_m)
    view.area_m2 = round(polygon_area(view.polygon), 2)
    return view


# ------------------------------------------------------------------ raster

def grid_cell_m(width_m: float, height_m: float) -> float:
    cell = max(GRID_MIN_CELL_M, math.sqrt(max(width_m * height_m, 1e-9) / GRID_MAX_CELLS))
    return round(cell, 3)


@dataclass
class Grid:
    width_m: float
    height_m: float
    cell: float
    nx: int = 0
    ny: int = 0
    xs: np.ndarray = field(default_factory=lambda: np.zeros(0))
    ys: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @classmethod
    def for_site(cls, width_m: float, height_m: float, cell: Optional[float] = None) -> "Grid":
        c = cell or grid_cell_m(width_m, height_m)
        nx, ny = max(1, int(math.ceil(width_m / c))), max(1, int(math.ceil(height_m / c)))
        xs = (np.arange(nx) + 0.5) * c
        ys = (np.arange(ny) + 0.5) * c
        g = cls(width_m=width_m, height_m=height_m, cell=c, nx=nx, ny=ny)
        g.xs, g.ys = np.meshgrid(xs, ys)
        return g

    @property
    def cell_m2(self) -> float:
        return self.cell * self.cell

    def mask(self, poly: Sequence[Point]) -> np.ndarray:
        """Cells whose centre lies inside ``poly`` (even-odd rule)."""
        m = np.zeros((self.ny, self.nx), dtype=bool)
        n = len(poly)
        if n < 3:
            return m
        xs_p = [p[0] for p in poly]
        ys_p = [p[1] for p in poly]
        c = self.cell
        i0 = max(0, int(math.floor(min(xs_p) / c)) - 1)
        i1 = min(self.nx, int(math.ceil(max(xs_p) / c)) + 1)
        j0 = max(0, int(math.floor(min(ys_p) / c)) - 1)
        j1 = min(self.ny, int(math.ceil(max(ys_p) / c)) + 1)
        if i0 >= i1 or j0 >= j1:
            return m
        X, Y = self.xs[j0:j1, i0:i1], self.ys[j0:j1, i0:i1]
        inside = np.zeros(X.shape, dtype=bool)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if yi != yj:
                cond = ((yi > Y) != (yj > Y)) & (X < (xj - xi) * (Y - yi) / (yj - yi) + xi)
                inside ^= cond
            j = i
        m[j0:j1, i0:i1] = inside
        return m


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """4-connected components of True cells."""
    ny, nx = mask.shape
    seen = np.zeros_like(mask)
    comps = []
    for j0, i0 in zip(*np.nonzero(mask)):
        if seen[j0, i0]:
            continue
        stack = [(j0, i0)]
        seen[j0, i0] = True
        comp = []
        while stack:
            j, i = stack.pop()
            comp.append((j, i))
            for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = j + dj, i + di
                if 0 <= a < ny and 0 <= b < nx and mask[a, b] and not seen[a, b]:
                    seen[a, b] = True
                    stack.append((a, b))
        comps.append(comp)
    return comps


# ------------------------------------------------------------- shelf faces

def shelf_faces(poly: Sequence[Point]) -> tuple[list[tuple[Point, Point]], float]:
    """The long edges (shelf fronts) of a shelf polygon and its length (m).

    Edges within 30 degrees of the oriented box's long side count as front;
    a gondola has two (both sides are sellable), a wall bay one exposed and
    one against the wall (the wall then hides it from every camera).
    """
    length, _depth, ang = oriented_box(poly)
    faces = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        el = math.hypot(b[0] - a[0], b[1] - a[1])
        if el < 1e-6:
            continue
        ea = math.atan2(b[1] - a[1], b[0] - a[0])
        if abs(math.cos(ea - ang)) >= math.cos(math.radians(30)):
            faces.append((a, b))
    return faces, length


def face_samples(poly: Sequence[Point]) -> tuple[list[Point], float, float]:
    """Points just outside the shelf fronts, one per ``SHELF_SAMPLE_M``.

    Returns (points, metres of front per point, shelf length m).
    """
    faces, length = shelf_faces(poly)
    ccw = _signed_area(poly) > 0
    pts: list[Point] = []
    total = 0.0
    for a, b in faces:
        el = math.hypot(b[0] - a[0], b[1] - a[1])
        total += el
        k = max(1, int(round(el / SHELF_SAMPLE_M)))
        ux, uy = (b[0] - a[0]) / el, (b[1] - a[1]) / el
        # outward normal: right of the edge for a ccw polygon (y down flips nothing here:
        # the sign test below uses the signed area in the same frame)
        nx_, ny_ = (uy, -ux) if ccw else (-uy, ux)
        for s in range(k):
            t = (s + 0.5) / k
            pts.append((a[0] + ux * el * t + nx_ * SHELF_FACE_OFFSET_M,
                        a[1] + uy * el * t + ny_ * SHELF_FACE_OFFSET_M))
    per = total / len(pts) if pts else 0.0
    return pts, per, length


# ---------------------------------------------------------- whole store

FLOOR_EXCLUDED_ZONES = ("STOCKROOM", "EXCLUDED")


def analyse(layout: dict, structures: list[dict], zones: list[dict], cameras: list[dict]) -> dict:
    """Coverage of every camera and of the whole store.

    ``layout`` needs ``width_m``/``height_m``; ``structures`` / ``zones`` are
    the serialised blueprint; ``cameras`` serialised cameras with
    ``homography_matrix``. Store numbers count only cameras that are switched
    on and placed on the map (``counted``).
    """
    W = float(layout.get("width_m") or 1.0)
    Hm = float(layout.get("height_m") or 1.0)
    grid = Grid.for_site(W, Hm)
    blockers = blocking_segments(structures)

    # Walkable floor: indoor rooms and the countable zones (a store may draw only
    # some rooms, e.g. the cool room, and zones for the sales floor), else the
    # site; minus fixtures.
    floor, floor_source = _floor_mask(grid, structures, zones)
    for s in structures:
        if str(s.get("kind")).upper() in ("SHELF", "COUNTER", "OBSTACLE"):
            p = _pts(s.get("polygon"))
            if len(p) >= 3:
                floor &= ~grid.mask(p)

    views: list[CameraView] = []
    masks: dict[str, np.ndarray] = {}
    for cam in cameras:
        v = camera_view(cam, blockers, footprint=cam.get("calibrated_footprint"), site=(W, Hm))
        views.append(v)
        masks[v.camera_id] = grid.mask(v.polygon)

    counted = [v for v in views if v.counted]
    count = np.zeros((grid.ny, grid.nx), dtype=np.int32)
    for v in counted:
        count += masks[v.camera_id]
    covered = count > 0
    cm2 = grid.cell_m2

    zone_masks = {}
    for z in zones:
        p = _pts(z.get("polygon"))
        if len(p) >= 3:
            zone_masks[z["id"]] = (z, grid.mask(p), p)

    shelf_items = []
    for s in structures:
        if str(s.get("kind")).upper() in ("SHELF", "COUNTER"):
            p = _pts(s.get("polygon"))
            if len(p) >= 3:
                samples, per, length = face_samples(p)
                shelf_items.append({"id": s.get("id"), "name": s.get("name") or "", "kind": s.get("kind"),
                                    "samples": samples, "per": per, "length_m": round(length, 2),
                                    "front_m": round(per * len(samples), 2), "seen": np.zeros(len(samples), bool),
                                    "cameras": []})

    cams_out = []
    for v in views:
        m = masks[v.camera_id]
        zlist = []
        for zid, (z, zm, _p) in zone_masks.items():
            cells = int(zm.sum())
            if not cells:
                continue
            seen = int((zm & m).sum())
            if seen:
                zlist.append({"zone_id": zid, "name": z.get("name"), "category": z.get("category"),
                              "covered_fraction": round(seen / cells, 3), "seen_m2": round(seen * cm2, 2)})
        zlist.sort(key=lambda d: -d["seen_m2"])
        slist = []
        for sh in shelf_items:
            if not sh["samples"]:
                continue
            flags = np.array([point_in_polygon(x, y, v.polygon) for x, y in sh["samples"]], dtype=bool)
            if flags.any():
                seen_m = round(float(flags.sum()) * sh["per"], 2)
                slist.append({"structure_id": sh["id"], "name": sh["name"], "kind": sh["kind"],
                              "seen_front_m": seen_m, "front_m": sh["front_m"], "length_m": sh["length_m"]})
                if v.counted:
                    sh["seen"] |= flags
                    sh["cameras"].append({"camera_id": v.camera_id, "seen_front_m": seen_m})
        slist.sort(key=lambda d: -d["seen_front_m"])
        overlaps = []
        if v.counted:
            for o in counted:
                if o.camera_id == v.camera_id:
                    continue
                both = int((m & masks[o.camera_id]).sum())
                if both:
                    overlaps.append({"camera_id": o.camera_id, "m2": round(both * cm2, 2)})
        cams_out.append({
            "camera_id": v.camera_id, "name": v.name, "mode": v.mode, "counted": v.counted, "enabled": v.enabled,
            "watch_zone_id": v.watch_zone_id,
            "calibrated": v.calibrated, "footprint_available": v.footprint_available,
            "occlusion_applied": v.occlusion_applied,
            "range_m": round(v.range_m, 2), "range_is_default": v.range_is_default,
            "near_m": v.near_m, "far_m": None if v.far_m is None else round(v.far_m, 2),
            "polygon": [{"x": round(x, 3), "y": round(y, 3)} for x, y in v.polygon],
            "area_m2": v.area_m2,
            "floor_seen_m2": round(float((m & floor).sum()) * cm2, 2),
            "zones": zlist, "shelves": slist, "overlaps": overlaps,
        })

    floor_cells = int(floor.sum())
    covered_floor = int((covered & floor).sum())
    uncovered = floor & ~covered
    gaps = []
    for comp in _components(uncovered):
        area = len(comp) * cm2
        if area < GAP_MIN_M2:
            continue
        jj = np.array([c[0] for c in comp])
        ii = np.array([c[1] for c in comp])
        cx, cy = float(((ii + 0.5) * grid.cell).mean()), float(((jj + 0.5) * grid.cell).mean())
        comp_mask = np.zeros_like(uncovered)
        comp_mask[jj, ii] = True
        hits = []
        for zid, (z, zm, _p) in zone_masks.items():
            k = int((zm & comp_mask).sum())
            if k:
                hits.append((k, z))
        hits.sort(key=lambda h: -h[0])
        best = hits[0][1] if hits else None
        gaps.append({"area_m2": round(area, 2), "x": round(cx, 2), "y": round(cy, 2),
                     "bbox": {"min_x": round(float(ii.min() * grid.cell), 2), "min_y": round(float(jj.min() * grid.cell), 2),
                              "max_x": round(float((ii.max() + 1) * grid.cell), 2), "max_y": round(float((jj.max() + 1) * grid.cell), 2)},
                     "zone_id": best.get("id") if best else None, "zone_name": best.get("name") if best else None,
                     # Zones the blind spot reaches into, most-affected first (at most 3).
                     "zone_names": [z.get("name") for _k, z in hits[:3]]})
    gaps.sort(key=lambda g: -g["area_m2"])

    # Floor the calibrated cameras place people on (zone visits come only from
    # them): their footprint, after line of sight when the operator uses it.
    cal_views = [v for v in views if v.enabled and v.calibrated and v.footprint_polygon]
    cal_masks = {v.camera_id: masks[v.camera_id] if v.mode == "calibrated" else grid.mask(v.footprint_polygon)
                 for v in cal_views}
    zones_out = []
    for zid, (z, zm, p) in zone_masks.items():
        cells = int(zm.sum())
        cams = []
        for v in counted:
            k = int((zm & masks[v.camera_id]).sum())
            if k and cells:
                cams.append({"camera_id": v.camera_id, "covered_fraction": round(k / cells, 3),
                             "calibrated": v.calibrated})
        cams.sort(key=lambda c: -c["covered_fraction"])
        cal_union = np.zeros_like(zm)
        for v in cal_views:
            cal_union |= cal_masks[v.camera_id]
        zones_out.append({
            "zone_id": zid, "name": z.get("name"), "category": z.get("category"),
            "area_m2": round(polygon_area(p), 2),
            "covered_fraction": round(int((zm & covered).sum()) / cells, 3) if cells else None,
            "calibrated_covered_fraction": round(int((zm & cal_union).sum()) / cells, 3) if cells else None,
            "cameras": cams,
        })

    shelves_out = []
    for sh in shelf_items:
        shelves_out.append({"structure_id": sh["id"], "name": sh["name"], "kind": sh["kind"],
                            "length_m": sh["length_m"], "front_m": sh["front_m"],
                            "seen_front_m": round(float(sh["seen"].sum()) * sh["per"], 2),
                            "cameras": sorted(sh["cameras"], key=lambda c: -c["seen_front_m"])})

    return {
        "grid_cell_m": grid.cell,
        "cameras": cams_out,
        "zones": zones_out,
        "shelves": shelves_out,
        "store": {
            "floor_source": floor_source,
            "floor_m2": round(floor_cells * cm2, 2),
            "covered_m2": round(covered_floor * cm2, 2),
            "uncovered_m2": round((floor_cells - covered_floor) * cm2, 2),
            "coverage_pct": round(100.0 * covered_floor / floor_cells, 1) if floor_cells else None,
            "overlap_m2": round(float(((count >= 2) & floor).sum()) * cm2, 2),
            "cameras_counted": len(counted),
            "gaps": gaps[:20],
        },
    }


def _floor_mask(grid: "Grid", structures: list[dict], zones: list[dict]) -> tuple[np.ndarray, str]:
    """Union of indoor ROOM structures and countable zones (not STOCKROOM /
    EXCLUDED); the whole site when neither is drawn. Source: "rooms",
    "zones", "rooms+zones" or "site"."""
    floor = np.zeros((grid.ny, grid.nx), dtype=bool)
    rooms = [p for p in (_pts(s.get("polygon")) for s in structures
                         if str(s.get("kind")).upper() == "ROOM") if len(p) >= 3]
    zpolys = [p for p in (_pts(z.get("polygon")) for z in zones
                          if z.get("category") not in FLOOR_EXCLUDED_ZONES) if len(p) >= 3]
    for p in rooms + zpolys:
        floor |= grid.mask(p)
    if not rooms and not zpolys:
        floor[:] = True
        return floor, "site"
    return floor, "rooms+zones" if rooms and zpolys else ("rooms" if rooms else "zones")


def sales_floor_area(width_m: float, height_m: float, structures: list[dict],
                     zones: list[dict]) -> tuple[Optional[float], Optional[str]]:
    """Selling floor in m^2 and where it came from.

    Indoor ROOM structures and countable zones (union), minus STOCKROOM /
    EXCLUDED zones. Fixtures stay included (sales per m^2 is quoted over the
    selling area, shelves and all). (None, None) when neither rooms nor zones
    are drawn.
    """
    grid = Grid.for_site(float(width_m or 1.0), float(height_m or 1.0))
    floor, source = _floor_mask(grid, structures, zones)
    if source == "site":
        return None, None
    for z in zones:
        if z.get("category") in FLOOR_EXCLUDED_ZONES:
            p = _pts(z.get("polygon"))
            if len(p) >= 3:
                floor &= ~grid.mask(p)
    area = float(floor.sum()) * grid.cell_m2
    return (round(area, 1) if area > 0 else None), source


# ------------------------------------------- shelf attribution (live pose)

def shelf_on_sight_line(camera_xy: Point, camera_h: float, projected_xy: Point,
                        shelves: Sequence[tuple[Any, Sequence[Point]]],
                        hand_h_min: float = 0.4, hand_h_max: float = 2.0) -> Optional[Any]:
    """The shelf a hand above the floor is reaching into, along the line of sight.

    A ground homography maps a wrist pixel to where the camera's line of sight
    through it meets the floor: ``projected_xy``, farther from the camera than
    the hand really is. A hand at height h actually sits above
    ``C + (P - C) * (H - h) / H`` (C = point under the camera, H = camera
    height). For hand heights ``hand_h_min..hand_h_max`` that is a segment of
    the sight line; the first shelf polygon it crosses, seen from the camera,
    is the shelf face the camera can see the hand at. Returns that shelf's key
    (the first element of its tuple) or None.
    """
    if camera_h is None or camera_h <= hand_h_min + 0.2:
        return None
    cx, cy = camera_xy
    px, py = projected_xy
    hi = min(hand_h_max, camera_h - 0.2)
    near = ((camera_h - hi) / camera_h)
    far = ((camera_h - hand_h_min) / camera_h)
    ax, ay = cx + (px - cx) * near, cy + (py - cy) * near
    bx, by = cx + (px - cx) * far, cy + (py - cy) * far
    best_key, best_t = None, math.inf
    for key, poly in shelves:
        if len(poly) < 3:
            continue
        if point_in_polygon(ax, ay, poly):
            t = 0.0
        else:
            t = math.inf
            n = len(poly)
            for i in range(n):
                (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
                ex, ey = x2 - x1, y2 - y1
                dx, dy = bx - ax, by - ay
                den = dx * ey - dy * ex
                if abs(den) < 1e-12:
                    continue
                tt = ((x1 - ax) * ey - (y1 - ay) * ex) / den
                uu = ((x1 - ax) * dy - (y1 - ay) * dx) / den
                if 0 <= tt <= 1 and 0 <= uu <= 1:
                    t = min(t, tt)
        if t < best_t:
            best_key, best_t = key, t
    return best_key
