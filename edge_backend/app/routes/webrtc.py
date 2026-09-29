"""Live video over direct peer-to-peer WebRTC (docs/REMOTE_VIDEO_CONTRACT.md).

GET    /api/v1/webrtc/config                   what the page must use for live video, ICE servers (STUN only)
POST   /api/v1/webrtc/sessions                 open a session: browser offer (gathering complete) -> answer
POST   /api/v1/webrtc/sessions/{id}/heartbeat  keep it open (404: it has ended, close the connection)
DELETE /api/v1/webrtc/sessions/{id}            close it (204, idempotent)
POST   /api/v1/webrtc/sessions/{id}/report     browser-side outcome (candidate pair, frames) for diagnostics
GET    /api/v1/webrtc/sessions                 admin: active sessions
GET    /api/v1/webrtc/diagnostics              admin: go2rtc, NAT check (on demand), last 50 outcomes, advice

Compatibility for the phone app (to be updated to the session API):
GET  /api/v1/webrtc/ice-servers   STUN only, ``turn_enabled: false``
POST /api/v1/webrtc/offer         opens a session without heartbeats; it ends when the connection
                                  closes, the camera is turned off, or after max_session_s
GET  /api/v1/webrtc/token         short-lived stream token (unchanged)

Camera video never passes through the online-access tunnel: only this
signalling does. The media goes directly between the browser and go2rtc here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.models.schemas import WebRtcAnswer, WebRtcOffer, WebRtcReport, WebRtcSessionCreate
from app.routes import ResilientRoute
from app.services import public_exposure
from app.services.auth_service import RateLimiter, auth_service, general_rate_limiter
from app.services.turn_service import assert_no_relay, turn_service
from app.services.webrtc_sessions import SessionError, webrtc_sessions

# Rate limits. The per-session calls (heartbeat, report, DELETE) are not
# limited: they are O(1) on an existing session, and refusing one leaks the
# session (a refused DELETE keeps the camera pulled until the idle timeout).
# They must also not share the dashboard's general per-address budget
# (100/min), which one viewer's rotating camera grid alone exceeded (4 tiles
# x open + 2 reports + DELETE per rotation, plus heartbeats). Opening a
# session has its own budget, sized for a few viewers behind one address at
# the fastest rotation (4 tiles every 3 s = 80/min each); the real bound on
# load is max_video_sessions. Everything else keeps the general limiter.
webrtc_open_limiter = RateLimiter(requests=240, window=60)

router = APIRouter(
    prefix="/api/v1/webrtc",
    tags=["WebRTC"],
    dependencies=[Depends(auth_service.verify_api_access)],
    route_class=ResilientRoute,
)
GENERAL = [Depends(general_rate_limiter)]
OPEN = [Depends(webrtc_open_limiter)]

LIVE_TRANSPORT_HEADER = "x-live-transport"


def _user(request: Request) -> Optional[dict]:
    return getattr(request.state, "user", None)


def _require_admin(request: Request) -> None:
    """Session list and diagnostics: an operator signed in to the dashboard (not a viewer, phone or stream token)."""
    user = _user(request)
    if user is None:
        return  # internal API key, or AUTH_DISABLED
    if user.get("type") != "user_session" or user.get("pd") or user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Only an administrator can see live video sessions.")


def _check_watch(request: Request, camera_id: str) -> None:
    """Viewers may watch. A stream token only for its own camera; clip tokens never."""
    user = _user(request)
    if user is None:
        return
    kind = user.get("type")
    if kind == "stream_access" and user.get("camera_id") not in (None, camera_id):
        raise HTTPException(status_code=403, detail="This stream token is for another camera.")
    if kind == "clip_access":
        raise HTTPException(status_code=403, detail="A clip token cannot open live video.")


def _error(exc: SessionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


def _transport(request: Request, remote: bool) -> str:
    if remote:
        return "webrtc"  # never local through the tunnel: no live pixels over it
    lan = webrtc_sessions.video_settings().get("live_transport_on_lan")
    header = (request.headers.get(LIVE_TRANSPORT_HEADER) or "").strip().lower()
    return "webrtc" if lan == "webrtc" or header == "webrtc" else "local"


def _ice_servers() -> list[dict]:
    return assert_no_relay(turn_service.generate_ice_servers())


@router.get("/config", dependencies=GENERAL)
async def webrtc_config(request: Request):
    remote = public_exposure.is_remote_request(request)
    s = webrtc_sessions.video_settings()
    g = webrtc_sessions.go2rtc.status()
    reason = None
    if not g["installed"]:
        reason = "go2rtc is not installed on this device"
    elif g["state"] == "error" and g.get("last_error"):
        reason = f"go2rtc failed: {g['last_error']}"
    t = webrtc_sessions.timings()
    return {
        "enabled": bool(g["installed"]),
        "available": bool(g["installed"]) and reason is None,
        "reason": reason,
        "remote": remote,
        "transport": _transport(request, remote),
        "ice_servers": _ice_servers(),
        "ice_transport_policy": "all",
        "relay": False,
        **t,
        "max_sessions": s["max_video_sessions"],
        "active_sessions": webrtc_sessions.active_count(),
        "mode": s["webrtc_mode"],
    }


@router.post("/sessions", status_code=201, dependencies=OPEN)
async def create_session(body: WebRtcSessionCreate, request: Request):
    _check_watch(request, body.camera_id)
    user = _user(request) or {}
    try:
        sess, answer = await webrtc_sessions.create(
            camera_id=body.camera_id, offer_sdp=body.sdp, purpose=body.purpose,
            viewer_ip=public_exposure.client_ip(request), remote=public_exposure.is_remote_request(request),
            user=user.get("sub"))
    except SessionError as exc:
        return _error(exc)
    t = webrtc_sessions.timings()
    return JSONResponse(status_code=201, content={
        "session_id": sess.id,
        "sdp": answer,
        "heartbeat_s": t["heartbeat_s"],
        "expires_at": sess.expires_at(t["idle_timeout_s"], t["max_session_s"]),
        "codec": sess.codec,
        "transcoded": sess.transcoded,
    })


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat(session_id: str):
    expires = webrtc_sessions.heartbeat(session_id)
    if expires is None:
        return JSONResponse(status_code=404, content={"code": "session_gone",
                                                      "detail": "This live video session has ended."})
    return {"expires_at": expires}


@router.delete("/sessions/{session_id}", status_code=204)
async def close_session(session_id: str):
    await webrtc_sessions.close(session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/report")
async def report_session(session_id: str, body: WebRtcReport):
    known = webrtc_sessions.report(session_id, body.model_dump())
    return {"stored": True, "active": known}


@router.get("/sessions", dependencies=GENERAL)
async def list_sessions(request: Request):
    _require_admin(request)
    return {"sessions": webrtc_sessions.list(), "active_sessions": webrtc_sessions.active_count(),
            "max_sessions": webrtc_sessions.video_settings()["max_video_sessions"]}


@router.get("/diagnostics", dependencies=GENERAL)
async def diagnostics(request: Request, nat: bool = True):
    """Runs the STUN NAT check now (on demand only; nothing polls the internet in the background)."""
    _require_admin(request)
    return await webrtc_sessions.diagnostics(probe_nat=nat)


# --------------------------------------------------------------------------- #
# Phone app compatibility
# --------------------------------------------------------------------------- #

@router.get("/token", dependencies=GENERAL)
async def get_stream_token(camera_id: str):
    """Generates a short-lived authorization token for live WebRTC stream viewing."""
    token = auth_service.generate_stream_token(camera_id)
    return {"token": token, "camera_id": camera_id, "expires_in": settings.STREAM_TOKEN_EXPIRE_SECONDS}


@router.get("/ice-servers", dependencies=GENERAL)
async def get_ice_servers(request: Request, client_id: str = "mobile_client"):
    """STUN only (no relay exists). WebRTC reaches the store directly from any network with
    usable UDP; ``webrtc_scope`` says so. There is no MJPEG fallback through the tunnel."""
    remote = public_exposure.is_remote_request(request)
    return {
        "iceServers": _ice_servers(),
        "turn_enabled": False,
        "webrtc_scope": "internet",  # direct (STUN) from any network with usable UDP; no relay
        "remote_fallback": None,
        "transport": _transport(request, remote),
    }


@router.post("/offer", response_model=WebRtcAnswer, dependencies=OPEN)
async def exchange_webrtc_offer(offer: WebRtcOffer, request: Request):
    """Older phone app flow: offer in, answer out, as a session without heartbeats.

    The session ends when the connection closes (go2rtc drops the consumer),
    when the camera is turned off, or after max_session_s. The app should move
    to POST /sessions + heartbeat + DELETE.
    """
    _check_watch(request, offer.camera_id)
    user = _user(request) or {}
    try:
        sess, answer = await webrtc_sessions.create(
            camera_id=offer.camera_id, offer_sdp=offer.sdp, purpose="focus",
            viewer_ip=public_exposure.client_ip(request), remote=public_exposure.is_remote_request(request),
            user=user.get("sub"), heartbeat_required=False)
    except SessionError as exc:
        raise HTTPException(status_code=exc.status, detail=f"WebRTC signaling not available: {exc.reason}")
    return WebRtcAnswer(camera_id=offer.camera_id, sdp=answer, type="answer", session_id=sess.id)
