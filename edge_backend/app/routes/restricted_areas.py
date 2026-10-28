"""Restricted areas API (high security): areas, arming, announcements, events, audit.

Everything is under ``/api/v1/restricted-areas`` and gated by the iKorex plan
feature ``restricted_areas`` (403 ``feature_disabled``, FleetGateMiddleware).

Reads: any signed-in person. Writes by role (auth_service, enforced centrally):

* **operators** may announce entry ("I'm going in"), end their own
  announcement ("I'm out"), arm an area (raise security) and record the outcome
  of an alert;
* **owners / administrators** also create, change and delete areas, disarm or
  return an area to its schedule, send a test alert and forget learnt still
  figures; they may end anyone's announcement.

Every write is audited in ``restricted_area_audit`` (who, role, when, what).
``GET /status`` is the one cheap poll for the dashboard (memory + two small
queries); instant alerts arrive on the existing ``/api/v1/events/ws`` feed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.db_models import (AdminUserModel, CameraModel, RestrictedAreaAnnouncementModel,
                                  RestrictedAreaAuditModel, RestrictedAreaModel, SecurityEventModel,
                                  StoreZoneModel)
from app.routes import ResilientRoute
from app.services import restricted_areas as ra
from app.services.auth_service import (auth_service, effective_role, general_rate_limiter, is_admin_role,
                                       operator_allowed, require_admin)
from app.services.restricted_area_engine import restricted_area_engine
from app.services.restricted_areas import AreaError, restricted_areas
from app.services.timeutil import to_local

logger = logging.getLogger("edge.routes.restricted_areas")

router = APIRouter(
    prefix="/api/v1/restricted-areas",
    tags=["Restricted areas"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)
# The dashboard's one cheap poll (every few seconds on every open dashboard and
# phone, often several behind one address such as the online-access tunnel):
# signed in, but not counted against the general request budget. It reads
# memory plus one small query. Included before ``router`` (``/{area_id}``).
status_router = APIRouter(
    prefix="/api/v1/restricted-areas",
    tags=["Restricted areas"],
    dependencies=[Depends(auth_service.verify_api_access)],
    route_class=ResilientRoute,
)


# --------------------------------------------------------------------------- helpers

def _signed_in(request: Request) -> None:
    user = getattr(request.state, "user", None)
    if user and user.get("type") != "user_session":
        raise HTTPException(status_code=403, detail="Sign in to see restricted areas")


async def _actor(request: Request, db: AsyncSession) -> Dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        return {"id": None, "name": "Service", "role": "owner"}
    uid = user.get("sub")
    name = None
    if uid:
        try:
            acct = await db.get(AdminUserModel, str(uid))
            if acct is not None:
                name = acct.display_name or acct.username
        except Exception:  # noqa: BLE001
            pass
    return {"id": str(uid) if uid else None, "name": name or (str(uid) if uid else "Someone"),
            "role": effective_role(user)}


def _err(e: AreaError):
    raise HTTPException(status_code=e.status, detail=str(e))


def _store_time(ts: Optional[float]) -> Optional[dict]:
    if ts is None:
        return None
    local = to_local(ra.dt_of(ts))
    return {"iso": ra.iso(ts), "hhmm": local.strftime("%H:%M"), "date": local.date().isoformat(),
            "label": f"{local.strftime('%a %d %b %H:%M')} {local.tzname() or ''}".strip()}


async def _area_row(db: AsyncSession, area_id: str) -> RestrictedAreaModel:
    row = await db.get(RestrictedAreaModel, area_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Restricted area not found")
    return row


def _state(area, now: float, me: Optional[str] = None) -> Dict[str, Any]:
    armed, source = area.armed_state(now)
    anns = area.active_announcements(now)
    nxt = ra.next_schedule_change(area.arming, now) if source == "schedule" else None
    manual = None
    if source == "manual":
        manual = {"state": area.manual_state, "until": _store_time(area.manual_until), "reason": area.manual_reason,
                  "by": area.manual_by}
    occ = restricted_area_engine.occupancy(area.id, now)
    return {
        "armed": armed, "armed_source": source, "announced": bool(anns), "guarded": armed and not anns,
        "next_change": _store_time(nxt), "manual": manual,
        "announcements": [dict(a.to_dict(), until_store=_store_time(a.until), mine=bool(me and a.user_id == me),
                               seconds_left=max(0, int(a.until - now))) for a in anns],
        "occupancy": occ,
        "problems": [{"camera_id": c, "message": m} for c, m in area.masked.items()],
        "watching_cameras": area.cameras() if area.enabled else [],
        "still_figures": restricted_area_engine.figure_count(area.id),
    }


async def _serialise(db: AsyncSession, row: RestrictedAreaModel, area, now: float, me: Optional[str]) -> Dict[str, Any]:
    cams = {c.id: c for c in (await db.execute(select(CameraModel))).scalars().all()}
    zone = await db.get(StoreZoneModel, row.floor_zone_id) if row.floor_zone_id else None
    try:
        from app.services.ai_zone_service import ai_zone_service

        intrusions = {iz["id"]: iz for iz in ai_zone_service.get_all_zones().get("intrusion_zones", [])}
    except Exception:  # noqa: BLE001
        intrusions = {}
    regions = []
    for reg in row.camera_regions or []:
        iz = intrusions.get(reg.get("intrusion_zone_id")) if reg.get("intrusion_zone_id") else None
        cid = (iz or {}).get("camera_id") or reg.get("camera_id")
        regions.append({**reg, "camera_id": cid, "camera_name": cams[cid].name if cid in cams else cid,
                        "region_name": (iz or {}).get("name"), "missing": bool(reg.get("intrusion_zone_id") and iz is None),
                        "points": (iz or {}).get("points") or reg.get("points")})
    watching = []
    if area is not None:
        for cid in area.cameras():
            src = "region" if cid in area.regions else ("floor" if cid in area.floor_cameras else "linked")
            watching.append({"camera_id": cid, "name": cams[cid].name if cid in cams else cid, "source": src,
                             "calibrated": src == "floor", "covered_fraction": area.floor_cameras.get(cid)})
    out = {
        "id": row.id, "name": row.name, "enabled": bool(row.enabled), "floor_zone_id": row.floor_zone_id,
        "floor_zone_name": zone.name if zone else None, "floor_zone_category": zone.category if zone else None,
        "floor_zone_missing": bool(row.floor_zone_id and zone is None),
        "camera_regions": regions, "cameras": watching,
        "arming": row.arming or {"mode": "always", "schedule": [], "schedule_mode": "armed_during"},
        "triggers": ra.merge_triggers(row.triggers), "points_of_interest": row.points_of_interest or [],
        "security": ra.validate_security(None, row.security), "legacy_intrusion_id": row.legacy_intrusion_id,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None, "updated_by": row.updated_by,
    }
    out["state"] = _state(area, now, me) if area is not None else None
    out["last_events"] = restricted_area_engine.last_events(row.id)
    return out


async def _refresh_and_get(area_id: str):
    snap = await restricted_areas.refresh()
    return snap.area(area_id)


# --------------------------------------------------------------------------- reads

@router.get("/catalogue", dependencies=[Depends(_signed_in)])
async def get_catalogue():
    """Trigger definitions (labels, explanations, examples, defaults, ranges), levels and outcomes."""
    return ra.catalogue()


@status_router.get("/status", dependencies=[Depends(_signed_in)])
async def get_status(request: Request, db: AsyncSession = Depends(get_db)):
    """The one cheap poll: every area's armed / announced state, live occupancy, open events."""
    now = restricted_areas.clock()
    snap = restricted_areas.snapshot
    me = (getattr(request.state, "user", None) or {}).get("sub")
    rows = (await db.execute(
        select(SecurityEventModel.id, SecurityEventModel.metadata_json, SecurityEventModel.acknowledged,
               SecurityEventModel.timestamp)
        .where(SecurityEventModel.event_type == "RESTRICTED_AREA")
        .order_by(desc(SecurityEventModel.timestamp)).limit(200))).all()
    open_by: Dict[str, int] = {}
    for _id, meta, _ack, _ts in rows:
        meta = meta or {}
        if meta.get("ra_area_id") and meta.get("ra_requires_outcome") and not meta.get("ra_outcome"):
            open_by[meta["ra_area_id"]] = open_by.get(meta["ra_area_id"], 0) + 1
    areas = []
    for a in snap.areas:
        st = _state(a, now, me)
        areas.append({"id": a.id, "name": a.name, "enabled": a.enabled, **st,
                      "open_events": open_by.get(a.id, 0), "last_events": restricted_area_engine.last_events(a.id)[:3]})
    return {"server_time": ra.iso(now), "store_time": _store_time(now), "areas": areas,
            "open_events_total": sum(open_by.values()), "latency_ms": restricted_area_engine.stats.get("latency_ms")}


