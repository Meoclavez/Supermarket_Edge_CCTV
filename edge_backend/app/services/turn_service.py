"""ICE server list for WebRTC clients: STUN only, never a relay.

Remote live video is direct peer-to-peer (docs/REMOTE_VIDEO_CONTRACT.md, rule 1):
there is no TURN server anywhere in this system and the backend never emits a
``turn:``/``turns:`` URL. The STUN servers are the operator's Settings ->
Online access list (``stun_servers``, default ``stun:stun.ikorex.com.au:3478``),
used for address discovery only.

The module keeps its historical name because the phone app's
``GET /api/v1/webrtc/ice-servers`` is served from it. ``TURN_ENABLED`` and the
``COTURN_*`` settings are ignored (a warning is logged once if TURN is asked for).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.config import settings

logger = logging.getLogger("edge.webrtc")

_warned_turn = False


def stun_servers() -> List[str]:
    """Configured ``stun:host:port`` URLs (validated on save; relays filtered again here)."""
    try:
        from app.services.remote_access_service import remote_access_service

        urls = remote_access_service.webrtc_settings().get("stun_servers") or []
    except Exception:
        from app.services.remote_access_service import DEFAULT_STUN_SERVERS

        urls = list(DEFAULT_STUN_SERVERS)
    return [u for u in urls if isinstance(u, str) and u.lower().startswith("stun:")]


def assert_no_relay(ice_servers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Raise if any URL is not ``stun:`` (the last guard before a list leaves the backend)."""
    for entry in ice_servers:
        for url in entry.get("urls") or []:
            if not str(url).lower().startswith("stun:"):
                raise ValueError(f"refusing to emit a non-STUN ICE server URL: {str(url).split(':', 1)[0]}:")
    return ice_servers


class TurnCredentialService:
    """Kept for the import path; issues STUN entries only."""

    def turn_configured(self) -> bool:
        global _warned_turn
        if settings.TURN_ENABLED and not _warned_turn:
            _warned_turn = True
            logger.warning("TURN_ENABLED=true is ignored: remote video is direct peer-to-peer and no relay is "
                           "ever offered (docs/REMOTE_VIDEO_CONTRACT.md).")
        return False

    def generate_ice_servers(self, client_id: str = "", ttl_seconds: int = 0) -> List[Dict[str, Any]]:
        self.turn_configured()
        urls = stun_servers()
        return assert_no_relay([{"urls": urls}] if urls else [])


turn_service = TurnCredentialService()
