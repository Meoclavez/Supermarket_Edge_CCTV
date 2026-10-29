"""Restricted areas, high security: settings, arming, announcements and audit.

What a restricted area is
-------------------------
A place (stockroom, cash office, safe room, after-hours floor) where every
movement is tracked and flagged at once while the area is **armed**:

* **Where** -- a store-map zone (``floor_zone_id``, floor metres: works on
  every *calibrated* camera that sees it, and on an uncalibrated camera linked
  to that zone with "Which area this camera watches", whose whole view then
  counts) and/or per-camera image regions for uncalibrated cameras
  (``camera_regions``: ``{camera_id, intrusion_zone_id}`` drawn with Camera
  setup's restricted-area tool, or ``{camera_id, points}`` normalised 0..1).
* **When** -- ``arming``: ``always`` or ``schedule`` (weekly rows of
  ``days`` + ``from``/``to`` in store time; ``schedule_mode`` ``armed_during``
  (default) or ``armed_outside``; a row whose ``to`` is earlier than ``from``
  runs past midnight). An administrator may arm or disarm by hand with a reason
  (``manual_state`` until ``manual_until``); operators may arm (raise security)
  but only administrators disarm. Everything is audited.
* **Who may be inside** -- "I'm going in" announcements: a signed-in person
  (operators included) announces entry for 5-120 minutes; while announced the
  area is armed but not *guarded*: entry is expected. Ending early ("I'm out")
  or the announcement running out with someone still inside raises "Stayed
  longer than announced" after a grace period.
* **What is flagged** -- the triggers in :data:`TRIGGERS`, each on/off with
  its thresholds, an alert level (tier) and a repeat cooldown. The levels use
  the theft alert routing (``theft_alert_policy.channels``), so Critical gets
  banner + sound + every phone + repeats, like a Critical theft alert.

Privacy first: an area (or camera region) that lies inside privacy masks for
``RESTRICTED_MASK_REFUSE_FRACTION`` or more is refused with a clear message;
AI_IGNORE areas keep dropping detections before anything here sees them, and
evidence is always the privacy-masked frame. No face recognition, no identity:
a track id and pose keypoints, nothing else.

This module is the settings side (validation, the database, the compiled
snapshot the camera workers read). The live evaluation is
``services/restricted_area_engine.py``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("edge.restricted_areas")

TIERS = ("review", "watch", "alert", "critical")
OUTCOMES = {
    "false_alarm": "False alarm",
    "authorised": "Authorised (staff or contractor)",
    "incident": "Real incident",
}
ARMING_MODES = ("always", "schedule")
# Where an armed schedule comes from: left out = typed by hand.
ARMING_SOURCES = ("store_hours",)
SCHEDULE_MODES = ("armed_during", "armed_outside")
ANNOUNCE_MIN, ANNOUNCE_MAX, ANNOUNCE_DEFAULT = 5, 120, 15
ANNOUNCE_PEOPLE_MAX = 10
# "I'm out" with someone still inside: they have this long to walk out.
EARLY_END_GRACE_SEC = 15.0
MANUAL_MAX_MIN = 12 * 60
NAME_MAX = 64
NOTE_MAX = 200
REASON_MAX = 200
MAX_AREAS = 32
MAX_POIS = 12
MAX_REGION_POINTS = 64
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# --------------------------------------------------------------------------- trigger catalogue
#
# One entry per trigger: plain-language label / explanation / example for the
# dashboard, default tier, whether it applies while someone has announced entry
# by default, and its parameters {name: (default, min, max, unit, label)}.
# ``bool`` parameters have (default, None, None, "bool", label).

def _p(default, lo, hi, unit, label):
    return {"default": default, "min": lo, "max": hi, "unit": unit, "label": label}


TRIGGERS: Dict[str, Dict[str, Any]] = {
    "unannounced_entry": {
        "label": "Entry without notice",
        "explain": ("Someone is inside while the area is armed and nobody tapped \"I'm going in\". "
                    "The foot point must be inside for the minimum time, so a flicker or a shadow "
                    "does not count."),
        "example": "The cash office at 2 am: anyone inside is an urgent alert within a second.",
        "tier": "critical", "while_announced": False, "cooldown": 60,
        "params": {"min_presence_sec": _p(0.5, 0.2, 5.0, "s", "Inside for at least")},
    },
    "announced_overstay": {
        "label": "Stayed longer than announced",
        "explain": ("Someone announced entry but is still inside after the announcement ran out "
                    "(plus the grace time), or after \"I'm out\" was tapped."),
        "example": "Announced 15 minutes in the stockroom, still there after 17 minutes.",
        "tier": "alert", "while_announced": True, "cooldown": 300,
        "params": {"grace_min": _p(2.0, 0.0, 30.0, "min", "Grace after the announced time")},
    },
    "dwell": {
        "label": "Inside too long",
        "explain": "Someone has been inside longer than the set time while the area is armed.",
        "example": "More than 60 seconds in the safe room.",
        "tier": "watch", "while_announced": False, "cooldown": 300,
        "params": {"max_seconds": _p(60.0, 5.0, 3600.0, "s", "Longer than")},
    },
    "speed": {
        "label": "Moving fast (running)",
        "explain": ("Someone moves faster than the set speed. On a calibrated camera the speed is "
                    "measured on the floor in metres per second; on an uncalibrated camera it is "
                    "estimated in body heights per second, which is less accurate."),
        "example": "Running out of the stockroom at more than 2.5 m/s.",
        "tier": "alert", "while_announced": False, "cooldown": 60,
        "params": {"max_mps": _p(2.5, 1.0, 6.0, "m/s", "Faster than (calibrated)"),
                   "max_bh_per_sec": _p(1.5, 0.5, 4.0, "body heights/s", "Faster than (uncalibrated, less accurate)"),
                   "min_duration_sec": _p(0.6, 0.3, 3.0, "s", "For at least")},
    },
    "pacing": {
        "label": "Pacing back and forth",
        "explain": "Someone walks back and forth: the direction reverses several times in a short time.",
        "example": "Walking up and down in front of the safe three times within 30 seconds.",
        "tier": "alert", "while_announced": False, "cooldown": 120,
        "params": {"reversals": _p(3, 2, 10, "turns", "Direction changes"),
                   "window_sec": _p(30.0, 10.0, 120.0, "s", "Within"),
                   "min_leg_m": _p(0.8, 0.3, 5.0, "m", "Each walk at least (calibrated)"),
                   "min_leg_bh": _p(0.5, 0.2, 3.0, "body heights", "Each walk at least (uncalibrated)")},
    },
    "erratic": {
        "label": "Zig-zag or erratic movement",
        "explain": "Sharp turns left and right in quick succession, as when looking for a way out or avoiding a camera.",
        "example": "Four sharp alternating turns within 12 seconds.",
        "tier": "alert", "while_announced": False, "cooldown": 120,
        "params": {"turns": _p(4, 3, 12, "turns", "Sharp turns"),
                   "window_sec": _p(12.0, 5.0, 60.0, "s", "Within"),
                   "turn_deg": _p(60.0, 40.0, 150.0, "degrees", "Sharper than")},
    },
    "crouch": {
        "label": "Crouching or low posture",
        "explain": ("The body pose shows someone crouching, kneeling or bending low (hips near knee "
                    "height) for the set time. Needs the hips and knees to be visible."),
        "example": "Kneeling at the safe or under a desk.",
        "tier": "alert", "while_announced": False, "cooldown": 120,
        "params": {"min_seconds": _p(2.0, 0.5, 30.0, "s", "For at least")},
    },
    "reach_up": {
        "label": "Reaching up high",
        "explain": "A hand is held above the head (elbow above the shoulder) for the set time.",
        "example": "Reaching to a top shelf of cigarettes or the cash box on a high shelf.",
        "tier": "alert", "while_announced": False, "cooldown": 120,
        "params": {"min_seconds": _p(1.0, 0.3, 10.0, "s", "For at least")},
    },
    "poi_approach": {
        "label": "Near a marked point",
        "explain": ("Someone comes within the set distance of a point you marked (safe, cash "
                    "drawer, key box). Needs at least one point of interest."),
        "example": "Within 1 m of the safe for a second.",
        "tier": "alert", "while_announced": False, "cooldown": 120,
        "params": {"distance_m": _p(1.0, 0.2, 5.0, "m", "Closer than (calibrated)"),
                   "distance_bh": _p(0.6, 0.2, 3.0, "body heights", "Closer than (uncalibrated)"),
                   "min_seconds": _p(1.0, 0.0, 10.0, "s", "For at least")},
    },
    "boundary_linger": {
        "label": "Lingering at the edge",
        "explain": ("Someone waits just outside the area, near its edge, without going in: peeking "
                    "in or waiting for the door. Only cameras that see the outside of the area can tell."),
        "example": "Standing at the cash office door for 10 seconds while it is armed.",
        "tier": "watch", "while_announced": False, "cooldown": 120,
        "params": {"distance_m": _p(1.0, 0.2, 3.0, "m", "Within (calibrated)"),
                   "distance_bh": _p(0.5, 0.1, 2.0, "body heights", "Within (uncalibrated)"),
                   "min_seconds": _p(8.0, 3.0, 120.0, "s", "For at least")},
    },
    "people_count": {
        "label": "Too many people inside",
        "explain": ("More people inside than allowed. While someone announced entry, the number "
                    "they announced is allowed too."),
        "example": "Two people in the cash office when only one is allowed.",
        "tier": "alert", "while_announced": True, "cooldown": 120,
        "params": {"max_people": _p(1, 1, 20, "people", "More than")},
    },
    "wrong_door": {
        "label": "Entry not through the door",
        "explain": ("Someone came in without crossing one of the door lines you chose (lines drawn "
                    "in Camera setup on the same camera)."),
        "example": "Climbing in through the loading bay instead of the staff door.",
        "tier": "alert", "while_announced": True, "cooldown": 120, "enabled": False,
        "params": {"window_sec": _p(4.0, 1.0, 15.0, "s", "Door crossed within")},
    },
    "tamper": {
        "label": "Camera tampered with",
        "explain": ("A camera that watches the area stops sending pictures, is covered or blacked "
                    "out, or is turned to look elsewhere. Checked carefully to avoid false alarms: "
                    "a lighting change is not tampering."),
        "example": "The cash office camera goes dark at 1 am.",
        "tier": "critical", "while_announced": True, "cooldown": 300,
        "params": {"offline_sec": _p(20.0, 5.0, 300.0, "s", "No picture for"),
                   "covered_sec": _p(10.0, 3.0, 120.0, "s", "Covered for"),
                   "moved_sec": _p(15.0, 5.0, 300.0, "s", "Turned away for")},
    },
}
TRIGGER_ORDER = tuple(TRIGGERS)
# The triggers Easy setup shows (the rest are in Full settings).
MAIN_TRIGGERS = ("unannounced_entry", "dwell", "speed", "pacing", "people_count", "tamper")

DEFAULT_SECURITY = {"confidence_delta": 0.05}
SECURITY_LIMITS = {"confidence_delta": (0.0, 0.2)}


class AreaError(ValueError):
    """A restricted-area request that cannot be accepted (shown to the user as is)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ts_of(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def dt_of(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _num(value: Any, lo: float, hi: float, what: str, integer: bool = False) -> float:
    if isinstance(value, bool):
        raise AreaError(f"{what} must be a number.")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise AreaError(f"{what} must be a number.")
    if not math.isfinite(v) or v < lo or v > hi:
        raise AreaError(f"{what} must be between {lo:g} and {hi:g}.")
    return int(round(v)) if integer else round(v, 3)


def _text(value: Any, max_len: int, what: str, required: bool = False) -> Optional[str]:
    s = " ".join(str(value or "").split())
    if required and not s:
        raise AreaError(f"{what} is required.")
    if len(s) > max_len:
        raise AreaError(f"{what} is too long (at most {max_len} characters).")
    return s or None


def default_triggers() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for tid, spec in TRIGGERS.items():
        cfg = {"enabled": spec.get("enabled", True), "tier": spec["tier"],
               "cooldown_sec": float(spec["cooldown"]), "while_announced": bool(spec["while_announced"])}
        for name, p in spec["params"].items():
            cfg[name] = p["default"]
        if tid == "wrong_door":
            cfg["door_line_ids"] = []
        out[tid] = cfg
    return out


def merge_triggers(stored: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    """Stored trigger settings over the defaults (unknown keys dropped)."""
    out = default_triggers()
    for tid, cfg in (stored or {}).items():
        if tid in out and isinstance(cfg, dict):
            for k, v in cfg.items():
                if k in out[tid]:
                    out[tid][k] = v
    return out


def validate_triggers(raw: Any, base: Optional[dict] = None) -> Dict[str, Dict[str, Any]]:
    """Merge a (partial) trigger update over ``base``; raises AreaError."""
    out = merge_triggers(base)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise AreaError("triggers must be an object of trigger settings.")
    for tid, cfg in raw.items():
        spec = TRIGGERS.get(tid)
        if spec is None:
            raise AreaError(f"Unknown trigger '{tid}'.")
        if not isinstance(cfg, dict):
            raise AreaError(f"Settings of '{spec['label']}' must be an object.")
        cur = out[tid]
        for k, v in cfg.items():
            if k == "enabled" or k == "while_announced":
                cur[k] = bool(v)
            elif k == "tier":
                if v not in TIERS:
                    raise AreaError(f"Alert level of '{spec['label']}' must be one of {', '.join(TIERS)}.")
                cur[k] = v
            elif k == "cooldown_sec":
                cur[k] = _num(v, 5, 3600, f"{spec['label']}: repeat after")
            elif k == "door_line_ids" and tid == "wrong_door":
                if not isinstance(v, list) or len(v) > 16:
                    raise AreaError("Door lines must be a list of up to 16 line ids.")
                cur[k] = [str(x)[:64] for x in v if str(x).strip()]
            elif k in spec["params"]:
                p = spec["params"][k]
                cur[k] = _num(v, p["min"], p["max"], f"{spec['label']}: {p['label']}",
                              integer=isinstance(p["default"], int) and not isinstance(p["default"], bool))
            else:
                raise AreaError(f"Unknown setting '{k}' for '{spec['label']}'.")
    if out["wrong_door"]["enabled"] and not out["wrong_door"]["door_line_ids"]:
        raise AreaError("Entry not through the door needs at least one door line. Draw a line in Camera "
                        "setup on a camera that sees the door, then choose it here.")
    return out


def validate_schedule_rows(rows: Any) -> List[Dict[str, Any]]:
    from app.services.tripwire_engine import normalise_days, parse_hhmm

    if not isinstance(rows, list) or len(rows) > 14:
        raise AreaError("The schedule must be a list of up to 14 rows.")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise AreaError("Each schedule row needs days, from and to.")
        try:
            days = normalise_days(row.get("days") or [])
            start, end = str(row.get("from") or ""), str(row.get("to") or "")
            parse_hhmm(start)
            parse_hhmm(end)
        except (ValueError, AttributeError) as e:
            raise AreaError(f"Schedule row not valid: {e}.")
        if not days:
            raise AreaError("Each schedule row needs at least one day.")
        out.append({"days": days, "from": start, "to": end})
    return out


def validate_arming(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {"mode": "always", "schedule": [], "schedule_mode": "armed_during"}
    if not isinstance(raw, dict):
        raise AreaError("arming must be an object.")
    mode = raw.get("mode") or "always"
    if mode not in ARMING_MODES:
        raise AreaError("Arming must be 'always' or 'schedule'.")
    smode = raw.get("schedule_mode") or "armed_during"
    if smode not in SCHEDULE_MODES:
        raise AreaError("schedule_mode must be 'armed_during' or 'armed_outside'.")
    rows = validate_schedule_rows(raw.get("schedule") or [])
    if mode == "schedule" and not rows:
        raise AreaError("Add at least one time when the area is armed, or choose \"Always armed\".")
    out = {"mode": mode, "schedule": rows, "schedule_mode": smode}
    # "Outside store hours" (services/automation.py): the schedule is rewritten
    # whenever the learnt or edited store hours change.
    source = raw.get("source")
    if source not in (None, "", *ARMING_SOURCES):
        raise AreaError("arming.source must be 'store_hours' or left out.")
    if source:
        out["source"] = source
    tz = str(raw.get("timezone") or "").strip()
    if tz:
        # Only for an area that must follow another zone than the store's.
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            raise AreaError(f"Unknown time zone '{tz[:64]}'. Leave it empty to use the store's time zone.")
        out["timezone"] = tz[:64]
    return out


def validate_security(raw: Any, base: Optional[dict] = None) -> Dict[str, Any]:
    out = dict(DEFAULT_SECURITY)
    out.update({k: v for k, v in (base or {}).items() if k in DEFAULT_SECURITY})
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise AreaError("security must be an object.")
    for k, v in raw.items():
        if k not in SECURITY_LIMITS:
            raise AreaError(f"Unknown security setting '{k}'.")
        lo, hi = SECURITY_LIMITS[k]
        out[k] = _num(v, lo, hi, "Extra sensitivity inside the area")
    return out


def _norm_points(points: Any, what: str) -> List[Dict[str, float]]:
    if not isinstance(points, list) or not 3 <= len(points) <= MAX_REGION_POINTS:
        raise AreaError(f"{what} needs 3 to {MAX_REGION_POINTS} points.")
    out = []
    for p in points:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (TypeError, KeyError, ValueError):
            raise AreaError(f"{what}: each point needs x and y.")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise AreaError(f"{what}: points are fractions of the picture (0 to 1).")
        out.append({"x": round(x, 4), "y": round(y, 4)})
    return out


def validate_regions(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 32:
        raise AreaError("camera_regions must be a list of up to 32 camera regions.")
    out = []
    seen = set()
    for r in raw:
        if not isinstance(r, dict) or not r.get("camera_id"):
            raise AreaError("Each camera region needs a camera.")
        cam = str(r["camera_id"])[:64]
        rec: Dict[str, Any] = {"camera_id": cam}
        if r.get("intrusion_zone_id"):
            rec["intrusion_zone_id"] = str(r["intrusion_zone_id"])[:64]
            key = ("iz", rec["intrusion_zone_id"])
        else:
            rec["points"] = _norm_points(r.get("points"), "A camera region")
            key = ("pts", cam, tuple((p["x"], p["y"]) for p in rec["points"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def validate_pois(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_POIS:
        raise AreaError(f"At most {MAX_POIS} points of interest per area.")
    out = []
    for p in raw:
        if not isinstance(p, dict):
            raise AreaError("Each point of interest needs a name and a place.")
        name = _text(p.get("name"), 40, "Point name", required=True)
        rec: Dict[str, Any] = {"id": str(p.get("id") or new_id("poi"))[:40], "name": name}
        if p.get("camera_id"):
            rec["camera_id"] = str(p["camera_id"])[:64]
            rec["u"] = _num(p.get("u"), 0.0, 1.0, f"{name}: position across the picture")
            rec["v"] = _num(p.get("v"), 0.0, 1.0, f"{name}: position down the picture")
        else:
            rec["x"] = _num(p.get("x"), -1000, 1000, f"{name}: x (metres)")
            rec["y"] = _num(p.get("y"), -1000, 1000, f"{name}: y (metres)")
        out.append(rec)
    return out


# --------------------------------------------------------------------------- arming

def schedule_armed(arming: dict, now: float) -> bool:
    from app.services.tripwire_engine import local_time, schedule_active

    if (arming or {}).get("mode", "always") != "schedule":
        return True
    rows = arming.get("schedule") or []
    if not rows:
        return False
    when = local_time(now, arming.get("timezone"))
    mode = "restricted_during" if arming.get("schedule_mode", "armed_during") == "armed_during" else "allowed_during"
    return schedule_active({"schedule": rows, "schedule_mode": mode}, when)


def next_schedule_change(arming: dict, now: float, days: int = 8) -> Optional[float]:
    """Epoch seconds of the next time the schedule flips armed <-> disarmed (None: never).

    Candidates are every row boundary on the next ``days`` store-local dates,
    built from wall-clock times with zoneinfo (DST-correct: a time in the
    skipped hour is read with the offset before the change).
    """
    from app.services.tripwire_engine import local_time, parse_hhmm, resolve_timezone

    if (arming or {}).get("mode", "always") != "schedule" or not arming.get("schedule"):
        return None
    tz = resolve_timezone(arming.get("timezone"))
    start_local = local_time(now, arming.get("timezone"))
    minutes = set()
    for row in arming["schedule"]:
        for key in ("from", "to"):
            try:
                minutes.add(parse_hhmm(row.get(key, "00:00")))
            except ValueError:
                pass
    cands = []
    for d in range(days + 1):
        day = (start_local + timedelta(days=d)).date()
        for m in minutes:
            hh, mm = divmod(m, 60)
            if hh == 24:
                base = datetime(day.year, day.month, day.day) + timedelta(days=1)
                hh, mm = 0, 0
            else:
                base = datetime(day.year, day.month, day.day)
            local = base.replace(hour=hh, minute=mm)
            if tz is not None:
                local = local.replace(tzinfo=tz)
                ts = local.timestamp()
            else:
                ts = local.timestamp()
            if ts > now + 0.5:
                cands.append(ts)
    current = schedule_armed(arming, now)
    for ts in sorted(set(cands)):
        if schedule_armed(arming, ts + 1.0) != current:
            return ts
    return None


@dataclass
class Announcement:
    id: str
    area_id: str
    user_id: Optional[str]
    user_name: str
    note: Optional[str]
    people: int
    started_at: float
    until: float
    ended_at: Optional[float]
    source: Optional[str] = None

    @property
    def end(self) -> float:
        return min(self.until, self.ended_at) if self.ended_at is not None else self.until

    def active(self, now: float) -> bool:
        return self.started_at <= now < self.end

    def grace_until(self, overstay_grace_min: float) -> float:
        """Until when people who were inside before the end are still covered."""
        if self.ended_at is not None and self.ended_at < self.until:
            return self.end + EARLY_END_GRACE_SEC
        return self.end + max(0.0, overstay_grace_min) * 60.0

    def to_dict(self) -> dict:
        return {"id": self.id, "area_id": self.area_id, "user_id": self.user_id, "user_name": self.user_name,
                "note": self.note, "people": self.people, "source": self.source,
                "started_at": iso(self.started_at), "until": iso(self.until),
                "ended_at": iso(self.ended_at) if self.ended_at is not None else None}


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CompiledArea:
    """Everything the camera workers need about one area, read-only."""

    id: str
    name: str
    enabled: bool
    arming: dict
    triggers: Dict[str, Dict[str, Any]]
    security: dict
    pois: List[dict]
    floor_zone_id: Optional[str] = None
    floor_polygon: Optional[List[Tuple[float, float]]] = None
    # camera_id -> list of normalised polygons (explicit regions)
    regions: Dict[str, List[List[Tuple[float, float]]]] = field(default_factory=dict)
    # cameras that test the floor polygon through their homography
    floor_cameras: Dict[str, float] = field(default_factory=dict)   # camera_id -> covered fraction
    # uncalibrated cameras linked to the floor zone: their whole view is inside
    linked_cameras: List[str] = field(default_factory=list)
    # camera_id -> {line_id: ((x1, y1), (x2, y2))} normalised door lines
    door_lines: Dict[str, Dict[str, tuple]] = field(default_factory=dict)
    manual_state: Optional[str] = None
    manual_until: Optional[float] = None
    manual_reason: Optional[str] = None
    manual_by: Optional[str] = None
    announcements: List[Announcement] = field(default_factory=list)
    # camera_id -> reason: regions dropped because privacy masks cover them
    masked: Dict[str, str] = field(default_factory=dict)
    legacy_intrusion_id: Optional[str] = None

    def cameras(self) -> List[str]:
        out = list(self.regions)
        for c in list(self.floor_cameras) + list(self.linked_cameras):
            if c not in out:
                out.append(c)
        return out

    def armed_state(self, now: float) -> Tuple[bool, str]:
        """(armed, source): source is manual | always | schedule | off."""
        if not self.enabled:
            return False, "off"
        if self.manual_state in ("armed", "disarmed") and (self.manual_until is None or now < self.manual_until):
            return self.manual_state == "armed", "manual"
        if (self.arming or {}).get("mode", "always") != "schedule":
            return True, "always"
        return schedule_armed(self.arming, now), "schedule"

    def active_announcements(self, now: float) -> List[Announcement]:
        return [a for a in self.announcements if a.active(now)]

    def announced_people(self, now: float) -> int:
        return sum(max(1, a.people) for a in self.active_announcements(now))


@dataclass
class Snapshot:
    areas: List[CompiledArea] = field(default_factory=list)
    by_camera: Dict[str, List[CompiledArea]] = field(default_factory=dict)
    linked_intrusion_ids: frozenset = frozenset()
    built_at: float = 0.0

    def area(self, area_id: str) -> Optional[CompiledArea]:
        for a in self.areas:
            if a.id == area_id:
                return a
        return None


# --------------------------------------------------------------------------- geometry helpers

def _raster_fraction(poly: List[Tuple[float, float]], masks: List[List[Tuple[float, float]]],
                     w: int = 320, h: int = 240) -> float:
    """Share of ``poly`` (normalised 0..1) covered by the union of ``masks``."""
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        return 0.0
    area = np.zeros((h, w), np.uint8)
    cv2.fillPoly(area, [np.array([[x * w, y * h] for x, y in poly], np.int32)], 1)
    total = int(area.sum())
    if total == 0 or not masks:
        return 0.0
    m = np.zeros((h, w), np.uint8)
    for poly_m in masks:
        if len(poly_m) >= 3:
            cv2.fillPoly(m, [np.array([[x * w, y * h] for x, y in poly_m], np.int32)], 1)
    return float((area & m).sum()) / float(total)


def _camera_mask_polys(camera_id: str) -> List[List[Tuple[float, float]]]:
    try:
        from app.services.privacy_mask import camera_masks

        return [[(float(p["x"]), float(p["y"])) for p in m.get("points") or []] for m in camera_masks(camera_id)]
    except Exception:  # noqa: BLE001
        return []


def _point_in_poly(x: float, y: float, poly) -> bool:
    from app.services.tripwire_engine import point_in_polygon

    return point_in_polygon(x, y, poly)


_COVERAGE_CACHE: Dict[tuple, Tuple[float, float, int]] = {}


def floor_coverage(camera_id: str, floor_polygon: List[Tuple[float, float]],
                   frame_size: Optional[Tuple[int, int]],
                   masks: Optional[List[List[Tuple[float, float]]]] = None) -> Tuple[float, float, int]:
    """Cached :func:`_floor_coverage` (keyed by camera, zone, homography, size and masks)."""
    from app.services.tracking_service import floor_projector

    m = floor_projector._matrices.get(camera_id)
    masks = masks if masks is not None else _camera_mask_polys(camera_id)
    key = (camera_id, tuple(floor_polygon), None if m is None else tuple(round(float(v), 9) for v in m.ravel()),
           tuple(frame_size or floor_projector.authored_size(camera_id) or ()),
           tuple(tuple(p) for p in masks))
    hit = _COVERAGE_CACHE.get(key)
    if hit is None:
        if len(_COVERAGE_CACHE) > 512:
            _COVERAGE_CACHE.clear()
        hit = _COVERAGE_CACHE[key] = _floor_coverage(camera_id, floor_polygon, frame_size, masks)
    return hit


def _floor_coverage(camera_id: str, floor_polygon: List[Tuple[float, float]],
                    frame_size: Optional[Tuple[int, int]],
                    masks: Optional[List[List[Tuple[float, float]]]] = None) -> Tuple[float, float, int]:
    """(covered fraction of the zone, masked share of the camera's view of it, samples inside).

    The camera's picture is sampled on a grid, each sample projected to the
    floor through the homography; the zone's 0.25 m cells within 0.6 m of a
    projected sample count as covered. The masked share is the part of those
    samples that fall inside the camera's privacy masks.
    """
    import numpy as np

    from app.services.tracking_service import floor_projector

    if not floor_projector.has_homography(camera_id):
        return 0.0, 0.0, 0
    fw, fh = frame_size or floor_projector.authored_size(camera_id) or (1280, 720)
    xs, ys = np.linspace(0.02, 0.98, 32), np.linspace(0.02, 0.98, 24)
    inside_pts, masked = [], 0
    masks = masks if masks is not None else _camera_mask_polys(camera_id)
    for v in ys:
        for u in xs:
            p = floor_projector.to_floor(camera_id, float(u) * fw, float(v) * fh, frame_size=(fw, fh))
            if p is None:
                continue
            if _point_in_poly(p[0], p[1], floor_polygon):
                inside_pts.append(p)
                if any(_point_in_poly(float(u), float(v), m) for m in masks if len(m) >= 3):
                    masked += 1
    if not inside_pts:
        return 0.0, 0.0, 0
    xs_z = [p[0] for p in floor_polygon]
    ys_z = [p[1] for p in floor_polygon]
    gx = np.arange(min(xs_z), max(xs_z) + 0.25, 0.25)
    gy = np.arange(min(ys_z), max(ys_z) + 0.25, 0.25)
    cells = [(x, y) for y in gy for x in gx if _point_in_poly(x, y, floor_polygon)][:4000]
    if not cells:
        return 1.0, masked / len(inside_pts), len(inside_pts)
    c = np.asarray(cells)
    s = np.asarray(inside_pts)
    d2 = ((c[:, None, :] - s[None, :, :]) ** 2).sum(axis=2).min(axis=1)
    return float((d2 <= 0.36).mean()), masked / len(inside_pts), len(inside_pts)


# --------------------------------------------------------------------------- the service

class RestrictedAreaService:
    """Database side of restricted areas plus the compiled snapshot for the workers."""

    def __init__(self) -> None:
        self._snapshot = Snapshot()
        self._compile_lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._task: Optional[asyncio.Task] = None
        self._adopt_checked_at = 0.0
        self.clock = time.time
        self.last_error: Optional[str] = None

    # ---- snapshot
    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._compile_lock = asyncio.Lock()
            self._lock_loop = loop
        return self._compile_lock

    def plan_on(self) -> bool:
        from app.services.entitlements import feature_safe

        return feature_safe("restricted_areas")

    async def refresh(self, session_factory=None) -> Snapshot:
        """Re-read areas, zones, cameras and announcements; swap the snapshot."""
        async with self._lock():
            try:
                snap = await self._compile(session_factory)
            except Exception as exc:  # noqa: BLE001 - keep the last good snapshot
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.error("Restricted areas could not be re-read: %s", exc)
                return self._snapshot
            self._snapshot = snap
            self.last_error = None
            return snap

    async def _compile(self, session_factory=None) -> Snapshot:
        from sqlalchemy import or_, select

        from app.models.db_models import (CameraModel, RestrictedAreaAnnouncementModel, RestrictedAreaModel,
                                          StoreZoneModel)

        if session_factory is None:
            from app.database import async_session_factory as session_factory
        now = self.clock()
        recent = dt_of(now - 3600)
        async with session_factory() as db:
            rows = (await db.execute(select(RestrictedAreaModel))).scalars().all()
            zone_ids = {r.floor_zone_id for r in rows if r.floor_zone_id}
            zones = {}
            if zone_ids:
                zones = {z.id: z for z in (await db.execute(
                    select(StoreZoneModel).where(StoreZoneModel.id.in_(zone_ids)))).scalars().all()}
            cams = (await db.execute(select(CameraModel.id, CameraModel.name, CameraModel.watch_zone_id,
                                            CameraModel.is_ai_enabled))).all()
            anns = (await db.execute(select(RestrictedAreaAnnouncementModel).where(
                or_(RestrictedAreaAnnouncementModel.ended_at.is_(None),
                    RestrictedAreaAnnouncementModel.ended_at >= recent),
                RestrictedAreaAnnouncementModel.until >= recent))).scalars().all()
        try:
            from app.services.ai_zone_service import ai_zone_service

            zone_store = ai_zone_service.get_all_zones()
        except Exception:  # noqa: BLE001
            zone_store = {}
        intrusions = {iz["id"]: iz for iz in zone_store.get("intrusion_zones", []) or []}
        tripwires = {tw["id"]: tw for tw in zone_store.get("tripwires", []) or []}
        from app.services.tracking_service import floor_projector

        areas: List[CompiledArea] = []
        linked: set = set()
        for r in rows:
            a = CompiledArea(
                id=r.id, name=r.name, enabled=bool(r.enabled),
                arming=dict(r.arming or {"mode": "always"}), triggers=merge_triggers(r.triggers),
                security=validate_security(None, r.security), pois=list(r.points_of_interest or []),
                floor_zone_id=r.floor_zone_id, manual_state=r.manual_state,
                manual_until=ts_of(r.manual_until), manual_reason=r.manual_reason, manual_by=r.manual_by,
                legacy_intrusion_id=r.legacy_intrusion_id,
            )
            for reg in r.camera_regions or []:
                cam = reg.get("camera_id")
                pts = None
                if reg.get("intrusion_zone_id"):
                    iz = intrusions.get(reg["intrusion_zone_id"])
                    if iz is not None:
                        linked.add(iz["id"])
                        cam = iz.get("camera_id") or cam
                        pts = iz.get("points")
                else:
                    pts = reg.get("points")
                if not cam or not pts or len(pts) < 3:
                    continue
                poly = [(float(p["x"]), float(p["y"])) for p in pts]
                frac = _raster_fraction(poly, _camera_mask_polys(cam))
                if frac >= float(settings.RESTRICTED_MASK_REFUSE_FRACTION):
                    a.masked[cam] = (f"{round(frac * 100)} % of this camera's part of the area is inside a "
                                     "privacy mask, so this camera does not watch it (privacy first).")
                    continue
                a.regions.setdefault(cam, []).append(poly)
            if r.legacy_intrusion_id:
                linked.add(r.legacy_intrusion_id)
            z = zones.get(r.floor_zone_id) if r.floor_zone_id else None
            if z is not None and len(z.polygon or []) >= 3:
                a.floor_polygon = [(float(p["x"]), float(p["y"])) for p in z.polygon]
                for cid, _name, watch_zone, ai_on in cams:
                    if cid in a.regions:
                        continue
                    if floor_projector.has_homography(cid):
                        frac, masked, n = floor_coverage(cid, a.floor_polygon, None)
                        if n >= 3:
                            if masked >= float(settings.RESTRICTED_MASK_REFUSE_FRACTION):
                                a.masked[cid] = (f"{round(masked * 100)} % of this camera's view of the area "
                                                 "is inside a privacy mask, so it does not watch it.")
                            else:
                                a.floor_cameras[cid] = round(frac, 3)
                    elif watch_zone == r.floor_zone_id:
                        a.linked_cameras.append(cid)
            wd = a.triggers.get("wrong_door") or {}
            for lid in wd.get("door_line_ids") or []:
                tw = tripwires.get(lid)
                if tw and all(isinstance(tw.get(k), (int, float)) for k in ("x1", "y1", "x2", "y2")):
                    a.door_lines.setdefault(tw.get("camera_id") or "", {})[lid] = (
                        (float(tw["x1"]), float(tw["y1"])), (float(tw["x2"]), float(tw["y2"])))
            a.announcements = [Announcement(
                id=x.id, area_id=x.area_id, user_id=x.user_id, user_name=x.user_name, note=x.note,
                people=int(x.people or 1), started_at=ts_of(x.started_at), until=ts_of(x.until),
                ended_at=ts_of(x.ended_at), source=x.source) for x in anns if x.area_id == r.id]
            areas.append(a)
        by_cam: Dict[str, List[CompiledArea]] = {}
        for a in areas:
            if not a.enabled:
                continue
            for c in a.cameras():
                by_cam.setdefault(c, []).append(a)
        return Snapshot(areas=areas, by_camera=by_cam, linked_intrusion_ids=frozenset(linked), built_at=now)

    # ---- audit
    async def audit(self, db, action: str, area=None, actor: Optional[dict] = None, event_id: Optional[str] = None,
                    detail: Optional[dict] = None) -> None:
        from fastapi.encoders import jsonable_encoder

        from app.models.db_models import RestrictedAreaAuditModel

        actor = actor or {}
        db.add(RestrictedAreaAuditModel(
            id=new_id("raa"), ts=utcnow(), area_id=getattr(area, "id", None) or (area if isinstance(area, str) else None),
            area_name=getattr(area, "name", None), action=action[:32], actor_id=actor.get("id"),
            actor_name=(actor.get("name") or None) and str(actor.get("name"))[:128],
            actor_role=(actor.get("role") or None) and str(actor.get("role"))[:16], event_id=event_id,
            detail=jsonable_encoder(detail) if detail else None))

    async def audit_now(self, action: str, area=None, actor: Optional[dict] = None, event_id: Optional[str] = None,
                        detail: Optional[dict] = None, session_factory=None) -> None:
        if session_factory is None:
            from app.database import async_session_factory as session_factory
        try:
            async with session_factory() as db:
                await self.audit(db, action, area, actor, event_id, detail)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - an audit failure is logged loudly
            logger.error("Restricted area audit entry %s not written: %s", action, exc)

    # ---- validation of a whole area
    async def validate_area(self, db, payload: dict, current=None) -> dict:
        """A clean area record from ``payload`` merged over ``current``; raises AreaError."""
        from sqlalchemy import select

        from app.models.db_models import CameraModel, RestrictedAreaModel, StoreZoneModel

        if not isinstance(payload, dict):
            raise AreaError("The area settings must be an object.")
        known = {"name", "enabled", "floor_zone_id", "camera_regions", "arming", "triggers",
                 "points_of_interest", "security"}
        extra = set(payload) - known
        if extra:
            raise AreaError(f"Unknown field(s): {', '.join(sorted(extra))}.")
        rec = {
            "name": current.name if current is not None else None,
            "enabled": current.enabled if current is not None else True,
            "floor_zone_id": current.floor_zone_id if current is not None else None,
            "camera_regions": list(current.camera_regions or []) if current is not None else [],
            "arming": dict(current.arming or {}) if current is not None else None,
            "triggers": merge_triggers(current.triggers if current is not None else None),
            "points_of_interest": list(current.points_of_interest or []) if current is not None else [],
            "security": validate_security(None, current.security if current is not None else None),
        }
        if "name" in payload or current is None:
            rec["name"] = _text(payload.get("name"), NAME_MAX, "Area name", required=True)
        if "enabled" in payload:
            rec["enabled"] = bool(payload["enabled"])
        if "floor_zone_id" in payload:
            rec["floor_zone_id"] = (str(payload["floor_zone_id"])[:64] or None) if payload["floor_zone_id"] else None
        if "camera_regions" in payload:
            rec["camera_regions"] = validate_regions(payload["camera_regions"])
        if "arming" in payload or rec["arming"] is None:
            raw_arming = payload.get("arming")
            old_tz = (rec["arming"] or {}).get("timezone")
            if isinstance(raw_arming, dict) and "timezone" not in raw_arming and old_tz:
                raw_arming = dict(raw_arming, timezone=old_tz)   # the dashboard does not send it
            rec["arming"] = validate_arming(raw_arming)
        if "triggers" in payload:
            rec["triggers"] = validate_triggers(payload["triggers"], rec["triggers"])
        if "points_of_interest" in payload:
            rec["points_of_interest"] = validate_pois(payload["points_of_interest"])
        if "security" in payload:
            rec["security"] = validate_security(payload["security"], rec["security"])
        if not rec["floor_zone_id"] and not rec["camera_regions"]:
            raise AreaError("Choose where the area is: a zone on the store map, or an area drawn on a camera.")
        # Unique name.
        q = select(RestrictedAreaModel.id).where(RestrictedAreaModel.name == rec["name"])
        if current is not None:
            q = q.where(RestrictedAreaModel.id != current.id)
        if (await db.execute(q)).first():
            raise AreaError(f"Another restricted area is already called '{rec['name']}'.", status=409)
        cams = {c.id: c for c in (await db.execute(select(CameraModel))).scalars().all()}
        zone = None
        if rec["floor_zone_id"]:
            zone = await db.get(StoreZoneModel, rec["floor_zone_id"])
            if zone is None:
                raise AreaError("That store-map zone no longer exists. Reload and choose it again.")
        try:
            from app.services.ai_zone_service import ai_zone_service

            intrusions = {iz["id"]: iz for iz in ai_zone_service.get_all_zones().get("intrusion_zones", [])}
            tripwires = {tw["id"]: tw for tw in ai_zone_service.get_all_zones().get("tripwires", [])}
        except Exception:  # noqa: BLE001
            intrusions, tripwires = {}, {}
        warnings: List[str] = []
        refuse = float(settings.RESTRICTED_MASK_REFUSE_FRACTION)
        for reg in rec["camera_regions"]:
            if reg.get("intrusion_zone_id"):
                iz = intrusions.get(reg["intrusion_zone_id"])
                if iz is None:
                    raise AreaError("A chosen camera area no longer exists. Draw it again in Camera setup.")
                reg["camera_id"] = iz.get("camera_id") or reg["camera_id"]
                pts = iz.get("points") or []
            else:
                pts = reg["points"]
            cam = cams.get(reg["camera_id"])
            if cam is None:
                raise AreaError("A chosen camera no longer exists.")
            poly = [(float(p["x"]), float(p["y"])) for p in pts]
            frac = _raster_fraction(poly, _camera_mask_polys(cam.id))
            if frac >= refuse:
                raise AreaError(
                    f"This restricted area is inside a privacy mask on {cam.name} ({round(frac * 100)} % "
                    "covered). Privacy masks always win: people there are hidden from the system on purpose. "
                    "Move the area, or change the mask in Camera setup first.")
            if frac > 0.02:
                warnings.append(f"{round(frac * 100)} % of the area on {cam.name} is under a privacy mask; "
                                "nobody is watched in that part.")
        if zone is not None and len(zone.polygon or []) >= 3:
            poly = [(float(p["x"]), float(p["y"])) for p in zone.polygon]
            seen, clean = 0, 0
            from app.services.tracking_service import floor_projector

            for c in cams.values():
                if floor_projector.has_homography(c.id):
                    _frac, masked, n = floor_coverage(c.id, poly, None)
                    if n >= 3:
                        seen += 1
                        if masked < refuse:
                            clean += 1
                        else:
                            warnings.append(f"{c.name}'s view of the area is under a privacy mask; it does not watch it.")
            if seen and not clean and not rec["camera_regions"]:
                raise AreaError(
                    f"Every camera that sees '{zone.name}' has it inside a privacy mask. Privacy masks always "
                    "win, so this area cannot be watched. Move the area or change the masks first.")
            linked = [c for c in cams.values() if c.watch_zone_id == zone.id and not floor_projector.has_homography(c.id)]
            if not seen and not linked and not rec["camera_regions"]:
                warnings.append("No camera sees this zone yet: calibrate a camera on the store map, link a camera "
                                "to the zone, or draw the area on a camera.")
        for lid in rec["triggers"]["wrong_door"].get("door_line_ids") or []:
            if lid not in tripwires:
                raise AreaError("A chosen door line no longer exists. Choose the door lines again.")
        rec["_warnings"] = warnings
        return rec

    # ---- adopt older zone-store restricted areas
    async def adopt_legacy(self, session_factory=None, actor: Optional[dict] = None) -> int:
        """Upgrade every zone-store restricted area not yet linked to a restricted area.

        Settings carried over: name, enabled, schedule (restricted_during ->
        armed during, allowed_during -> armed outside), minimum time inside ->
        "Entry without notice" minimum presence, repeat cooldown, severity ->
        level (HIGH = Alert, WARNING = Watch, INFO = Review). The other triggers
        start off, so the area behaves as before until someone switches more
        on. Returns the number of areas created.
        """
        from sqlalchemy import select

        from app.models.db_models import RestrictedAreaModel

        if session_factory is None:
            from app.database import async_session_factory as session_factory
        try:
            from app.services.ai_zone_service import ai_zone_service

            intrusions = list(ai_zone_service.get_all_zones().get("intrusion_zones", []) or [])
        except Exception:  # noqa: BLE001
            return 0
        if not intrusions:
            return 0
        created = 0
        async with session_factory() as db:
            rows = (await db.execute(select(RestrictedAreaModel))).scalars().all()
            linked = {r.legacy_intrusion_id for r in rows if r.legacy_intrusion_id}
            for r in rows:
                for reg in r.camera_regions or []:
                    if reg.get("intrusion_zone_id"):
                        linked.add(reg["intrusion_zone_id"])
            names = {r.name for r in rows}
            for iz in intrusions:
                if iz.get("id") in linked or len(iz.get("points") or []) < 3:
                    continue
                if len(rows) + created >= MAX_AREAS:
                    break
                name = (str(iz.get("name") or "Restricted area").strip() or "Restricted area")[:NAME_MAX]
                base, n = name, 2
                while name in names:
                    name = f"{base} ({n})"[:NAME_MAX]
                    n += 1
                names.add(name)
                rows_sched = iz.get("schedule") or []
                try:
                    sched = validate_schedule_rows(rows_sched)
                except AreaError:
                    sched = []
                arming = ({"mode": "schedule", "schedule": sched,
                           "schedule_mode": "armed_outside" if iz.get("schedule_mode") == "allowed_during" else "armed_during"}
                          if sched else {"mode": "always", "schedule": [], "schedule_mode": "armed_during"})
                if iz.get("timezone"):
                    arming["timezone"] = str(iz["timezone"])[:64]
                trig = default_triggers()
                for tid in trig:
                    trig[tid]["enabled"] = tid == "unannounced_entry"
                tier = {"HIGH": "alert", "WARNING": "watch", "INFO": "review"}.get(str(iz.get("severity") or "HIGH").upper(), "alert")
                dwell = float(iz.get("min_dwell_seconds", iz.get("dwell_time_seconds", 3.0)) or 0.5)
                trig["unannounced_entry"].update(
                    tier=tier, min_presence_sec=min(5.0, max(0.2, dwell)),
                    cooldown_sec=min(3600.0, max(5.0, float(iz.get("cooldown_seconds") or settings.RESTRICTED_AREA_COOLDOWN_SEC))))
                row = RestrictedAreaModel(
                    id=new_id("ra"), name=name, enabled=bool(iz.get("enabled", True)), floor_zone_id=None,
                    camera_regions=[{"camera_id": iz.get("camera_id") or "", "intrusion_zone_id": iz["id"]}],
                    arming=arming, triggers=trig, points_of_interest=[], security=dict(DEFAULT_SECURITY),
                    legacy_intrusion_id=iz["id"], created_at=utcnow(), updated_at=utcnow(),
                    updated_by="upgrade")
                db.add(row)
                await self.audit(db, "upgraded", row, actor or {"name": "System"},
                                 detail={"intrusion_zone_id": iz["id"], "camera_id": iz.get("camera_id"),
                                         "min_dwell_seconds": dwell, "severity": iz.get("severity"),
                                         "tier": tier, "schedule_rows": len(sched)})
                created += 1
            if created:
                await db.commit()
        if created:
            logger.info("Upgraded %d older restricted area(s) to high-security restricted areas", created)
        return created

    # ---- background refresh
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="restricted-areas-refresh")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        from app.services.restricted_area_engine import restricted_area_engine

        restricted_area_engine.bind_loop()
        first = True
        refreshed = 0.0
        while True:
            try:
                if self.plan_on():
                    mono = time.monotonic()
                    if first or mono - self._adopt_checked_at > 30:
                        self._adopt_checked_at = mono
                        await self.adopt_legacy()
                    if first or mono - refreshed >= float(settings.RESTRICTED_REFRESH_SEC):
                        refreshed = mono
                        await self.refresh()
                    await restricted_area_engine.tick()
                elif self._snapshot.areas:
                    self._snapshot = Snapshot(built_at=self.clock())
                first = False
                await asyncio.sleep(max(0.5, float(settings.RESTRICTED_TICK_SEC)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("restricted areas background check failed")
                await asyncio.sleep(2.0)


restricted_areas = RestrictedAreaService()


# --------------------------------------------------------------------------- serialisation helpers

def catalogue() -> Dict[str, Any]:
    """The trigger catalogue for the dashboard (labels, explanations, defaults, ranges)."""
    from app.services import theft_alert_policy as tp

    return {
        "triggers": [dict(id=tid, **{k: copy.deepcopy(v) for k, v in spec.items()}) for tid, spec in TRIGGERS.items()],
        "main_triggers": list(MAIN_TRIGGERS),
        "tiers": [{"id": t, "label": tp.TIER_LABELS[t], "help": tp.TIER_HELP[t], "channels": tp.channels(t)}
                  for t in TIERS],
        "outcomes": [{"id": k, "label": v} for k, v in OUTCOMES.items()],
        "announce": {"min": ANNOUNCE_MIN, "max": ANNOUNCE_MAX, "default": ANNOUNCE_DEFAULT,
                     "people_max": ANNOUNCE_PEOPLE_MAX},
        "manual_max_min": MANUAL_MAX_MIN,
        "security": {"confidence_delta": {"default": DEFAULT_SECURITY["confidence_delta"],
                                          "min": SECURITY_LIMITS["confidence_delta"][0],
                                          "max": SECURITY_LIMITS["confidence_delta"][1],
                                          "label": "Extra sensitivity inside the area",
                                          "explain": ("How much less sure the system needs to be that it sees a "
                                                      "person inside the area while it is armed (0.05 = 5 points "
                                                      "below the normal threshold). Higher finds more but may "
                                                      "flag shadows.")}},
        "days": list(DAY_KEYS),
    }


# --------------------------------------------------------------------------- event queries (audit M8)
#
# The status poll (every 3 s) and the event list counted "still needs an
# outcome" and filtered by area/trigger in Python over only the newest 200 or
# 2000 alerts, so older unresolved alerts were missed. These do it in SQL
# (json_extract on metadata_json) over every RESTRICTED_AREA row, using the
# m0024 index on security_events(event_type, timestamp).

_RA_AREA = "json_extract(metadata_json, '$.ra_area_id')"
_RA_TRIGGER = "json_extract(metadata_json, '$.ra_trigger')"
_RA_NEEDS = ("(json_extract(metadata_json, '$.ra_requires_outcome') IN (1, 'true') "
             "AND json_extract(metadata_json, '$.ra_outcome') IS NULL)")


async def open_outcome_counts(db) -> Dict[str, int]:
    """Area id -> number of its alerts still waiting for an outcome."""
    from sqlalchemy import text

    rows = (await db.execute(text(
        f"SELECT {_RA_AREA} AS area, count(*) FROM security_events "
        f"WHERE event_type = 'RESTRICTED_AREA' AND {_RA_AREA} IS NOT NULL AND {_RA_NEEDS} "
        f"GROUP BY area"))).all()
    return {str(a): int(n) for a, n in rows if a}


async def event_ids(db, *, area_id: Optional[str] = None, trigger: Optional[str] = None,
                    needs_outcome: Optional[bool] = None, limit: int = 100) -> List[str]:
    """Ids of restricted-area alerts matching the filters, newest first (filtered in SQL)."""
    from sqlalchemy import text

    where = ["event_type = 'RESTRICTED_AREA'", f"{_RA_AREA} IS NOT NULL"]
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if area_id:
        where.append(f"{_RA_AREA} = :area")
        params["area"] = area_id
    if trigger:
        where.append(f"{_RA_TRIGGER} = :trigger")
        params["trigger"] = trigger
    if needs_outcome is True:
        where.append(_RA_NEEDS)
    elif needs_outcome is False:
        where.append(f"NOT {_RA_NEEDS}")
    rows = (await db.execute(text(
        f"SELECT id FROM security_events WHERE {' AND '.join(where)} "
        f"ORDER BY timestamp DESC LIMIT :limit"), params)).all()
    return [r[0] for r in rows]