@router.get("", dependencies=[Depends(_signed_in)])
async def list_areas(request: Request, db: AsyncSession = Depends(get_db)):
    """Every area with its settings and live state, plus what the settings panel needs to choose from."""
    now = restricted_areas.clock()
    snap = restricted_areas.snapshot
    user = getattr(request.state, "user", None)
    me = (user or {}).get("sub")
    rows = (await db.execute(select(RestrictedAreaModel).order_by(RestrictedAreaModel.name))).scalars().all()
    out = [await _serialise(db, r, snap.area(r.id), now, me) for r in rows]
    from app.services.tracking_service import floor_projector

    cams = (await db.execute(select(CameraModel).order_by(CameraModel.name))).scalars().all()
    zones = (await db.execute(select(StoreZoneModel).order_by(StoreZoneModel.name))).scalars().all()
    try:
        from app.services.ai_zone_service import ai_zone_service

        zs = ai_zone_service.get_all_zones()
    except Exception:  # noqa: BLE001
        zs = {}
    return {
        "areas": out,
        "cameras": [{"id": c.id, "name": c.name, "calibrated": floor_projector.has_homography(c.id),
                     "watch_zone_id": c.watch_zone_id, "enabled": bool(c.is_ai_enabled)} for c in cams],
        "zones": [{"id": z.id, "name": z.name, "category": z.category, "polygon": z.polygon} for z in zones],
        "camera_areas": [{"id": iz["id"], "name": iz.get("name") or "Restricted area", "camera_id": iz.get("camera_id"),
                          "points": iz.get("points")} for iz in zs.get("intrusion_zones", []) or []],
        "lines": [{"id": tw["id"], "name": tw.get("name") or "Line", "camera_id": tw.get("camera_id")}
                  for tw in zs.get("tripwires", []) or []],
        "can_edit": (user is None) or is_admin_role(effective_role(user)),
        "store_time": _store_time(now),
    }


