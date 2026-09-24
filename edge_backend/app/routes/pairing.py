"""Device identity, phone pairing and push-notification settings.

* ``/api/v1/device/identity``  -- who this edge device is (GET is public).
* ``/api/v1/pairing/...``      -- pairing sessions (QR/code), claim, paired phones.
* ``/api/v1/push/...``         -- FCM service account, test push, recent deliveries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import device_identity, pairing_service
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector

logger = logging.getLogger("edge.pairing")

device_router = APIRouter(prefix="/api/v1/device", tags=["Device identity"])
pairing_router = APIRouter(prefix="/api/v1/pairing", tags=["Phone pairing"])
push_router = APIRouter(
    prefix="/api/v1/push", tags=["Push notifications"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(general_rate_limiter)],
)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _user(request: Request) -> dict:
    return getattr(request.state, "user", None) or {}


# --------------------------------------------------------------------------- identity

class DeviceNameReq(BaseModel):
    device_name: str


@device_router.get("/identity")
async def get_identity():
    """Public: lets a phone or the remote-access check prove a URL reaches THIS device."""
    ident = await asyncio.to_thread(device_identity.get_identity)
    if not ident["device_id"]:
        raise HTTPException(status_code=503, detail="Device identity is not initialised yet.")
    remote = await asyncio.to_thread(pairing_service.remote_url)
    return {**ident, "pairing": True, "remote_url": remote}


@device_router.put("/identity", dependencies=[Depends(auth_service.verify_api_access)])
async def rename_device(req: DeviceNameReq):
    try:
        ident = await asyncio.to_thread(device_identity.set_device_name, req.device_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    remote = await asyncio.to_thread(pairing_service.remote_url)
    return {**ident, "pairing": True, "remote_url": remote}


# --------------------------------------------------------------------------- pairing

class PhoneReq(BaseModel):
    name: str = ""
    platform: str
    app_instance_id: str
    push_provider: Optional[str] = None
    push_token: Optional[str] = None


class ClaimReq(BaseModel):
    device_id: str
    code: str
    phone: PhoneReq


class QuietHours(BaseModel):
    start: str
    end: str


class AlertPrefs(BaseModel):
    event_types: Optional[List[str]] = None
    min_severity: str = "INFO"
    camera_ids: Optional[List[str]] = None
    quiet_hours: Optional[QuietHours] = None


class DevicePatchReq(BaseModel):
    name: Optional[str] = None
    alert_prefs: Optional[AlertPrefs] = None


class PushTokenReq(BaseModel):
    push_provider: Optional[str] = None
    push_token: Optional[str] = None


@pairing_router.post("/sessions", dependencies=[Depends(auth_service.verify_api_access)])
async def create_pairing_session(request: Request, session: AsyncSession = Depends(get_db)):
    """Open a single-use pairing session (10 min). Any older unused session is closed."""
    try:
        return await pairing_service.create_session(session, _user(request).get("sub"), request)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@pairing_router.delete("/sessions/{session_id}", dependencies=[Depends(auth_service.verify_api_access)])
async def cancel_pairing_session(session_id: str, session: AsyncSession = Depends(get_db)):
    cancelled = await pairing_service.cancel_session(session, session_id)
    return {"cancelled": cancelled}


@pairing_router.post("/claim", dependencies=[Depends(general_rate_limiter)])
async def claim(req: ClaimReq, request: Request, session: AsyncSession = Depends(get_db)):
    """Unauthenticated: a phone exchanges the one-time code for tokens bound to this device."""
    ip = _ip(request)
    intrusion_detector.check_lockout(ip)
    try:  # validate before the code is spent
        pairing_service._clean_phone(req.phone.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    ident = device_identity.get_identity()
    if not ident["device_id"] or req.device_id.strip().lower() != ident["device_id"].lower():
        # A different edge box (e.g. another store on the same network): tell
        # the phone plainly so it does not pair with the wrong one.
        return JSONResponse(status_code=409, content={"detail": "device_mismatch"})
    row = await pairing_service.consume_code(session, req.code)
    if row is None:
        intrusion_detector.record_failure(ip)
        if pairing_service.record_global_failure() >= pairing_service.GLOBAL_FAILURE_LIMIT:
            logger.error("Too many wrong pairing codes; closing every open pairing session")
            await pairing_service.close_all_sessions(session)
        intrusion_detector.check_lockout(ip)
        raise HTTPException(status_code=403, detail="Pairing code is wrong, expired or already used.")
    try:
        result = await pairing_service.register_phone(session, req.phone.model_dump(), row.created_by, "code")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    row_id = row.id
    from sqlalchemy import update
    from app.models.db_models import PairingSessionModel
    await session.execute(update(PairingSessionModel).where(PairingSessionModel.id == row_id)
                          .values(paired_device_id=result["paired_device_id"]))
    await session.commit()
    intrusion_detector.record_success(ip, "pairing_code")
    return {**result, "urls": pairing_service.reachable_urls(request)}


@pairing_router.get("/devices", dependencies=[Depends(auth_service.verify_api_access)])
async def list_paired_devices(include_revoked: bool = False, session: AsyncSession = Depends(get_db)):
    from app.models.schemas import EventType

    devices = await pairing_service.list_devices(session, include_revoked)
    return {"devices": devices, "count": len(devices),
            # What alert prefs may select, so clients never hardcode the list.
            "event_types": [e.value for e in EventType],
            "severities": list(pairing_service.SEVERITY_ORDER)}


@pairing_router.put("/devices/me/push", dependencies=[Depends(auth_service.verify_api_access)])
async def update_my_push_token(req: PushTokenReq, request: Request, session: AsyncSession = Depends(get_db)):
    """The phone (authenticated with its own token) registers or clears its FCM token."""
    pd = _user(request).get("pd")
    if not pd:
        raise HTTPException(status_code=403, detail="Only a paired phone's token can update its push token.")
    try:
        device = await pairing_service.set_push_token(session, str(pd), req.push_provider, req.push_token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if device is None:
        raise HTTPException(status_code=401, detail="This phone is no longer paired.")
    return device


@pairing_router.patch("/devices/{paired_device_id}", dependencies=[Depends(auth_service.verify_api_access)])
async def patch_paired_device(paired_device_id: str, req: DevicePatchReq, session: AsyncSession = Depends(get_db)):
    given = req.model_dump(exclude_unset=True)
    prefs = given.get("alert_prefs")
    try:
        device = await pairing_service.update_device(
            session, paired_device_id, name=given.get("name"),
            alert_prefs=prefs, prefs_given="alert_prefs" in given,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if device is None:
        raise HTTPException(status_code=404, detail="Paired device not found (or revoked).")
    return device


@pairing_router.delete("/devices/{paired_device_id}", dependencies=[Depends(auth_service.verify_api_access)])
async def revoke_paired_device(paired_device_id: str, session: AsyncSession = Depends(get_db)):
    if not await pairing_service.revoke_device(session, paired_device_id):
        raise HTTPException(status_code=404, detail="Paired device not found (or already revoked).")
    return {"revoked": True, "id": paired_device_id}


# --------------------------------------------------------------------------- push settings

class TestPushReq(BaseModel):
    paired_device_id: str


def _push_status() -> dict:
    from app.services.alert_dispatcher import fcm_provider
    return fcm_provider.status()


@push_router.get("/config")
async def push_config():
    return await asyncio.to_thread(_push_status)


@push_router.put("/fcm-service-account")
async def upload_service_account(request: Request):
    """Body: the service-account JSON file from Firebase. Stored encrypted; never returned."""
    from app.services.alert_dispatcher import FCM_SECRET_NAME, fcm_provider, validate_service_account
    from app.services.secret_store import set_named_secret

    raw = await request.body()
    if len(raw) > 64 * 1024:
        raise HTTPException(status_code=413, detail="That file is too large to be a service-account key.")
    try:
        sa = validate_service_account(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    import json as _json
    await asyncio.to_thread(set_named_secret, settings.STORAGE_DIR, FCM_SECRET_NAME,
                            _json.dumps(sa), settings.NVR_CREDENTIAL_KEY)
    fcm_provider.invalidate()
    logger.warning("FCM service account configured: project=%s", sa["project_id"])
    return await asyncio.to_thread(_push_status)


@push_router.delete("/fcm-service-account")
async def remove_service_account():
    from app.services.alert_dispatcher import FCM_SECRET_NAME, fcm_provider
    from app.services.secret_store import delete_named_secret

    removed = await asyncio.to_thread(delete_named_secret, settings.STORAGE_DIR, FCM_SECRET_NAME)
    fcm_provider.invalidate()
    return {"removed": removed, **(await asyncio.to_thread(_push_status))}


@push_router.post("/test")
async def send_test_push(req: TestPushReq):
    from app.services.alert_dispatcher import alert_dispatcher

    try:
        return await alert_dispatcher.send_test(req.paired_device_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Paired device not found (or revoked).")


@push_router.get("/deliveries")
async def recent_deliveries(limit: int = 20):
    from app.services.alert_dispatcher import alert_dispatcher

    limit = max(1, min(int(limit), alert_dispatcher.RING_SIZE))
    return {"deliveries": list(alert_dispatcher.recent)[:limit]}
