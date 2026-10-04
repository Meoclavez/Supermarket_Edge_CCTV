"""Site settings set in the dashboard (services/site_settings.py).

GET    /api/v1/site-settings              every setting: value, .env default, overridden,
                                          allowed range; store time; disk (signed in)
GET    /api/v1/site-settings/timezones    IANA zone names for the time zone list (signed in)
PUT    /api/v1/site-settings              {"values": {KEY: value | null}}; null = back to
                                          the .env default. All or nothing (owner / admin)
DELETE /api/v1/site-settings/{key}        reset one setting to its .env default (owner / admin)

Keys are the .env names (STORE_NAME, SITE_TIMEZONE, ...). A refused change
answers 422 with ``detail = {"message", "errors": {KEY: reason}}``. Every
change applies without a restart and is logged with who made it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routes import ResilientRoute
from app.services.actor import describe_actor
from app.services.auth_service import auth_service, require_admin
from app.services.site_settings import SPECS_BY_KEY, SiteSettingsError, available_timezones, site_settings

router = APIRouter(
    prefix="/api/v1/site-settings",
    tags=["Site settings"],
    dependencies=[Depends(auth_service.verify_api_access)],
    route_class=ResilientRoute,
)


class SiteSettingsUpdate(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict, description="KEY -> new value; null resets to the .env default")


def _refused(exc: SiteSettingsError) -> HTTPException:
    return HTTPException(status_code=422, detail={"message": "Not saved: " + "; ".join(exc.errors.values()),
                                                  "errors": exc.errors})


@router.get("")
async def get_site_settings():
    return await asyncio.to_thread(site_settings.describe)


@router.get("/timezones")
async def get_timezones():
    return {"timezones": await asyncio.to_thread(available_timezones)}


@router.put("", dependencies=[Depends(require_admin)])
async def put_site_settings(body: SiteSettingsUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    if not body.values:
        raise HTTPException(status_code=422, detail={"message": "Nothing to save.", "errors": {}})
    actor = await describe_actor(request, db)
    try:
        changed = await asyncio.to_thread(site_settings.update, body.values, actor)
    except SiteSettingsError as exc:
        raise _refused(exc)
    out = await asyncio.to_thread(site_settings.describe)
    out["changed"] = sorted(changed)
    return out


@router.delete("/{key}", dependencies=[Depends(require_admin)])
async def reset_site_setting(key: str, request: Request, db: AsyncSession = Depends(get_db)):
    if key not in SPECS_BY_KEY:
        raise HTTPException(status_code=404, detail=f"{key} is not a site setting")
    actor = await describe_actor(request, db)
    try:
        changed = await asyncio.to_thread(site_settings.reset, key, actor)
    except SiteSettingsError as exc:
        raise _refused(exc)
    out = await asyncio.to_thread(site_settings.describe)
    out["changed"] = sorted(changed)
    return out
