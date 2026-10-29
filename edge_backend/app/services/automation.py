"""Things the box sets up by itself (owner decision 2026-10-29, brain #431).

Automatic (each shows what it chose; a value saved in Settings wins):

* **Store hours (A2).** Learnt from when shoppers are seen and when the till
  rings, over the last ``LEARN_DAYS`` complete store-local days: per weekday
  the median opening and closing time, rounded to the half hour. Needs
  ``MIN_DAYS`` usable days ("learning, N of 7 days" until then). A day counts
  when the till sent at least ``POS_MIN_TXNS`` sales that day, or when the
  cameras were watching for at least ``MIN_WATCHED_HOURS`` hours. Any day
  can be edited in Settings (a manual day wins over the learnt one).
* **Backup in the quietest hour (A4).** The middle of the longest stretch the
  store is closed on every day, else the quietest hour of the footfall
  profile, never 04:00 (the nightly database clean-up); 03:00 without data.
* **Storage sized to the disk (A5).** Already automatic in evidence_storage
  (EVIDENCE_MAX_GB 0 = 10 % of the disk) and backup_service (15 % of the
  disk); shown here.
* **Analysis speed (A7).** From the measured accelerator budget and the number
  of streaming cameras: the most / idle analysed frames per second, the frames
  read per camera and the accelerator share, written as *automatic* site
  settings (site_settings.set_auto), never below the restricted-area floor
  (RESTRICTED_MIN_ANALYSIS_FPS). Re-checked daily and 10 minutes after the
  number of cameras changes. A camera reconnects when DECODE_MAX_FPS changes,
  so that one only moves by 2 fps or more.
* **Stuck cameras restart (A9).** services/camera_restarts.py; counted here.

Suggested, applied only when an administrator confirms (every apply, dismiss
and undo is written to the audit trail, ``restricted_area_audit``):

* **Guard after hours (A3):** night watch on cameras that already use it
  follows the store hours (``NightWatchConfig.follow_store_hours``); with no
  night watch at all, turn it on for door cameras outside store hours;
  restricted areas that are already on a schedule follow the store hours
  (``arming.source = "store_hours"``). An area that is always armed is never
  offered this: it would guard less.
* **Learn from wrong alerts (A8):** a theft rule on one camera, or a
  non-critical restricted-area trigger in one area, with at least
  ``WRONG_MIN_ALERTS`` alerts with an outcome in ``WRONG_DAYS`` days of which
  at least ``WRONG_MIN_SHARE`` were false alarms: one step less sensitive.
  Never for a critical trigger, entry without notice, tampering, people
  count, the door rule or overstay. Undo restores the old value.
* **Link cameras to areas (A6):** a placed, uncalibrated camera with at least
  ``LINK_MIN_SHARE`` of what it sees in one countable zone that no other
  camera is linked to. Conflicts are listed as "check" and cannot be applied.

Stored in ``system_setup``: ``store_hours`` and ``automation_state``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import statistics
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("edge.automation")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SHORT = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
DAY_LONG = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
            "sat": "Saturday", "sun": "Sunday"}

HOURS_KEY = "store_hours"
STATE_KEY = "automation_state"

# --- store hours
LEARN_DAYS = 14
MIN_DAYS = 7
POS_MIN_TXNS = 5
MIN_WATCHED_HOURS = 20
MIN_UPTIME_PER_HOUR_S = 600
FOOTFALL_MIN_PEAK = 10          # a busiest hour below this = the store was closed that day
FOOTFALL_HOUR_SHARE = 0.15      # an hour counts as open at >= 15 % of the day's busiest hour
FOOTFALL_HOUR_MIN = 3
POS_HOUR_SHARE = 0.05
ROUND_MIN = 30
LEARN_EVERY_S = 6 * 3600

# --- backup hour
MAINTENANCE_HOUR = 4            # services/db_maintenance.py runs at 04:00
DEFAULT_BACKUP_HOUR = 3

# --- suggestions
WRONG_DAYS = 14
WRONG_MIN_ALERTS = 5
WRONG_MIN_SHARE = 0.70
THEFT_STEP = 0.8
THEFT_MIN_FACTOR = 0.5
LINK_MIN_SHARE = 0.60
DISMISS_DAYS = 30
MAX_APPLIED = 50
DOOR_ROLES = ("entrance", "exit", "entrance_exit")

# Restricted-area triggers that may be made less sensitive, and how one step does it.
# (param, kind, amount): "mul" multiplies, "add" adds; clamped to the trigger's range.
RA_STEPS: Dict[str, List[Tuple[str, str, float]]] = {
    "dwell": [("max_seconds", "mul", 1.5)],
    "speed": [("max_mps", "add", 0.5), ("max_bh_per_sec", "add", 0.3)],
    "pacing": [("reversals", "add", 1)],
    "erratic": [("turns", "add", 1)],
    "crouch": [("min_seconds", "mul", 1.5)],
    "reach_up": [("min_seconds", "mul", 1.5)],
    "poi_approach": [("min_seconds", "add", 1.0)],
    "boundary_linger": [("min_seconds", "mul", 1.5)],
}
RA_NEVER = ("unannounced_entry", "tamper", "people_count", "wrong_door", "announced_overstay")

# --- analysis speed
TUNE_EVERY_S = 24 * 3600
TUNE_SETTLE_S = 600
TUNE_KEYS = ("ANALYTICS_MAX_DETECT_FPS", "ANALYTICS_IDLE_DETECT_FPS", "DECODE_MAX_FPS", "POSE_BUDGET_UTILISATION")


# =========================================================================== helpers

def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now_utc()).replace(microsecond=0).isoformat() + "Z"


def hhmm(minutes: int) -> str:
    minutes = int(minutes)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(value: Any) -> int:
    hh, mm = str(value).strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def _sid(*parts: Any) -> str:
    return "sg_" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# =========================================================================== store hours: learning (pure)

def classify_day(activity: List[float], pos: List[float], watched_hours: Optional[int]) -> Any:
    """One store-local day -> (open_min, close_min), "closed", or None (not usable).

    ``activity``/``pos``: 24 hourly counts (people first seen, till sales).
    ``watched_hours``: hours the cameras were measuring (None = unknown).
    """
    if sum(pos) >= POS_MIN_TXNS:
        thr = max(1.0, POS_HOUR_SHARE * max(pos))
        hours = [h for h in range(24) if pos[h] >= thr]
        return (hours[0] * 60, (hours[-1] + 1) * 60)
    if watched_hours is not None and watched_hours < MIN_WATCHED_HOURS:
        return None
    if watched_hours is None and sum(activity) <= 0:
        return None
    peak = max(activity)
    if peak < FOOTFALL_MIN_PEAK:
        return "closed"
    thr = max(FOOTFALL_HOUR_MIN, FOOTFALL_HOUR_SHARE * peak)
    hours = [h for h in range(24) if activity[h] >= thr]
    return (hours[0] * 60, (hours[-1] + 1) * 60)


def _round_down(m: float) -> int:
    return int(math.floor(m / ROUND_MIN) * ROUND_MIN)


def _round_up(m: float) -> int:
    return min(1440, int(math.ceil(m / ROUND_MIN) * ROUND_MIN))


def aggregate(day_results: Dict[date, Any], profile_days: Dict[date, List[float]]) -> Dict[str, Any]:
    """Per weekday from usable days: median open / close, or closed when every sample was closed."""
    used = {d: r for d, r in day_results.items() if r is not None}
    by_wd: Dict[str, List[Any]] = defaultdict(list)
    for d, r in used.items():
        by_wd[DAYS[d.weekday()]].append(r)
    open_all = [r for r in used.values() if r != "closed"]
    days: Dict[str, Any] = {}
    estimated: List[str] = []
    for wd in DAYS:
        samples = by_wd.get(wd) or []
        if samples and all(r == "closed" for r in samples):
            days[wd] = {"closed": True}
            continue
        opens = [r for r in samples if r != "closed"]
        if not opens:
            if not open_all:
                days[wd] = None
                continue
            opens = open_all
            estimated.append(wd)
        o = _round_down(statistics.median(r[0] for r in opens))
        c = _round_up(statistics.median(r[1] for r in opens))
        days[wd] = {"open": hhmm(o), "close": hhmm(c)} if c > o else None
    profile = [0.0] * 24
    if profile_days:
        for series in profile_days.values():
            for h in range(24):
                profile[h] += float(series[h])
        profile = [round(v / len(profile_days), 2) for v in profile]
    return {"status": "learned" if len(used) >= MIN_DAYS else "learning", "days_measured": len(used),
            "needed": MIN_DAYS, "days": days, "estimated": estimated, "profile": profile,
            "learned_at": _iso()}


async def observe_days(db, end_local_date: date, days: int = LEARN_DAYS) -> Tuple[Dict[date, Any], Dict[date, List[float]]]:
    """Hourly people / sales / watched hours per store-local day from the database."""
    from sqlalchemy import text

    from app.services.timeutil import local_midnight_utc, to_local

    first = end_local_date - timedelta(days=days)
    start_utc, end_utc = local_midnight_utc(first), local_midnight_utc(end_local_date)
    params = {"a": start_utc.isoformat(sep=" "), "b": end_utc.isoformat(sep=" ")}
    act = defaultdict(lambda: [0.0] * 24)
    pos = defaultdict(lambda: [0.0] * 24)
    up = defaultdict(lambda: [0.0] * 24)
    uptime_known = False

    def put(store, key: str, value: float, agg: str = "sum") -> None:
        try:
            utc = datetime.strptime(key, "%Y-%m-%d %H")
        except (TypeError, ValueError):
            return
        loc = to_local(utc)
        d = loc.date()
        if not (first <= d < end_local_date):
            return
        if agg == "max":
            store[d][loc.hour] = max(store[d][loc.hour], float(value or 0))
        else:
            store[d][loc.hour] += float(value or 0)

    rows = (await db.execute(text(
        "SELECT strftime('%Y-%m-%d %H', start_time) AS h, count(*) FROM customer_tracks "
        "WHERE start_time >= :a AND start_time < :b "
        "AND (end_time IS NULL OR (julianday(end_time) - julianday(start_time)) * 86400 >= 3) "
        "GROUP BY h"), params)).all()
    for h, n in rows:
        put(act, h, n)
    rows = (await db.execute(text(
        "SELECT strftime('%Y-%m-%d %H', timestamp) AS h, count(DISTINCT transaction_id) FROM pos_transactions "
        "WHERE timestamp >= :a AND timestamp < :b GROUP BY h"), params)).all()
    for h, n in rows:
        put(pos, h, n)
    rows = (await db.execute(text(
        "SELECT strftime('%Y-%m-%d %H', bucket_start) AS h, max(uptime_seconds) FROM heatmap_snapshots "
        "WHERE bucket_minutes = 60 AND bucket_start >= :a AND bucket_start < :b "
        "AND uptime_seconds IS NOT NULL GROUP BY h"), params)).all()
    for h, n in rows:
        uptime_known = True
        put(up, h, n, "max")

    results: Dict[date, Any] = {}
    profile: Dict[date, List[float]] = {}
    d = first
    while d < end_local_date:
        watched = sum(1 for v in up[d] if v >= MIN_UPTIME_PER_HOUR_S) if uptime_known else None
        r = classify_day(act[d], pos[d], watched)
        results[d] = r
        if r is not None:
            profile[d] = list(act[d])
        d += timedelta(days=1)
    return results, profile


# =========================================================================== store hours: effective view

_cache_lock = threading.Lock()
_hours_doc: Dict[str, Any] = {}       # {"learned": {...}, "manual": {...}}
_state_doc: Dict[str, Any] = {}       # {"dismissed": {...}, "applied": [...], "auto_tune": {...}}


def _day_minutes(spec: Any) -> Any:
    """{"open","close"} -> (o, c); {"closed": True} -> "closed"; else None."""
    if not isinstance(spec, dict):
        return None
    if spec.get("closed"):
        return "closed"
    try:
        o, c = parse_hhmm(spec["open"]), parse_hhmm(spec["close"])
    except (KeyError, ValueError, AttributeError):
        return None
    return (o, c) if o != c else None


def effective_days(doc: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Per weekday: {"spec": {...} | None, "source": "manual" | "learned" | None}."""
    doc = _hours_doc if doc is None else doc
    manual = doc.get("manual") or {}
    learned = doc.get("learned") or {}
    ldays = (learned.get("days") or {}) if learned.get("status") == "learned" else {}
    out = {}
    for wd in DAYS:
        if wd in manual and _day_minutes(manual[wd]) is not None:
            out[wd] = {"spec": manual[wd], "source": "manual"}
        elif _day_minutes(ldays.get(wd)) is not None:
            out[wd] = {"spec": ldays[wd], "source": "learned"}
        else:
            out[wd] = {"spec": None, "source": None}
    return out


