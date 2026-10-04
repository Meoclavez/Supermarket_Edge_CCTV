"""Accounts: who can sign in to this edge device, and with which role.

All routes except ``GET /api/v1/auth/me`` require an owner or administrator
(``require_admin``). Roles and what they may do: see the "roles" section of
``app/services/auth_service.py``.

Guard rails:

* there is always at least one active owner: the last one cannot be removed,
  disabled or given another role;
* nobody can remove or disable their own account or change their own role
  (use another owner/administrator), nor reset their own password here (use
  "Change password", which asks for the current one);
* only an owner can create an owner, make someone an owner, or change,
  reset or remove an owner account;
* resetting a password signs that account out of every browser; removing or
  disabling an account also signs out the phones bound to it.

Usernames and passwords follow the same rules as first-run setup and
``scripts/manage_operator.py`` (``auth_service.username_policy_error`` /
``password_policy_error``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import AdminUserModel
from app.services.auth_service import (
    ASSIGNABLE_ROLES,
    OWNER,
    auth_service,
    describe_account,
    hash_password,
    invalidate_account_cache,
    password_policy_error,
    require_admin,
    username_policy_error,
    effective_role,
)

log = logging.getLogger("edge.accounts")

router = APIRouter(prefix="/api/v1", tags=["Accounts"])

ROLE_LABELS = {
    "owner": "Owner",
    "admin": "Administrator",
    "operator": "Operator",
}
ROLE_DESCRIPTIONS = {
    "owner": "everything, including setup and accounts",
    "admin": "everything, including setup and accounts",
    "operator": "daily use: cameras, alerts, insights; can't change setup",
}


class AccountCreateReq(BaseModel):
    username: str
    display_name: str = ""
    role: str = "operator"
    password: str = Field(..., description="Initial password, at least 8 characters")


class AccountUpdateReq(BaseModel):
    role: Optional[str] = None
    display_name: Optional[str] = None
    is_active: Optional[bool] = None


class PasswordResetReq(BaseModel):
    new_password: str


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _active(u: AdminUserModel) -> bool:
    return u.is_active is None or bool(u.is_active)


def _public(u: AdminUserModel, me: Optional[str]) -> Dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name or u.username,
        "role": u.role,
        "role_label": ROLE_LABELS.get(u.role, u.role),
        "is_active": _active(u),
        "created_at": _iso(u.created_at),
        "last_login": _iso(u.last_login),
        "is_you": bool(me) and u.id == me,
    }


def _actor(user: Dict[str, Any]) -> tuple:
    """(account id or None, role) of the caller."""
    if user.get("internal"):
        return None, OWNER
    return user.get("sub"), effective_role(user)


def _check_role_value(role: Optional[str]) -> str:
    role = (role or "").strip().lower()
    if role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=422, detail="Role must be owner, admin or operator.")
    return role


async def _get(db: AsyncSession, user_id: str) -> AdminUserModel:
    u = (await db.execute(select(AdminUserModel).where(AdminUserModel.id == user_id))).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="That account no longer exists.")
    return u


async def _active_owner_count(db: AsyncSession) -> int:
    return int(await db.scalar(
        select(func.count(AdminUserModel.id)).where(
            AdminUserModel.role == OWNER,
            or_(AdminUserModel.is_active.is_(True), AdminUserModel.is_active.is_(None)),
        )
    ) or 0)


def _forbid(detail: str):
    raise HTTPException(status_code=403, detail=detail)


async def _guard_owner_target(db: AsyncSession, target: AdminUserModel, actor_role: str,
                              losing_owner: bool) -> None:
    if target.role == OWNER and actor_role != OWNER:
        _forbid("Only an owner can change an owner account.")
    if losing_owner and target.role == OWNER and _active(target) and await _active_owner_count(db) <= 1:
        _forbid("This is the only owner. Make another account an owner first.")


# --------------------------------------------------------------------------- own account

@router.get("/auth/me")
async def who_am_i(request: Request, _auth: bool = Depends(auth_service.verify_api_access)):
    """The signed-in person: id, username, display name and role."""
    user = getattr(request.state, "user", None)
    if user is None:
        # Internal service key, or AUTH_DISABLED without a session.
        return {"id": None, "username": None, "display_name": None, "role": OWNER,
                "can_change_setup": True, "can_manage_accounts": True, "phone": False}
    return describe_account(user)


# --------------------------------------------------------------------------- accounts

@router.get("/users")
async def list_accounts(request: Request, db: AsyncSession = Depends(get_db),
                        user: Dict[str, Any] = Depends(require_admin)):
    me, _ = _actor(user)
    rows = (await db.execute(select(AdminUserModel).order_by(AdminUserModel.created_at))).scalars().all()
    return {
        "accounts": [_public(u, me) for u in rows],
        "roles": [{"role": r, "label": ROLE_LABELS[r], "description": ROLE_DESCRIPTIONS[r]}
                  for r in ASSIGNABLE_ROLES],
    }


@router.post("/users", status_code=201)
async def create_account(req: AccountCreateReq, request: Request, db: AsyncSession = Depends(get_db),
                         user: Dict[str, Any] = Depends(require_admin)):
    me, actor_role = _actor(user)
    role = _check_role_value(req.role)
    if role == OWNER and actor_role != OWNER:
        _forbid("Only an owner can create another owner.")
    username = (req.username or "").strip()
    problem = username_policy_error(username) or password_policy_error(req.password, username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    clash = (await db.execute(
        select(AdminUserModel.id).where(func.lower(AdminUserModel.username) == username.lower())
    )).first()
    if clash:
        raise HTTPException(status_code=409, detail="An account with that username already exists.")
    u = AdminUserModel(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=await asyncio.to_thread(hash_password, req.password),
        display_name=((req.display_name or "").strip() or username)[:128],
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    invalidate_account_cache()
    log.warning("Account %s (%s) created by %s", u.username, role, me or "service key")
    return _public(u, me)


@router.patch("/users/{user_id}")
async def update_account(user_id: str, req: AccountUpdateReq, db: AsyncSession = Depends(get_db),
                         user: Dict[str, Any] = Depends(require_admin)):
    me, actor_role = _actor(user)
    u = await _get(db, user_id)
    signed_out = False

    if req.role is not None:
        role = _check_role_value(req.role)
        if role != u.role:
            if me and u.id == me:
                _forbid("You can't change your own role. Ask another owner or administrator.")
            if role == OWNER and actor_role != OWNER:
                _forbid("Only an owner can make someone an owner.")
            await _guard_owner_target(db, u, actor_role, losing_owner=True)
            u.role = role

    if req.is_active is not None and bool(req.is_active) != _active(u):
        if me and u.id == me:
            _forbid("You can't disable your own account.")
        if not req.is_active:
            await _guard_owner_target(db, u, actor_role, losing_owner=True)
        elif u.role == OWNER and actor_role != OWNER:
            _forbid("Only an owner can change an owner account.")
        u.is_active = bool(req.is_active)
        signed_out = not req.is_active

    if req.display_name is not None:
        if u.role == OWNER and actor_role != OWNER and not (me and u.id == me):
            _forbid("Only an owner can change an owner account.")
        name = req.display_name.strip()
        u.display_name = (name or u.username)[:128]

    await db.commit()
    await db.refresh(u)
    invalidate_account_cache()
    if signed_out:
        await asyncio.to_thread(auth_service.sign_out_account, u.id, True)
    log.warning("Account %s updated by %s: role=%s active=%s", u.username, me or "service key",
                u.role, _active(u))
    return {**_public(u, me), "signed_out": signed_out}


@router.post("/users/{user_id}/reset-password")
async def reset_account_password(user_id: str, req: PasswordResetReq, db: AsyncSession = Depends(get_db),
                                 user: Dict[str, Any] = Depends(require_admin)):
    me, actor_role = _actor(user)
    u = await _get(db, user_id)
    if me and u.id == me:
        _forbid("Use “Change password” for your own account.")
    if u.role == OWNER and actor_role != OWNER:
        _forbid("Only an owner can reset an owner's password.")
    problem = password_policy_error(req.new_password, u.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    u.password_hash = await asyncio.to_thread(hash_password, req.new_password)
    await db.commit()
    await asyncio.to_thread(auth_service.sign_out_account, u.id, False)
    log.warning("Password of %s reset by %s; its browser sessions were signed out",
                u.username, me or "service key")
    return {"status": "success", "signed_out": True, "account": _public(u, me)}


@router.delete("/users/{user_id}")
async def remove_account(user_id: str, db: AsyncSession = Depends(get_db),
                         user: Dict[str, Any] = Depends(require_admin)):
    me, actor_role = _actor(user)
    u = await _get(db, user_id)
    if me and u.id == me:
        _forbid("You can't remove your own account.")
    await _guard_owner_target(db, u, actor_role, losing_owner=True)
    username = u.username
    await db.delete(u)
    await db.commit()
    invalidate_account_cache()
    await asyncio.to_thread(auth_service.sign_out_account, user_id, True)
    log.warning("Account %s removed by %s; its sessions and phones were signed out",
                username, me or "service key")
    return {"status": "removed", "id": user_id, "username": username}
