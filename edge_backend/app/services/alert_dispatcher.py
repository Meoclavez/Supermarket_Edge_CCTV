"""One path for every alert: log it, show it on the dashboard, push it to phones.

``dispatch(event_type, severity, title, body, data)``:

1. writes the alert to ``security_events`` (when the camera is known and the
   type is a valid :class:`EventType`);
2. broadcasts it on the dashboard/app websocket (``alert_hub``);
3. pushes it to every non-revoked paired phone whose alert prefs match
   (event types, minimum severity, cameras, quiet hours), unless the camera is
   muted or the same alert type fired on that camera within
   ``CAMERA_ALERT_COOLDOWN_SEC``.

It returns a delivery report stating what really happened per phone. With no
push provider configured the outcome is ``not_configured``; nothing is ever
reported as delivered that was not accepted by the provider.

Provider: Firebase Cloud Messaging HTTP v1. The legacy FCM API (server key)
is retired by Google and APNs is reached *through* FCM, so iOS phones need no
separate Apple key on this server. The service-account JSON is stored
encrypted as the named secret ``fcm_service_account``; an OAuth2 access token
is minted from it with an RS256 JWT signed by ``cryptography`` and cached
until shortly before it expires.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

import httpx
from fastapi.encoders import jsonable_encoder

from app.config import settings

logger = logging.getLogger("edge.alerts")

FCM_SECRET_NAME = "fcm_service_account"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
ANDROID_CHANNEL_ID = "loss_prevention_alerts"
APNS_CATEGORY = "LOSS_PREVENTION_ALERT"
HTTP_TIMEOUT_S = 8.0

# Tests replace this with httpx.MockTransport; None = real network.
http_transport: Optional[httpx.AsyncBaseTransport] = None


class PushError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- service account

REQUIRED_SA_FIELDS = ("type", "project_id", "client_email", "private_key")


def validate_service_account(raw: Any) -> dict:
    """Parse and check a Firebase service-account JSON. Raises ValueError.

    Error messages never include the private key.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("The file is not valid JSON.")
    if not isinstance(raw, dict):
        raise ValueError("The service account must be a JSON object.")
    missing = [k for k in REQUIRED_SA_FIELDS if not raw.get(k)]
    if missing:
        raise ValueError(f"Missing field(s): {', '.join(missing)}. Download the key from Firebase console > "
                         f"Project settings > Service accounts > Generate new private key.")
    if raw.get("type") != "service_account":
        raise ValueError(f"type is {raw.get('type')!r}; expected 'service_account' (this looks like a "
                         f"different kind of Google credential, e.g. google-services.json).")
    if "@" not in str(raw["client_email"]):
        raise ValueError("client_email is not an e-mail address.")
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(str(raw["private_key"]).encode("utf-8"), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("private_key is not an RSA key.")
    except ValueError as exc:
        if "RSA" in str(exc):
            raise
        raise ValueError("private_key is not a readable PEM private key.")
    token_uri = str(raw.get("token_uri") or DEFAULT_TOKEN_URI)
    if not token_uri.startswith("https://"):
        raise ValueError("token_uri must be an https URL.")
    return {
        "type": "service_account",
        "project_id": str(raw["project_id"]),
        "client_email": str(raw["client_email"]),
        "private_key": str(raw["private_key"]),
        "private_key_id": str(raw.get("private_key_id") or ""),
        "token_uri": token_uri,
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt_rs256(claims: dict, private_key_pem: str, kid: Optional[str] = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    header = {"alg": "RS256", "typ": "JWT"}
    if kid:
        header["kid"] = kid
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    key = load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


# --------------------------------------------------------------------------- FCM v1

class FcmV1Provider:
    name = "fcm"

    def __init__(self) -> None:
        self._sa: Optional[dict] = None
        self._sa_loaded = False
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._token_lock_loop = None

    # ---- configuration
    def invalidate(self) -> None:
        self._sa = None
        self._sa_loaded = False
        self._token = None
        self._token_exp = 0.0

    def service_account(self) -> Optional[dict]:
        if not self._sa_loaded:
            from app.services.secret_store import get_named_secret

            raw = get_named_secret(settings.STORAGE_DIR, FCM_SECRET_NAME, settings.NVR_CREDENTIAL_KEY)
            self._sa = None
            if raw:
                try:
                    self._sa = validate_service_account(raw)
                except ValueError as exc:
                    logger.error("Stored FCM service account is unusable: %s", exc)
            self._sa_loaded = True
        return self._sa

    def configured(self) -> bool:
        return self.service_account() is not None

    def status(self) -> dict:
        from app.services.secret_store import has_named_secret

        sa = self.service_account()
        return {
            "provider": "fcm_v1",
            "configured": sa is not None,
            "stored": has_named_secret(settings.STORAGE_DIR, FCM_SECRET_NAME),
            "project_id": sa["project_id"] if sa else None,
            "client_email": sa["client_email"] if sa else None,
            "access_token_cached": bool(self._token and time.time() < self._token_exp),
        }

    # ---- transport
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=http_transport, timeout=HTTP_TIMEOUT_S)

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._token_lock_loop is not loop:
            self._token_lock = asyncio.Lock()
            self._token_lock_loop = loop
        return self._token_lock

    async def access_token(self, force: bool = False) -> str:
        sa = self.service_account()
        if sa is None:
            raise PushError("not configured")
        async with self._lock():
            if not force and self._token and time.time() < self._token_exp - 60:
                return self._token
            now = int(time.time())
            assertion = sign_jwt_rs256(
                {"iss": sa["client_email"], "scope": FCM_SCOPE, "aud": sa["token_uri"],
                 "iat": now, "exp": now + 3600},
                sa["private_key"], sa.get("private_key_id") or None,
            )
            try:
                async with self._client() as client:
                    resp = await client.post(sa["token_uri"], data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    })
            except httpx.HTTPError as exc:
                raise PushError(f"OAuth token request failed: {type(exc).__name__}: {exc}")
            if resp.status_code != 200:
                detail = _oauth_error(resp)
                raise PushError(f"OAuth token request rejected: HTTP {resp.status_code} {detail}",
                                status=resp.status_code, code="oauth")
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise PushError("OAuth token response had no access_token", code="oauth")
            self._token = token
            self._token_exp = time.time() + float(body.get("expires_in") or 3600)
            return token

    def build_message(self, token: str, platform: str, title: str, body: str, data: Dict[str, Any]) -> dict:
        """FCM v1 message: high priority, loss-prevention channel, default sound.

        iOS is delivered by FCM through APNs with ``interruption-level:
        time-sensitive`` (needs only the Time Sensitive capability, never the
        critical-alert entitlement). Data values are strings, as FCM requires.
        """
        str_data = _as_str_map(dict(data, title=title, body=body, type=APNS_CATEGORY))
        thread = str(data.get("camera_id") or "store")
        return {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": str_data,
                "android": {
                    "priority": "HIGH",
                    "ttl": "3600s",
                    "notification": {
                        "channel_id": ANDROID_CHANNEL_ID,
                        "sound": "default",
                        "tag": f"{data.get('camera_id') or 'store'}:{data.get('event_type') or ''}",
                    },
                },
                "apns": {
                    "headers": {"apns-priority": "10", "apns-push-type": "alert"},
                    "payload": {
                        "aps": {
                            "alert": {"title": title, "body": body},
                            "sound": "default",
                            "interruption-level": "time-sensitive",
                            "category": APNS_CATEGORY,
                            "thread-id": thread,
                            "mutable-content": 1,
                        },
                    },
                },
            }
        }

    async def send(self, token: str, platform: str, title: str, body: str, data: Dict[str, Any]) -> dict:
        """Returns {"status": "sent"|"unregistered"|"failed", "message_id"?, "error"?}."""
        sa = self.service_account()
        if sa is None:
            return {"status": "not_configured"}
        url = FCM_SEND_URL.format(project_id=sa["project_id"])
        message = self.build_message(token, platform, title, body, data)
        attempts = 0
        refreshed = False
        while True:
            attempts += 1
            try:
                bearer = await self.access_token()
            except PushError as exc:
                return {"status": "failed", "error": str(exc)}
            try:
                async with self._client() as client:
                    resp = await client.post(url, json=message, headers={"Authorization": f"Bearer {bearer}"})
            except httpx.HTTPError as exc:
                if attempts < 2:
                    await asyncio.sleep(0.5)
                    continue
                return {"status": "failed", "error": f"FCM request failed: {type(exc).__name__}: {exc}"}
            if resp.status_code == 200:
                return {"status": "sent", "message_id": (resp.json() or {}).get("name")}
            code, msg = _fcm_error(resp)
            if code == "UNREGISTERED" or (resp.status_code == 404 and code in (None, "NOT_FOUND")):
                return {"status": "unregistered", "error": f"HTTP {resp.status_code} {code or ''} {msg}".strip()}
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._token = None
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempts < 2:
                await asyncio.sleep(1.0)
                continue
            return {"status": "failed", "error": f"HTTP {resp.status_code} {code or ''} {msg}".strip()}


