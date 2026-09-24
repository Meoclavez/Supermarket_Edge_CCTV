"""Camera roles API: presets, assigning a role, per-camera and store setup status.

* ``GET  /api/v1/camera-roles``              -- the presets (for pickers).
* ``PUT  /api/v1/cameras/{id}/role``         -- set / clear a role, optionally
  applying its defaults (feature flags + person-size gate).
* ``GET  /api/v1/cameras/{id}/setup``        -- the role's checklist, each item
  computed from real configuration.
* ``PUT  /api/v1/cameras/{id}/pos-register`` -- link a checkout lane to a POS
  register id (``null`` unlinks).
* ``GET  /api/v1/store/setup``               -- store-wide coverage: roles
  present, analytics available / limited / blocked, setup score.
* ``GET  /api/v1/store/pos-registers``       -- register ids seen in POS data.

The presets live in ``services/camera_roles.py``.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.db_models import CameraModel, POSTransactionModel
from ..models.schemas import CameraFeatureConfig
from ..services import camera_roles
from ..services.auth_service import auth_service
from ..services.feature_manager import feature_manager

router = APIRouter(tags=["Camera roles"], dependencies=[Depends(auth_service.verify_api_access)])

_REGISTER_RE = re.compile(r"^[A-Za-z0-9_.:/ -]{1,64}$")


class RoleUpdateRequest(BaseModel):
    role: Optional[str] = Field(None, description="Role id from GET /api/v1/camera-roles; null clears it")
    apply_defaults: bool = Field(True, description="Write the role's feature flags and person-size gate")


class RegisterLinkRequest(BaseModel):
    register_id: Optional[str] = Field(None, description="POS register_id this lane rings sales on; null unlinks")


async def _camera(db: AsyncSession, camera_id: str) -> CameraModel:
    cam = await db.get(CameraModel, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found")
    return cam


@router.get("/api/v1/camera-roles")
async def list_camera_roles():
    """Every role preset: label, description, mounting tip, defaults and setup items."""
    return camera_roles.presets_list()


@router.put("/api/v1/cameras/{camera_id}/role")
async def set_camera_role(camera_id: str, req: RoleUpdateRequest, db: AsyncSession = Depends(get_db)):
    """Set (or clear) the camera's role.

    With ``apply_defaults`` the preset's analytics flags and person-size gate
    are written now; toggles the operator changes afterwards are kept until
    defaults are applied again. Without it only the role changes. Clearing
    the role leaves the toggles as they are.
    """
    try:
        role = camera_roles.normalise_role(req.role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    cam = await _camera(db, camera_id)
    applied = camera_roles.apply_role(cam, role, req.apply_defaults)
    if role != "checkout" and cam.pos_register_id:
        # Only a checkout lane rings sales; a stale link would misattribute POS rows.
        cam.pos_register_id = None
    await db.commit()
    await db.refresh(cam)
    camera_roles.role_cache.invalidate()
    features = CameraFeatureConfig.model_validate(cam.features or {})
    if applied:
        feature_manager.set_camera_features(camera_id, features)
    ctx = await camera_roles.load_context(db)
    return {
        "camera_id": camera_id,
        "role": role,
        "role_label": camera_roles.ROLE_PRESETS[role].label if role else None,
        "applied_defaults": applied,
        "features": feature_manager.get_camera_features(camera_id, stored=cam.features).model_dump(),
        "pos_register_id": cam.pos_register_id,
        "setup": camera_roles.camera_setup(cam, ctx),
    }


@router.get("/api/v1/cameras/{camera_id}/setup")
async def get_camera_setup(camera_id: str, db: AsyncSession = Depends(get_db)):
    """The role's checklist with ``done`` computed from the stored configuration."""
    cam = await _camera(db, camera_id)
    ctx = await camera_roles.load_context(db)
    return camera_roles.camera_setup(cam, ctx)


@router.put("/api/v1/cameras/{camera_id}/pos-register")
async def link_pos_register(camera_id: str, req: RegisterLinkRequest, db: AsyncSession = Depends(get_db)):
    """Link a checkout-lane camera to the POS register its lane rings sales on."""
    cam = await _camera(db, camera_id)
    register = (req.register_id or "").strip() or None
    if register is not None:
        if not _REGISTER_RE.match(register):
            raise HTTPException(status_code=422, detail="Register id may contain letters, digits, spaces and _.:/- "
                                                        "(max 64 characters).")
        if cam.role != "checkout":
            raise HTTPException(status_code=409, detail="Only a camera with the Checkout / cashier role can be "
                                                        "linked to a POS register.")
    cam.pos_register_id = register
    await db.commit()
    await db.refresh(cam)
    ctx = await camera_roles.load_context(db)
    rows, last = ctx.pos_registers.get(register, (0, None)) if register else (0, None)
    return {
        "camera_id": camera_id,
        "pos_register_id": register,
        "pos_rows_seen": rows,
        "pos_last_seen": last.isoformat() if last else None,
        "setup": camera_roles.camera_setup(cam, ctx),
    }


@router.get("/api/v1/store/setup")
async def get_store_setup(db: AsyncSession = Depends(get_db)):
    """Roles present, which analytics that enables or blocks, and a setup score."""
    cams = (await db.execute(select(CameraModel).order_by(CameraModel.channel_number.asc()))).scalars().all()
    ctx = await camera_roles.load_context(db)
    return camera_roles.store_setup(list(cams), ctx)


@router.get("/api/v1/store/pos-registers")
async def list_pos_registers(db: AsyncSession = Depends(get_db)):
    """Register ids seen in ingested POS rows, with the cameras linked to each."""
    rows = (await db.execute(
        select(POSTransactionModel.register_id, func.count(POSTransactionModel.id),
               func.max(POSTransactionModel.timestamp))
        .group_by(POSTransactionModel.register_id)
        .order_by(POSTransactionModel.register_id))).all()
    links: dict = {}
    for cid, reg in (await db.execute(
            select(CameraModel.id, CameraModel.pos_register_id).where(CameraModel.pos_register_id.isnot(None)))).all():
        links.setdefault(reg, []).append(cid)
    seen = {str(r) for r, _, _ in rows if r}
    registers = [{"register_id": str(r), "rows": int(n or 0), "last_seen": ts.isoformat() if ts else None,
                  "linked_camera_ids": sorted(links.get(str(r), []))} for r, n, ts in rows if r]
    # Linked but never seen in POS data: listed so the operator notices a typo.
    registers += [{"register_id": reg, "rows": 0, "last_seen": None, "linked_camera_ids": sorted(cids)}
                  for reg, cids in links.items() if reg not in seen]
    return {"registers": registers, "pos_connected": bool(seen)}