@router.get("/cameras-for-zone/{zone_id}", dependencies=[Depends(_signed_in)])
async def cameras_for_zone(zone_id: str, db: AsyncSession = Depends(get_db)):
    """Cameras that see a store-map zone (calibrated: covered share; else linked with watch_zone_id)."""
    from app.services.tracking_service import floor_projector

    zone = await db.get(StoreZoneModel, zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    poly = [(float(p["x"]), float(p["y"])) for p in zone.polygon or []]
    out = []
    for c in (await db.execute(select(CameraModel))).scalars().all():
        if floor_projector.has_homography(c.id) and len(poly) >= 3:
            frac, masked, n = ra.floor_coverage(c.id, poly, None)
            if n >= 3:
                out.append({"camera_id": c.id, "name": c.name, "covered_fraction": round(frac, 3),
                            "masked_fraction": round(masked, 3), "source": "calibrated"})
        elif c.watch_zone_id == zone_id:
            out.append({"camera_id": c.id, "name": c.name, "covered_fraction": None, "source": "linked"})
    return {"zone_id": zone_id, "cameras": out}


def _event_entry(e: SecurityEventModel, names: Dict[str, str]) -> Dict[str, Any]:
    m = e.metadata_json or {}
    ts = e.timestamp.replace(tzinfo=None) if e.timestamp else None
    local = to_local(ts) if ts else None
    ack = m.get("acknowledged_by")
    return {
        "id": e.id, "area_id": m.get("ra_area_id"), "area_name": m.get("area_name"), "trigger": m.get("ra_trigger"),
        "trigger_label": m.get("ra_trigger_label"), "tier": m.get("alert_tier"), "title": m.get("title"),
        "body": m.get("body"), "camera_id": e.camera_id, "camera_name": names.get(e.camera_id) or e.camera_name,
        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        "store_time": {"hhmm": local.strftime("%H:%M"), "date": local.date().isoformat(),
                       "label": f"{local.strftime('%a %d %b %H:%M')} {local.tzname() or ''}".strip()} if local else None,
        "snapshot_url": e.snapshot_url, "clip_url": e.clip_url,
        "evidence_expired": e.evidence_expired_at is not None, "people_inside": m.get("people_inside"),
        "test": bool(m.get("test")), "acknowledged": bool(e.acknowledged),
        "acknowledged_by": ack.get("name") if isinstance(ack, dict) else ack,
        "outcome": m.get("ra_outcome"), "outcome_label": ra.OUTCOMES.get(m.get("ra_outcome") or ""),
        "outcome_note": m.get("ra_outcome_note"), "outcome_by": m.get("ra_outcome_by"),
        "outcome_at": m.get("ra_outcome_at"),
        "needs_outcome": bool(m.get("ra_requires_outcome")) and not m.get("ra_outcome"),
        "measured": {k: m.get(k) for k in ("speed", "speed_unit", "reversals", "turns", "people", "allowed",
                                           "inside_sec", "seconds", "poi_name", "distance", "tamper", "calibrated")
                     if m.get(k) is not None},
    }


@router.get("/events", dependencies=[Depends(_signed_in)])
async def list_events(area_id: Optional[str] = None, trigger: Optional[str] = None,
                      needs_outcome: Optional[bool] = None, limit: int = Query(100, ge=1, le=500),
                      db: AsyncSession = Depends(get_db)):
    """Restricted-area alerts, newest first (filters: area, trigger, still needing an outcome)."""
    if trigger and trigger not in ra.TRIGGERS:
        raise HTTPException(status_code=422, detail=f"Unknown trigger '{trigger}'")
    rows = (await db.execute(
        select(SecurityEventModel).where(SecurityEventModel.event_type == "RESTRICTED_AREA")
        .order_by(desc(SecurityEventModel.timestamp)).limit(2000))).scalars().all()
    names = dict((await db.execute(select(CameraModel.id, CameraModel.name))).all())
    out = []
    for e in rows:
        m = e.metadata_json or {}
        if not m.get("ra_area_id"):
            continue
        if area_id and m.get("ra_area_id") != area_id:
            continue
        if trigger and m.get("ra_trigger") != trigger:
            continue
        entry = _event_entry(e, names)
        if needs_outcome is not None and entry["needs_outcome"] != needs_outcome:
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return {"events": out, "total": len(out)}


async def _ra_event(db: AsyncSession, event_id: str) -> SecurityEventModel:
    row = await db.get(SecurityEventModel, event_id)
    if row is None or row.event_type != "RESTRICTED_AREA" or not (row.metadata_json or {}).get("ra_area_id"):
        raise HTTPException(status_code=404, detail="Restricted area alert not found")
    return row


@router.get("/events/{event_id}/snapshot")
async def event_snapshot(event_id: str, db: AsyncSession = Depends(get_db)):
    """The privacy-masked evidence still of a restricted-area alert (410 once the storage limit removed it)."""
    row = await _ra_event(db, event_id)
    path = restricted_area_engine.evidence_dir() / f"{event_id}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=410 if row.evidence_expired_at else 404, detail="No evidence still for this alert")
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/events/{event_id}/clip")
async def event_clip(event_id: str, db: AsyncSession = Depends(get_db)):
    """The evidence clip (about 5 s before to 10 s after, privacy-masked). Range requests work."""
    row = await _ra_event(db, event_id)
    path = (restricted_area_engine.evidence_dir() / f"{event_id}.mp4").resolve()
    if not path.is_file():
        raise HTTPException(status_code=410 if row.evidence_expired_at else 404, detail="No clip for this alert")
    return FileResponse(str(path), media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/events/{event_id}/outcome")
@operator_allowed
async def record_outcome(event_id: str, request: Request, body: Dict[str, Any] = Body(...),
                         db: AsyncSession = Depends(get_db)):
    """Acknowledge a restricted-area alert with what happened (false alarm / authorised / incident)."""
    row = await _ra_event(db, event_id)
    outcome = str((body or {}).get("outcome") or "")
    if outcome not in ra.OUTCOMES:
        raise HTTPException(status_code=422, detail="Choose what happened: false alarm, authorised or real incident.")
    try:
        note = ra._text((body or {}).get("note"), ra.NOTE_MAX, "Note")
    except AreaError as e:
        _err(e)
    meta = dict(row.metadata_json or {})
    if meta.get("ra_outcome"):
        raise HTTPException(status_code=409, detail=f"Already recorded as {ra.OUTCOMES.get(meta['ra_outcome'])} by "
                                                    f"{meta.get('ra_outcome_by') or 'someone'}.")
    who = await _actor(request, db)
    now = ra.utcnow()
    meta.update(ra_outcome=outcome, ra_outcome_note=note, ra_outcome_by=who["name"], ra_outcome_at=now.isoformat() + "Z")
    if not row.acknowledged:
        row.acknowledged = True
        row.acknowledged_at = now
        meta.setdefault("acknowledged_by", {"user_id": who["id"], "name": who["name"]})
    row.metadata_json = meta
    await restricted_areas.audit(db, "outcome", SimpleNamespace(id=meta.get("ra_area_id"), name=meta.get("area_name")),
                                 who, event_id=event_id, detail={"outcome": outcome, "note": note,
                                                                 "trigger": meta.get("ra_trigger")})
    await db.commit()
    try:  # stop phone repeats / escalation now
        from app.services.push_alerts import push_alerts

        await push_alerts.tick(only=event_id)
    except Exception:  # noqa: BLE001
        pass
    return {"event_id": event_id, "outcome": outcome, "outcome_label": ra.OUTCOMES[outcome], "by": who["name"]}


@router.get("/audit", dependencies=[Depends(_signed_in)])
async def list_audit(area_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000),
                     db: AsyncSession = Depends(get_db)):
    """The audit trail, newest first."""
    q = select(RestrictedAreaAuditModel).order_by(desc(RestrictedAreaAuditModel.ts)).limit(limit)
    if area_id:
        q = q.where(RestrictedAreaAuditModel.area_id == area_id)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for r in rows:
        local = to_local(r.ts)
        out.append({"id": r.id, "ts": r.ts.isoformat() + "Z", "store_time": local.strftime("%a %d %b %H:%M"),
                    "area_id": r.area_id, "area_name": r.area_name, "action": r.action, "actor": r.actor_name,
                    "actor_role": r.actor_role, "event_id": r.event_id, "detail": r.detail})
    return {"entries": out}


# --------------------------------------------------------------------------- settings (admins)

@router.post("", status_code=201)
async def create_area(request: Request, body: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db),
                      _admin=Depends(require_admin)):
    count = len((await db.execute(select(RestrictedAreaModel.id))).all())
    if count >= ra.MAX_AREAS:
        raise HTTPException(status_code=409, detail=f"At most {ra.MAX_AREAS} restricted areas.")
    try:
        rec = await restricted_areas.validate_area(db, body)
    except AreaError as e:
        _err(e)
    who = await _actor(request, db)
    warnings = rec.pop("_warnings", [])
    row = RestrictedAreaModel(id=ra.new_id("ra"), created_at=ra.utcnow(), updated_at=ra.utcnow(),
                              updated_by=who["name"], **rec)
    db.add(row)
    await restricted_areas.audit(db, "created", row, who, detail={"settings": rec})
    await db.commit()
    area = await _refresh_and_get(row.id)
    out = await _serialise(db, row, area, restricted_areas.clock(), who["id"])
    out["warnings"] = warnings
    return out


