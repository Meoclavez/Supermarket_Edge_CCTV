"""Till (point-of-sale) ingest and its dedicated key.

* ``POST /api/v1/analytics/pos/ingest``: the till system sends sales here with
  the till key in the ``X-Edge-API-Key`` header. For backward compatibility the
  device's internal service key and a signed-in session token still work.
* ``GET/POST/DELETE /api/v1/analytics/pos/till-key``: owner/admin only. Status
  (exists, created, last used), create or replace (the key is returned once),
  revoke. Settings > Sales data > Till key (static/js/pos_key.js).

The till key is accepted by the ingest route only; it opens nothing else.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routes import ResilientRoute
from app.routes.analytics import (
    api_key_header,
    ingest_pos_transactions,
    security_bearer,
    verify_analytics_access,
)
from app.services.auth_service import _client_ip, general_rate_limiter, intrusion_detector, require_admin
from app.services.till_key import HEADER_NAME, till_key_store

router = APIRouter(
    prefix="/api/v1/analytics",
    tags=["Retail Intelligence"],
    dependencies=[Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)


def verify_till_access(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
) -> bool:
    """The till key, else whatever the other analytics routes accept."""
    if api_key and till_key_store.verify(api_key):
        intrusion_detector.record_success(_client_ip(request), "till_key")
        return True
    return verify_analytics_access(request, api_key, bearer)


@router.post("/pos/ingest", dependencies=[Depends(verify_till_access)])
async def pos_ingest(payload: Any = Body(...), db: AsyncSession = Depends(get_db)):
    """Ingest POS sales receipts to drive real-time checkout conversion metrics."""
    return await ingest_pos_transactions(payload, db)


def _till_key_status() -> dict:
    return {**till_key_store.status(), "ingest_path": "/api/v1/analytics/pos/ingest"}


@router.get("/pos/till-key")
def get_till_key(_admin: dict = Depends(require_admin)):
    """Whether a till key exists, when it was created and last used. Never the key."""
    return _till_key_status()


@router.post("/pos/till-key")
def create_till_key(admin: dict = Depends(require_admin)):
    """Create the till key, or replace it (the old key stops working at once).

    The response carries the key once; only its hash is stored.
    """
    actor = (admin or {}).get("sub") or ("internal key" if (admin or {}).get("internal") else None)
    key = till_key_store.create(actor=actor)
    return {**_till_key_status(), "key": key,
            "note": "Shown once. Store it in the till system now.", "header": HEADER_NAME}


@router.delete("/pos/till-key")
def revoke_till_key(_admin: dict = Depends(require_admin)):
    """Delete the till key; tills that use it are refused from now on."""
    revoked = till_key_store.revoke()
    return {**_till_key_status(), "revoked": revoked}
