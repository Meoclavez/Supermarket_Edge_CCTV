"""Who made this request, in words fit for an audit trail.

Used where the system records a person's action (theft outcome, "staff sent",
acknowledgement, a manual analysis run). The name comes from the verified
session (``request.state.user``, set by ``auth_service.verify_api_access``),
never from a default: an unauthenticated request under ``AUTH_DISABLED`` is
recorded as exactly that.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


async def describe_actor(request: Optional[Request], db: Optional[AsyncSession] = None) -> str:
    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    if isinstance(user, dict) and user.get("sub"):
        sub = str(user["sub"])
        name = sub
        if db is not None and not sub.startswith("paired:"):
            try:
                from app.models.db_models import AdminUserModel

                row = await db.get(AdminUserModel, sub)
                if row is not None:
                    name = row.username
            except Exception:
                pass
        pd = user.get("pd")
        if pd:
            phone = None
            if db is not None:
                try:
                    from app.models.db_models import PairedDeviceModel

                    row = await db.get(PairedDeviceModel, pd)
                    phone = row.name if row is not None else None
                except Exception:
                    phone = None
            if sub.startswith("paired:"):
                name = f"phone {phone or pd}"
            else:
                name = f"{name} (phone {phone or pd})"
        return name[:128]
    if request is not None and request.headers.get("X-Edge-API-Key"):
        return "service (API key)"
    if settings.AUTH_DISABLED:
        return "unauthenticated (AUTH_DISABLED)"
    return "unknown"
