"""Online access (dashboard through the owner's VPS) settings and status.

GET  /api/v1/remote-access          status + settings (the token is never returned)
PUT  /api/v1/remote-access          configure / enable / disable (operator session)
POST /api/v1/remote-access/verify   fetch https://<hostname>/api/v1/device/identity
                                    and compare device_id with this device

The same settings carry remote live video (direct WebRTC, STUN only):
``stun_servers``, ``webrtc_mode``, ``webrtc_port``, ``max_video_sessions`` and
``live_transport_on_lan`` (routes/webrtc.py uses them).
"""

from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

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
    provider: Optional[str] = Field(default=None, description="vps_tunnel (the only provider)")
    hostname: Optional[str] = Field(default=None, max_length=253, description="public address, e.g. cctv.example.com")
    server_url: Optional[str] = Field(default=None, max_length=300,
                                      description="tunnel server, wss://tunnel.example.com or tcp://vps.example.com:7000")
    store_id: Optional[str] = Field(default=None, max_length=64, description="store ID issued with the tunnel")
    # Write-only secrets. Omit or send "" to keep the stored value.
    token: Optional[str] = Field(default=None, max_length=4096, description="store token")
    clear_token: bool = False
    server_key: Optional[str] = Field(default=None, max_length=4096,
                                      description="optional shared server key (frp auth.token)")
    clear_server_key: bool = False
    extra_proxies: Optional[int] = Field(default=None, ge=0, le=2,
                                         description="proxies in front of the VPS's reverse proxy (e.g. a CDN): 0-2")
    # Remote live video: direct peer-to-peer WebRTC (docs/REMOTE_VIDEO_CONTRACT.md).
    stun_servers: Optional[List[str]] = Field(default=None, max_length=4,
                                              description="STUN servers (stun:host:port); TURN is refused")
    webrtc_mode: Optional[Literal["auto", "fixed_port"]] = Field(
        default=None, description="auto: no router change; fixed_port: a forwarded UDP port")
    webrtc_port: Optional[int] = Field(default=None, ge=1024, le=65535, description="fixed_port mode: UDP/TCP port")
    max_video_sessions: Optional[int] = Field(default=None, ge=1, le=32, description="live video sessions at once")
    live_transport_on_lan: Optional[Literal["local", "webrtc"]] = Field(
        default=None, description="live video on the store network: local (snapshots/MJPEG) or webrtc")


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
            server_url=body.server_url,
            store_id=body.store_id,
            token=body.token or None,
            clear_token=body.clear_token,
            server_key=body.server_key or None,
            clear_server_key=body.clear_server_key,
            extra_proxies=body.extra_proxies,
            stun_servers=body.stun_servers,
            webrtc_mode=body.webrtc_mode,
            webrtc_port=body.webrtc_port,
            max_video_sessions=body.max_video_sessions,
            live_transport_on_lan=body.live_transport_on_lan,
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
