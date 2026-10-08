"""Installed web app alerts (Web Push): phones, roster, acknowledgement.

GET    /api/v1/web-push/config                    VAPID public key, roster summary, this person's phones (signed in)
POST   /api/v1/web-push/subscriptions             "Enable alerts on this phone": register / move this browser to me
POST   /api/v1/web-push/subscriptions/unsubscribe {"endpoint"}: this browser stops getting alerts (its owner)
GET    /api/v1/web-push/subscriptions             connected phones: all (owner/admin) or my own (operator)
DELETE /api/v1/web-push/subscriptions/{id}        remove a phone (owner/admin, or the phone's own person)
POST   /api/v1/web-push/subscriptions/{id}/test   send a test alert (owner/admin, or the phone's own person)
GET    /api/v1/web-push/roster                    who gets alerts, escalation, threshold, watchdog (signed in)
PUT    /api/v1/web-push/roster                    save it (owner/admin); at least 2 first-priority people
GET    /api/v1/web-push/deliveries                the last pushes and their outcome (owner/admin)
POST   /api/v1/web-push/watchdog/upload           upload the store-offline bundle now (owner/admin)
POST   /api/v1/web-push/ack                       a notification's Acknowledge button (no session:
                                              the per-phone token in the push is the credential)

Web Push needs a secure page: it works on the public https address (and
localhost), not on the plain-http LAN or Tailscale address.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import WebPushSubscriptionModel
from app.services import push_alerts as pa
from app.services import web_push
from app.services.auth_service import (auth_service, effective_role, general_rate_limiter, is_admin_role,
                                       operator_allowed, require_admin)

logger = logging.getLogger("edge.push_alerts")

router = APIRouter(prefix="/api/v1/web-push", tags=["Phone alerts"],
                   dependencies=[Depends(auth_service.verify_api_access)])
# No session: a phone notification's button. Rate limited like other public routes.
ack_router = APIRouter(prefix="/api/v1/web-push", tags=["Phone alerts"],
                       dependencies=[Depends(general_rate_limiter)])


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(..., max_length=200)
    auth: str = Field(..., max_length=100)


class SubscribeRequest(BaseModel):
    endpoint: str = Field(..., max_length=2048)
    keys: SubscriptionKeys
    label: Optional[str] = Field(None, max_length=80, description="e.g. 'Sam's iPhone'")
    platform: Optional[str] = Field(None, max_length=16)


class EndpointRequest(BaseModel):
    endpoint: str = Field(..., max_length=2048)


class AckRequest(BaseModel):
    alert_id: str = Field(..., max_length=64)
    sid: str = Field(..., max_length=64)
    token: str = Field(..., max_length=64)


def _caller(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or user.get("type") != "user_session" or not user.get("sub"):
        raise HTTPException(status_code=403, detail="Sign in with your own account to manage phone alerts.")
    return user


def _is_admin(user: Dict[str, Any]) -> bool:
    return is_admin_role(effective_role(user))


def _sub_out(row: WebPushSubscriptionModel, accounts: Dict[str, Dict[str, Any]], current_key: str,
             caller: Optional[str] = None) -> dict:
    """One phone for the dashboard. The endpoint is included for the caller's own
    phones only, so a browser can recognise itself ("this device")."""
    acct = accounts.get(row.user_id) or {}
    return {
        "id": row.id,
        "user_id": row.user_id,
        "person": acct.get("display_name") or acct.get("username") or "(removed account)",
        "label": row.label,
        "platform": row.platform,
        "created_at": row.created_at,
        "last_seen_at": row.last_seen_at,
        "last_push_at": row.last_push_at,
        "last_push_status": row.last_push_status,
        "last_push_error": row.last_push_error,
        "usable": row.vapid_key_id == current_key and bool(acct.get("is_active")),
        "endpoint": row.endpoint if caller and caller == row.user_id else None,
    }


def _default_label(platform: str, ua: str) -> str:
    browser = "Safari" if "safari" in ua and "chrome" not in ua and "crios" not in ua else \
        "Edge" if "edg" in ua else "Firefox" if "firefox" in ua or "fxios" in ua else "Chrome"
    device = {"ios": "iPhone" if "iphone" in ua else "iPad", "android": "Android phone",
              "desktop": "Computer"}.get(platform, "Device")
    return f"{device} · {browser}"


# --------------------------------------------------------------------------- phones

@router.get("/config")
async def push_config(request: Request, db: AsyncSession = Depends(get_db)):
    user = getattr(request.state, "user", None) or {}
    roster = pa.load_roster()
    mine: List[dict] = []
    if user.get("sub"):
        accounts = await pa.load_accounts()
        rows = (await db.execute(select(WebPushSubscriptionModel)
                                 .where(WebPushSubscriptionModel.user_id == str(user["sub"])))).scalars().all()
        key = web_push.vapid_key_id()
        mine = [_sub_out(r, accounts, key, str(user["sub"])) for r in rows]
    tier = None
    if user.get("sub"):
        sub = str(user["sub"])
        tier = "first_priority" if sub in roster["first_priority"] else "backup" if sub in roster["backup"] else None
    return {
        "vapid_public_key": web_push.vapid_public_key(),
        "roster_configured": roster["configured"],
        "min_confidence": roster["min_confidence"],
        "my_tier": tier,
        "my_subscriptions": mine,
    }


@router.post("/subscriptions")
@operator_allowed
async def subscribe(body: SubscribeRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Register this browser for alerts, bound to the signed-in person.

    The same browser signing in as someone else moves to that person; enabling
    again just refreshes it.
    """
    user = _caller(request)
    try:
        clean = web_push.validate_subscription(body.endpoint, body.keys.p256dh, body.keys.auth)
    except web_push.WebPushError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    ua = (request.headers.get("user-agent") or "")[:256]
    platform = body.platform if body.platform in ("android", "ios", "desktop", "other") else pa.guess_platform(ua)
    now = datetime.utcnow()
    row = (await db.execute(select(WebPushSubscriptionModel)
                            .where(WebPushSubscriptionModel.endpoint == clean["endpoint"]))).scalar_one_or_none()
    # A re-sync without a name keeps the name given when alerts were enabled.
    label = (body.label or "").strip() or (row.label if row is not None else "") or _default_label(platform, ua.lower())
    created = row is None
    if row is None:
        row = WebPushSubscriptionModel(id=f"wp_{uuid.uuid4().hex[:12]}", endpoint=clean["endpoint"],
                                       created_at=now)
        db.add(row)
    moved = (not created) and row.user_id != str(user["sub"])
    row.user_id = str(user["sub"])
    row.p256dh, row.auth = clean["p256dh"], clean["auth"]
    row.label, row.platform, row.user_agent = label[:128], platform, ua or None
    row.vapid_key_id = web_push.vapid_key_id()
    row.last_seen_at = now
    await db.commit()
    if created or moved:
        logger.info("Phone alerts %s: %s (%s) for account %s", "enabled" if created else "moved", row.id,
                    row.label, row.user_id)
        _poke_watchdog()
    return {"id": row.id, "label": row.label, "platform": row.platform, "created": created}


