import logging

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Any, Optional, List

from app.database import get_db
from app.services.setup_service import setup_service
from app.services.auth_service import auth_service
from app.config import settings

router = APIRouter()

class AdminCreateReq(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "owner"

class LoginReq(BaseModel):
    username: str
    password: str

class RefreshReq(BaseModel):
    refresh_token: str

class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str
    
class PairReq(BaseModel):
    pairing_code: Optional[str] = None
    code: Optional[str] = None

class TestCameraReq(BaseModel):
    url: str

class ScanNetworkReq(BaseModel):
    # None = let discovery derive the subnet from the host's own interfaces.
    subnet: Optional[str] = None

async def verify_setup_or_admin_access(
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    is_completed = await setup_service.is_setup_completed(session)
    if not is_completed:
        return True
    return auth_service.verify_api_access(request)

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
    from sqlalchemy import func, select

    from app.models.db_models import AdminUserModel

    count = await session.scalar(select(func.count(AdminUserModel.id)))

    authenticated = False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        payload = auth_service.verify_token(auth_header.split(" ", 1)[1])
        authenticated = bool(payload and payload.get("type") == "user_session")

    return {
        "admin_exists": bool(count),
        "authenticated": authenticated,
        "debug_bypass_active": settings.DEBUG,
    }


@router.post("/setup/admin")
async def create_first_admin(req: AdminCreateReq, session: AsyncSession = Depends(get_db)):
    # Check if we can create admin
    # Only allow if setup is not completed or if no admins exist
    from app.models.db_models import AdminUserModel
    from sqlalchemy import select
    
    stmt = select(AdminUserModel)
    result = await session.execute(stmt)
    if result.scalars().first():
        raise HTTPException(status_code=403, detail="Admin user already exists")
        
    user = await auth_service.create_admin_user(session, req.username, req.password, req.display_name, req.role)
    await setup_service.set_setup_step(session, 2)
    return {"status": "success", "user_id": user.id}

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

    added = []
    for c in req.cameras:
        cam_id = f"cam_{uuid.uuid4().hex[:8]}"
        webrtc_url = f"{settings.EDGE_BASE_URL}/api/v1/webrtc/offer?camera_id={cam_id}"

        db_cam = CameraModel(
            id=cam_id,
            name=c.name,
            location=c.location,
            rtsp_url=c.rtsp_url,
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

    await session.commit()
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

# Auth routes
@router.post("/auth/login")
async def login(req: LoginReq, session: AsyncSession = Depends(get_db)):
    tokens = await auth_service.authenticate_user(session, req.username, req.password)
    if not tokens:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return tokens

@router.post("/auth/refresh")
async def refresh(req: RefreshReq):
    new_access = auth_service.refresh_access_token(req.refresh_token)
    return {"access_token": new_access}

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

@router.get("/auth/pairing-code")
async def get_pairing_code(
    has_access: bool = Depends(auth_service.verify_api_access)
):
    code = auth_service.generate_app_pairing_code()
    return {"pairing_code": code, "expires_in": 300}

@router.post("/auth/pair")
async def pair_app(req: PairReq, session: AsyncSession = Depends(get_db)):
    code_val = req.pairing_code or req.code
    if not code_val:
        raise HTTPException(status_code=400, detail="Missing pairing code")

    tokens = await auth_service.verify_app_pairing_code(session, code_val.strip())
    if not tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired 6-digit pairing code")

    return tokens