@router.get("/{area_id}", dependencies=[Depends(_signed_in)])
async def get_area(area_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _area_row(db, area_id)
    me = (getattr(request.state, "user", None) or {}).get("sub")
    return await _serialise(db, row, restricted_areas.snapshot.area(area_id), restricted_areas.clock(), me)


@router.put("/{area_id}")
async def update_area(area_id: str, request: Request, body: Dict[str, Any] = Body(...),
                      db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    row = await _area_row(db, area_id)
    before = {k: getattr(row, k) for k in ("name", "enabled", "floor_zone_id", "camera_regions", "arming",
                                            "triggers", "points_of_interest", "security")}
    try:
        rec = await restricted_areas.validate_area(db, body, current=row)
    except AreaError as e:
        _err(e)
    who = await _actor(request, db)
    warnings = rec.pop("_warnings", [])
    changed = {k: {"from": before[k], "to": v} for k, v in rec.items() if before.get(k) != v}
    for k, v in rec.items():
        setattr(row, k, v)
    row.updated_at, row.updated_by = ra.utcnow(), who["name"]
    if changed:
        await restricted_areas.audit(db, "settings_changed", row, who, detail={"changed": changed})
    await db.commit()
    area = await _refresh_and_get(area_id)
    out = await _serialise(db, row, area, restricted_areas.clock(), who["id"])
    out["warnings"] = warnings
    return out


@router.delete("/{area_id}")
async def delete_area(area_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      _admin=Depends(require_admin)):
    row = await _area_row(db, area_id)
    who = await _actor(request, db)
    await restricted_areas.audit(db, "deleted", row, who, detail={"name": row.name})
    if row.legacy_intrusion_id:
        # The zone-store area stays drawn; it is not evaluated by the old engine either.
        pass
    await db.delete(row)
    await db.commit()
    await restricted_areas.refresh()
    return {"status": "deleted", "id": area_id}


# --------------------------------------------------------------------------- arming

def _minutes(body: dict, required: bool) -> Optional[int]:
    v = (body or {}).get("minutes")
    if v is None or v == "":
        if required:
            raise HTTPException(status_code=422, detail="Choose for how long (minutes).")
        return None
    try:
        return int(ra._num(v, 1, ra.MANUAL_MAX_MIN, "Minutes", integer=True))
    except AreaError as e:
        _err(e)


def _reason(body: dict) -> str:
    try:
        return ra._text((body or {}).get("reason"), ra.REASON_MAX, "Reason", required=True)
    except AreaError as e:
        _err(e)


async def _set_manual(db, request, area_id: str, state: Optional[str], body: dict, action: str):
    row = await _area_row(db, area_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="This area is switched off. Switch it on in its settings first.")
    reason = _reason(body)
    now = restricted_areas.clock()
    until = None
    if state is not None:
        minutes = _minutes(body, required=False)
        if minutes is not None:
            until = now + minutes * 60
        else:
            nxt = ra.next_schedule_change(row.arming or {}, now) if (row.arming or {}).get("mode") == "schedule" else None
            if nxt is None:
                if state == "disarmed":
                    raise HTTPException(status_code=422, detail=(
                        "This area is always armed: choose for how long to disarm it (at most 12 hours)."))
                until = now + ra.MANUAL_MAX_MIN * 60
            else:
                until = min(nxt, now + ra.MANUAL_MAX_MIN * 60)
    who = await _actor(request, db)
    row.manual_state = state
    row.manual_until = ra.dt_of(until) if until else None
    row.manual_reason = reason if state else None
    row.manual_by = who["name"] if state else None
    row.updated_at = ra.utcnow()
    await restricted_areas.audit(db, action, row, who, detail={"reason": reason, "until": ra.iso(until)})
    await db.commit()
    area = await _refresh_and_get(area_id)
    return {"id": area_id, "state": _state(area, restricted_areas.clock(), who["id"]) if area else None}


@router.post("/{area_id}/arm")
@operator_allowed
async def arm_area(area_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_db)):
    """Arm now (operators too: it only raises security), with a reason; until ``minutes`` or the next schedule change."""
    return await _set_manual(db, request, area_id, "armed", body, "armed")


@router.post("/{area_id}/disarm")
async def disarm_area(area_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                      db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    """Disarm now (owners / administrators), with a reason; until ``minutes`` or the next schedule change."""
    return await _set_manual(db, request, area_id, "disarmed", body, "disarmed")


@router.post("/{area_id}/resume")
async def resume_schedule(area_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                          db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    """End a manual arm/disarm: the area follows its schedule (or stays always armed) again."""
    return await _set_manual(db, request, area_id, None, body, "schedule_resumed")


# --------------------------------------------------------------------------- announcements

@router.post("/{area_id}/announce", status_code=201)
@operator_allowed
async def announce(area_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
                   db: AsyncSession = Depends(get_db)):
    """"I'm going in": announce entry for 5-120 minutes (default 15), optional note and number of people."""
    row = await _area_row(db, area_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="This area is switched off.")
    try:
        minutes = int(ra._num(body.get("minutes", ra.ANNOUNCE_DEFAULT), ra.ANNOUNCE_MIN, ra.ANNOUNCE_MAX,
                              "Minutes", integer=True))
        people = int(ra._num(body.get("people", 1), 1, ra.ANNOUNCE_PEOPLE_MAX, "People going in", integer=True))
        note = ra._text(body.get("note"), ra.NOTE_MAX, "Note")
    except AreaError as e:
        _err(e)
    source = str(body.get("source") or "dashboard")[:32]
    who = await _actor(request, db)
    now = ra.utcnow()
    # One announcement per person and area: announcing again replaces it.
    if who["id"]:
        old = (await db.execute(select(RestrictedAreaAnnouncementModel).where(
            RestrictedAreaAnnouncementModel.area_id == area_id, RestrictedAreaAnnouncementModel.user_id == who["id"],
            RestrictedAreaAnnouncementModel.ended_at.is_(None), RestrictedAreaAnnouncementModel.until > now))).scalars().all()
        for o in old:
            o.ended_at, o.ended_by = now, f"{who['name']} (announced again)"
    ann = RestrictedAreaAnnouncementModel(
        id=ra.new_id("ann"), area_id=area_id, user_id=who["id"], user_name=who["name"], note=note, people=people,
        source=source, started_at=now, until=now + timedelta(minutes=minutes))
    db.add(ann)
    await restricted_areas.audit(db, "announced", row, who, detail={"minutes": minutes, "people": people,
                                                                     "note": note, "source": source,
                                                                     "announcement_id": ann.id})
    await db.commit()
    area = await _refresh_and_get(area_id)
    return {"announcement": {"id": ann.id, "area_id": area_id, "area_name": row.name, "user_name": who["name"],
                             "until": ann.until.isoformat() + "Z", "until_store": _store_time(ra.ts_of(ann.until)),
                             "minutes": minutes, "people": people, "note": note},
            "state": _state(area, restricted_areas.clock(), who["id"]) if area else None}


@router.post("/announcements/{ann_id}/end")
@operator_allowed
async def end_announcement(ann_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """"I'm out": end an announcement now (your own; owners / administrators may end anyone's)."""
    ann = await db.get(RestrictedAreaAnnouncementModel, ann_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    who = await _actor(request, db)
    if ann.user_id and who["id"] != ann.user_id and not is_admin_role(who["role"]):
        raise HTTPException(status_code=403, detail="Only the person who announced it (or an administrator) can end it.")
    now = ra.utcnow()
    if ann.ended_at is not None or ann.until <= now:
        return {"id": ann_id, "status": "already_ended"}
    ann.ended_at, ann.ended_by = now, who["name"]
    row = await db.get(RestrictedAreaModel, ann.area_id)
    await restricted_areas.audit(db, "announcement_ended", row or ann.area_id, who,
                                 detail={"announcement_id": ann_id, "announced_by": ann.user_name})
    await db.commit()
    await restricted_areas.refresh()
    return {"id": ann_id, "status": "ended"}


# --------------------------------------------------------------------------- admin tools

@router.post("/{area_id}/test-alert")
async def test_alert(area_id: str, request: Request, db: AsyncSession = Depends(get_db),
                     _admin=Depends(require_admin)):
    """A clearly labelled test alert through the real path (banner, sound, phones at the entry level)."""
    await _area_row(db, area_id)
    area = await _refresh_and_get(area_id)
    if area is None:
        raise HTTPException(status_code=404, detail="Restricted area not found")
    who = await _actor(request, db)
    alert = restricted_area_engine.raise_test(area, who)
    return {"alert_id": alert.alert_id, "tier": alert.tier, "title": alert.title}


@router.post("/{area_id}/forget-still-figures")
async def forget_still_figures(area_id: str, request: Request, db: AsyncSession = Depends(get_db),
                               _admin=Depends(require_admin)):
    """Forget the posters / mannequins learnt in this area (they are learnt again only while it is not guarded)."""
    row = await _area_row(db, area_id)
    n = restricted_area_engine.forget_figures(area_id)
    who = await _actor(request, db)
    await restricted_areas.audit(db, "still_figures_cleared", row, who, detail={"forgotten": n})
    await db.commit()
    return {"forgotten": n}


if settings.RESTRICTED_QA_HOOK:
    @router.post("/qa/feed", include_in_schema=False)
    async def qa_feed(body: Dict[str, Any] = Body(...), _admin=Depends(require_admin)):
        """QA / tests only (EDGE_RESTRICTED_QA_HOOK=1): feed synthetic tracks into the engine."""
        cam = str(body.get("camera_id") or "")
        if not cam:
            raise HTTPException(status_code=422, detail="camera_id required")
        await restricted_areas.refresh()
        return restricted_area_engine.qa_feed(cam, body.get("frames") or [], int(body.get("width", 1280)),
                                              int(body.get("height", 720)), camera_name=body.get("camera_name"))

    @router.post("/qa/health", include_in_schema=False)
    async def qa_health(body: Dict[str, Any] = Body(...), _admin=Depends(require_admin)):
        """QA / tests only: mark a camera silent for ``seconds`` (offline tamper)."""
        cam = str(body.get("camera_id") or "")
        restricted_area_engine._frames[cam] = restricted_area_engine.clock() - float(body.get("seconds", 60))
        return {"raised": await restricted_area_engine.tick()}
