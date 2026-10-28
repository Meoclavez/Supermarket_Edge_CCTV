"""What this box may run under its iKorex plan (docs/FLEET_CONTROL_CONTRACT.md).

The signed policy that ``fleet_control`` verified and stored in
``system_setup['fleet_control']`` decides:

* **paused** -- an explicit signed *Suspend*, or the licence end date plus its
  grace days has passed (evaluated on this box's own clock, so it works offline),
  or **standby**: the policy sets ``licence.offline_grace_hours`` and the box
  has had no *verified* contact with iKorex (a signed check-in ack, see
  ``fleet_control.verify_ack``) for longer than that (owner decision 2026-10-21);
* **features** -- each paid feature on or off (a missing key means on);
* **limits** -- ``max_cameras``;
* **support** -- the iKorex support contact shown to store users (Help panel,
  paused/standby page, Report a problem). Only from the signed policy, never
  from unsigned check-in fields (a spoofed phone number would be phishing).

Before any policy arrived (a box never set up for fleet control) everything is
on and nothing is paused: the box behaves exactly as before.

This module is read on every API request (``FleetGateMiddleware``) and by the
camera workers (``feature_enabled``), so the stored policy is cached in memory
and only re-read when ``fleet_control`` writes it (``invalidate``) or after
``CACHE_TTL_S`` (a restored backup, another process).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger("edge.entitlements")

STATE_KEY = "fleet_control"
CACHE_TTL_S = 30.0
DEFAULT_GRACE_DAYS = 14
MAX_GRACE_DAYS = 365
MAX_MESSAGE_CHARS = 300
# licence.offline_grace_hours: None = never stand by (the default), else 1..2160 (90 days).
MIN_OFFLINE_GRACE_HOURS = 1
MAX_OFFLINE_GRACE_HOURS = 2160
# The "stands by soon" banner shows when less than this share of the grace is left.
STANDBY_WARN_SHARE = 0.25

FEATURES = (
    "theft_detection",    # theft / loss alerts (pose analytics incidents)
    "zone_alerts",        # restricted-area and line-crossing alerts
    "restricted_areas",   # high-security restricted areas (announce entry, triggers, tamper)
    "night_watch",        # after-hours motion / person watch
    "phone_alerts",       # Web Push + paired phone app push, store-offline watchdog
    "heatmaps",           # recorded heatmap history
    "business_insights",  # recommendations, business analysis, daily report
    "online_access",      # the VPS tunnel (dashboard from outside the store)
    "remote_live_video",  # WebRTC live video requested through the public address
    "pos_integration",    # till key and sales receipt ingest
)
# Plain names for the dashboard and the 403 message.
FEATURE_LABELS = {
    "theft_detection": "Theft alerts",
    "zone_alerts": "Area and line alerts",
    "restricted_areas": "Restricted areas",
    "night_watch": "Night watch",
    "phone_alerts": "Phone alerts",
    "heatmaps": "Heatmaps",
    "business_insights": "Business insights",
    "online_access": "Online access",
    "remote_live_video": "Live video from outside the store",
    "pos_integration": "Sales data (point of sale)",
}
# Features that keep working while the service is paused: the tunnel stays up
# so remote users see the paused page instead of "store offline".
WORKS_WHILE_PAUSED = frozenset({"online_access"})

PAUSED_MESSAGE = "Service paused. Please contact iKorex."
EXPIRED_MESSAGE = "Your iKorex service licence has ended. Please contact iKorex."
STANDBY_MESSAGE = ("Standby: this box can't reach iKorex. It will restart automatically once the connection "
                   "is back and verified.")
NOT_IN_PLAN = "Not included in your plan."

# policy.support (docs/SUPPORT_ISSUES_CONTRACT.md): plain text <= 120 chars each,
# website https only, e-mail checked. Unknown keys are ignored; a value that does
# not pass is dropped on its own (never the whole policy: a bad phone number must
# not stop a Suspend from applying).
SUPPORT_FIELDS = ("name", "phone", "email", "hours", "website", "note")
SUPPORT_MAX_CHARS = 120
_SUPPORT_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                               r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_SUPPORT_PHONE_RE = re.compile(r"^\+?[0-9(][0-9 ()./-]{4,30}$")
_SUPPORT_WEBSITE_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:[/?#][^\s<>\"']*)?$")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"policy": None, "contact": {}, "loaded_at": None}
# The fields of the stored state the offline standby rule needs (fleet_control writes them).
CONTACT_FIELDS = ("last_verified_contact_at", "contact_anchor", "clock_skew_s", "policy_applied_at")


class PolicyError(ValueError):
    """A verified policy payload that does not follow the contract (machine code in ``code``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GateError(HTTPException):
    """An HTTPException whose JSON body is ``body`` as is (see main.py's handler).

    Subclassing HTTPException keeps ResilientRoute from turning it into a 500.
    """

    def __init__(self, status_code: int, body: Dict[str, Any]):
        super().__init__(status_code=status_code, detail=body.get("detail") or body.get("message"))
        self.body = body


# --------------------------------------------------------------------------- policy

def default_policy() -> Dict[str, Any]:
    return {"type": "policy", "store_id": None, "version": 0, "issued_at": None, "state": "active",
            "message": "", "licence": {"valid_until": None, "grace_days": DEFAULT_GRACE_DAYS,
                                       "offline_grace_hours": None},
            "features": {k: True for k in FEATURES}, "limits": {"max_cameras": None}, "support": None}


def normalise_support(value: Any) -> Optional[Dict[str, str]]:
    """policy.support reduced to valid plain-text fields; None = unknown (box falls back)."""
    if not isinstance(value, dict):
        return None
    out: Dict[str, str] = {}
    for key in SUPPORT_FIELDS:
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        text = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
        if not text or len(text) > SUPPORT_MAX_CHARS:
            continue
        if key == "email" and not _SUPPORT_EMAIL_RE.match(text):
            continue
        if key == "phone" and (not _SUPPORT_PHONE_RE.match(text) or sum(c.isdigit() for c in text) < 6):
            continue
        if key == "website" and not _SUPPORT_WEBSITE_RE.match(text):
            continue
        out[key] = text
    return out or None


def _parse_day(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise PolicyError("bad_licence_date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise PolicyError("bad_licence_date") from None


def normalise_policy(payload: Any, store_id: Optional[str] = None) -> Dict[str, Any]:
    """The contract's policy payload, checked and reduced to known fields.

    Raises PolicyError for anything that makes the policy unusable. Unknown
    feature keys are dropped; missing ones are on (forward compatible).
    """
    if not isinstance(payload, dict) or payload.get("type") != "policy":
        raise PolicyError("not_a_policy")
    if store_id is not None and payload.get("store_id") != store_id:
        raise PolicyError("wrong_store")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PolicyError("bad_version")
    state = payload.get("state", "active")
    if state not in ("active", "suspended"):
        raise PolicyError("bad_state")
    message = payload.get("message") or ""
    if not isinstance(message, str):
        raise PolicyError("bad_message")
    message = " ".join(message.split())[:MAX_MESSAGE_CHARS]
    licence = payload.get("licence") or {}
    if not isinstance(licence, dict):
        raise PolicyError("bad_licence")
    valid_until = _parse_day(licence.get("valid_until"))
    grace = licence.get("grace_days", DEFAULT_GRACE_DAYS)
    if grace is None:
        grace = DEFAULT_GRACE_DAYS
    if not isinstance(grace, int) or isinstance(grace, bool) or not 0 <= grace <= MAX_GRACE_DAYS:
        raise PolicyError("bad_grace_days")
    offline_grace = licence.get("offline_grace_hours")
    if offline_grace is not None and (not isinstance(offline_grace, int) or isinstance(offline_grace, bool)
                                      or not MIN_OFFLINE_GRACE_HOURS <= offline_grace <= MAX_OFFLINE_GRACE_HOURS):
        raise PolicyError("bad_offline_grace_hours")
    raw_features = payload.get("features") or {}
    if not isinstance(raw_features, dict):
        raise PolicyError("bad_features")
    features = {}
    for key in FEATURES:
        value = raw_features.get(key, True)
        if not isinstance(value, bool):
            raise PolicyError("bad_features")
        features[key] = value
    limits = payload.get("limits") or {}
    if not isinstance(limits, dict):
        raise PolicyError("bad_limits")
    max_cameras = limits.get("max_cameras")
    if max_cameras is not None and (not isinstance(max_cameras, int) or isinstance(max_cameras, bool)
                                    or max_cameras < 0):
        raise PolicyError("bad_limits")
    issued_at = payload.get("issued_at")
    return {
        "type": "policy", "store_id": payload.get("store_id"), "version": version,
        "issued_at": issued_at if isinstance(issued_at, str) else None,
        "state": state, "message": message,
        "licence": {"valid_until": valid_until.isoformat() if valid_until else None, "grace_days": grace,
                    "offline_grace_hours": offline_grace},
        "features": features, "limits": {"max_cameras": max_cameras},
        "support": normalise_support(payload.get("support")),
    }


# --------------------------------------------------------------------------- storage (cached)

def read_stored_state() -> Dict[str, Any]:
    """The ``fleet_control`` row as a dict ({} when absent or unreadable)."""
    try:
        with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=5.0)) as conn:
            row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (STATE_KEY,)).fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
    except ValueError:
        logger.warning("Stored fleet control state is not valid JSON; ignoring it")
        return {}
    return value if isinstance(value, dict) else {}


