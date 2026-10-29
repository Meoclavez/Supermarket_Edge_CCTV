"""Automatic set-up: store hours, what the box chose by itself, and suggestions.

GET  /api/v1/store-hours                       any signed-in person
PUT  /api/v1/store-hours                       administrators: edit days ({"days": {mon: {...} | null}})
POST /api/v1/store-hours/relearn               administrators: learn again now
GET  /api/v1/automation/status                 any signed-in person: time zone, backup time, storage,
                                               analysis speed, camera restarts, applied suggestions
GET  /api/v1/automation/suggestions            any signed-in person (operators see them read-only)
POST /api/v1/automation/suggestions/{id}/apply administrators ({"camera_ids": [...]} optional for links)
POST /api/v1/automation/suggestions/{id}/dismiss administrators
POST /api/v1/automation/applied/{id}/undo      administrators
POST /api/v1/automation/tune                   administrators: choose the analysis speed again now

Writes need an owner or administrator (services/auth_service.require_admin):
operators see what is suggested but cannot apply it. Every apply, dismiss,
undo and edit is in the audit trail. See services/automation.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import AdminUserModel
from app.routes import ResilientRoute
from app.services import automation as auto
from app.services.auth_service import auth_service, effective_role, general_rate_limiter, is_admin_role, require_admin

router = APIRouter(
    prefix="/api/v1",
    tags=["Automation"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)


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
    return {"id": uid, "name": name or str(uid or "unknown"), "role": effective_role(user)}


def _can_edit(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return user is None or is_admin_role(effective_role(user))


def _err(e: auto.SuggestionError) -> None:
    raise HTTPException(status_code=e.status, detail=str(e))


@router.get("/store-hours")
async def get_store_hours(request: Request):
    out = auto.hours_status()
    out["can_edit"] = _can_edit(request)
    return out


@router.put("/store-hours")
async def put_store_hours(request: Request, body: Dict[str, Any] = Body(...),
                          db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    try:
        changes = auto.validate_manual(body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await auto.set_manual(db, changes, await _actor(request, db))
    out = auto.hours_status()
    out["can_edit"] = True
    return out


@router.post("/store-hours/relearn")
async def relearn_store_hours(request: Request, db: AsyncSession = Depends(get_db),
                              _admin=Depends(require_admin)):
    await auto.relearn(db, await _actor(request, db))
    out = auto.hours_status()
    out["can_edit"] = True
    return out


@router.get("/automation/status")
async def automation_status(request: Request):
    return {
        "store_hours": auto.hours_status(),
        "storage": await asyncio.to_thread(auto.storage_view),
        "analysis": auto.tune_status(),
        "restarts": auto.restarts_status(),
        "applied": auto.applied_list(),
        "can_edit": _can_edit(request),
    }


@router.get("/automation/suggestions")
async def list_suggestions(request: Request, db: AsyncSession = Depends(get_db)):
    return {"suggestions": await auto.suggestions(db), "can_apply": _can_edit(request)}


@router.post("/automation/suggestions/{sid}/apply")
async def apply_suggestion(sid: str, request: Request, body: Optional[Dict[str, Any]] = Body(None),
                           db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    cams = (body or {}).get("camera_ids")
    if cams is not None and not (isinstance(cams, list) and all(isinstance(c, str) for c in cams)):
        raise HTTPException(status_code=422, detail="camera_ids must be a list of camera ids")
    try:
        rec = await auto.apply(db, sid, await _actor(request, db), camera_ids=cams)
    except auto.SuggestionError as e:
        _err(e)
    return {k: v for k, v in rec.items() if k != "undo"}


@router.post("/automation/suggestions/{sid}/dismiss")
async def dismiss_suggestion(sid: str, request: Request, db: AsyncSession = Depends(get_db),
                             _admin=Depends(require_admin)):
    await auto.dismiss(db, sid, await _actor(request, db))
    return {"status": "dismissed", "id": sid, "days": auto.DISMISS_DAYS}


@router.post("/automation/applied/{apply_id}/undo")
async def undo_applied(apply_id: str, request: Request, db: AsyncSession = Depends(get_db),
                       _admin=Depends(require_admin)):
    try:
        rec = await auto.undo(db, apply_id, await _actor(request, db))
    except auto.SuggestionError as e:
        _err(e)
    return {k: v for k, v in rec.items() if k != "undo"}


@router.post("/automation/tune")
async def tune_now(request: Request, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    rec = await asyncio.to_thread(auto.tune_now, "asked by an administrator")
    from app.services.restricted_areas import restricted_areas

    await restricted_areas.audit(db, "auto_tune", None, await _actor(request, db),
                                 detail={"chosen": rec.get("chosen"), "cameras": rec.get("cameras")})
    await db.commit()
    return auto.tune_status()