def _oauth_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return f"{body.get('error', '')} {body.get('error_description', '')}".strip()[:200]
    except Exception:
        return resp.text[:200]


def _fcm_error(resp: httpx.Response) -> tuple:
    try:
        err = (resp.json() or {}).get("error") or {}
    except Exception:
        return None, resp.text[:200]
    code = None
    for d in err.get("details") or []:
        if isinstance(d, dict) and d.get("errorCode"):
            code = d["errorCode"]
            break
    return code or err.get("status"), str(err.get("message") or "")[:200]


def _as_str_map(data: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        out[str(key)] = value if isinstance(value, str) else json.dumps(jsonable_encoder(value))
    return out


fcm_provider = FcmV1Provider()


# --------------------------------------------------------------------------- dispatcher

class AlertDispatcher:
    RING_SIZE = 50

    def __init__(self) -> None:
        self._last_push: Dict[tuple, float] = {}
        self.recent: Deque[dict] = collections.deque(maxlen=self.RING_SIZE)

    def reset(self) -> None:
        self._last_push.clear()
        self.recent.clear()

    # ---- public API
    async def dispatch(self, event_type: str, severity: str, title: str, body: str, data: Optional[dict] = None,
                       *, persist: bool = True, broadcast: bool = True, bypass_cooldown: bool = False) -> dict:
        """Log, broadcast and push one alert.

        ``bypass_cooldown`` is for alerts a person asked for explicitly (e.g.
        "staff sent" on a theft incident): the per-camera cooldown exists to
        stop automatic alerts repeating, not to swallow an operator's action.
        Phone preferences and camera mutes still apply.
        """
        from app.models.schemas import EventSeverity, EventType
        from app.services import device_identity

        data = dict(data or {})
        et = str(event_type or data.get("event_type") or EventType.THEFT_SUSPECTED.value)
        try:
            et_enum: Optional[EventType] = EventType(et)
        except ValueError:
            et_enum = None
            logger.warning("dispatch: unknown event type %r (not logged to the alert history)", et)
        try:
            sev = EventSeverity(str(severity or "HIGH").upper()).value
        except ValueError:
            sev = EventSeverity.HIGH.value
        ident = device_identity.get_identity()
        camera_id = data.get("camera_id")
        alert_id = data.get("alert_id") or f"evt_{uuid.uuid4().hex[:8]}"
        data.update(event_type=et, severity=sev, alert_id=alert_id,
                    device_id=ident["device_id"], device_name=ident["device_name"])
        data.setdefault("camera_id", camera_id)
        data.setdefault("incident_id", None)
        data.setdefault("timestamp", datetime.utcnow().isoformat())

        camera = await self._get_camera(camera_id) if camera_id else None
        if camera is not None:
            data.setdefault("camera_name", camera.name)
            data.setdefault("location", camera.location)

        logged = False
        if persist and camera is not None and et_enum is not None:
            logged = await self._log_alert(alert_id, camera, et, sev, title, body, data)

        ws_clients = 0
        if broadcast:
            from app.services.notification_service import alert_hub

            ws_clients = await alert_hub.broadcast_event(jsonable_encoder({
                "id": alert_id, "title": title, "body": body, **data,
            }))
        push = await self._push(title, body, data, camera, bypass_cooldown=bypass_cooldown)
        return {
            "alert_id": alert_id,
            "event_type": et,
            "severity": sev,
            "logged": logged,
            "websocket_clients": ws_clients,
            "push": push,
        }

    async def send_test(self, paired_device_id: str) -> dict:
        """Send a clearly-labelled test push to one phone (prefs, mute and cooldown ignored)."""
        from app.database import async_session_factory
        from app.services import device_identity, pairing_service

        ident = device_identity.get_identity()
        async with async_session_factory() as session:
            row = await pairing_service.get_device(session, paired_device_id)
            if row is None or row.revoked_at is not None:
                raise LookupError("paired device not found")
            target = _Target(row)
        alert_id = f"test_{uuid.uuid4().hex[:8]}"
        data = {"event_type": "TEST", "severity": "INFO", "alert_id": alert_id, "incident_id": None,
                "camera_id": None, "device_id": ident["device_id"], "device_name": ident["device_name"],
                "test": True, "timestamp": datetime.utcnow().isoformat()}
        title = f"Test alert - {ident['device_name']}"
        body = "Push notifications from this edge device are working."
        result = await self._deliver(target, title, body, data, test=True)
        return {"alert_id": alert_id, "provider": fcm_provider.status()["provider"], **result}

    # ---- internals
    async def _get_camera(self, camera_id: str):
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models.db_models import CameraModel

        try:
            async with async_session_factory() as session:
                return (await session.execute(select(CameraModel).where(CameraModel.id == camera_id))).scalar_one_or_none()
        except Exception as exc:
            logger.warning("Camera lookup for alert failed: %s", exc)
            return None

    async def _log_alert(self, alert_id, camera, event_type, severity, title, body, data) -> bool:
        from app.database import async_session_factory
        from app.models.db_models import SecurityEventModel

        confidence = data.get("confidence")
        measured = isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        meta = {k: v for k, v in data.items() if k not in ("camera_id", "event_type", "severity")}
        meta.update({"title": title, "body": body, "confidence_measured": measured})
        try:
            async with async_session_factory() as session:
                session.add(SecurityEventModel(
                    id=alert_id, camera_id=camera.id, camera_name=camera.name, location=camera.location,
                    event_type=event_type, severity=severity,
                    # NOT NULL column; an unmeasured confidence is flagged in
                    # metadata and served back as null by the events API.
                    confidence=float(confidence) if measured else 0.0,
                    timestamp=datetime.utcnow(),
                    snapshot_url=data.get("snapshot_url") or None,
                    clip_url=data.get("clip_url") or None,
                    metadata_json=jsonable_encoder(meta),
                    acknowledged=False,
                ))
                await session.commit()
            return True
        except Exception as exc:
            logger.error("Could not log alert %s: %s", alert_id, exc)
            return False

    async def _targets(self) -> List["_Target"]:
        from sqlalchemy import select
        from app.database import async_session_factory
        from app.models.db_models import PairedDeviceModel

        async with async_session_factory() as session:
            rows = (await session.execute(
                select(PairedDeviceModel).where(PairedDeviceModel.revoked_at.is_(None))
            )).scalars().all()
            return [_Target(r) for r in rows]

    async def _push(self, title: str, body: str, data: Dict[str, Any], camera,
                    bypass_cooldown: bool = False) -> dict:
        from app.services.pairing_service import prefs_filter_reason

        result: Dict[str, Any] = {"provider": "fcm_v1", "devices": 0, "targets": 0, "sent": 0, "failed": 0,
                                  "not_configured": 0, "unregistered": 0, "filtered": 0, "no_token": 0,
                                  "skipped": None, "results": []}
        if camera is not None and camera.muted_until and camera.muted_until > datetime.utcnow():
            result["skipped"] = f"camera alerts muted until {camera.muted_until.isoformat()}Z"
            return result
        key = (data.get("camera_id"), data.get("event_type"))
        now = time.monotonic()
        last = self._last_push.get(key)
        cooldown = float(settings.CAMERA_ALERT_COOLDOWN_SEC)
        if not bypass_cooldown and last is not None and now - last < cooldown:
            result["skipped"] = f"cooldown ({cooldown:.0f}s per camera and alert type)"
            return result

        try:
            targets = await self._targets()
        except Exception as exc:
            result["skipped"] = f"paired devices unavailable: {exc}"
            return result
        result["devices"] = len(targets)
        if not targets:
            result["skipped"] = "no paired phones"
            return result

        to_send: List[_Target] = []
        for t in targets:
            reason = prefs_filter_reason(t.prefs, data["event_type"], data["severity"], data.get("camera_id"))
            if reason:
                result["filtered"] += 1
                result["results"].append({"paired_device_id": t.id, "name": t.name, "status": "filtered",
                                          "reason": reason})
            elif not t.push_token:
                result["no_token"] += 1
                result["results"].append({"paired_device_id": t.id, "name": t.name, "status": "no_token"})
            else:
                to_send.append(t)
        result["targets"] = len(to_send)
        if not to_send:
            return result
        self._last_push[key] = now
        outcomes = await asyncio.gather(*(self._deliver(t, title, body, data) for t in to_send))
        for o in outcomes:
            result["results"].append(o)
            status = o["status"]
            if status in ("sent", "failed", "not_configured", "unregistered"):
                result[status] += 1
        return result

    async def _deliver(self, target: "_Target", title: str, body: str, data: Dict[str, Any],
                       test: bool = False) -> dict:
        entry = {"paired_device_id": target.id, "name": target.name, "platform": target.platform}
        if not target.push_token:
            outcome = {"status": "no_token", "error": "the phone has not registered a push token"}
        elif target.push_provider not in (None, "fcm"):
            outcome = {"status": "failed", "error": f"unsupported push provider {target.push_provider}"}
        else:
            try:
                outcome = await fcm_provider.send(target.push_token, target.platform, title, body, data)
            except Exception as exc:  # never let one phone break the fan-out
                logger.exception("push to %s failed", target.id)
                outcome = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        entry.update(outcome)
        if outcome["status"] == "unregistered":
            await self._clear_token(target.id, target.push_token)
        await self._record(target.id, entry)
        self.recent.appendleft({
            "at": datetime.utcnow().isoformat() + "Z",
            "alert_id": data.get("alert_id"),
            "event_type": data.get("event_type"),
            "severity": data.get("severity"),
            "camera_id": data.get("camera_id"),
            "title": title,
            "test": test,
            **{k: v for k, v in entry.items() if k in ("paired_device_id", "name", "platform", "status", "error")},
        })
        level = logging.INFO if outcome["status"] in ("sent", "not_configured") else logging.WARNING
        logger.log(level, "push %s -> %s (%s): %s %s", data.get("alert_id"), target.name, target.id,
                   outcome["status"], outcome.get("error") or "")
        return entry

    async def _clear_token(self, pd: str, token: Optional[str]) -> None:
        from sqlalchemy import update
        from app.database import async_session_factory
        from app.models.db_models import PairedDeviceModel

        try:
            async with async_session_factory() as session:
                await session.execute(
                    update(PairedDeviceModel)
                    .where(PairedDeviceModel.id == pd, PairedDeviceModel.push_token == token)
                    .values(push_token=None, push_provider=None)
                )
                await session.commit()
            logger.warning("FCM says the push token of %s is no longer registered; cleared it", pd)
        except Exception as exc:
            logger.error("Could not clear dead push token for %s: %s", pd, exc)

    async def _record(self, pd: str, entry: dict) -> None:
        from sqlalchemy import update
        from app.database import async_session_factory
        from app.models.db_models import PairedDeviceModel

        try:
            async with async_session_factory() as session:
                await session.execute(
                    update(PairedDeviceModel).where(PairedDeviceModel.id == pd).values(
                        last_push_status=entry["status"], last_push_at=datetime.utcnow(),
                        last_push_error=(entry.get("error") or None) and str(entry["error"])[:500],
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.debug("could not record push status for %s: %s", pd, exc)


class _Target:
    __slots__ = ("id", "name", "platform", "push_token", "push_provider", "prefs")

    def __init__(self, row) -> None:
        self.id = row.id
        self.name = row.name
        self.platform = row.platform
        self.push_token = row.push_token
        self.push_provider = row.push_provider
        self.prefs = row.alert_prefs


alert_dispatcher = AlertDispatcher()


async def dispatch(event_type: str, severity: str, title: str, body: str, data: Optional[dict] = None) -> dict:
    """Module-level convenience: ``alert_dispatcher.dispatch(...)``."""
    return await alert_dispatcher.dispatch(event_type, severity, title, body, data)