def invalidate() -> None:
    with _lock:
        _cache["loaded_at"] = None


def _load_cached() -> Dict[str, Any]:
    now = time.monotonic()
    with _lock:
        if _cache["loaded_at"] is not None and now - _cache["loaded_at"] < CACHE_TTL_S:
            return {"policy": _cache["policy"], "contact": _cache["contact"]}
    stored = read_stored_state()
    policy = stored.get("policy")
    if policy is not None:
        try:
            policy = normalise_policy(policy)
        except PolicyError as exc:  # stored by an older build or edited by hand
            logger.warning("Stored fleet policy unusable (%s); using the default policy", exc.code)
            policy = None
    contact = {k: stored.get(k) for k in CONTACT_FIELDS}
    with _lock:
        _cache.update(policy=policy, contact=contact, loaded_at=now)
    return {"policy": policy, "contact": contact}


def stored_policy() -> Optional[Dict[str, Any]]:
    """The last verified policy (None = never configured for fleet control)."""
    return _load_cached()["policy"]


def stored_contact() -> Dict[str, Any]:
    """last_verified_contact_at, contact_anchor, clock_skew_s, policy_applied_at (cached)."""
    return dict(_load_cached()["contact"])


# --------------------------------------------------------------------------- offline standby clocks
#
# How long since the last verified contact, without trusting the wall clock
# alone (a clock set back must not extend the grace forever):
# * same boot as the contact: the boot clock (CLOCK_BOOTTIME: monotonic,
#   counts suspend, ignores wall clock changes);
# * rebooted since: the wall clock against the box-clock time of the contact
#   (skew-neutral), but never less than the floor;
# * the floor: ``floor_s`` as fleet_control last persisted it (every check-in
#   attempt, i.e. at least every 15 min while offline, with the boot and boot
#   clock it was taken on), plus the boot-clock time since then (all of this
#   boot's uptime when the box has rebooted since).
# So the time the box runs always counts; only time it is switched off with
# its clock set back (and at most the 15 min before it was switched off) can
# be lost. Tests replace these three functions.

