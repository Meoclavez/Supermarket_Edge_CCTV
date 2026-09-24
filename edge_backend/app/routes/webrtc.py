"""WebRTC signaling route for exchanging SDP offer/answer with go2rtc and ICE servers."""

import httpx
from fastapi import APIRouter, HTTPException, Depends
from app.config import settings
from app.models.schemas import WebRtcOffer, WebRtcAnswer
from app.services.auth_service import auth_service, general_rate_limiter
from app.services.resilience import ServiceHealthTracker
from app.services.turn_service import turn_service
from app.routes import ResilientRoute

router = APIRouter(
    prefix="/api/v1/webrtc",
    tags=["WebRTC Signaling"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute
)

GO2RTC_API_URL = "http://127.0.0.1:1984"


@router.get("/token")
async def get_stream_token(camera_id: str):
    """Generates a short-lived authorization token for live WebRTC stream viewing."""
    token = auth_service.generate_stream_token(camera_id)
    return {"token": token, "camera_id": camera_id, "expires_in": settings.STREAM_TOKEN_EXPIRE_SECONDS}


@router.get("/ice-servers")
async def get_ice_servers(client_id: str = "mobile_client"):
    """STUN, plus time-limited TURN credentials when the optional TURN relay is enabled.

    ``turn_enabled: false`` means WebRTC can only connect on the store LAN;
    clients off-site should use the MJPEG ``/stream`` over HTTPS instead.
    """
    ice_servers = turn_service.generate_ice_servers(client_id=client_id, ttl_seconds=86400)
    turn_enabled = turn_service.turn_configured()
    return {
        "iceServers": ice_servers,
        "turn_enabled": turn_enabled,
        "webrtc_scope": "internet" if turn_enabled else "lan_only",
        "remote_fallback": "/stream",
    }


@router.post("/offer", response_model=WebRtcAnswer)
async def exchange_webrtc_offer(offer: WebRtcOffer):
    """Exchanges SDP Offer from client with local go2rtc gateway to establish WebRTC P2P stream."""
    stream_name = offer.camera_id

    async with httpx.AsyncClient() as client:
        try:
            go2rtc_res = await client.post(
                f"{GO2RTC_API_URL}/api/webrtc",
                params={"src": stream_name},
                headers={"Content-Type": "application/sdp"},
                content=offer.sdp,
                timeout=5.0
            )

            # A real negotiation is the only go2rtc check the health endpoint
            # reports; until one happens go2rtc stays NOT_CHECKED there.
            if go2rtc_res.status_code != 200:
                ServiceHealthTracker.report_status(
                    "go2rtc", ServiceHealthTracker.DEGRADED, f"WebRTC negotiation HTTP {go2rtc_res.status_code}"
                )
                raise HTTPException(
                    status_code=go2rtc_res.status_code,
                    detail=f"go2rtc WebRTC negotiation error: {go2rtc_res.text}"
                )

            ServiceHealthTracker.report_status("go2rtc", ServiceHealthTracker.HEALTHY)
            answer_sdp = go2rtc_res.text
            return WebRtcAnswer(camera_id=stream_name, sdp=answer_sdp, type="answer")

        except httpx.RequestError as exc:
            ServiceHealthTracker.report_status(
                "go2rtc", ServiceHealthTracker.FAILED,
                f"not reachable at {GO2RTC_API_URL} ({exc.__class__.__name__})",
            )
            # No gateway means no negotiation. This used to hand back a
            # hand-written SDP "answer" that no peer could ever connect to.
            raise HTTPException(
                status_code=501,
                detail=(
                    "WebRTC signaling not available: go2rtc gateway is not reachable "
                    f"at {GO2RTC_API_URL} ({exc.__class__.__name__}). Use the MJPEG /stream endpoint."
                ),
            )
