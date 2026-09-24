import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, Optional, List

from app.database import get_db
from app.services.setup_service import (
    active_admin_exists,
    ensure_setup_code_if_needed,
    setup_code_manager,
    setup_service,
)
from app.services.auth_service import auth_service, intrusion_detector
from app.config import settings

router = APIRouter()

class AdminCreateReq(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "owner"          # ignored: the first account is always the owner
    setup_code: Optional[str] = None  # may also be sent as the X-Setup-Code header

class PhoneInfo(BaseModel):
    """A phone signing in: registers it as a paired device (see /api/v1/pairing)."""
    name: str = ""
    platform: str
    app_instance_id: str
    push_provider: Optional[str] = None
    push_token: Optional[str] = None


class LoginReq(BaseModel):
    username: str
    password: str
    phone: Optional[PhoneInfo] = None

class RefreshReq(BaseModel):
    refresh_token: str

class LogoutReq(BaseModel):
    # Optional: the refresh token of the same sign-in, revoked as well.
    refresh_token: Optional[str] = None

class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str
    
class TestCameraReq(BaseModel):
    url: str

class ScanNetworkReq(BaseModel):
    # None = let discovery derive the subnet from the host's own interfaces.
    subnet: Optional[str] = None

_setup_log = logging.getLogger("edge.setup")
# Serialises the "no admin yet -> create one" check-and-insert, so two
# requests carrying the right code cannot both become owner.
_first_admin_lock = asyncio.Lock()


def _client_ip(request: Request) -> str:
    from app.services.public_exposure import client_ip

    return client_ip(request)


def _raise_if_locked_out(ip: str) -> None:
    now = time.time()
    recent = [t for t in intrusion_detector.failed_attempts.get(ip, []) if now - t < intrusion_detector.window]
    if len(recent) >= intrusion_detector.max_attempts:
        raise HTTPException(
            status_code=429,
            detail=(f"Too many failed attempts from this address. Wait "
                    f"{int(intrusion_detector.window // 60)} minutes and try again."),
            headers={"Retry-After": str(int(intrusion_detector.window))},
        )


def _require_setup_code(request: Request, supplied: Optional[str]) -> None:
    """Refuse a first-run request that does not carry the one-time setup code."""
    ip = _client_ip(request)
    _raise_if_locked_out(ip)
    code = supplied or request.headers.get("X-Setup-Code")
    # Make sure a code exists to be checked against (e.g. the file was deleted
    # while the server was running); generating one logs it.
    setup_code_manager.ensure("requested by the first-run form")
    if not code or not setup_code_manager.verify(code):
        intrusion_detector.record_failure(ip)
        _raise_if_locked_out(ip)
        raise HTTPException(
            status_code=403,
            detail=("Setup code is missing or incorrect. It is printed in the server log "
                    "at startup and stored in storage/setup_code.txt on the server."),
        )
    intrusion_detector.record_success(ip, "setup_code")


def _has_valid_credential(request: Request) -> bool:
    import secrets as _secrets

    api_key = request.headers.get("X-Edge-API-Key")
    if api_key and _secrets.compare_digest(api_key, settings.INTERNAL_SERVICE_KEY):
        return True
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None
    token = token or request.cookies.get("edge_cctv_token")
    payload = auth_service.verify_session_token(token) if token else None
    if payload:
        request.state.user = payload
        return True
    return False


async def verify_setup_or_admin_access(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Gate for the setup wizard's step endpoints.

    - Once an operator account exists: a real session (or the internal API
      key) is required, exactly like any other API route.
    - Before that: a session is accepted if one is presented, otherwise the
      one-time setup code must be sent in the ``X-Setup-Code`` header.
      Previously every step was open to any caller until setup was marked
      complete, which exposed camera scanning and network config on the LAN.
    """
    if settings.AUTH_DISABLED:
        return True
    if await active_admin_exists(session):
        return auth_service.verify_api_access(request)
    if _has_valid_credential(request):
        return True
    _require_setup_code(request, None)
    return True

@router.get("/setup/status")
async def get_setup_status(session: AsyncSession = Depends(get_db)):
    is_completed = await setup_service.is_setup_completed(session)
    current_step = await setup_service.get_setup_step(session)
    hardware = setup_service.detect_hardware()
    
    return {
        "is_completed": is_completed,
        "current_step": current_step,
        "hardware_report": hardware
    }

@router.get("/auth/status")
async def get_auth_status(request: Request, session: AsyncSession = Depends(get_db)):
    """Whether an operator account exists, and whether this caller is signed in.

    The dashboard needs this before it can decide between a first-run account
    setup and an ordinary sign-in. It is deliberately unauthenticated, and
    reveals only whether *an* account exists -- never who, or how many.
    """
    admin_exists = await active_admin_exists(session)
    if not admin_exists:
        # Normally issued at startup; this covers a code file removed at
        # runtime. An existing code is not re-logged on every poll.
        try:
            await ensure_setup_code_if_needed(session, "requested by the dashboard", announce_existing=False)
        except Exception as exc:
            _setup_log.error("Could not issue a setup code: %s", exc)

    authenticated = False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        authenticated = auth_service.verify_session_token(auth_header.split(" ", 1)[1]) is not None

    return {
        "admin_exists": admin_exists,
        "authenticated": authenticated,
        "setup_code_required": not admin_exists,
        # Name kept for the dashboard/mobile clients; it now reflects the
        # explicit AUTH_DISABLED switch, never DEBUG.
        "debug_bypass_active": settings.AUTH_DISABLED,
    }


@router.post("/setup/admin")
async def create_first_admin(req: AdminCreateReq, request: Request, session: AsyncSession = Depends(get_db)):
    """Create the first operator account. Requires the one-time setup code.

    Returns a session so the operator is signed in immediately.
    """
    async with _first_admin_lock:
        if await active_admin_exists(session):
            raise HTTPException(
                status_code=403,
                detail=("An operator account already exists. Sign in instead, or reset access "
                        "on the server with scripts/manage_operator.py."),
            )
        _require_setup_code(request, req.setup_code)

        # The first account is always the owner, whatever the client sent.
        user = await auth_service.create_admin_user(
            session, req.username, req.password, req.display_name, "owner"
        )
        setup_code_manager.invalidate()
        from datetime import datetime, timezone
        user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        await setup_service.set_setup_step(session, 2)

    _setup_log.warning("First operator account created: username=%s from %s. Setup code invalidated.",
                       user.username, _client_ip(request))
    tokens = auth_service.issue_session_tokens(user.id, user.role)
    return {"status": "success", "user_id": user.id, "token_type": "bearer", **tokens}

@router.post("/setup/hardware-scan")
async def hardware_scan(
    session: AsyncSession = Depends(get_db), 
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    hardware = setup_service.detect_hardware()
    return {"hardware": hardware}

@router.post("/setup/camera-scan")
async def scan_cameras(
    req: Optional[ScanNetworkReq] = None, 
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    """Discover cameras on USB and the network. Reports only devices that answered."""
    from app.services.camera_discovery import camera_discovery_service

    subnet = req.subnet if req and req.subnet else None
    found = await camera_discovery_service.discover(subnet=subnet)
    return {"cameras": [d.to_dict() for d in found], "count": len(found)}

@router.post("/setup/test-camera")
async def test_camera(
    req: TestCameraReq,
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    result = await setup_service.test_rtsp_url(req.url)
    return result

class CameraSetupItem(BaseModel):
    name: str
    location: str
    rtsp_url: str

class AddCamerasReq(BaseModel):
    cameras: List[CameraSetupItem]

@router.post("/setup/add-cameras")
async def add_cameras(
    req: AddCamerasReq,
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    """Persist cameras entered during setup.

    Status, fps and resolution are not written here: they are measured by the
    pipeline worker once the stream is open. The previous version stored
    ONLINE / 25 fps / 1920x1080 for every camera before a single frame had
    been read, and started a second, independent RTSP reader per camera.
    """
    from app.models.db_models import CameraModel
    from app.services.pipeline_supervisor import pipeline_supervisor
    import uuid

    from app.services import camera_source

    added = []
    for c in req.cameras:
        cam_id = f"cam_{uuid.uuid4().hex[:8]}"
        webrtc_url = f"{settings.EDGE_BASE_URL}/api/v1/webrtc/offer?camera_id={cam_id}"
        # A user:pw@ typed into the URL is stored encrypted, not in rtsp_url.
        try:
            clean_url = camera_source.detach_credentials(cam_id, c.rtsp_url)
        except Exception as exc:  # noqa: BLE001
            for done in added:
                camera_source.delete_credentials(done)
            logging.getLogger("Setup").error("Could not store credentials for camera %s: %s", cam_id, type(exc).__name__)
            raise HTTPException(status_code=500, detail="Could not store the camera credentials securely.") from None

        db_cam = CameraModel(
            id=cam_id,
            name=c.name,
            location=c.location,
            rtsp_url=clean_url,
            webrtc_url=webrtc_url,
            status="OFFLINE",
            fps=0,
            resolution="unknown",
            is_ai_enabled=True,
            ai_models=[],
            dvr_enabled=True,
            dvr_retention_days=7,
            dvr_quota_gb=100.0,
        )
        session.add(db_cam)
        added.append(cam_id)

    try:
        await session.commit()
    except Exception:
        for done in added:
            camera_source.delete_credentials(done)
        raise
    await setup_service.set_setup_step(session, 3)
    try:
        await pipeline_supervisor.reconcile_cameras()
    except Exception as exc:  # the periodic reconcile loop will pick them up
        logging.getLogger("Setup").warning("reconcile after add-cameras: %s", exc)
    return {"status": "success", "added_cameras": added}

@router.post("/setup/network-config")
async def save_network_config(
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    # Stub
    await setup_service.set_setup_step(session, 4)
    return {"status": "success"}

@router.post("/setup/notifications")
async def save_notifications(
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    # Stub
    await setup_service.set_setup_step(session, 5)
    return {"status": "success"}

@router.post("/setup/complete")
async def complete_setup(
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(verify_setup_or_admin_access)
):
    await setup_service.complete_setup(session)
    setup_service.generate_secure_secrets()
    return {"status": "success"}

# While an address is locked out for wrong passwords, its password checks are
# throttled to one per LOGIN_LOCKED_RETRY_SEC instead of refused outright, so
# the right password still signs the operator in (lockout by bad-token polling
# lives in a separate bucket and never reaches this) while guessing stays slow.
LOGIN_LOCKED_RETRY_SEC = 2.0
_last_locked_login: Dict[str, float] = {}


def _throttle_locked_login(ip: str) -> None:
    if not intrusion_detector.is_locked_out(ip, "login"):
        _last_locked_login.pop(ip, None)
        return
    now = time.monotonic()
    last = _last_locked_login.get(ip)
    if last is not None and now - last < LOGIN_LOCKED_RETRY_SEC:
        raise HTTPException(
            status_code=429,
            detail=("Too many failed sign-in attempts from this address. Wait a few seconds "
                    "between attempts."),
            headers={"Retry-After": str(max(1, int(LOGIN_LOCKED_RETRY_SEC + 0.999)))},
        )
    _last_locked_login[ip] = now
    if len(_last_locked_login) > 4096:
        _last_locked_login.clear()


# Auth routes
@router.post("/auth/login")
async def login(req: LoginReq, request: Request, session: AsyncSession = Depends(get_db)):
    """Password sign-in.

    The password is checked first: a correct one always signs in and clears
    this address's counters. A wrong one counts toward the lockout (429); while
    locked out, attempts are throttled rather than refused (see above).
    """
    ip = _client_ip(request)
    _throttle_locked_login(ip)
    tokens = await auth_service.authenticate_user(session, req.username, req.password)
    if not tokens:
        intrusion_detector.record_failure(ip)
        _raise_if_locked_out(ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    intrusion_detector.record_success(ip, "password")
    _last_locked_login.pop(ip, None)
    if req.phone is None:
        return tokens
    # A phone signing in with the operator's password ends up exactly where a
    # QR/code pairing does: a paired device and tokens bound to it.
    from app.services import pairing_service

    payload = auth_service.verify_token(tokens["access_token"]) or {}
    try:
        result = await pairing_service.register_phone(
            session, req.phone.model_dump(), payload.get("sub"), "password", payload.get("role") or "owner")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {**result, "urls": pairing_service.reachable_urls(request)}

@router.post("/auth/refresh")
async def refresh(req: RefreshReq):
    # Sync DB checks for phone tokens (revocation, stored refresh hash).
    new_access = await asyncio.to_thread(auth_service.refresh_access_token, req.refresh_token)
    return {"access_token": new_access, "token_type": "bearer"}

@router.post("/auth/logout")
async def logout(request: Request, req: Optional[LogoutReq] = None):
    """Sign out server-side: the presented session token stops working now.

    The session token comes from the Authorization header (or the dashboard
    cookie). Its whole sign-in session is revoked, which includes the refresh
    token issued with it; a ``refresh_token`` in the body is revoked as well.
    Idempotent: an already expired or signed-out token returns 200 with
    ``revoked: 0``. Revocations are kept until the tokens would have expired.
    """
    auth_header = request.headers.get("Authorization", "")
    access = auth_header.split(" ", 1)[1].strip() if auth_header.startswith("Bearer ") else None
    access = access or request.cookies.get("edge_cctv_token")
    refresh_tok = req.refresh_token if req else None
    if not access and not refresh_tok:
        raise HTTPException(status_code=401, detail="No session token was presented.")
    revoked = await asyncio.to_thread(auth_service.revoke_session, access, refresh_tok)
    return {"status": "signed_out", "revoked": revoked}


@router.post("/auth/change-password")
async def change_password(
    req: ChangePasswordReq, 
    request: Request,
    session: AsyncSession = Depends(get_db),
    has_access: bool = Depends(auth_service.verify_api_access)
):
    if not hasattr(request.state, "user") or "sub" not in request.state.user:
        raise HTTPException(status_code=401, detail="User context missing")
        
    await auth_service.change_password(session, request.state.user["sub"], req.old_password, req.new_password)
    return {"status": "success"}

# The old in-memory 6-digit pairing code (/auth/pairing-code, /auth/pair) issued
# owner tokens bound to no device and impossible to revoke. It is replaced by
# /api/v1/pairing (sessions, claim, paired devices).
_PAIR_GONE = "Replaced by POST /api/v1/pairing/sessions (dashboard) and POST /api/v1/pairing/claim (phone)."


@router.get("/auth/pairing-code", status_code=410)
async def get_pairing_code_gone():
    raise HTTPException(status_code=410, detail=_PAIR_GONE)


@router.post("/auth/pair", status_code=410)
async def pair_app_gone():
    raise HTTPException(status_code=410, detail=_PAIR_GONE)