def wall_clock() -> float:
    return time.time()


def boot_clock() -> Optional[float]:
    try:
        return float(time.clock_gettime(time.CLOCK_BOOTTIME))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


_boot_id_value: Dict[str, Optional[str]] = {}


def boot_id() -> Optional[str]:
    if "id" not in _boot_id_value:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            value = ""
        _boot_id_value["id"] = value if re.fullmatch(r"[0-9a-f-]{36}", value) else None
    return _boot_id_value["id"]


def contact_anchor(source: str) -> Dict[str, Any]:
    """A fresh anchor: "now" on every clock this module counts with."""
    bid, bt = boot_id(), boot_clock()
    return {"box_at": wall_clock(), "boot_id": bid, "boottime_s": bt, "source": source,
            "floor_s": 0.0, "floor_boot_id": bid, "floor_boottime_s": bt}


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def contact_elapsed_s(anchor: Any) -> Optional[float]:
    """Seconds since ``anchor`` (see above); None without an anchor."""
    if not isinstance(anchor, dict):
        return None
    now_boot, now_id = boot_clock(), boot_id()

    def since(then_id: Any, then_boot: Optional[float]) -> Optional[float]:
        """Boot-clock seconds since (then_id, then_boot); None when unknown."""
        if not then_id or not now_id or now_boot is None:
            return None
        if then_id != now_id:
            return now_boot              # rebooted since: at least this boot's uptime
        if then_boot is None or now_boot < then_boot:
            return None
        return now_boot - then_boot

    floor = max(_num(anchor.get("floor_s")) or 0.0, 0.0)
    floor += since(anchor.get("floor_boot_id"), _num(anchor.get("floor_boottime_s"))) or 0.0
    then_id = anchor.get("boot_id")
    if then_id and then_id == now_id:
        exact = since(then_id, _num(anchor.get("boottime_s")))
        if exact is not None:
            return max(exact, 0.0)
    box_at = _num(anchor.get("box_at"))
    elapsed = (wall_clock() - box_at) if box_at is not None else 0.0
    return max(elapsed, floor, 0.0)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def offline_info(policy: Dict[str, Any], contact: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The offline standby rule: grace, time since the last verified contact, standby, warning."""
    contact = contact if contact is not None else stored_contact()
    grace_h = (policy.get("licence") or {}).get("offline_grace_hours")
    grace_h = grace_h if isinstance(grace_h, int) and not isinstance(grace_h, bool) and grace_h > 0 else None
    elapsed = contact_elapsed_s(contact.get("contact_anchor"))
    if elapsed is None and grace_h is not None:
        # No anchor at all (state from an older build or edited by hand): count
        # from when the policy was applied, on the wall clock.
        applied = _parse_iso(contact.get("policy_applied_at"))
        if applied is not None:
            elapsed = max(0.0, wall_clock() - applied.timestamp())
    standby = warning = False
    remaining = None
    if grace_h is not None and elapsed is not None:
        grace_s = grace_h * 3600.0
        remaining = grace_s - elapsed
        standby = remaining < 0
        warning = not standby and remaining < STANDBY_WARN_SHARE * grace_s
    skew = _num(contact.get("clock_skew_s"))
    return {
        "grace_hours": grace_h,
        "last_verified_contact_at": contact.get("last_verified_contact_at"),
        "elapsed_s": int(elapsed) if elapsed is not None else None,
        "remaining_s": int(max(remaining, 0)) if remaining is not None else None,
        "standby": standby,
        "warning": warning,
        "clock_skew_s": int(skew) if skew is not None else None,
    }


# --------------------------------------------------------------------------- effective state

def local_today() -> date:
    """Today in the store's time zone (the licence date is a store date)."""
    try:
        from app.services.timeutil import local_now

        return local_now().date()
    except Exception:  # noqa: BLE001 - never fail a request on the clock
        return date.today()


def licence_info(policy: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """ok | warning (past valid_until, within grace) | expired, with the dates."""
    licence = policy.get("licence") or {}
    grace = licence.get("grace_days")
    grace = DEFAULT_GRACE_DAYS if grace is None else int(grace)
    valid_until = licence.get("valid_until")
    if not valid_until:
        return {"status": "ok", "valid_until": None, "grace_days": grace, "grace_until": None}
    end = date.fromisoformat(valid_until)
    grace_until = end + timedelta(days=grace)
    today = today or local_today()
    if today <= end:
        status = "ok"
    elif today <= grace_until:
        status = "warning"
    else:
        status = "expired"
    return {"status": status, "valid_until": end.isoformat(), "grace_days": grace,
            "grace_until": grace_until.isoformat()}


def effective_state(today: Optional[date] = None) -> Dict[str, Any]:
    cached = _load_cached()
    policy = cached["policy"]
    managed = policy is not None
    policy = policy or default_policy()
    lic = licence_info(policy, today)
    offline = offline_info(policy, cached["contact"])  # the default policy has no grace: never standby
    suspended = policy.get("state") == "suspended"
    expired = lic["status"] == "expired"
    standby = offline["standby"]
    paused = suspended or expired or standby
    if suspended:
        message, reason = policy.get("message") or PAUSED_MESSAGE, "suspended"
    elif expired:
        message, reason = EXPIRED_MESSAGE, "licence_expired"
    elif standby:
        message, reason = STANDBY_MESSAGE, "standby"
    else:
        message, reason = "", None
    return {
        "managed": managed,
        "version": int(policy.get("version") or 0),
        "state": policy.get("state") or "active",
        "paused": paused,
        "paused_reason": reason,
        "standby": standby,
        "offline": offline,
        "message": message,
        "licence": lic,
        "features": dict(policy.get("features") or {}),
        "limits": dict(policy.get("limits") or {"max_cameras": None}),
        "support": dict(policy["support"]) if isinstance(policy.get("support"), dict) else None,
    }


def is_paused(today: Optional[date] = None) -> bool:
    if stored_policy() is None:
        return False
    return effective_state(today)["paused"]


def feature_enabled(key: str) -> bool:
    """Whether ``key`` may run now: in the plan, and not paused (except online access)."""
    if key not in FEATURES:
        raise KeyError(f"Unknown plan feature: {key}")
    policy = stored_policy()
    if policy is None:
        return True
    if not (policy.get("features") or {}).get(key, True):
        return False
    if key in WORKS_WHILE_PAUSED:
        return True
    return not effective_state()["paused"]


def feature_safe(key: str) -> bool:
    """feature_enabled for hot paths: an unreadable state never turns a feature off."""
    try:
        return feature_enabled(key)
    except Exception:  # noqa: BLE001
        return True


def max_cameras() -> Optional[int]:
    policy = stored_policy()
    if policy is None:
        return None
    value = (policy.get("limits") or {}).get("max_cameras")
    return int(value) if isinstance(value, int) else None


def alert_feature(event_type: Optional[str], data: Optional[dict] = None) -> Optional[str]:
    """The plan feature an alert belongs to (None: operational, e.g. camera offline)."""
    et = str(event_type or "").upper()
    if et == "RESTRICTED_AREA" and data and data.get("ra_area_id"):
        return "restricted_areas"
    if et in ("RESTRICTED_AREA", "TRIPWIRE_ALERT"):
        return "zone_alerts"
    if et in ("NIGHT_INTRUSION", "NIGHT_MOTION"):
        return "night_watch"
    if et in ("THEFT_SUSPECTED", "CONCEALMENT", "SHELF_SWEEP", "EXIT_WITHOUT_CHECKOUT", "LOITERING"):
        return "theft_detection"
    if data and data.get("incident_id"):
        return "theft_detection"
    return None


def feature_disabled_body(key: str) -> Dict[str, Any]:
    label = FEATURE_LABELS.get(key, key)
    return {"code": "feature_disabled", "feature": key,
            "detail": f"{label}: {NOT_IN_PLAN} Please contact iKorex to add it."}


def require_feature(key: str) -> None:
    """Raise 403 feature_disabled when ``key`` is off (for use inside a route)."""
    if not feature_enabled(key):
        raise GateError(403, feature_disabled_body(key))


# --------------------------------------------------------------------------- camera limit

def enabled_camera_count() -> int:
    try:
        with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=5.0)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM cameras WHERE is_ai_enabled = 1").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def camera_limit_body(limit: int) -> Dict[str, Any]:
    return {"code": "camera_limit", "limit": limit,
            "detail": (f"Your plan includes {limit} camera{'s' if limit != 1 else ''}. Turn another camera off "
                       "first, or contact iKorex to add more.")}


def ensure_camera_room(adding: int = 1) -> None:
    """Raise 409 camera_limit when ``adding`` more enabled cameras would pass max_cameras."""
    limit = max_cameras()
    if limit is None or adding <= 0:
        return
    if enabled_camera_count() + adding > limit:
        raise GateError(409, camera_limit_body(limit))


# --------------------------------------------------------------------------- API gate

_FEATURE_ROUTES = tuple((feature, re.compile(rx)) for feature, rx in (
    ("theft_detection", r"^/api/v1/theft(/|$)"),
    # Restricted areas and the zone alert snapshots. Tripwire drawing stays
    # available: tripwires also count footfall (entrances); only their alerts are gated.
    ("zone_alerts", r"^/api/zones/(intrusion|alerts)(/|$)"),
    # High-security restricted areas (announce entry, triggers, tamper, audit).
    ("restricted_areas", r"^/api/v1/restricted-areas(/|$)"),
    ("night_watch", r"^/api/v1/(night-watch|cameras/[^/]+/night-watch)(/|$)"),
    ("phone_alerts", r"^/api/v1/(web-push|push)(/|$)"),
    ("heatmaps", r"^/api/v1/analytics/heatmaps(/|$)"),
    ("business_insights",
     r"^/api/v1/analytics/(decisions|actions|report/daily|digest|business|market|recommendations)(/|$)"),
    # Only the access check: the Online access settings (server, store ID, token)
    # stay editable because the fleet check-in itself uses them. With the
    # feature off the tunnel does not run (remote_access_service.should_run).
    ("online_access", r"^/api/v1/remote-access/verify(/|$)"),
    ("pos_integration", r"^/api/v1/(analytics/pos|store/pos-registers|cameras/[^/]+/pos-register)(/|$)"),
))
# Camera-drawn restricted areas are also the camera regions of high-security
# restricted areas: drawing them stays open when either feature is in the plan.
_SHARED_DRAWING = re.compile(r"^/api/zones/intrusion(/|$)")
_REMOTE_VIDEO_ROUTES = re.compile(r"^/api/v1/webrtc/(sessions|offer)(/|$)")
_ADOPT_ROUTES = re.compile(r"^/api/v1/layout/devices/adopt/?$")

# Open while paused: sign-in, first-run setup, this status, the support contact
# and Report a problem (a paused store must still be able to ask for help), health,
# the device identity phones check, and everything that is not the API (pages,
# static files).
_PAUSE_EXEMPT = re.compile(
    r"^/api/v1/(auth|setup|fleet|support|health|device/identity)(/|$)")


def feature_for_path(path: str) -> Optional[str]:
    for feature, rx in _FEATURE_ROUTES:
        if rx.match(path):
            return feature
    return None


def paused_blocks(path: str) -> bool:
    path = re.sub(r"/{2,}", "/", path or "")
    if path.startswith("/api/"):
        return not _PAUSE_EXEMPT.match(path)
    return path == "/stream" or path.startswith("/stream/")


class FleetGateMiddleware:
    """423 service_paused / service_standby, 403 feature_disabled and 409 camera_limit at the door.

    Pure ASGI (no buffering of MJPEG or websockets). A box with no policy
    passes straight through.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            refusal = self._refusal(scope)
        except Exception as exc:  # noqa: BLE001 - a broken gate must never lock the store out
            logger.warning("fleet gate check failed: %s", exc)
            refusal = None
        if refusal is None:
            await self.app(scope, receive, send)
            return
        status, body = refusal
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
            (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": raw})

    @staticmethod
    def _refusal(scope) -> Optional[tuple]:
        if stored_policy() is None:
            return None
        path = scope.get("path", "") or ""
        st = effective_state()
        if st["paused"] and paused_blocks(path):
            code = "service_standby" if st["paused_reason"] == "standby" else "service_paused"
            return 423, {"code": code, "message": st["message"], "detail": st["message"]}
        feature = feature_for_path(path)
        if feature == "zone_alerts" and _SHARED_DRAWING.match(path) and feature_enabled("restricted_areas"):
            feature = None   # restricted areas draw their camera regions with the same tool
        if feature and not feature_enabled(feature):
            return 403, feature_disabled_body(feature)
        method = scope.get("method", "GET")
        if method == "POST" and _REMOTE_VIDEO_ROUTES.match(path) and not feature_enabled("remote_live_video"):
            from starlette.requests import HTTPConnection

            from app.services.public_exposure import is_remote_request

            if is_remote_request(HTTPConnection(scope)):
                return 403, feature_disabled_body("remote_live_video")
        if method == "POST" and _ADOPT_ROUTES.match(path):
            limit = max_cameras()
            if limit is not None and enabled_camera_count() + 1 > limit:
                return 409, camera_limit_body(limit)
        return None
