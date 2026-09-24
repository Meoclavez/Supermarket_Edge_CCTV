"""Tripwires (entrance in/out counting) and restricted areas, evaluated live.

Geometry and coordinate space
-----------------------------
Tripwires and restricted areas are drawn per camera in Camera Studio and
stored by ``ai_zone_service`` (``storage/zones_config.json``) in **image
coordinates normalised to 0..1 against that camera's own frame** -- the same
space as privacy masks and product shelves (``studio.js`` maps clicks through
the letterboxed content box and divides by the rendered size; the original
Studio tools posted ``x1,y1,x2,y2`` / ``points`` in that space). They are *not*
floor metres, so they work on uncalibrated cameras too.

Evaluation therefore uses each confirmed track's **foot point** (bottom centre
of its last measured box) in pixels, with the rule geometry scaled by the
frame size. Everything here is plain arithmetic on the camera worker thread;
snapshots, database writes and notifications are handed to a background
writer so the capture loop never blocks.

Tripwire crossings
------------------
A tripwire is the segment A=(x1,y1) -> B=(x2,y2). The signed distance of the
foot point from the line (``side_distance``) puts it on the *left* or *right*
of A->B as seen on screen (image y points down). ``in_side`` names the side a
person is on after walking *in*; the Studio arrow points that way.

Jitter never counts:

* a dead band of ``max(TRIPWIRE_HYSTERESIS_FRAC * frame diagonal,
  TRIPWIRE_HYSTERESIS_BOX_FRAC * box height)`` pixels surrounds the line; a
  point inside it changes nothing;
* the committed side only flips after the point has been clear of the band on
  the other side for ``TRIPWIRE_CONFIRM_FRAMES`` consecutive analysed frames;
* the path from the last committed position to the new one must pass through
  the segment itself (walking round the end of a short line is not a
  crossing), and
* a crossing only *settles* (is recorded / alerts) once the track has stayed
  on the new side for ``TRIPWIRE_MIN_REPEAT_SEC`` or has left the view there;
  stepping straight back over the line before that cancels it, so a bounce
  is net zero rather than a lone "in" or "out".

A track's first observation only establishes its side: someone who appears
already past the line has not been seen crossing it.

Restricted areas
----------------
A point-in-polygon test on the normalised foot point. The area is *active*
according to its schedule (rows of days + ``from``/``to`` local times, a row
whose ``to`` is earlier than ``from`` runs past midnight into the next day;
``schedule_mode`` says whether the rows are when the area is restricted or when
presence is allowed; no rows means always restricted). A person must be
inside for ``min_dwell_seconds`` (brief exits up to
``RESTRICTED_AREA_EXIT_GRACE_SEC`` do not reset the clock) while the area is
active, and each (area, track) pair then waits ``cooldown_seconds`` before it
can alert again.

Feature flags and privacy
-------------------------
Crossings are recorded (``tripwire_events``) only while the camera's
``people_counting`` flag is on. Alerts follow each rule's own toggles. People
inside AI_IGNORE privacy masks were already removed before tracking, so they
never reach this module.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from app.config import settings
from app.services.timeutil import to_local, utc_from_ts

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SEVERITIES = ("INFO", "WARNING", "HIGH")
SCHEDULE_MODES = ("restricted_during", "allowed_during")

_logged_once: set[str] = set()


def _log_once(key: str, message: str, level: int = logging.WARNING) -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    logger.log(level, message)


# =========================================================================
# Pure geometry
# =========================================================================

def side_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Signed distance (same units as the inputs) of P from the line A->B.

    Positive = right of A->B as drawn on screen (image y down), negative = left.
    """
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return 0.0
    return (dx * (py - ay) - dy * (px - ax)) / length


def side_name(sign: int) -> str:
    return "right" if sign > 0 else "left"


