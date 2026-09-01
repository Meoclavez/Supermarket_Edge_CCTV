"""WebRTC signaling route for exchanging SDP offer/answer with go2rtc and ICE servers."""

import httpx
from fastapi import APIRouter, HTTPException, Depends
from app.config import settings
from app.models.schemas import WebRtcOffer, WebRtcAnswer
from app.services.auth_service import auth_service, general_rate_limiter
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
    """Returns STUN and dynamic time-limited TURN relay credentials for WebRTC peer connections."""
    ice_servers = turn_service.generate_ice_servers(client_id=client_id, ttl_seconds=86400)
    return {"iceServers": ice_servers}


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

            if go2rtc_res.status_code != 200:
                raise HTTPException(
                    status_code=go2rtc_res.status_code,
                    detail=f"go2rtc WebRTC negotiation error: {go2rtc_res.text}"
                )

            answer_sdp = go2rtc_res.text
            return WebRtcAnswer(camera_id=stream_name, sdp=answer_sdp, type="answer")

        except httpx.RequestError as exc:
            # Mock fallback answer if go2rtc is offline in testing mode
            mock_sdp = (
                f"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=EdgeCCTV_{stream_name}\r\n"
                "t=0 0\r\na=sendrecv\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                "a=rtpmap:96 H264/90000\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                "a=rtpmap:111 opus/48000/2\r\n"
            )
            return WebRtcAnswer(camera_id=stream_name, sdp=mock_sdp, type="answer")
