"""What this box may run under its iKorex plan (docs/FLEET_CONTROL_CONTRACT.md).

The signed policy that ``fleet_control`` verified and stored in
``system_setup['fleet_control']`` decides:

* **paused** -- an explicit signed *Suspend*, or the licence end date plus its
  grace days has passed (evaluated on this box's own clock, so it works offline);
* **features** -- each paid feature on or off (a missing key means on);
* **limits** -- ``max_cameras``.

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
from datetime import date, timedelta
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger("edge.entitlements")

STATE_KEY = "fleet_control"
CACHE_TTL_S = 30.0
DEFAULT_GRACE_DAYS = 14
MAX_GRACE_DAYS = 365
MAX_MESSAGE_CHARS = 300

FEATURES = (
    "theft_detection",    # theft / loss alerts (pose analytics incidents)
    "zone_alerts",        # restricted-area and line-crossing alerts
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
NOT_IN_PLAN = "Not included in your plan."

_lock = threading.Lock()
_cache: Dict[str, Any] = {"policy": None, "loaded_at": None}


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
            "message": "", "licence": {"valid_until": None, "grace_days": DEFAULT_GRACE_DAYS},
            "features": {k: True for k in FEATURES}, "limits": {"max_cameras": None}}


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
        "licence": {"valid_until": valid_until.isoformat() if valid_until else None, "grace_days": grace},
        "features": features, "limits": {"max_cameras": max_cameras},
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


def stored_policy() -> Optional[Dict[str, Any]]:
    """The last verified policy (None = never configured for fleet control)."""
    now = time.monotonic()
    with _lock:
        if _cache["loaded_at"] is not None and now - _cache["loaded_at"] < CACHE_TTL_S:
            return _cache["policy"]
    policy = read_stored_state().get("policy")
    if policy is not None:
        try:
            policy = normalise_policy(policy)
        except PolicyError as exc:  # stored by an older build or edited by hand
            logger.warning("Stored fleet policy unusable (%s); using the default policy", exc.code)
            policy = None
    with _lock:
        _cache.update(policy=policy, loaded_at=now)
    return policy


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
    policy = stored_policy()
    managed = policy is not None
    policy = policy or default_policy()
    lic = licence_info(policy, today)
    suspended = policy.get("state") == "suspended"
    expired = lic["status"] == "expired"
    paused = suspended or expired
    if suspended:
        message = policy.get("message") or PAUSED_MESSAGE
    elif expired:
        message = EXPIRED_MESSAGE
    else:
        message = ""
    return {
        "managed": managed,
        "version": int(policy.get("version") or 0),
        "state": policy.get("state") or "active",
        "paused": paused,
        "paused_reason": "suspended" if suspended else ("licence_expired" if expired else None),
        "message": message,
        "licence": lic,
        "features": dict(policy.get("features") or {}),
        "limits": dict(policy.get("limits") or {"max_cameras": None}),
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
_REMOTE_VIDEO_ROUTES = re.compile(r"^/api/v1/webrtc/(sessions|offer)(/|$)")
_ADOPT_ROUTES = re.compile(r"^/api/v1/layout/devices/adopt/?$")

# Open while paused: sign-in, first-run setup, this status, health, the device
# identity phones check, and everything that is not the API (pages, static files).
_PAUSE_EXEMPT = re.compile(
    r"^/api/v1/(auth|setup|fleet|health|device/identity)(/|$)")


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
    """423 service_paused, 403 feature_disabled and 409 camera_limit at the door.

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
            return 423, {"code": "service_paused", "message": st["message"], "detail": st["message"]}
        feature = feature_for_path(path)
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