def path_crosses_segment(p1: tuple[float, float], p2: tuple[float, float],
                         a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when the path P1->P2 passes through segment A-B (endpoints inclusive).

    The caller guarantees P1 and P2 are on opposite sides of the infinite
    line, so the only question is whether the crossing point lies between A
    and B.
    """
    (x1, y1), (x2, y2) = p1, p2
    (ax, ay), (bx, by) = a, b
    rx, ry = x2 - x1, y2 - y1
    sx, sy = bx - ax, by - ay
    denom = rx * sy - ry * sx
    if abs(denom) <= 1e-12:
        return False
    # Parameter along A->B of the intersection point.
    u = ((ax - x1) * ry - (ay - y1) * rx) / denom
    t = ((ax - x1) * sy - (ay - y1) * sx) / denom
    eps = 1e-9
    return -eps <= u <= 1 + eps and -eps <= t <= 1 + eps


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd ray cast. Points exactly on an edge may fall either way."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def hysteresis_band_px(frame_w: int, frame_h: int, box_h: float) -> float:
    diag = math.hypot(frame_w, frame_h)
    return max(settings.TRIPWIRE_HYSTERESIS_FRAC * diag,
               settings.TRIPWIRE_HYSTERESIS_BOX_FRAC * max(0.0, box_h), 1.0)


@dataclass
class WireTrackState:
    """Where one track stands relative to one tripwire."""

    side: int = 0                     # committed side: -1 left, +1 right, 0 unknown
    anchor: Optional[tuple[float, float]] = None   # last point clear of the band on ``side``
    pending_side: int = 0
    pending_count: int = 0
    # A crossing waiting to settle: (side crossed into, time of crossing).
    tentative: Optional[tuple[int, float]] = None
    last_alert_at: Optional[float] = None
    last_bbox: Optional[tuple] = None
    seen_at: float = 0.0


def step_crossing(state: WireTrackState, foot: tuple[float, float],
                  a: tuple[float, float], b: tuple[float, float],
                  band: float, now: float,
                  confirm_frames: Optional[int] = None,
                  min_repeat_sec: Optional[float] = None) -> Optional[tuple[int, float]]:
    """Advance one track against one tripwire (all in pixels).

    Returns ``(side, crossed_at)`` when a crossing *settles*: +1 = crossed into
    the right side of A->B, -1 = into the left. A crossing settles once the
    track has stayed on the new side for ``min_repeat_sec`` (or the track ends
    there, see :func:`settle_crossing`). Crossing straight back before that
    cancels both, so a bounce is net zero instead of one-sided.
    """
    confirm = max(1, int(settings.TRIPWIRE_CONFIRM_FRAMES if confirm_frames is None else confirm_frames))
    repeat = settings.TRIPWIRE_MIN_REPEAT_SEC if min_repeat_sec is None else min_repeat_sec
    state.seen_at = now
    settled = None
    if state.tentative is not None and now - state.tentative[1] >= repeat:
        settled, state.tentative = state.tentative, None
    d = side_distance(foot[0], foot[1], a[0], a[1], b[0], b[1])
    if abs(d) < band:
        # On the line: neither side. Does not reset a pending flip either, so
        # a slow walker who lingers on the line still counts once through.
        return settled
    s = 1 if d > 0 else -1
    if state.side == 0:
        state.side, state.anchor = s, foot
        state.pending_side, state.pending_count = 0, 0
        return settled
    if s == state.side:
        state.anchor = foot
        state.pending_side, state.pending_count = 0, 0
        return settled
    # Clear of the band on the opposite side.
    if state.pending_side != s:
        state.pending_side, state.pending_count = s, 0
    state.pending_count += 1
    if state.pending_count < confirm:
        return settled
    anchor = state.anchor or foot
    through = path_crosses_segment(anchor, foot, a, b)
    state.side, state.anchor = s, foot
    state.pending_side, state.pending_count = 0, 0
    if not through:
        return settled       # went round the end of the line
    if state.tentative is not None:
        state.tentative = None   # bounced straight back: the two crossings cancel
        return settled
    if repeat <= 0:
        return settled or (s, now)
    state.tentative = (s, now)
    return settled


def settle_crossing(state: WireTrackState) -> Optional[tuple[int, float]]:
    """The track ended (left the view, lost): a pending crossing stands."""
    t, state.tentative = state.tentative, None
    return t


# =========================================================================
# Schedules
# =========================================================================

def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes after midnight; '24:00' allowed as end of day."""
    hh, mm = str(value).strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m != 0):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def normalise_days(days: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for d in days:
        if isinstance(d, bool):
            raise ValueError(f"invalid day {d!r}")
        if isinstance(d, int):
            if not 0 <= d <= 6:
                raise ValueError(f"invalid day {d!r} (0=Monday..6=Sunday)")
            key = DAYS[d]
        else:
            key = str(d).strip().lower()[:3]
            if key not in DAYS:
                raise ValueError(f"invalid day {d!r}")
        if key not in out:
            out.append(key)
    return sorted(out, key=DAYS.index)


def resolve_timezone(name: Optional[str]):
    """ZoneInfo for ``name`` (or SITE_TIMEZONE); None means the host's local time."""
    name = (name or settings.SITE_TIMEZONE or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        _log_once(f"tz:{name}", f"Unknown time zone {name!r}; restricted-area schedules use host local time")
        return None


def local_time(now: float, tz_name: Optional[str] = None) -> datetime:
    tz = resolve_timezone(tz_name)
    return datetime.fromtimestamp(now, tz) if tz is not None else datetime.fromtimestamp(now)


def _in_windows(schedule: list[dict], when: datetime) -> bool:
    day = when.weekday()
    minute = when.hour * 60 + when.minute
    today, yesterday = DAYS[day], DAYS[(day - 1) % 7]
    for row in schedule or []:
        try:
            days = normalise_days(row.get("days") or [])
            start = parse_hhmm(row.get("from", "00:00"))
            end = parse_hhmm(row.get("to", "24:00"))
        except Exception:
            continue
        if start == end or (start == 0 and end == 24 * 60):
            if today in days:
                return True
        elif start < end:
            if today in days and start <= minute < end:
                return True
        else:
            # Runs past midnight: the evening part belongs to the listed day,
            # the early-morning part to the day after it.
            if today in days and minute >= start:
                return True
            if yesterday in days and minute < end:
                return True
    return False


def schedule_active(area: dict, when: datetime) -> bool:
    """Whether the restriction applies at local time ``when``."""
    schedule = area.get("schedule") or []
    mode = area.get("schedule_mode") or "restricted_during"
    if not schedule:
        return True
    inside = _in_windows(schedule, when)
    return (not inside) if mode == "allowed_during" else inside


@dataclass
class AreaTrackState:
    entered_at: Optional[float] = None
    last_inside_at: Optional[float] = None
    last_alert_at: Optional[float] = None
    seen_at: float = 0.0


def step_area(state: AreaTrackState, inside: bool, active: bool, now: float,
              min_dwell: float, cooldown: float,
              grace: Optional[float] = None) -> bool:
    """Advance one track against one area; True when an alert should fire now."""
    grace = settings.RESTRICTED_AREA_EXIT_GRACE_SEC if grace is None else grace
    state.seen_at = now
    if inside:
        if state.entered_at is None:
            state.entered_at = now
        state.last_inside_at = now
    elif state.entered_at is not None and state.last_inside_at is not None:
        if now - state.last_inside_at > grace:
            state.entered_at = None
            state.last_inside_at = None
        return False
    if not inside or not active or state.entered_at is None:
        return False
    if now - state.entered_at < max(0.0, min_dwell):
        return False
    if state.last_alert_at is not None and now - state.last_alert_at < cooldown:
        return False
    state.last_alert_at = now
    return True


# =========================================================================
# Engine
# =========================================================================

@dataclass
class TrackView:
    """What the engine needs from a tracker Track (duck-typed for tests)."""

    track_id: str
    bbox: tuple[float, float, float, float]
    keypoints: Any = None


@dataclass
class PendingAlert:
    alert_id: str
    event_type: str
    severity: str
    title: str
    body: str
    data: dict
    frame: Optional[np.ndarray] = None
    bbox: Optional[tuple] = None
    keypoints: Any = None
    caption: str = ""


@dataclass
class CameraRuleState:
    wires: dict = field(default_factory=dict)     # (tw_id, track_id) -> WireTrackState
    areas: dict = field(default_factory=dict)     # (area_id, track_id) -> AreaTrackState
    last_prune: float = 0.0


AsyncDispatch = Callable[[str, str, str, str, dict], Any]


class TripwireEngine:
    """Per-camera tripwire / restricted-area evaluation plus an alert writer."""

    STATE_TTL_SEC = 30.0

    def __init__(self) -> None:
        self._cams: dict[str, CameraRuleState] = {}
        self._lock = threading.Lock()
        self._pending_events: list[dict] = []
        self._events_lock = threading.Lock()
        self._alerts: list[PendingAlert] = []
        self._alerts_lock = threading.Lock()
        self._wake = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Tests: process alerts on the calling thread via process_pending_alerts().
        self.synchronous = False
        # Tests may inject an async callable(event_type, severity, title, body, data).
        self.dispatcher_override: Optional[AsyncDispatch] = None
        self.stats = {"crossings": 0, "alerts": 0, "alerts_delivered": 0, "alert_errors": 0,
                      "events_persisted": 0}
        self.last_delivery: Optional[dict] = None

    # ------------------------------------------------------------ config

    @staticmethod
    def _rules_for(camera_id: str) -> tuple[list[dict], list[dict]]:
        try:
            from app.services.ai_zone_service import ai_zone_service

            data = ai_zone_service.get_all_zones(camera_id)
        except Exception as e:
            _log_once("rules", f"tripwire/restricted-area config unreadable ({e}); not evaluating")
            return [], []
        wires = [w for w in data.get("tripwires", []) if w.get("enabled", True)
                 and all(isinstance(w.get(k), (int, float)) for k in ("x1", "y1", "x2", "y2"))]
        areas = [a for a in data.get("intrusion_zones", []) if a.get("enabled", True)
                 and len(a.get("points") or []) >= 3]
        return wires, areas

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    def reset_camera(self, camera_id: str) -> None:
        with self._lock:
            self._cams.pop(camera_id, None)

    # ------------------------------------------------------------ evaluation

    def evaluate(self, camera_id: str, tracks: Iterable[Any], frame_w: int, frame_h: int,
                 now: float, frame: Optional[np.ndarray] = None,
                 counting: bool = True, camera_name: Optional[str] = None) -> dict:
        """Evaluate confirmed tracks against this camera's rules.

        ``frame`` must already carry the privacy masks: it is only used for
        alert snapshots. Returns ``{"crossings": [...], "alerts": [...]}``
        (alert ids), mainly for tests and diagnostics.
        """
        out = {"crossings": [], "alerts": []}
        if not frame_w or not frame_h:
            return out
        wires, areas = self._rules_for(camera_id)
        tracks = list(tracks)
        with self._lock:
            cs = self._cams.setdefault(camera_id, CameraRuleState())
            if not wires and not areas:
                cs.wires.clear()
                cs.areas.clear()
                return out
            for tw in wires:
                self._eval_wire(cs, camera_id, camera_name, tw, tracks, frame_w, frame_h, now,
                                frame, counting, out)
            for area in areas:
                self._eval_area(cs, camera_id, camera_name, area, tracks, frame_w, frame_h, now,
                                frame, out)
            if now - cs.last_prune > 5.0:
                cs.last_prune = now
                cutoff = now - self.STATE_TTL_SEC
                live_ids = {str(tr.track_id) for tr in tracks}
                wire_ids = {w["id"] for w in wires}
                area_ids = {a["id"] for a in areas}
                cs.wires = {k: v for k, v in cs.wires.items()
                            if k[0] in wire_ids and (k[1] in live_ids or v.seen_at >= cutoff)}
                cs.areas = {k: v for k, v in cs.areas.items()
                            if k[0] in area_ids and (k[1] in live_ids or v.seen_at >= cutoff)}
        return out

    def _eval_wire(self, cs, camera_id, camera_name, tw, tracks, fw, fh, now, frame, counting, out):
        a = (float(tw["x1"]) * fw, float(tw["y1"]) * fh)
        b = (float(tw["x2"]) * fw, float(tw["y2"]) * fh)
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 2.0:
            return
        in_sign = 1 if str(tw.get("in_side", "right")).lower() == "right" else -1
        present: set[str] = set()
        for tr in tracks:
            x1, y1, x2, y2 = (float(v) for v in tr.bbox)
            foot = ((x1 + x2) / 2.0, y2)
            key = (tw["id"], str(tr.track_id))
            present.add(key[1])
            st = cs.wires.get(key)
            if st is None:
                st = cs.wires[key] = WireTrackState()
            st.last_bbox = tuple(tr.bbox)
            crossed = step_crossing(st, foot, a, b, hysteresis_band_px(fw, fh, y2 - y1), now)
            if crossed is not None:
                self._emit_crossing(tw, st, camera_id, camera_name, str(tr.track_id), crossed, in_sign,
                                    now, frame, tr, counting, out)
        # Tracks that are gone (left the view, lost) keep a crossing they had
        # already made but not yet settled.
        for (tw_id, track_id), st in list(cs.wires.items()):
            if tw_id != tw["id"] or track_id in present or st.tentative is None:
                continue
            crossed = settle_crossing(st)
            view = TrackView(track_id=track_id, bbox=st.last_bbox or (0, 0, 0, 0))
            self._emit_crossing(tw, st, camera_id, camera_name, track_id, crossed, in_sign,
                                now, frame if st.last_bbox else None, view, counting, out)

    def _emit_crossing(self, tw, st, camera_id, camera_name, track_id, crossed, in_sign, now,
                       frame, tr, counting, out):
        side, crossed_at = crossed
        direction = "in" if side == in_sign else "out"
        event = {
            "tripwire_id": tw["id"],
            "tripwire_name": tw.get("name") or tw["id"],
            "camera_id": camera_id,
            "track_id": track_id,
            "direction": direction,
            "ts": utc_from_ts(crossed_at),           # stored as naive UTC
            "counts_footfall": bool(tw.get("counts_footfall", True)),
        }
        out["crossings"].append(event)
        self.stats["crossings"] += 1
        if counting:
            with self._events_lock:
                self._pending_events.append(event)
        want = str(tw.get("alert_direction") or "in").lower()
        if tw.get("alert_enabled") and (want == "both" or want == direction):
            cooldown = float(tw.get("alert_cooldown_seconds") or settings.TRIPWIRE_ALERT_COOLDOWN_SEC)
            if st.last_alert_at is None or now - st.last_alert_at >= cooldown:
                st.last_alert_at = now
                name = tw.get("name") or "Tripwire"
                cam = camera_name or camera_id
                alert = self._alert(
                    "TRIPWIRE_ALERT", str(tw.get("severity") or "WARNING").upper(),
                    f"Line crossed ({direction}): {name} - {cam}",
                    f"A person crossed '{name}' going {direction} on {cam}.",
                    {"camera_id": camera_id, "tripwire_id": tw["id"], "tripwire_name": name,
                     "zone_id": tw["id"], "zone_name": name, "track_id": track_id,
                     "direction": direction},
                    frame, tr, f"LINE CROSSED ({direction.upper()}): {name}", now)
                out["alerts"].append(alert.alert_id)

    def _eval_area(self, cs, camera_id, camera_name, area, tracks, fw, fh, now, frame, out):
        try:
            poly = [(float(p["x"]), float(p["y"])) for p in area.get("points") or []]
        except Exception:
            return
        active = schedule_active(area, local_time(now, area.get("timezone")))
        min_dwell = float(area.get("min_dwell_seconds", area.get("dwell_time_seconds", 3.0)) or 0.0)
        cooldown = float(area.get("cooldown_seconds") or settings.RESTRICTED_AREA_COOLDOWN_SEC)
        for tr in tracks:
            x1, y1, x2, y2 = (float(v) for v in tr.bbox)
            nx, ny = ((x1 + x2) / 2.0) / fw, y2 / fh
            key = (area["id"], str(tr.track_id))
            st = cs.areas.get(key)
            if st is None:
                st = cs.areas[key] = AreaTrackState()
            inside = point_in_polygon(nx, ny, poly)
            if not step_area(st, inside, active, now, min_dwell, cooldown):
                continue
            name = area.get("name") or "Restricted area"
            cam = camera_name or camera_id
            dwell = now - (st.entered_at or now)
            alert = self._alert(
                "RESTRICTED_AREA", str(area.get("severity") or "HIGH").upper(),
                f"Person in restricted area: {name} - {cam}",
                f"A person has been inside '{name}' for {dwell:.0f}s while it is restricted.",
                {"camera_id": camera_id, "zone_id": area["id"], "zone_name": name,
                 "area_id": area["id"], "area_name": name, "track_id": str(tr.track_id),
                 "dwell_seconds": round(dwell, 1)},
                frame, tr, f"RESTRICTED AREA: {name}", now)
            out["alerts"].append(alert.alert_id)

    def _alert(self, event_type, severity, title, body, data, frame, tr, caption, now) -> PendingAlert:
        if severity not in SEVERITIES:
            severity = "WARNING"
        alert_id = f"za_{uuid.uuid4().hex[:12]}"
        data = dict(data)
        data.update({
            "event_type": event_type,
            "severity": severity,
            "alert_id": alert_id,
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        })
        alert = PendingAlert(
            alert_id=alert_id, event_type=event_type, severity=severity, title=title, body=body,
            data=data, frame=None if frame is None else frame.copy(), bbox=tuple(tr.bbox),
            keypoints=getattr(tr, "keypoints", None), caption=caption,
        )
        self.stats["alerts"] += 1
        with self._alerts_lock:
            self._alerts.append(alert)
        if not self.synchronous:
            self._ensure_writer()
            self._wake.set()
        return alert

    # ------------------------------------------------------------ crossings out

    def drain_events(self) -> list[dict]:
        with self._events_lock:
            events, self._pending_events = self._pending_events, []
        return events

    # ------------------------------------------------------------ alert writer

    @staticmethod
    def evidence_dir() -> Path:
        d = (Path(settings.ZONE_ALERT_EVIDENCE_DIR) if settings.ZONE_ALERT_EVIDENCE_DIR
             else Path(settings.STORAGE_DIR) / "zone_alerts")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def snapshot_path(cls, alert_id: str) -> Optional[Path]:
        if not alert_id.replace("_", "").isalnum():
            return None
        p = cls.evidence_dir() / f"{alert_id}.jpg"
        return p if p.is_file() else None

    def _write_snapshot(self, alert: PendingAlert) -> Optional[str]:
        if alert.frame is None:
            return None
        try:
            import cv2
        except Exception:
            return None
        try:
            try:
                from app.services.pose_analytics import render_evidence

                kps = alert.keypoints if isinstance(alert.keypoints, np.ndarray) else None
                img = render_evidence(alert.frame, alert.bbox, kps, alert.caption,
                                      f"{alert.data.get('camera_id')}  track {alert.data.get('track_id')}  "
                                      f"{alert.data.get('timestamp', '')[:19].replace('T', ' ')} UTC")
            except Exception as e:  # helper missing: the masked frame with the box drawn
                _log_once("render_evidence", f"render_evidence unavailable ({e}); saving the plain masked frame")
                img = alert.frame.copy()
                if alert.bbox:
                    x1, y1, x2, y2 = (int(v) for v in alert.bbox)
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 165, 255), 2)
            path = self.evidence_dir() / f"{alert.alert_id}.jpg"
            ok = cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.THEFT_EVIDENCE_JPEG_QUALITY)])
            return str(path) if ok else None
        except Exception as e:
            logger.error(f"Could not write zone alert snapshot {alert.alert_id}: {e}")
            return None

    def _dispatcher(self) -> AsyncDispatch:
        if self.dispatcher_override is not None:
            return self.dispatcher_override
        try:
            from app.services.alert_dispatcher import dispatch  # built by the pairing/push agent

            return dispatch
        except Exception:
            from app.services.notification_service import notification_service

            async def _fallback(event_type, severity, title, body, data):
                payload = dict(data)
                payload["event_type"] = event_type
                payload["severity"] = severity
                return await notification_service.notify_loss_prevention(title, body, payload)

            return _fallback

    def _deliver(self, alert: PendingAlert) -> None:
        snap = self._write_snapshot(alert)
        if snap:
            url = f"/api/zones/alerts/{alert.alert_id}/snapshot"
            alert.data["snapshot_url"] = url
            alert.data["evidence_snapshot_url"] = url
        alert.frame = None
        dispatch = self._dispatcher()
        coro_fn = lambda: dispatch(alert.event_type, alert.severity, alert.title, alert.body, dict(alert.data))

        async def _send():
            try:
                result = await asyncio.wait_for(coro_fn(), timeout=15.0)
                self.stats["alerts_delivered"] += 1
                self.last_delivery = {"alert_id": alert.alert_id, "result": result}
            except Exception as e:
                self.stats["alert_errors"] += 1
                self.last_delivery = {"alert_id": alert.alert_id, "error": str(e)}
                logger.error(f"{alert.event_type} alert {alert.alert_id} not delivered: {e}")

        loop = self._loop
        if loop is None:
            try:
                from app.services.pose_analytics import pose_analytics

                loop = getattr(pose_analytics, "_loop", None)
            except Exception:
                loop = None
        if loop is not None and not loop.is_closed() and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_send(), loop)
            if self.synchronous:
                try:
                    fut.result(timeout=20)
                except Exception:
                    pass
            return
        if self.synchronous or self.dispatcher_override is not None:
            asyncio.run(_send())
            return
        self.stats["alert_errors"] += 1
        logger.warning(f"{alert.event_type} alert {alert.alert_id} not sent: no application event loop bound")

    def process_pending_alerts(self) -> int:
        with self._alerts_lock:
            batch, self._alerts = self._alerts, []
        for alert in batch:
            try:
                self._deliver(alert)
            except Exception as e:
                self.stats["alert_errors"] += 1
                logger.error(f"zone alert {alert.alert_id} failed: {e}")
        return len(batch)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer = threading.Thread(target=self._writer_loop, name="zone-alert-writer", daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            self.process_pending_alerts()

    def status(self) -> dict:
        with self._lock:
            cams = {cid: {"wire_states": len(cs.wires), "area_states": len(cs.areas)}
                    for cid, cs in self._cams.items()}
        return {"stats": dict(self.stats), "cameras": cams, "last_delivery": self.last_delivery}


tripwire_engine = TripwireEngine()


# =========================================================================
# Persistence and aggregates
# =========================================================================

async def flush_tripwire_events(session_factory=None) -> int:
    """Move buffered crossings into ``tripwire_events``. Returns rows written."""
    events = tripwire_engine.drain_events()
    if not events:
        return 0
    from app.models.db_models import TripwireEventModel

    if session_factory is None:
        from app.database import async_session_factory as session_factory
    try:
        async with session_factory() as db:
            for e in events:
                db.add(TripwireEventModel(
                    tripwire_id=e["tripwire_id"], tripwire_name=e.get("tripwire_name"),
                    camera_id=e["camera_id"], track_id=e["track_id"], direction=e["direction"],
                    ts=e["ts"], counts_footfall=bool(e.get("counts_footfall", True)),
                ))
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to persist {len(events)} tripwire crossing(s): {e}")
        with tripwire_engine._events_lock:          # keep them for the next flush
            tripwire_engine._pending_events[:0] = events[-5000:]
        return 0
    tripwire_engine.stats["events_persisted"] += len(events)
    return len(events)


async def tripwire_footfall(db, start: datetime, end: datetime, bucket: str = "hour") -> dict:
    """In/out per tripwire over [start, end), optionally bucketed by hour or day.

    ``start``/``end`` are naive UTC (see services/timeutil.py). Buckets are
    store-local hours/days: crossings are grouped per UTC minute in SQL and
    folded into local buckets here, which stays right for DST and for
    half-hour time zones. A tripwire with no recorded crossing in the window
    reports ``null`` in/out (not observed), never 0.
    ``net_occupancy_estimate`` = entries - exits over footfall-counting lines,
    floored at 0; it drifts with any missed crossing and is labelled an
    estimate.
    """
    from sqlalchemy import and_, func, select

    from app.models.db_models import TripwireEventModel as T

    window = and_(T.ts >= start, T.ts < end)
    rows = (await db.execute(
        select(T.tripwire_id, T.camera_id, T.direction, func.count(T.id),
               func.max(T.tripwire_name), func.max(T.counts_footfall))
        .where(window).group_by(T.tripwire_id, T.camera_id, T.direction)
    )).all()

    per: dict[str, dict] = {}
    for tw_id, cam_id, direction, n, name, counts in rows:
        rec = per.setdefault(tw_id, {"tripwire_id": tw_id, "camera_id": cam_id, "name": name,
                                     "counts_footfall": bool(counts), "in": 0, "out": 0,
                                     "observed": True, "configured": False, "buckets": []})
        rec["in" if direction == "in" else "out"] += int(n)

    try:
        from app.services.ai_zone_service import ai_zone_service

        configured = ai_zone_service.get_all_zones().get("tripwires", [])
    except Exception:
        configured = []
    for tw in configured:
        rec = per.get(tw["id"])
        if rec is None:
            per[tw["id"]] = {"tripwire_id": tw["id"], "camera_id": tw.get("camera_id"),
                             "name": tw.get("name") or tw["id"],
                             "counts_footfall": bool(tw.get("counts_footfall", True)),
                             "in": None, "out": None, "observed": False, "configured": True,
                             "enabled": bool(tw.get("enabled", True)), "buckets": []}
        else:
            rec["configured"] = True
            rec["name"] = tw.get("name") or rec["name"]
            rec["counts_footfall"] = bool(tw.get("counts_footfall", True))
            rec["enabled"] = bool(tw.get("enabled", True))

    if bucket in ("hour", "day") and rows:
        minute = func.strftime("%Y-%m-%d %H:%M", T.ts)
        brows = (await db.execute(
            select(T.tripwire_id, minute, T.direction, func.count(T.id))
            .where(window).group_by(T.tripwire_id, minute, T.direction)
        )).all()
        agg: dict[tuple, dict] = {}
        for tw_id, m, direction, n in brows:
            if tw_id not in per or not m:
                continue
            local = to_local(datetime.strptime(m, "%Y-%m-%d %H:%M"))
            b = local.replace(minute=0, second=0, microsecond=0)
            if bucket == "day":
                b = b.replace(hour=0)
            rec = agg.setdefault((tw_id, b.isoformat()), {"start": b.isoformat(), "in": 0, "out": 0})
            rec["in" if direction == "in" else "out"] += int(n)
        for (tw_id, _), rec in sorted(agg.items(), key=lambda kv: kv[0][1]):
            per[tw_id]["buckets"].append(rec)

    lines = sorted(per.values(), key=lambda r: (r.get("name") or "", r["tripwire_id"]))
    for r in lines:
        r["net"] = (r["in"] - r["out"]) if r["observed"] else None
    counted = [r for r in lines if r["observed"] and r["counts_footfall"]]
    total_in = sum(r["in"] for r in counted) if counted else None
    total_out = sum(r["out"] for r in counted) if counted else None
    return {
        "from": to_local(start).isoformat(),
        "to": to_local(end).isoformat(),
        "bucket": bucket,
        "tripwires": lines,
        "totals": {"in": total_in, "out": total_out,
                   "net": (total_in - total_out) if counted else None},
        "observed": bool(counted),
        "net_occupancy_estimate": max(0, total_in - total_out) if counted else None,
        "estimate_note": ("Entries minus exits on footfall-counting lines since the start of the "
                          "window. An estimate: every missed or doubled crossing shifts it, and "
                          "people inside before the window began are not included."),
    }


async def tripwire_entries(db, start: datetime, end: datetime) -> Optional[int]:
    """Entries ('in' crossings) on footfall-counting tripwires, or None if none recorded.

    ``start``/``end`` are naive UTC, like the stored ``ts``.
    """
    from sqlalchemy import and_, func, select

    from app.models.db_models import TripwireEventModel as T

    n = await db.scalar(select(func.count(T.id)).where(and_(
        T.ts >= start, T.ts < end, T.direction == "in", T.counts_footfall.is_(True))))
    return int(n) if n else None
