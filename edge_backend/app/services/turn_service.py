"""Ephemeral TURN credential generator implementing RFC 5766 / REST API authentication."""

import time
import hmac
import hashlib
import base64
from typing import Dict, Any, List
from app.config import settings


class TurnCredentialService:
    def __init__(self, secret: str, realm: str, turn_host: str, turn_port: int = 3478):
        self.secret = secret
        self.realm = realm
        self.turn_host = turn_host
        self.turn_port = turn_port

    def generate_ice_servers(self, client_id: str = "mobile_client", ttl_seconds: int = 86400) -> List[Dict[str, Any]]:
        """Generates dynamic ICE server dictionary with STUN + HMAC-SHA1 authenticated TURN."""
        expiry = int(time.time()) + ttl_seconds
        username = f"{expiry}:{client_id}"

        # Calculate HMAC-SHA1 signature using static auth secret
        digest = hmac.new(
            self.secret.encode("utf-8"),
            username.encode("utf-8"),
            hashlib.sha1
        ).digest()
        password = base64.b64encode(digest).decode("utf-8")

        return [
            {
                "urls": [
                    "stun:stun.l.google.com:19302",
                    f"stun:{self.turn_host}:{self.turn_port}"
                ]
            },
            {
                "urls": [
                    f"turn:{self.turn_host}:{self.turn_port}?transport=udp",
                    f"turn:{self.turn_host}:{self.turn_port}?transport=tcp"
                ],
                "username": username,
                "credential": password
            }
        ]


turn_service = TurnCredentialService(
    secret=settings.COTURN_SECRET,
    realm=settings.COTURN_REALM,
    turn_host=settings.COTURN_PUBLIC_IP,
    turn_port=settings.COTURN_PORT
)
