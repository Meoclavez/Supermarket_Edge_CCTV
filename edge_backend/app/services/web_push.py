"""Web Push to installed dashboard web apps (Android Chrome, iPhone Safari 16.4+).

The phone's browser gives the dashboard a *subscription*: an endpoint on its
vendor's push service (fcm.googleapis.com for Chrome, web.push.apple.com for
Safari, ...) plus two keys. To alert the phone this box POSTs an encrypted
message to that endpoint; the push service wakes the phone and the dashboard's
service worker (``static/sw.js``) shows the notification.

* **Encryption (RFC 8291, aes128gcm):** the body is end-to-end encrypted to the
  phone's key, so Google/Apple carry it without being able to read it.
* **VAPID (RFC 8292):** every request carries an ES256 JWT signed with this
  box's P-256 key. A subscription is bound to that public key, so only this
  box can push to it. The key is created once and stored encrypted as the
  named secret ``web_push_vapid`` (never leaves the box; the VPS watchdog only
  gets requests that are already signed and encrypted, see
  ``offline_watchdog.py``).

No Firebase project, no app-store account and no extra Python package:
``cryptography`` (already a dependency) does ECDH, HKDF, AES-GCM and ECDSA.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

from app.config import settings

logger = logging.getLogger("edge.web_push")

VAPID_SECRET_NAME = "web_push_vapid"
RECORD_SIZE = 4096
# Push services accept 4096 bytes of body: 86 header + 16 GCM tag + 1 delimiter.
MAX_PLAINTEXT = RECORD_SIZE - 86 - 16 - 1
HTTP_TIMEOUT_S = 10.0
# RFC 8292: a JWT must expire within 24 h of the request.
MAX_VAPID_TTL_S = 24 * 3600

# Only real push services. A subscription endpoint anywhere else is refused,
# so a signed-in user cannot make this box (or the VPS watchdog) POST to an
# arbitrary URL. Matches the VPS allowlist (docs/OFFLINE_WATCHDOG_CONTRACT.md).
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com")
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")

# Tests replace this with httpx.MockTransport; None = real network.
http_transport: Optional[httpx.AsyncBaseTransport] = None


class WebPushError(ValueError):
    """A subscription or payload this module cannot use (message is operator-readable)."""


# --------------------------------------------------------------------------- encoding helpers

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    s = "".join(str(text or "").split())
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def push_host_allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(str(endpoint or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return host in PUSH_HOSTS or any(host.endswith(sfx) for sfx in PUSH_HOST_SUFFIXES)


def validate_subscription(endpoint: Any, p256dh: Any, auth: Any) -> Dict[str, str]:
    """Normalise a browser PushSubscription; raises WebPushError."""
    endpoint = str(endpoint or "").strip()
    if len(endpoint) > 2048 or not push_host_allowed(endpoint):
        raise WebPushError("This browser's push service is not supported (expected Google, Apple, "
                           "Mozilla or Microsoft over https).")
    try:
        ua_pub = b64url_decode(str(p256dh or ""))
        secret = b64url_decode(str(auth or ""))
    except (ValueError, TypeError):
        raise WebPushError("The subscription keys are not valid base64url.")
    if len(ua_pub) != 65 or ua_pub[0] != 4:
        raise WebPushError("The subscription's p256dh key is not an uncompressed P-256 point.")
    if len(secret) != 16:
        raise WebPushError("The subscription's auth secret must be 16 bytes.")
    _load_public(ua_pub)  # raises on a point not on the curve
    return {"endpoint": endpoint, "p256dh": b64url(ua_pub), "auth": b64url(secret)}


# --------------------------------------------------------------------------- RFC 8291

def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _load_public(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise WebPushError("The subscription's p256dh key is not a valid P-256 point.")


def _public_bytes(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(plaintext: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None,
            as_private=None) -> bytes:
    """aes128gcm body for one push message (RFC 8291 section 3, one record).

    ``salt`` and ``as_private`` (the sender's one-off ECDH key) are random per
    message; tests pass the RFC's values to check against its example.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(plaintext) > MAX_PLAINTEXT:
        raise WebPushError(f"push payload is {len(plaintext)} bytes; the limit is {MAX_PLAINTEXT}")
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    if as_private is None:
        as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private.public_key())
    salt = os.urandom(16) if salt is None else salt

    ecdh_secret = as_private.exchange(ec.ECDH(), _load_public(ua_public))
    # HKDF-SHA256 with a 32-byte output is one HMAC round each for extract and expand.
    prk_key = _hmac(auth_secret, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hmac(prk_key, key_info + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


# --------------------------------------------------------------------------- VAPID (RFC 8292)

_vapid_lock = threading.Lock()
_vapid_cache: Dict[str, Any] = {}


def _vapid_key():
    """This box's VAPID private key, created and stored on first use."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.services.secret_store import get_named_secret, set_named_secret

    storage = str(settings.STORAGE_DIR)
    with _vapid_lock:
        cached = _vapid_cache.get(storage)
        if cached is not None:
            return cached
        raw = get_named_secret(settings.STORAGE_DIR, VAPID_SECRET_NAME, settings.NVR_CREDENTIAL_KEY)
        key = None
        if raw:
            try:
                key = serialization.load_pem_private_key(raw, password=None)
            except ValueError as exc:
                logger.error("Stored Web Push key is unreadable (%s); creating a new one. "
                             "Every phone must enable alerts again.", exc)
        if key is None:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            set_named_secret(settings.STORAGE_DIR, VAPID_SECRET_NAME, pem, settings.NVR_CREDENTIAL_KEY)
            logger.info("Created this box's Web Push (VAPID) key")
        _vapid_cache[storage] = key
        return key


def reset_cache() -> None:
    with _vapid_lock:
        _vapid_cache.clear()


def vapid_public_key() -> str:
    """The applicationServerKey browsers subscribe with (base64url, 65 bytes)."""
    return b64url(_public_bytes(_vapid_key().public_key()))


def vapid_key_id() -> str:
    """Short fingerprint of the VAPID public key, stored with each subscription."""
    return hashlib.sha256(vapid_public_key().encode("ascii")).hexdigest()[:16]


def vapid_contact() -> str:
    """The JWT ``sub``: a URL or mailto the push service can use to reach the sender.

    Apple refuses a missing or malformed ``sub``. The store's public address is
    used; ``WEB_PUSH_CONTACT`` overrides it.
    """
    explicit = (os.environ.get("WEB_PUSH_CONTACT") or "").strip()
    if explicit.startswith(("mailto:", "https://")):
        return explicit
    try:
        from app.services.remote_access_service import remote_access_service

        url = remote_access_service.public_url()
        if url and url.startswith("https://"):
            return url.rstrip("/")
    except Exception:
        pass
    return "https://localhost"


def vapid_authorization(endpoint: str, expires_at: int) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(expires_at), "sub": vapid_contact()}
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + b64url(json.dumps(claims, separators=(",", ":")).encode()))
    der = _vapid_key().sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={vapid_public_key()}"


# --------------------------------------------------------------------------- requests

def encode_payload(payload: Dict[str, Any]) -> bytes:
    """JSON for the service worker, trimmed to fit one push message."""
    data = dict(payload)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    while len(raw) > MAX_PLAINTEXT and len(str(data.get("body") or "")) > 40:
        data["body"] = str(data["body"])[: max(40, len(str(data["body"])) - (len(raw) - MAX_PLAINTEXT) - 8)] + "…"
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_PLAINTEXT:
        raise WebPushError(f"push payload is {len(raw)} bytes; the limit is {MAX_PLAINTEXT}")
    return raw


def build_request(sub: Dict[str, str], payload: Dict[str, Any], *, ttl: int = 3600,
                  urgency: str = "high", topic: Optional[str] = None,
                  vapid_ttl_s: int = 12 * 3600) -> Dict[str, Any]:
    """Everything needed to POST one push later: endpoint, headers, body, not_after."""
    if urgency not in ("very-low", "low", "normal", "high"):
        raise WebPushError(f"bad urgency {urgency!r}")
    not_after = int(time.time()) + max(60, min(int(vapid_ttl_s), MAX_VAPID_TTL_S - 60))
    body = encrypt(encode_payload(payload), sub["p256dh"], sub["auth"])
    headers = {
        "Authorization": vapid_authorization(sub["endpoint"], not_after),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(max(0, int(ttl))),
        "Urgency": urgency,
    }
    if topic:
        # RFC 8030: at most 32 characters of the base64url alphabet.
        clean = "".join(ch for ch in topic if ch.isalnum() or ch in "-_")[:32]
        if clean:
            headers["Topic"] = clean
    return {"endpoint": sub["endpoint"], "headers": headers, "body": body, "not_after": not_after}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=http_transport, timeout=HTTP_TIMEOUT_S)


async def send(sub: Dict[str, str], payload: Dict[str, Any], *, ttl: int = 3600, urgency: str = "high",
               topic: Optional[str] = None) -> Dict[str, Any]:
    """Push one message. Returns {"status": "sent"|"gone"|"failed", "error"?, "http_status"?}.

    ``gone`` (404/410) means the phone removed the subscription (app deleted,
    notifications turned off, browser data cleared): the caller deletes it.
    """
    try:
        req = build_request(sub, payload, ttl=ttl, urgency=urgency, topic=topic)
    except WebPushError as exc:
        return {"status": "failed", "error": str(exc)}
    host = urlsplit(req["endpoint"]).hostname
    attempts = 0
    while True:
        attempts += 1
        try:
            async with _client() as client:
                resp = await client.post(req["endpoint"], content=req["body"], headers=req["headers"])
        except httpx.HTTPError as exc:
            if attempts < 2:
                await asyncio.sleep(0.5)
                continue
            return {"status": "failed", "error": f"{host}: {type(exc).__name__}: {exc}"[:300]}
        code = resp.status_code
        if code in (200, 201, 202):
            return {"status": "sent", "http_status": code}
        if code in (404, 410):
            return {"status": "gone", "http_status": code, "error": f"{host}: HTTP {code} subscription expired"}
        if code in (429, 500, 502, 503, 504) and attempts < 2:
            await asyncio.sleep(1.0)
            continue
        detail = (resp.text or "").strip().replace("\n", " ")[:160]
        return {"status": "failed", "http_status": code, "error": f"{host}: HTTP {code} {detail}".strip()}