@router.post("/subscriptions/unsubscribe")
@operator_allowed
async def unsubscribe(body: EndpointRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = _caller(request)
    row = (await db.execute(select(WebPushSubscriptionModel)
                            .where(WebPushSubscriptionModel.endpoint == body.endpoint))).scalar_one_or_none()
    if row is None:
        return {"removed": False}
    if row.user_id != str(user["sub"]) and not _is_admin(user):
        raise HTTPException(status_code=403, detail="This phone belongs to someone else's account.")
    await db.delete(row)
    await db.commit()
    _poke_watchdog()
    return {"removed": True, "id": row.id}


@router.get("/subscriptions")
async def list_subscriptions(request: Request, db: AsyncSession = Depends(get_db)):
    user = _caller(request)
    q = select(WebPushSubscriptionModel).order_by(WebPushSubscriptionModel.created_at)
    if not _is_admin(user):
        q = q.where(WebPushSubscriptionModel.user_id == str(user["sub"]))
    rows = (await db.execute(q)).scalars().all()
    accounts = await pa.load_accounts()
    key = web_push.vapid_key_id()
    return {"subscriptions": [_sub_out(r, accounts, key, str(user["sub"])) for r in rows]}


async def _owned(sid: str, request: Request, db: AsyncSession) -> WebPushSubscriptionModel:
    user = _caller(request)
    row = await db.get(WebPushSubscriptionModel, sid)
    if row is None:
        raise HTTPException(status_code=404, detail="That phone is not registered any more.")
    if row.user_id != str(user["sub"]) and not _is_admin(user):
        raise HTTPException(status_code=403, detail="Only an administrator can manage someone else's phone.")
    return row


@router.delete("/subscriptions/{sid}")
@operator_allowed
async def remove_subscription(sid: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _owned(sid, request, db)
    await db.delete(row)
    await db.commit()
    logger.info("Phone alerts removed: %s (%s)", sid, row.label)
    _poke_watchdog()
    return {"removed": True, "id": sid}


@router.post("/subscriptions/{sid}/test")
@operator_allowed
async def test_subscription(sid: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _owned(sid, request, db)
    if row.vapid_key_id != web_push.vapid_key_id():
        raise HTTPException(status_code=409, detail="This phone was set up with an older key of this box. "
                                                    "Open the app on it and tap Enable alerts again.")
    result = await pa.push_alerts.send_test(row)
    return result


# --------------------------------------------------------------------------- roster

def _roster_out(roster: Dict[str, Any], accounts: Dict[str, Dict[str, Any]],
                subs: List[WebPushSubscriptionModel]) -> dict:
    key = web_push.vapid_key_id()
    counts: Dict[str, int] = {}
    for s in subs:
        if s.vapid_key_id == key:
            counts[s.user_id] = counts.get(s.user_id, 0) + 1
    first, backup, mode = pa.resolve_tiers(roster, accounts)
    people = []
    for a in sorted(accounts.values(), key=lambda a: (a["display_name"] or a["username"]).lower()):
        tier = "first_priority" if a["id"] in roster["first_priority"] else \
            "backup" if a["id"] in roster["backup"] else "none"
        people.append({**a, "tier": tier, "phones": counts.get(a["id"], 0),
                       "receives_now": a["id"] in first or a["id"] in backup})
    warnings = []
    if not roster["configured"]:
        warnings.append(f"Not set up yet: until at least {pa.MIN_FIRST_PRIORITY} first-priority people are "
                        "saved, alerts go to every owner and administrator, with no escalation.")
    no_phone = [p["display_name"] for p in people if p["tier"] == "first_priority" and not p["phones"]]
    if no_phone:
        warnings.append("No phone with alerts on yet: " + ", ".join(no_phone) + ". Each person opens the "
                        "app on their phone and taps Enable alerts.")
    if roster["configured"] and not roster["backup"]:
        warnings.append("No backup people: an alert nobody acknowledges is not escalated.")
    return {
        **{k: roster[k] for k in pa.DEFAULT_ROSTER},
        "configured": roster["configured"],
        "mode": mode,
        "updated_at": roster.get("updated_at"),
        "updated_by": roster.get("updated_by"),
        "min_first_priority": pa.MIN_FIRST_PRIORITY,
        "limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in pa.LIMITS.items()},
        "people": people,
        "warnings": warnings,
    }


@router.get("/roster")
async def get_roster(db: AsyncSession = Depends(get_db)):
    accounts = await pa.load_accounts()
    subs = (await db.execute(select(WebPushSubscriptionModel))).scalars().all()
    out = _roster_out(pa.load_roster(), accounts, list(subs))
    from app.services.offline_watchdog import offline_watchdog

    out["watchdog"] = offline_watchdog.status()
    return out


@router.put("/roster")
async def put_roster(body: Dict[str, Any], db: AsyncSession = Depends(get_db),
                     user: Dict[str, Any] = Depends(require_admin)):
    accounts = await pa.load_accounts()
    try:
        clean = pa.validate_roster(body, accounts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    who = None
    if user.get("sub"):
        acct = accounts.get(str(user["sub"])) or {}
        who = acct.get("username") or str(user["sub"])
    saved = pa.save_roster(clean, who)
    logger.info("Phone alert roster saved by %s: %d first priority, %d backup, escalate after %d min, "
                "level %.2f, watchdog %s/%d min", who, len(saved["first_priority"]), len(saved["backup"]),
                saved["escalate_after_min"], saved["min_confidence"],
                "on" if saved["watchdog_enabled"] else "off", saved["watchdog_offline_min"])
    _poke_watchdog()
    subs = (await db.execute(select(WebPushSubscriptionModel))).scalars().all()
    return _roster_out(saved, accounts, list(subs))


@router.get("/deliveries", dependencies=[Depends(require_admin)])
async def deliveries():
    return {"deliveries": list(pa.push_alerts.recent)}


@router.post("/watchdog/upload", dependencies=[Depends(require_admin)])
async def watchdog_upload():
    from app.services.offline_watchdog import offline_watchdog

    return await offline_watchdog.upload_now()


def _poke_watchdog() -> None:
    try:
        from app.services.offline_watchdog import offline_watchdog

        offline_watchdog.poke()
    except Exception as exc:
        logger.debug("watchdog poke failed: %s", exc)


# --------------------------------------------------------------------------- acknowledgement

@ack_router.post("/ack")
async def acknowledge_from_notification(body: AckRequest):
    try:
        return await pa.push_alerts.acknowledge_from_push(body.alert_id, body.sid, body.token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