def store_hours_minutes(doc: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """mon..sun -> (open_min, close_min) or None (closed); None until every day is known."""
    eff = effective_days(doc)
    out: Dict[str, Any] = {}
    for wd in DAYS:
        m = _day_minutes(eff[wd]["spec"])
        if m is None:
            return None
        out[wd] = None if m == "closed" else m
    return out


def schedule_rows(hours: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Open times as restricted-area schedule rows: days with the same hours share a row."""
    if not hours:
        return []
    groups: Dict[Tuple[int, int], List[str]] = {}
    for wd in DAYS:
        m = hours.get(wd)
        if m:
            groups.setdefault((int(m[0]), int(m[1])), []).append(wd)
    rows = [{"days": days, "from": hhmm(o), "to": hhmm(c % 1440 if c > 1440 else c)}
            for (o, c), days in groups.items()]
    rows.sort(key=lambda r: DAYS.index(r["days"][0]))
    return rows


def _days_label(days: List[str]) -> str:
    idx = sorted(DAYS.index(d) for d in days)
    parts, start = [], idx[0]
    prev = start
    for i in idx[1:] + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        if prev > start:
            parts.append(f"{DAY_SHORT[DAYS[start]]}–{DAY_SHORT[DAYS[prev]]}")
        else:
            parts.append(DAY_SHORT[DAYS[start]])
        if i is not None:
            start = prev = i
    return ", ".join(parts)


def hours_label(hours: Optional[Dict[str, Any]]) -> Optional[str]:
    if not hours:
        return None
    rows = schedule_rows(hours)
    parts = [f"{_days_label(r['days'])} {r['from']}–{r['to']}" for r in rows]
    closed = [wd for wd in DAYS if not hours.get(wd)]
    if closed:
        parts.append(f"{_days_label(closed)} closed")
    return ", ".join(parts)


def store_hours_view(doc: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """What window.edgeAutomation.storeHours() returns: null until every day is known."""
    hours = store_hours_minutes(doc)
    if hours is None:
        return None
    return {"learnt": True, "label": hours_label(hours), "rows": schedule_rows(hours)}


# =========================================================================== backup hour

def quietest_hour(hours: Optional[Dict[str, Any]], profile: Optional[List[float]]) -> Tuple[int, str]:
    """(hour, why) for the nightly backup."""
    if hours:
        closed = []
        for h in range(24):
            if h == MAINTENANCE_HOUR:
                closed.append(False)
                continue
            ok = True
            for wd in DAYS:
                m = hours.get(wd)
                if not m:
                    continue
                o, c = m
                minute = h * 60 + 30
                if (o < c and o <= minute < c) or (o >= c and (minute >= o or minute < c)):
                    ok = False
                    break
            closed.append(ok)
        if any(closed):
            # Longest circular run of hours closed on every day; its middle hour.
            best, best_start = 0, None
            for s in range(24):
                if not closed[s] or closed[(s - 1) % 24] and not all(closed):
                    continue
                n = 0
                while n < 24 and closed[(s + n) % 24]:
                    n += 1
                if n > best:
                    best, best_start = n, s
            if best_start is not None:
                return (best_start + best // 2) % 24, "the middle of the hours the store is closed"
    if profile and any(v > 0 for v in profile):
        cands = [h for h in range(24) if h != MAINTENANCE_HOUR]
        h = min(cands, key=lambda x: (profile[x], abs(x - DEFAULT_BACKUP_HOUR)))
        return h, "the quietest hour seen by the cameras"
    return DEFAULT_BACKUP_HOUR, "the default (no store hours or footfall yet)"


def backup_hour() -> int:
    """Hour (store time) of tonight's automatic backup; 03:00 when nothing is known."""
    try:
        learned = (_hours_doc.get("learned") or {})
        return quietest_hour(store_hours_minutes(), learned.get("profile"))[0]
    except Exception:  # noqa: BLE001
        return DEFAULT_BACKUP_HOUR


# =========================================================================== analysis speed (pure)

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def choose_rates(budget_per_sec: Optional[float], cameras: int, cpu_cores: Optional[int],
                 cpu_only: bool, current_decode: float) -> Dict[str, float]:
    """Analysis-speed values for this hardware and camera count ({} until the budget is measured)."""
    if not budget_per_sec or budget_per_sec <= 0 or cameras <= 0:
        return {}
    floor = max(float(settings.RESTRICTED_MIN_ANALYSIS_FPS), 3.0)
    # About 40 % of cameras have people or movement at a busy moment.
    busy = max(1, math.ceil(cameras * 0.4))
    per_busy = budget_per_sec / busy
    max_fps = round(_clamp(round(per_busy * 2) / 2, floor, 10.0), 1)
    idle = round(_clamp(budget_per_sec * 0.1 / cameras, 0.3, 1.0), 1)
    # Never above the shipped default of 10: more frames read costs decode work on every camera.
    decode = _clamp(math.ceil(max(max_fps, 5.0)), max(5.0, floor), 10.0)
    if cpu_cores:
        # Software decode guide: about 25 frames per second per core over every camera.
        cpu_room = (cpu_cores * 25.0) / cameras
        if cpu_only and cpu_room < decode:
            decode = _clamp(math.floor(cpu_room), floor, decode)
    decode = float(decode)
    if abs(decode - float(current_decode)) < 2.0:
        decode = float(current_decode)     # a change reconnects every camera: only for a real gain
    max_fps = min(max_fps, decode)
    return {"ANALYTICS_MAX_DETECT_FPS": max_fps, "ANALYTICS_IDLE_DETECT_FPS": idle,
            "DECODE_MAX_FPS": decode, "POSE_BUDGET_UTILISATION": 0.5 if cpu_only else 0.6}


# =========================================================================== persistence

def _read_key_sync(key: str) -> Dict[str, Any]:
    import sqlite3

    try:
        conn = sqlite3.connect(str(settings.DATABASE_PATH), timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_cache() -> None:
    """Read both documents into memory (called at start and after every change)."""
    global _hours_doc, _state_doc
    with _cache_lock:
        _hours_doc = _read_key_sync(HOURS_KEY)
        _state_doc = _read_key_sync(STATE_KEY)


async def _put(db, key: str, doc: Dict[str, Any]) -> None:
    from app.models.db_models import SystemSetupModel

    row = await db.get(SystemSetupModel, key)
    value = json.dumps(doc, separators=(",", ":"))
    if row is None:
        db.add(SystemSetupModel(key=key, value=value, updated_at=_now_utc()))
    else:
        row.value, row.updated_at = value, _now_utc()


async def _get(db, key: str) -> Dict[str, Any]:
    from app.models.db_models import SystemSetupModel

    row = await db.get(SystemSetupModel, key)
    if row is None:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _audit(db, action: str, actor: Optional[dict], detail: dict, area=None) -> None:
    from app.services.restricted_areas import restricted_areas

    await restricted_areas.audit(db, action, area, actor, detail=detail)


# =========================================================================== store hours: changes

def validate_manual(raw: Any) -> Dict[str, Any]:
    """PUT body ``{"days": {mon: {"open","close"} | {"closed": true} | null}}`` -> clean changes."""
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        raise ValueError("Send the days to change, e.g. {\"days\": {\"mon\": {\"open\": \"08:00\", \"close\": \"21:00\"}}}.")
    out: Dict[str, Any] = {}
    for k, v in raw["days"].items():
        wd = str(k).strip().lower()[:3]
        if wd not in DAYS:
            raise ValueError(f"Unknown day '{k}'.")
        if v is None:
            out[wd] = None
            continue
        if not isinstance(v, dict):
            raise ValueError(f"{DAY_LONG[wd]}: give open and close times, or closed.")
        if v.get("closed"):
            out[wd] = {"closed": True}
            continue
        try:
            o, c = parse_hhmm(v.get("open")), parse_hhmm(v.get("close"))
        except (ValueError, AttributeError):
            raise ValueError(f"{DAY_LONG[wd]}: times must be HH:MM.") from None
        if o == c:
            raise ValueError(f"{DAY_LONG[wd]}: opening and closing times are the same.")
        out[wd] = {"open": hhmm(o), "close": hhmm(c)}
    return out


async def set_manual(db, changes: Dict[str, Any], actor: Optional[dict]) -> Dict[str, Any]:
    doc = await _get(db, HOURS_KEY)
    before = store_hours_minutes(doc)
    manual = dict(doc.get("manual") or {})
    for wd, v in changes.items():
        if v is None:
            manual.pop(wd, None)
        else:
            manual[wd] = v
    doc["manual"] = manual
    await _put(db, HOURS_KEY, doc)
    await _audit(db, "store_hours_set", actor, {"changes": changes})
    await db.commit()
    load_cache()
    if store_hours_minutes() != before:
        await on_hours_changed(actor)
    return doc


async def relearn(db=None, actor: Optional[dict] = None) -> Dict[str, Any]:
    """Learn the hours again from the last LEARN_DAYS days; rewrites store-hours schedules on change."""
    from app.services.timeutil import local_now

    async def run(session) -> Dict[str, Any]:
        doc = await _get(session, HOURS_KEY)
        before = store_hours_minutes(doc)
        results, profile = await observe_days(session, local_now().date())
        doc["learned"] = aggregate(results, profile)
        await _put(session, HOURS_KEY, doc)
        await session.commit()
        return {"doc": doc, "changed": store_hours_minutes(doc) != before}

    if db is not None:
        res = await run(db)
    else:
        from app.database import async_session_factory

        async with async_session_factory() as session:
            res = await run(session)
    load_cache()
    if res["changed"]:
        await on_hours_changed(actor or {"id": None, "name": "Automatic", "role": "system"})
    return res["doc"]


async def on_hours_changed(actor: Optional[dict]) -> int:
    """Rewrite every restricted area that follows the store hours. Returns how many changed."""
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.db_models import RestrictedAreaModel
    from app.services.restricted_areas import AreaError, restricted_areas, validate_arming

    hours = store_hours_minutes()
    rows = schedule_rows(hours) if hours else []
    changed = 0
    async with async_session_factory() as db:
        areas = (await db.execute(select(RestrictedAreaModel))).scalars().all()
        for a in areas:
            arming = dict(a.arming or {})
            if arming.get("source") != "store_hours" or hours is None:
                continue
            if rows:
                new = {"mode": "schedule", "schedule_mode": "armed_outside", "schedule": rows,
                       "source": "store_hours"}
            else:
                new = {"mode": "always", "source": "store_hours"}   # closed every day: always guarded
            if arming.get("timezone"):
                new["timezone"] = arming["timezone"]
            try:
                new = validate_arming(new)
            except AreaError as e:
                logger.warning(f"Store hours not applied to restricted area {a.id}: {e}")
                continue
            if new == arming:
                continue
            a.arming = new
            a.updated_at, a.updated_by = _now_utc(), (actor or {}).get("name") or "Automatic"
            await restricted_areas.audit(db, "hours_rewrite", a, actor,
                                         detail={"from": arming.get("schedule"), "to": new.get("schedule"),
                                                 "label": hours_label(hours)})
            changed += 1
        if changed:
            await db.commit()
    if changed:
        await restricted_areas.refresh()
    return changed


# =========================================================================== analysis speed: run

def _cameras_streaming() -> int:
    try:
        from app.services.live_analytics_engine import live_engine

        return sum(1 for rt in list(live_engine.runtimes.values()) if getattr(rt, "enabled", False))
    except Exception:  # noqa: BLE001
        return 0


def _cpu_only() -> bool:
    try:
        from app.services.inference_backend import person_detector

        st = person_detector.status() or {}
        prov = str(st.get("provider") or st.get("inference_provider") or "").lower()
        return (not prov) or prov.startswith("cpu")
    except Exception:  # noqa: BLE001
        return False


def tune_now(reason: str = "check") -> Dict[str, Any]:
    """Pick and apply the analysis-speed values; returns the record kept for the status."""
    import os

    from app.services.inference_scheduler import inference_scheduler
    from app.services.site_settings import ENV_DEFAULTS, site_settings

    cap = inference_scheduler.capacity()
    cams = _cameras_streaming()
    chosen = choose_rates(cap.get("budget_per_sec"), cams, os.cpu_count(), _cpu_only(),
                          float(site_settings.effective("DECODE_MAX_FPS")))
    rec = {"at": _iso(), "reason": reason, "cameras": cams, "budget_per_sec": cap.get("budget_per_sec"),
           "cost_ms": cap.get("cost_ms"), "chosen": chosen,
           "overridden": sorted(k for k in TUNE_KEYS if k in site_settings.overrides)}
    if chosen:
        site_settings.set_auto(chosen, source="auto_tune")
    else:
        rec["note"] = "Waiting for the first measurement of the AI accelerator."
    rec["defaults"] = {k: ENV_DEFAULTS.get(k) for k in TUNE_KEYS}
    with _cache_lock:
        _state_doc["auto_tune"] = rec
    return rec


# =========================================================================== suggestions

def _plan(key: str) -> bool:
    try:
        from app.services.entitlements import feature_safe

        return feature_safe(key)
    except Exception:  # noqa: BLE001
        return True


async def _guard_suggestions(db) -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from app.models.db_models import CameraModel, RestrictedAreaModel

    hours = store_hours_minutes()
    if hours is None:
        return []
    label = hours_label(hours)
    out: List[Dict[str, Any]] = []
    if _plan("night_watch"):
        cams = (await db.execute(select(CameraModel).order_by(CameraModel.name))).scalars().all()
        watching = [c for c in cams if ((c.features or {}).get("night_watch") or {}).get("enabled")]
        follow = [c for c in watching if not ((c.features or {}).get("night_watch") or {}).get("follow_store_hours")]
        if follow:
            out.append({
                "id": _sid("guard_nw", *sorted(c.id for c in follow)), "kind": "guard_hours",
                "target": "night_watch", "level": "confirm",
                "title": "Guard after hours: night watch follows your store hours",
                "detail": (f"Night watch on {len(follow)} camera(s) uses fixed times. Switch it to "
                           f"\"outside store hours\" ({label}), so it changes when your hours change."),
                "cameras": [{"id": c.id, "name": c.name} for c in follow],
            })
        elif not watching:
            from app.services import camera_roles as cr

            roles = cr.role_cache.all()
            doors = [c for c in cams if roles.get(c.id) in DOOR_ROLES and c.is_ai_enabled]
            if doors:
                out.append({
                    "id": _sid("guard_nw_on", *sorted(c.id for c in doors)), "kind": "guard_hours",
                    "target": "night_watch_on", "level": "confirm",
                    "title": "Guard after hours: turn on night watch at the doors",
                    "detail": (f"Nobody is told if a person is seen at night. Turn on night watch on "
                               f"{len(doors)} door camera(s), armed outside store hours ({label})."),
                    "cameras": [{"id": c.id, "name": c.name} for c in doors],
                })
    if _plan("restricted_areas"):
        areas = (await db.execute(select(RestrictedAreaModel).order_by(RestrictedAreaModel.name))).scalars().all()
        for a in areas:
            arming = a.arming or {}
            if not a.enabled or arming.get("mode") != "schedule" or arming.get("source") == "store_hours":
                continue
            if after_hours_share(arming, hours) < GUARD_MIN_SHARE:
                continue    # guarded mostly while the store is open: on purpose, leave it
            out.append({
                "id": _sid("guard_ra", a.id, json.dumps(arming.get("schedule"), sort_keys=True)),
                "kind": "guard_hours", "target": "restricted_area", "level": "confirm",
                "title": f"Guard after hours: {a.name} follows your store hours",
                "detail": (f"{a.name} is armed by times typed in by hand. Arm it whenever the store is "
                           f"closed ({label}) instead, so it follows your hours."),
                "area": {"id": a.id, "name": a.name},
            })
    return out


GUARD_MIN_SHARE = 0.7


def after_hours_share(arming: Dict[str, Any], hours: Dict[str, Any], start: Optional[datetime] = None) -> float:
    """Share of the area's armed time (one week, half-hour steps) that falls while the store is closed."""
    from app.services.restricted_areas import schedule_armed
    from app.services.timeutil import local_now

    base = (start or local_now()).replace(hour=0, minute=15, second=0, microsecond=0)
    armed = closed_and_armed = 0
    for k in range(7 * 48):
        when = base + timedelta(minutes=30 * k)
        if not schedule_armed(arming, when.timestamp()):
            continue
        armed += 1
        spec = hours.get(DAYS[when.weekday()])
        minute = when.hour * 60 + when.minute
        prev = hours.get(DAYS[(when.weekday() - 1) % 7])
        open_now = bool(spec) and ((spec[0] < spec[1] and spec[0] <= minute < spec[1])
                                   or (spec[0] >= spec[1] and minute >= spec[0]))
        if prev and prev[0] >= prev[1] and minute < prev[1]:
            open_now = True                       # yesterday's hours run past midnight
        if not open_now:
            closed_and_armed += 1
    return closed_and_armed / armed if armed else 0.0


async def _wrong_alert_suggestions(db) -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from app.models.db_models import CameraModel, RestrictedAreaModel, SecurityEventModel, TheftIncidentModel
    from app.services.restricted_areas import TRIGGERS, merge_triggers
    from app.services.theft_detection_service import RULE_LABELS, review_outcome

    since = _now_utc() - timedelta(days=WRONG_DAYS)
    out: List[Dict[str, Any]] = []
    names = dict((await db.execute(select(CameraModel.id, CameraModel.name))).all())
    if _plan("theft_detection"):
        rows = (await db.execute(
            select(TheftIncidentModel.camera_id, TheftIncidentModel.rule, TheftIncidentModel.theft_type,
                   TheftIncidentModel.status, TheftIncidentModel.resolution, TheftIncidentModel.resolved_by)
            .where(TheftIncidentModel.timestamp >= since))).all()
        stats: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
        for cam_id, rule, ttype, status, resolution, resolved_by in rows:
            kind, outcome = review_outcome(status, resolution, resolved_by)
            if kind != "reviewed":
                continue
            st = stats[(cam_id, rule or ttype or "UNKNOWN")]
            st[0] += 1
            if outcome == "FALSE_ALARM":
                st[1] += 1
        factors: Dict[str, Dict[str, float]] = {}
        for (cam_id, rule), (n, fa) in sorted(stats.items()):
            if n < WRONG_MIN_ALERTS or fa / n < WRONG_MIN_SHARE or cam_id not in names:
                continue
            if cam_id not in factors:
                cam = await db.get(CameraModel, cam_id)
                factors[cam_id] = dict(((cam.features or {}) if cam else {}).get("theft_rule_sensitivity") or {})
            cur = float(factors[cam_id].get(rule, 1.0))
            new = round(max(THEFT_MIN_FACTOR, cur * THEFT_STEP), 2)
            if new >= cur:
                continue
            out.append({
                "id": _sid("wrong_theft", cam_id, rule, cur), "kind": "less_sensitive", "target": "theft_rule",
                "level": "confirm",
                "title": f"Fewer wrong alerts: {RULE_LABELS.get(rule, rule)} on {names[cam_id]}",
                "detail": (f"{fa} of the last {n} alerts of this kind on {names[cam_id]} were marked wrong "
                           f"({round(100 * fa / n)} %). Make this camera a little less quick to raise it "
                           "(only this kind of alert, only this camera). Real thefts still alert; they need "
                           "a little more evidence."),
                "camera": {"id": cam_id, "name": names[cam_id]}, "rule": rule,
                "from_factor": cur, "to_factor": new, "reviewed": n, "false_alarms": fa,
            })
    if _plan("restricted_areas"):
        rows = (await db.execute(
            select(SecurityEventModel.metadata_json).where(SecurityEventModel.event_type == "RESTRICTED_AREA")
            .where(SecurityEventModel.timestamp >= since))).all()
        stats2: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
        for (meta,) in rows:
            m = meta or {}
            area_id, trig, outcome = m.get("ra_area_id"), m.get("ra_trigger"), m.get("ra_outcome")
            if not area_id or not trig or not outcome or m.get("alert_tier") == "critical":
                continue
            st = stats2[(area_id, trig)]
            st[0] += 1
            if outcome == "false_alarm":
                st[1] += 1
        for (area_id, trig), (n, fa) in sorted(stats2.items()):
            if trig in RA_NEVER or trig not in RA_STEPS or n < WRONG_MIN_ALERTS or fa / n < WRONG_MIN_SHARE:
                continue
            area = await db.get(RestrictedAreaModel, area_id)
            if area is None:
                continue
            cfg = merge_triggers(area.triggers).get(trig) or {}
            if cfg.get("tier") == "critical" or not cfg.get("enabled", True):
                continue
            new_params = step_params(trig, cfg)
            if not new_params:
                continue
            label = TRIGGERS[trig]["label"]
            out.append({
                "id": _sid("wrong_ra", area_id, trig, json.dumps(new_params, sort_keys=True)),
                "kind": "less_sensitive", "target": "restricted_trigger", "level": "confirm",
                "title": f"Fewer wrong alerts: \"{label}\" in {area.name}",
                "detail": (f"{fa} of the last {n} \"{label}\" alerts in {area.name} were marked false alarm. "
                           "Make this check a little less quick (only this check, only this area). "
                           "Entry without notice and tampering are never changed."),
                "area": {"id": area_id, "name": area.name}, "trigger": trig,
                "from": {k: cfg.get(k) for k in new_params}, "to": new_params, "reviewed": n, "false_alarms": fa,
            })
    return out


def step_params(trigger: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One step less sensitive for a restricted-area trigger ({} when already at the limit)."""
    from app.services.restricted_areas import TRIGGERS

    spec = TRIGGERS.get(trigger) or {}
    out: Dict[str, Any] = {}
    for name, kind, amount in RA_STEPS.get(trigger, []):
        p = (spec.get("params") or {}).get(name)
        if p is None:
            continue
        cur = cfg.get(name, p["default"])
        try:
            cur = float(cur)
        except (TypeError, ValueError):
            continue
        new = cur * amount if kind == "mul" else cur + amount
        new = min(float(p["max"]), new)
        if isinstance(p["default"], int) and not isinstance(p["default"], bool):
            new = int(round(new))
        else:
            new = round(new, 2)
        if new > cur:
            out[name] = new
    return out


async def _link_suggestions(db) -> List[Dict[str, Any]]:
    from app.services.coverage_geometry import FLOOR_EXCLUDED_ZONES
    from app.services.store_layout_service import store_layout_service

    try:
        report = await store_layout_service.coverage_report(db)
    except Exception as exc:  # noqa: BLE001 - no layout yet
        logger.debug(f"coverage unavailable for link suggestions: {exc}")
        return []
    cams = report.get("cameras") or []
    linked = {c.get("watch_zone_id") for c in cams if c.get("watch_zone_id")}
    skip = set(FLOOR_EXCLUDED_ZONES) | {"RESTRICTED"}
    cands = []
    for c in cams:
        if c.get("calibrated") or c.get("watch_zone_id") or not c.get("counted") or not c.get("enabled", True):
            continue
        zones = [z for z in (c.get("zones") or []) if (z.get("category") or "") not in skip]
        seen = sum(float(z.get("seen_m2") or 0) for z in zones)
        denom = max(seen, float(c.get("floor_seen_m2") or 0))
        if not zones or denom <= 0:
            continue
        top = max(zones, key=lambda z: float(z.get("seen_m2") or 0))
        share = float(top.get("seen_m2") or 0) / denom
        if share < LINK_MIN_SHARE:
            continue
        cands.append({"camera_id": c["camera_id"], "name": c.get("name") or c["camera_id"],
                      "zone_id": top["zone_id"], "zone_name": top.get("name") or "", "share": round(share, 2)})
    by_zone: Dict[str, List[dict]] = defaultdict(list)
    for x in cands:
        by_zone[x["zone_id"]].append(x)
    items = []
    for zid, xs in by_zone.items():
        xs.sort(key=lambda x: -x["share"])
        for i, x in enumerate(xs):
            conflict = None
            if zid in linked:
                conflict = "another camera is already linked to this area"
            elif i > 0:
                conflict = f"{xs[0]['name']} sees more of this area"
            items.append({**x, "ok": conflict is None, "check": conflict})
    if not items:
        return []
    items.sort(key=lambda x: (not x["ok"], x["name"].lower()))
    ok = [x for x in items if x["ok"]]
    return [{
        "id": _sid("link", *(f"{x['camera_id']}>{x['zone_id']}" for x in items)), "kind": "link_zone",
        "target": "cameras", "level": "confirm",
        "title": f"Count {len(ok)} camera(s) into the area each one mostly sees" if ok
        else "Cameras to link to areas: please check",
        "detail": ("These cameras are on the map but not calibrated, so their shoppers count for no area. "
                   "Each one mostly sees one area. Linking counts its shoppers there until it is calibrated."),
        "links": items,
    }]


async def suggestions(db) -> List[Dict[str, Any]]:
    """Every open suggestion (dismissed ones hidden until their dismissal runs out)."""
    out: List[Dict[str, Any]] = []
    for fn in (_guard_suggestions, _wrong_alert_suggestions, _link_suggestions):
        try:
            out.extend(await fn(db))
        except Exception as exc:  # noqa: BLE001 - one broken source never hides the others
            logger.warning(f"automation suggestions ({fn.__name__}) failed: {exc}")
    dismissed = (_state_doc.get("dismissed") or {})
    now = time.time()
    return [s for s in out if float(dismissed.get(s["id"], 0) or 0) < now]


# =========================================================================== apply / dismiss / undo

class SuggestionError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def _save_features(db, camera_id: str, mutate) -> Tuple[dict, dict]:
    from app.models.db_models import CameraModel
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise SuggestionError("That camera no longer exists.", 404)
    before = dict(cam.features or {})
    after = mutate(json.loads(json.dumps(before)))
    merged = CameraFeatureConfig.model_validate(after)
    cam.features = merged.model_dump()
    return before, merged


async def apply(db, sid: str, actor: Optional[dict], camera_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Apply one suggestion (it must still be open). Returns the applied record (with its undo)."""
    from app.models.db_models import CameraModel, RestrictedAreaModel
    from app.services.feature_manager import feature_manager
    from app.services.restricted_areas import restricted_areas, validate_arming, validate_triggers

    sg = next((s for s in await suggestions(db) if s["id"] == sid), None)
    if sg is None:
        raise SuggestionError("This suggestion is no longer open (it was applied, dismissed or no longer fits).", 404)
    undo: Dict[str, Any] = {}
    after_cams: List[Tuple[str, Any]] = []
    refresh_areas = False
    if sg["kind"] == "guard_hours" and sg["target"] in ("night_watch", "night_watch_on"):
        turn_on = sg["target"] == "night_watch_on"
        undo["cameras"] = {}
        for c in sg["cameras"]:
            def mut(f, _on=turn_on):
                nw = dict(f.get("night_watch") or {})
                if _on:
                    nw["enabled"] = True
                nw["follow_store_hours"] = True
                f["night_watch"] = nw
                return f
            before, merged = await _save_features(db, c["id"], mut)
            undo["cameras"][c["id"]] = before.get("night_watch")
            after_cams.append((c["id"], merged))
    elif sg["kind"] == "guard_hours" and sg["target"] == "restricted_area":
        area = await db.get(RestrictedAreaModel, sg["area"]["id"])
        if area is None:
            raise SuggestionError("That restricted area no longer exists.", 404)
        hours = store_hours_minutes()
        rows = schedule_rows(hours)
        new = validate_arming({"mode": "schedule", "schedule_mode": "armed_outside", "schedule": rows,
                               "source": "store_hours", **({"timezone": area.arming["timezone"]}
                                                          if (area.arming or {}).get("timezone") else {})}) \
            if rows else validate_arming({"mode": "always", "source": "store_hours"})
        undo["area_id"], undo["arming"] = area.id, dict(area.arming or {})
        area.arming = new
        area.updated_at, area.updated_by = _now_utc(), (actor or {}).get("name") or "Automatic"
        await restricted_areas.audit(db, "settings_changed", area, actor,
                                     detail={"changed": {"arming": {"from": undo["arming"], "to": new}},
                                             "suggestion": sid})
        refresh_areas = True
    elif sg["kind"] == "less_sensitive" and sg["target"] == "theft_rule":
        cam_id, rule, to = sg["camera"]["id"], sg["rule"], sg["to_factor"]

        def mut(f):
            m = dict(f.get("theft_rule_sensitivity") or {})
            m[rule] = to
            f["theft_rule_sensitivity"] = m
            return f
        before, merged = await _save_features(db, cam_id, mut)
        undo["camera_id"], undo["rule"] = cam_id, rule
        undo["factor"] = (before.get("theft_rule_sensitivity") or {}).get(rule)
        after_cams.append((cam_id, merged))
    elif sg["kind"] == "less_sensitive" and sg["target"] == "restricted_trigger":
        area = await db.get(RestrictedAreaModel, sg["area"]["id"])
        if area is None:
            raise SuggestionError("That restricted area no longer exists.", 404)
        trig = sg["trigger"]
        area.triggers = validate_triggers({trig: dict(sg["to"])}, area.triggers)
        undo["area_id"], undo["trigger"], undo["params"] = area.id, trig, dict(sg["from"])
        area.updated_at, area.updated_by = _now_utc(), (actor or {}).get("name") or "Automatic"
        await restricted_areas.audit(db, "settings_changed", area, actor,
                                     detail={"changed": {"triggers": {trig: {"from": sg["from"], "to": sg["to"]}}},
                                             "suggestion": sid})
        refresh_areas = True
    elif sg["kind"] == "link_zone":
        from app.services.live_analytics_engine import live_engine

        wanted = set(camera_ids) if camera_ids else None
        undo["links"] = {}
        for x in sg["links"]:
            if not x["ok"] or (wanted is not None and x["camera_id"] not in wanted):
                continue
            cam = await db.get(CameraModel, x["camera_id"])
            if cam is None or cam.watch_zone_id:
                continue
            undo["links"][cam.id] = cam.watch_zone_id
            cam.watch_zone_id = x["zone_id"]
        if not undo["links"]:
            raise SuggestionError("Nothing to link: every camera here needs a check first.", 409)
    else:
        raise SuggestionError("This suggestion cannot be applied.")
    record = {"apply_id": "ap_" + uuid.uuid4().hex[:12], "suggestion_id": sid, "kind": sg["kind"],
              "target": sg["target"], "title": sg["title"], "at": _iso(), "by": (actor or {}).get("name"),
              "undo": undo}
    state = await _get(db, STATE_KEY)
    state["applied"] = ([record] + list(state.get("applied") or []))[:MAX_APPLIED]
    await _put(db, STATE_KEY, state)
    await _audit(db, "auto_apply", actor, {"suggestion": sid, "kind": sg["kind"], "target": sg["target"],
                                           "title": sg["title"], "apply_id": record["apply_id"]})
    await db.commit()
    for cam_id, merged in after_cams:
        feature_manager.set_camera_features(cam_id, merged)
    if sg["kind"] == "link_zone":
        from app.services.live_analytics_engine import live_engine

        for cam_id in undo["links"]:
            x = next(i for i in sg["links"] if i["camera_id"] == cam_id)
            live_engine.set_camera_zone(cam_id, x["zone_id"])
    if refresh_areas:
        await restricted_areas.refresh()
    load_cache()
    return record


async def dismiss(db, sid: str, actor: Optional[dict]) -> None:
    state = await _get(db, STATE_KEY)
    dismissed = {k: v for k, v in (state.get("dismissed") or {}).items() if float(v or 0) > time.time()}
    dismissed[sid] = time.time() + DISMISS_DAYS * 86400
    state["dismissed"] = dismissed
    await _put(db, STATE_KEY, state)
    await _audit(db, "auto_dismiss", actor, {"suggestion": sid, "days": DISMISS_DAYS})
    await db.commit()
    load_cache()


async def undo(db, apply_id: str, actor: Optional[dict]) -> Dict[str, Any]:
    from app.models.db_models import CameraModel, RestrictedAreaModel
    from app.services.feature_manager import feature_manager
    from app.services.restricted_areas import restricted_areas, validate_triggers

    state = await _get(db, STATE_KEY)
    applied = list(state.get("applied") or [])
    rec = next((r for r in applied if r.get("apply_id") == apply_id), None)
    if rec is None or rec.get("undone_at"):
        raise SuggestionError("Nothing to undo: it was already undone or is too old.", 404)
    u = rec.get("undo") or {}
    after_cams = []
    links_back: Dict[str, Optional[str]] = {}
    refresh = False
    if rec["kind"] == "guard_hours" and rec["target"] in ("night_watch", "night_watch_on"):
        for cam_id, old in (u.get("cameras") or {}).items():
            def mut(f, _old=old):
                if _old is None:
                    f.pop("night_watch", None)
                else:
                    f["night_watch"] = _old
                return f
            try:
                _b, merged = await _save_features(db, cam_id, mut)
                after_cams.append((cam_id, merged))
            except SuggestionError:
                continue
    elif rec["target"] == "restricted_area":
        area = await db.get(RestrictedAreaModel, u.get("area_id"))
        if area is not None:
            area.arming = u.get("arming") or {"mode": "always"}
            await restricted_areas.audit(db, "settings_changed", area, actor,
                                         detail={"undo": apply_id, "changed": {"arming": {"to": area.arming}}})
            refresh = True
    elif rec["target"] == "theft_rule":
        def mut(f):
            m = dict(f.get("theft_rule_sensitivity") or {})
            if u.get("factor") is None:
                m.pop(u["rule"], None)
            else:
                m[u["rule"]] = u["factor"]
            f["theft_rule_sensitivity"] = m
            return f
        _b, merged = await _save_features(db, u["camera_id"], mut)
        after_cams.append((u["camera_id"], merged))
    elif rec["target"] == "restricted_trigger":
        area = await db.get(RestrictedAreaModel, u.get("area_id"))
        if area is not None:
            area.triggers = validate_triggers({u["trigger"]: dict(u["params"])}, area.triggers)
            await restricted_areas.audit(db, "settings_changed", area, actor,
                                         detail={"undo": apply_id, "changed": {"triggers": {u["trigger"]: u["params"]}}})
            refresh = True
    elif rec["kind"] == "link_zone":
        for cam_id, old in (u.get("links") or {}).items():
            cam = await db.get(CameraModel, cam_id)
            if cam is not None:
                cam.watch_zone_id = old
                links_back[cam_id] = old
    rec["undone_at"], rec["undone_by"] = _iso(), (actor or {}).get("name")
    state["applied"] = applied
    # The suggestion comes back only after its dismissal period: undo means "not now".
    dismissed = dict(state.get("dismissed") or {})
    dismissed[rec["suggestion_id"]] = time.time() + DISMISS_DAYS * 86400
    state["dismissed"] = dismissed
    await _put(db, STATE_KEY, state)
    await _audit(db, "auto_undo", actor, {"apply_id": apply_id, "suggestion": rec["suggestion_id"],
                                          "title": rec.get("title")})
    await db.commit()
    for cam_id, merged in after_cams:
        feature_manager.set_camera_features(cam_id, merged)
    if links_back:
        from app.services.live_analytics_engine import live_engine

        for cam_id, old in links_back.items():
            live_engine.set_camera_zone(cam_id, old)
    if refresh:
        await restricted_areas.refresh()
    load_cache()
    return rec


# =========================================================================== status

def storage_view() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from app.services.site_settings import _auto_cap_bytes, disk_info

        disk = disk_info()
        gb = float(getattr(settings, "EVIDENCE_MAX_GB", 0) or 0)
        auto = _auto_cap_bytes(disk)
        out["disk_total_gb"] = round((disk.get("total_bytes") or 0) / 1024 ** 3, 1) or None
        out["disk_free_gb"] = round((disk.get("free_bytes") or 0) / 1024 ** 3, 1) or None
        out["evidence_gb"] = round(gb, 1) if gb > 0 else (round(auto / 1024 ** 3, 1) if auto else None)
        out["evidence_source"] = "setting" if gb > 0 else "automatic"
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"storage view: {exc}")
    try:
        from app.services.backup_service import backup_service

        out["backups_budget_gb"] = round(backup_service.budget_bytes() / 1024 ** 3, 1)
        out["backups_source"] = "setting" if backup_service.max_bytes_setting else "automatic"
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"backup budget: {exc}")
    return out


def hours_status() -> Dict[str, Any]:
    from app.services.timeutil import site_tz_source

    learned = _hours_doc.get("learned") or {}
    eff = effective_days()
    hours = store_hours_minutes()
    bh, why = quietest_hour(hours, learned.get("profile"))
    days = []
    for wd in DAYS:
        e = eff[wd]
        days.append({"day": wd, "label": DAY_LONG[wd], "hours": e["spec"], "source": e["source"],
                     "learned": (learned.get("days") or {}).get(wd), "estimated": wd in (learned.get("estimated") or []),
                     "manual": (_hours_doc.get("manual") or {}).get(wd)})
    return {
        "status": learned.get("status") or "learning", "days_measured": learned.get("days_measured", 0),
        "needed": MIN_DAYS, "learned_at": learned.get("learned_at"), "days": days,
        "view": store_hours_view(), "timezone": site_tz_source(),
        "backup": {"hour": bh, "time": hhmm(bh * 60), "why": why},
    }


def tune_status() -> Dict[str, Any]:
    from app.services.site_settings import site_settings

    rec = dict(_state_doc.get("auto_tune") or {})
    rec["current"] = {k: site_settings.effective(k) for k in TUNE_KEYS}
    rec["source"] = {k: ("setting" if k in site_settings.overrides else
                         "automatic" if k in site_settings.auto else "default") for k in TUNE_KEYS}
    return rec


def restarts_status() -> List[Dict[str, Any]]:
    try:
        from app.services.camera_restarts import camera_restarts
        from app.services.live_analytics_engine import live_engine

        names = {k: getattr(rt, "name", k) for k, rt in list(live_engine.runtimes.items())}
        return [{"camera_id": k, "name": names.get(k, k), **v} for k, v in camera_restarts.today().items()]
    except Exception:  # noqa: BLE001
        return []


def applied_list() -> List[Dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "undo"} for r in (_state_doc.get("applied") or [])][:20]


# =========================================================================== background loop

class AutomationService:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._last_learn = 0.0
        self._last_tune = 0.0
        self._cams_seen: Optional[int] = None
        self._cams_changed_at = 0.0

    async def start(self) -> None:
        load_cache()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="automation")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if now - self._last_learn >= LEARN_EVERY_S:
            self._last_learn = now
            try:
                await relearn()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Store hours not learnt this time: {exc}")
        cams = _cameras_streaming()
        if cams != self._cams_seen:
            self._cams_seen, self._cams_changed_at = cams, now
        due = now - self._last_tune >= TUNE_EVERY_S
        settled_change = (self._cams_changed_at > self._last_tune and now - self._cams_changed_at >= TUNE_SETTLE_S)
        never = not (_state_doc.get("auto_tune") or {}).get("chosen")
        if due or settled_change or never:
            try:
                rec = await asyncio.to_thread(tune_now, "daily" if due else "cameras changed" if settled_change
                                              else "first measurement")
                if rec.get("chosen"):
                    self._last_tune = now
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Analysis speed not tuned this time: {exc}")

    async def _run(self) -> None:
        await asyncio.sleep(120)
        while True:
            await self.tick()
            await asyncio.sleep(600)


automation = AutomationService()
