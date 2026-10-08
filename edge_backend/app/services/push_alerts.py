"""Alerts to installed web-app phones: who gets what, and escalation.

**Roster** (Settings -> Phone alerts, saved in ``system_setup`` under
``push_alert_roster``): accounts are *first priority* (at least
:data:`MIN_FIRST_PRIORITY`), *backup*, or neither. An alert goes to every
phone of the first-priority people at once. When nobody acknowledges it within
``escalate_after_min`` minutes, every phone of the backup people gets it as
well, marked as not acknowledged. If no first-priority phone accepted the
alert at all, it escalates straight away. Until a roster is saved, alerts go to
every active owner and administrator (no escalation), so a new store is never
silent.

**What is pushed** (from :meth:`AlertDispatcher.dispatch`):

* loss-prevention incidents with confidence >= ``min_confidence`` (default
  0.75, the HIGH severity band). Lower-confidence incidents are still recorded
  and shown on the dashboard; they just do not wake anyone;
* night-watch intrusions and area / line alerts, each switchable;
* "staff sent" messages to the first-priority phones (never escalated).

Camera mutes apply, and the same alert type on the same camera within
``CAMERA_ALERT_COOLDOWN_SEC`` is held back (escalation is never held back).

**Acknowledgement**: in the dashboard (incident or alert "Acknowledge"), or
the notification's own Acknowledge button. Each pushed message carries a
token bound to that alert and that phone (HMAC with this box's JWT secret), so
the button works without a live session. Once acknowledged, the phones that
got the alert see it replaced by "Acknowledged by <name>".
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from app.config import settings
from app.services import web_push

logger = logging.getLogger("edge.push_alerts")

ROSTER_KEY = "push_alert_roster"
MIN_FIRST_PRIORITY = 2
THEFT_TYPES = frozenset({"THEFT_SUSPECTED", "CONCEALMENT", "SHELF_SWEEP", "EXIT_WITHOUT_CHECKOUT", "LOITERING"})
AREA_TYPES = frozenset({"RESTRICTED_AREA", "TRIPWIRE_ALERT"})
NIGHT_TYPES = frozenset({"NIGHT_INTRUSION"})
TICK_S = 15.0
OPEN_FOR = timedelta(hours=24)
# An acknowledgement later than this updates the record but sends no notice.
HANDLED_NOTICE_WITHIN = timedelta(hours=6)
ALERT_TTL_S = 4 * 3600

DEFAULT_ROSTER: Dict[str, Any] = {
    "first_priority": [],
    "backup": [],
    "escalate_after_min": 5,
    "min_confidence": 0.75,
    "night_intrusion": True,
    "area_alerts": True,
    "watchdog_enabled": True,
    "watchdog_offline_min": 15,
}
LIMITS = {
    "escalate_after_min": (1, 60),
    "min_confidence": (0.3, 0.99),
    "watchdog_offline_min": (5, 240),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- roster

def _db_path() -> Path:
    return Path(settings.DATABASE_PATH)


def load_roster() -> Dict[str, Any]:
    """The saved roster merged over the defaults, plus ``configured``."""
    out = json.loads(json.dumps(DEFAULT_ROSTER))
    saved: Optional[dict] = None
    path = _db_path()
    if path.exists():
        try:
            conn = sqlite3.connect(str(path), timeout=5.0)
            try:
                row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (ROSTER_KEY,)).fetchone()
            finally:
                conn.close()
            if row:
                saved = json.loads(row[0])
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("Phone alert roster not read (%s); using defaults", exc)
    if isinstance(saved, dict):
        for key in DEFAULT_ROSTER:
            if key in saved:
                out[key] = saved[key]
        for key in ("updated_at", "updated_by"):
            if saved.get(key):
                out[key] = saved[key]
    out["configured"] = len(out.get("first_priority") or []) >= MIN_FIRST_PRIORITY
    return out


def validate_roster(data: Dict[str, Any], accounts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise a roster from the dashboard; raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("The phone alert settings must be an object.")
    out = json.loads(json.dumps(DEFAULT_ROSTER))
    for tier in ("first_priority", "backup"):
        ids = data.get(tier) or []
        if not isinstance(ids, list):
            raise ValueError(f"{tier} must be a list of account ids.")
        clean: List[str] = []
        for uid in ids:
            uid = str(uid)
            acct = accounts.get(uid)
            if acct is None:
                raise ValueError("One of the chosen people no longer has an account. Reload the page.")
            if not acct.get("is_active"):
                raise ValueError(f"{acct.get('display_name') or acct.get('username')} is switched off; "
                                 "switch the account on first or choose someone else.")
            if uid not in clean:
                clean.append(uid)
        out[tier] = clean
    if len(out["first_priority"]) < MIN_FIRST_PRIORITY:
        raise ValueError(f"Choose at least {MIN_FIRST_PRIORITY} first-priority people, so an alert never "
                         "depends on one phone. Add accounts under Accounts if needed.")
    both = set(out["first_priority"]) & set(out["backup"])
    if both:
        raise ValueError("A person can be first priority or backup, not both.")
    for key, (lo, hi) in LIMITS.items():
        if key in data and data[key] is not None:
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number.")
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}.")
            out[key] = round(value, 2) if key == "min_confidence" else int(round(value))
    for key in ("night_intrusion", "area_alerts", "watchdog_enabled"):
        if key in data:
            out[key] = bool(data[key])
    return out


def save_roster(roster: Dict[str, Any], updated_by: Optional[str]) -> Dict[str, Any]:
    stored = {k: roster[k] for k in DEFAULT_ROSTER}
    stored["updated_at"] = _utcnow().isoformat() + "Z"
    stored["updated_by"] = updated_by
    payload = json.dumps(stored)
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    try:
        conn.execute(
            "INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (ROSTER_KEY, payload, _utcnow().isoformat(sep=" ")),
        )
        conn.commit()
    finally:
        conn.close()
    return load_roster()


async def load_accounts() -> Dict[str, Dict[str, Any]]:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.db_models import AdminUserModel

    async with async_session_factory() as session:
        rows = (await session.execute(select(AdminUserModel))).scalars().all()
    return {
        r.id: {"id": r.id, "username": r.username, "display_name": r.display_name or r.username,
               "role": r.role, "is_active": r.is_active is None or bool(r.is_active)}
        for r in rows
    }


def resolve_tiers(roster: Dict[str, Any], accounts: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str], str]:
    """(first-priority user ids, backup user ids, "roster" | "fallback"), active accounts only."""
    from app.services.auth_service import ADMIN_ROLES

    if roster.get("configured"):
        first = [u for u in roster["first_priority"] if accounts.get(u, {}).get("is_active")]
        backup = [u for u in roster["backup"] if accounts.get(u, {}).get("is_active") and u not in first]
        return first, backup, "roster"
    first = [a["id"] for a in accounts.values() if a["is_active"] and a["role"] in ADMIN_ROLES]
    return first, [], "fallback"


# --------------------------------------------------------------------------- subscriptions

def ack_token(alert_id: str, subscription_id: str) -> str:
    key = (settings.JWT_SECRET or "").encode("utf-8")
    msg = f"push-ack:{alert_id}:{subscription_id}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def check_ack_token(alert_id: str, subscription_id: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(ack_token(alert_id, subscription_id), str(token))


def guess_platform(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "ios"
    if "android" in ua:
        return "android"
    if any(k in ua for k in ("windows", "macintosh", "x11", "cros", "linux")):
        return "desktop"
    return "other"


async def subscriptions_for(user_ids: Iterable[str]) -> List[Any]:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.db_models import WebPushSubscriptionModel

    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return []
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(WebPushSubscriptionModel)
            .where(WebPushSubscriptionModel.user_id.in_(ids),
                   WebPushSubscriptionModel.vapid_key_id == web_push.vapid_key_id())
            .order_by(WebPushSubscriptionModel.created_at)
        )).scalars().all()
    return list(rows)


async def recipient_subscriptions() -> List[Any]:
    """Every phone that gets alerts now (both tiers), for the offline watchdog."""
    accounts = await load_accounts()
    first, backup, _mode = resolve_tiers(load_roster(), accounts)
    return await subscriptions_for(first + backup)


# --------------------------------------------------------------------------- service

class PushAlertService:
    RING_SIZE = 60

    def __init__(self) -> None:
        self._last_push: Dict[tuple, float] = {}
        self.recent: Deque[dict] = collections.deque(maxlen=self.RING_SIZE)
        self._task: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None

    def reset(self) -> None:
        self._last_push.clear()
        self.recent.clear()

    def _tick_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # ---- which alerts
    @staticmethod
    def qualifies(data: Dict[str, Any], roster: Dict[str, Any]) -> Tuple[bool, str, bool]:
        """(push it?, reason when not, track it for escalation?)."""
        et = str(data.get("event_type") or "")
        if data.get("kind") == "staff_dispatch":
            return True, "", False
        if et in THEFT_TYPES:
            conf = data.get("confidence")
            if not isinstance(conf, (int, float)) or isinstance(conf, bool):
                return False, "no measured confidence", False
            if float(conf) < float(roster["min_confidence"]):
                return False, f"confidence {float(conf):.2f} below the phone alert level {roster['min_confidence']:.2f}", False
            return True, "", True
        if et in NIGHT_TYPES:
            if not roster.get("night_intrusion"):
                return False, "night intrusion alerts are off for phones", False
            return True, "", True
        if et in AREA_TYPES:
            if not roster.get("area_alerts"):
                return False, "area and line alerts are off for phones", False
            return True, "", True
        return False, f"{et or 'this alert type'} is not sent to phones", False

    # ---- dispatch hook
    async def on_alert(self, title: str, body: str, data: Dict[str, Any], camera=None,
                       bypass_cooldown: bool = False) -> Dict[str, Any]:
        report: Dict[str, Any] = {"provider": "web_push", "pushed": False, "skipped": None,
                                  "tier": 1, "sent": 0, "failed": 0, "gone": 0, "phones": 0}
        roster = load_roster()
        ok, reason, track = self.qualifies(data, roster)
        if not ok:
            report["skipped"] = reason
            return report
        if camera is not None and camera.muted_until and camera.muted_until > datetime.utcnow():
            report["skipped"] = f"camera alerts muted until {camera.muted_until.isoformat()}Z"
            return report
        key = (data.get("camera_id"), data.get("event_type"))
        now_m = time.monotonic()
        cooldown = float(settings.CAMERA_ALERT_COOLDOWN_SEC)
        last = self._last_push.get(key)
        if not bypass_cooldown and data.get("kind") != "staff_dispatch" and last is not None \
                and now_m - last < cooldown:
            report["skipped"] = f"cooldown ({cooldown:.0f}s per camera and alert type)"
            return report

        accounts = await load_accounts()
        first, backup, mode = resolve_tiers(roster, accounts)
        report["mode"] = mode
        subs = await subscriptions_for(first)
        report["phones"] = len(subs)
        self._last_push[key] = now_m
        alert_id = str(data.get("alert_id") or f"evt_{uuid.uuid4().hex[:8]}")
        base = self._payload("alert", title, body, data, alert_id)
        results = await self._send_all(subs, base, alert_id, data, tier=1)
        accepted = [r["subscription_id"] for r in results if r["status"] == "sent"]
        for r in results:
            report[r["status"]] = report.get(r["status"], 0) + 1
        report["pushed"] = bool(accepted)

        now = _utcnow()
        escalate_at = None
        if track and backup:
            delay = timedelta(minutes=int(roster["escalate_after_min"]))
            escalate_at = now if not accepted else now + delay
        report["escalate_at"] = escalate_at.isoformat() + "Z" if escalate_at else None
        if not accepted:
            logger.warning("Alert %s reached no first-priority phone (%d phones; %s)%s", alert_id, len(subs),
                           mode, "; escalating to backup now" if escalate_at else "")
        await self._record_alert(alert_id, title, body, data, base, now, escalate_at, accepted,
                                 closed=None if track else now, report=report)
        return report

    # ---- payloads
    @staticmethod
    def _payload(kind: str, title: str, body: str, data: Dict[str, Any], alert_id: str) -> Dict[str, Any]:
        incident_id = data.get("incident_id")
        url = f"/dashboard?incident={incident_id}" if incident_id else f"/dashboard?alert={alert_id}"
        return {
            "v": 1, "kind": kind, "title": str(title)[:120], "body": str(body)[:600],
            "tag": f"alert-{incident_id or alert_id}", "url": url,
            "alert_id": alert_id, "incident_id": incident_id,
            "severity": data.get("severity"), "event_type": data.get("event_type"),
            "camera": data.get("camera_name") or data.get("camera_id"),
            "ts": int(time.time()),
        }

    async def _send_all(self, subs: List[Any], base: Dict[str, Any], alert_id: str, data: Dict[str, Any],
                        tier: int, ack: bool = True) -> List[Dict[str, Any]]:
        async def one(sub) -> Dict[str, Any]:
            payload = dict(base)
            if ack:
                payload["ack"] = {"sid": sub.id, "token": ack_token(alert_id, sub.id)}
            urgency = "high" if base["kind"] in ("alert", "escalation", "offline") else "normal"
            try:
                outcome = await web_push.send({"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth},
                                              payload, ttl=ALERT_TTL_S, urgency=urgency)
            except Exception as exc:  # one phone must never break the fan-out
                logger.exception("web push to %s failed", sub.id)
                outcome = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            entry = {"subscription_id": sub.id, "user_id": sub.user_id, "label": sub.label,
                     "platform": sub.platform, **outcome}
            await self._note_result(sub.id, outcome)
            self.recent.appendleft({
                "at": _utcnow().isoformat() + "Z", "alert_id": alert_id, "kind": base["kind"], "tier": tier,
                "event_type": data.get("event_type"), "title": base["title"],
                **{k: entry.get(k) for k in ("subscription_id", "user_id", "label", "status", "error")},
            })
            level = logging.INFO if outcome["status"] == "sent" else logging.WARNING
            logger.log(level, "web push %s %s -> %s (%s): %s %s", base["kind"], alert_id, sub.label, sub.id,
                       outcome["status"], outcome.get("error") or "")
            return entry

        if not subs:
            return []
        return list(await asyncio.gather(*(one(s) for s in subs)))

    async def _note_result(self, sid: str, outcome: Dict[str, Any]) -> None:
        from sqlalchemy import delete, update

        from app.database import async_session_factory
        from app.models.db_models import WebPushSubscriptionModel

        try:
            async with async_session_factory() as session:
                if outcome["status"] == "gone":
                    await session.execute(delete(WebPushSubscriptionModel).where(WebPushSubscriptionModel.id == sid))
                    logger.warning("Push service says subscription %s is gone; removed it", sid)
                else:
                    await session.execute(update(WebPushSubscriptionModel).where(WebPushSubscriptionModel.id == sid)
                                          .values(last_push_at=_utcnow(), last_push_status=outcome["status"],
                                                  last_push_error=(outcome.get("error") or None)
                                                  and str(outcome["error"])[:500]))
                await session.commit()
        except Exception as exc:
            logger.debug("could not record push result for %s: %s", sid, exc)

    async def _record_alert(self, alert_id, title, body, data, payload, now, escalate_at, sent_to,
                            closed, report) -> None:
        from fastapi.encoders import jsonable_encoder

        from app.database import async_session_factory
        from app.models.db_models import PushAlertModel

        try:
            async with async_session_factory() as session:
                row = await session.get(PushAlertModel, alert_id)
                if row is None:
                    row = PushAlertModel(alert_id=alert_id)
                    session.add(row)
                row.incident_id = data.get("incident_id")
                row.event_type = str(data.get("event_type") or "")
                row.severity = str(data.get("severity") or "")
                row.camera_id = data.get("camera_id")
                row.title, row.body = str(title)[:256], str(body)[:1024]
                row.payload = payload
                row.created_at = now
                row.escalate_at = escalate_at
                row.sent_to = sent_to
                row.closed_at = closed
                row.report = jsonable_encoder({k: v for k, v in report.items() if k != "results"})
                await session.commit()
        except Exception as exc:
            logger.error("Could not record pushed alert %s: %s", alert_id, exc)

    # ---- acknowledgement
    async def _ack_state(self, session, row) -> Tuple[bool, Optional[str]]:
        from app.models.db_models import SecurityEventModel, TheftIncidentModel

        if row.incident_id:
            inc = await session.get(TheftIncidentModel, row.incident_id)
            if inc is not None and (inc.status or "ACTIVE") != "ACTIVE":
                return True, inc.guard_id or inc.dispatched_by or inc.resolved_by
        ev = await session.get(SecurityEventModel, row.alert_id)
        if ev is not None and ev.acknowledged:
            who = (ev.metadata_json or {}).get("acknowledged_by")
            return True, who.get("name") if isinstance(who, dict) else None
        return False, None

    async def acknowledge_from_push(self, alert_id: str, sid: str, token: str) -> Dict[str, Any]:
        """The notification's Acknowledge button. Raises PermissionError / LookupError."""
        from app.database import async_session_factory
        from app.models.db_models import (AdminUserModel, PushAlertModel, SecurityEventModel,
                                          WebPushSubscriptionModel)
        from app.services.theft_detection_service import theft_detection_service

        if not check_ack_token(alert_id, sid, token):
            raise PermissionError("This acknowledge link is not valid.")
        async with async_session_factory() as session:
            row = await session.get(PushAlertModel, alert_id)
            if row is None:
                raise LookupError("This alert is no longer on the box.")
            sub = await session.get(WebPushSubscriptionModel, sid)
            user = await session.get(AdminUserModel, sub.user_id) if sub is not None else None
            if user is None or user.is_active is False:
                raise PermissionError("This phone is no longer allowed to acknowledge alerts.")
            name = user.display_name or user.username
            done, by = await self._ack_state(session, row)
            if not done:
                if row.incident_id:
                    await theft_detection_service.acknowledge_incident(
                        incident_id=row.incident_id, guard_id=f"{name} (phone)", db=session)
                ev = await session.get(SecurityEventModel, row.alert_id)
                if ev is not None and not ev.acknowledged:
                    ev.acknowledged = True
                    ev.acknowledged_at = _utcnow()
                    ev.metadata_json = {**(ev.metadata_json or {}),
                                        "acknowledged_by": {"user_id": user.id, "name": name, "via": "phone"}}
                    await session.commit()
                by = name
        logger.info("Alert %s acknowledged from a phone notification by %s", alert_id, by)
        await self.tick(only=alert_id)
        return {"alert_id": alert_id, "acknowledged_by": by}

    # ---- escalation worker
    async def tick(self, only: Optional[str] = None) -> Dict[str, int]:
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.db_models import PushAlertModel

        counts = {"open": 0, "acknowledged": 0, "escalated": 0, "expired": 0}
        async with self._tick_lock():
            now = _utcnow()
            async with async_session_factory() as session:
                q = select(PushAlertModel).where(PushAlertModel.closed_at.is_(None))
                if only:
                    q = q.where(PushAlertModel.alert_id == only)
                rows = (await session.execute(q)).scalars().all()
                todo = []
                for row in rows:
                    counts["open"] += 1
                    if row.created_at and now - row.created_at > OPEN_FOR:
                        row.closed_at = now
                        counts["expired"] += 1
                        continue
                    done, by = await self._ack_state(session, row)
                    if done:
                        row.acknowledged_at, row.acknowledged_by = now, (by or "someone")[:128]
                        row.closed_at = now
                        counts["acknowledged"] += 1
                        if row.sent_to and row.created_at and now - row.created_at <= HANDLED_NOTICE_WITHIN:
                            todo.append(("handled", row.alert_id, dict(row.payload or {}), list(row.sent_to),
                                         row.acknowledged_by))
                    elif row.escalate_at is not None and row.escalated_at is None and row.escalate_at <= now:
                        row.escalated_at = now
                        counts["escalated"] += 1
                        todo.append(("escalate", row.alert_id, dict(row.payload or {}), list(row.sent_to or []),
                                     row.created_at))
                await session.commit()
            for kind, alert_id, payload, sent_to, extra in todo:
                try:
                    if kind == "handled":
                        await self._send_handled(alert_id, payload, sent_to, extra)
                    else:
                        await self._escalate(alert_id, payload, sent_to, extra)
                except Exception:
                    logger.exception("push alert follow-up (%s) for %s failed", kind, alert_id)
        return counts

    async def _escalate(self, alert_id: str, payload: Dict[str, Any], already: List[str],
                        created_at: Optional[datetime]) -> None:
        from app.database import async_session_factory
        from app.models.db_models import PushAlertModel

        roster = load_roster()
        accounts = await load_accounts()
        _first, backup, _mode = resolve_tiers(roster, accounts)
        subs = [s for s in await subscriptions_for(backup) if s.id not in set(already)]
        minutes = max(0, int(((_utcnow() - created_at).total_seconds() if created_at else 0) // 60))
        base = dict(payload, kind="escalation", ts=int(time.time()))
        base["title"] = ("Not acknowledged: " + str(payload.get("title") or "alert"))[:120]
        lead = (f"Nobody acknowledged this in {minutes} min. " if minutes else
                "No first-priority phone could be reached. ")
        base["body"] = (lead + str(payload.get("body") or ""))[:600]
        results = await self._send_all(subs, base, alert_id, {"event_type": payload.get("event_type")}, tier=2)
        accepted = [r["subscription_id"] for r in results if r["status"] == "sent"]
        logger.warning("Alert %s escalated to %d backup phone(s), %d accepted", alert_id, len(subs), len(accepted))
        async with async_session_factory() as session:
            row = await session.get(PushAlertModel, alert_id)
            if row is not None:
                row.sent_to = list(dict.fromkeys(list(row.sent_to or []) + accepted))
                row.report = {**(row.report or {}), "escalation": {
                    "phones": len(subs), "sent": len(accepted), "at": _utcnow().isoformat() + "Z"}}
                await session.commit()

    async def _send_handled(self, alert_id: str, payload: Dict[str, Any], sent_to: List[str],
                            by: Optional[str]) -> None:
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.db_models import WebPushSubscriptionModel

        async with async_session_factory() as session:
            subs = (await session.execute(select(WebPushSubscriptionModel)
                                          .where(WebPushSubscriptionModel.id.in_(sent_to)))).scalars().all()
        base = dict(payload, kind="handled", ts=int(time.time()))
        base["title"] = ("Acknowledged: " + str(payload.get("title") or "alert"))[:120]
        base["body"] = f"{by or 'Someone'} has acknowledged this alert."
        await self._send_all(list(subs), base, alert_id, {"event_type": payload.get("event_type")},
                             tier=0, ack=False)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_S)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("push alert escalation check failed")

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="push-alert-escalation")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- single messages
    async def send_test(self, sub) -> Dict[str, Any]:
        from app.services import device_identity

        ident = device_identity.get_identity()
        alert_id = f"test_{uuid.uuid4().hex[:8]}"
        base = {"v": 1, "kind": "test", "title": f"Test alert - {ident['device_name']}",
                "body": "Alerts from the store's CCTV reach this phone.", "tag": alert_id,
                "url": "/dashboard", "alert_id": alert_id, "ts": int(time.time())}
        results = await self._send_all([sub], base, alert_id, {"event_type": "TEST"}, tier=0, ack=False)
        return {"alert_id": alert_id, **(results[0] if results else {"status": "failed"})}

    async def send_notice(self, kind: str, title: str, body: str, subs: List[Any], tag: str) -> Dict[str, int]:
        """A plain message (e.g. "back online") to the given phones."""
        alert_id = f"{kind}_{uuid.uuid4().hex[:8]}"
        base = {"v": 1, "kind": kind, "title": title[:120], "body": body[:600], "tag": tag,
                "url": "/dashboard", "alert_id": alert_id, "ts": int(time.time())}
        results = await self._send_all(subs, base, alert_id, {"event_type": kind.upper()}, tier=0, ack=False)
        return {"phones": len(subs), "sent": sum(1 for r in results if r["status"] == "sent")}


push_alerts = PushAlertService()
