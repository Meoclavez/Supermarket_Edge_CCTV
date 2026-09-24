"""Remote access (online dashboard) settings and status.

GET  /api/v1/remote-access          status + settings (the token is never returned)
PUT  /api/v1/remote-access          configure / enable / disable (operator session)
POST /api/v1/remote-access/verify   fetch https://<hostname>/api/v1/device/identity
                                    and compare device_id with this device
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.routes import ResilientRoute
from app.services.auth_service import auth_service
from app.services.remote_access_service import RemoteAccessError, remote_access_service

router = APIRouter(
    prefix="/api/v1/remote-access",
    tags=["Remote access"],
    dependencies=[Depends(auth_service.verify_api_access)],
    route_class=ResilientRoute,
)


class RemoteAccessUpdate(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = Field(default=None, description="cloudflare_tunnel | direct")
    hostname: Optional[str] = None
    # Write-only. Omit or send "" to keep the stored token.
    token: Optional[str] = Field(default=None, max_length=4096)
    clear_token: bool = False


def _require_operator(request: Request) -> None:
    """Stream/clip tokens and paired-phone tokens may not reconfigure the tunnel."""
    user = getattr(request.state, "user", None)
    if user is None:
        return  # internal API key, or AUTH_DISABLED (which refuses enabling anyway)
    if user.get("type") != "user_session" or user.get("pd"):
        raise HTTPException(status_code=403, detail="Only an operator signed in to the dashboard can change remote access.")


@router.get("")
async def get_remote_access():
    return await asyncio.to_thread(remote_access_service.status)


@router.put("")
async def put_remote_access(body: RemoteAccessUpdate, request: Request):
    _require_operator(request)
    try:
        return await asyncio.to_thread(
            remote_access_service.update,
            enabled=body.enabled,
            provider=body.provider,
            hostname=body.hostname,
            token=body.token or None,
            clear_token=body.clear_token,
        )
    except RemoteAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/verify")
async def verify_remote_access(request: Request):
    _require_operator(request)
    result = await asyncio.to_thread(remote_access_service.verify)
    status = await asyncio.to_thread(remote_access_service.status)
    status["verify_error"] = result.get("verify_error")
    return status
