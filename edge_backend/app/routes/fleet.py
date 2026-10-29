"""iKorex service (fleet control) endpoints for the dashboard.

GET  /api/v1/fleet/status       any signed-in person: plan state, licence, features,
                                last contact, control key fingerprint (never the token)
POST /api/v1/fleet/reset-key    owner/administrator: forget the pinned control key
POST /api/v1/fleet/checkin-now  owner/administrator: check in with iKorex now (also the standby
                                page's "Check connection now": a verified answer lifts standby)

All three stay open while the service is paused or in standby (entitlements.FleetGateMiddleware).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from app.services.actor import describe_actor
from app.services.auth_service import auth_service, require_admin
from app.services.fleet_control import fleet_control

router = APIRouter(prefix="/api/v1/fleet", tags=["iKorex service"])


@router.get("/status", dependencies=[Depends(auth_service.verify_api_access)])
async def fleet_status() -> Dict[str, Any]:
    return await asyncio.to_thread(fleet_control.status)


@router.post("/reset-key")
async def fleet_reset_key(request: Request, _admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    from app.database import async_session_factory

    async with async_session_factory() as db:
        actor = await describe_actor(request, db)
    try:
        return await fleet_control.reset_key(f"local admin {actor}")
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/checkin-now")
async def fleet_checkin_now(_admin: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return await fleet_control.checkin_now()
