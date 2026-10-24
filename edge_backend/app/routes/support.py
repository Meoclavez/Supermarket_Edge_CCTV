"""Support contact and Report a problem (docs/SUPPORT_ISSUES_CONTRACT.md).

GET  /api/v1/support                 any signed-in person: the support contact from the signed
                                     policy (null: the dashboard uses its built-in one) and
                                     whether reports can be sent now
GET  /api/v1/support/diagnostics     what is attached to a report (numbers and codes only),
                                     shown to the person before sending (?tab=&width=)
GET  /api/v1/support/reports         this box's reports, newest first, with status and reply
POST /api/v1/support/reports         any signed-in person (operators too): a new report,
                                     stored on the box first, then sent to iKorex

All stay open while the service is paused or in standby (entitlements._PAUSE_EXEMPT):
a paused store must still be able to ask for help.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from app.services import entitlements
from app.services import support_reports as sr
from app.services.auth_service import auth_service, effective_role, is_admin_role

router = APIRouter(prefix="/api/v1/support", tags=["support"])


def _person(request: Request, _auth: bool = Depends(auth_service.verify_api_access)) -> Dict[str, Any]:
    """A signed-in person (not a stream / clip token). AUTH_DISABLED / internal key: the local owner."""
    user = getattr(request.state, "user", None)
    if user is None:
        return {"sub": None, "admin": True}
    if user.get("type") != "user_session":
        raise HTTPException(status_code=403, detail="Sign in to the dashboard to report a problem.")
    return {"sub": user.get("sub"), "admin": is_admin_role(effective_role(user))}


def _public(report: Dict[str, Any], person: Dict[str, Any]) -> Dict[str, Any]:
    mine = bool(person.get("sub")) and report.get("created_by") == person.get("sub")
    out = {k: report.get(k) for k in ("id", "created_at", "area", "urgency", "text", "status", "reply",
                                      "updated_at", "diagnostics")}
    out["area_label"] = sr.AREAS.get(report.get("area"), "Other")
    out["sent"] = bool(report.get("accepted_at"))
    out["mine"] = mine
    # A call-back phone number is shown only to whoever left it and to administrators.
    out["contact"] = report.get("contact") if (mine or person.get("admin")) else None
    return out


def _delivery() -> str:
    try:
        return sr.uploader.delivery()
    except Exception:  # noqa: BLE001
        return "offline"


@router.get("")
async def support_info(person: Dict[str, Any] = Depends(_person)) -> Dict[str, Any]:
    eff = await asyncio.to_thread(entitlements.effective_state)
    queued = await asyncio.to_thread(sr.get_reports().count_queued)
    return {"support": eff.get("support"), "source": "policy" if eff.get("support") else None,
            "delivery": await asyncio.to_thread(_delivery), "queued_reports": queued,
            "areas": [{"id": k, "label": v} for k, v in sr.AREAS.items()],
            "limits": {"text_min": sr.TEXT_MIN, "text_max": sr.TEXT_MAX, "name_max": sr.CONTACT_NAME_MAX,
                       "phone_max": sr.CONTACT_PHONE_MAX}}


@router.get("/diagnostics")
async def support_diagnostics(tab: Optional[str] = Query(None, max_length=32),
                              width: Optional[int] = Query(None, ge=0, le=100000),
                              _person_: Dict[str, Any] = Depends(_person)) -> Dict[str, Any]:
    return await asyncio.to_thread(sr.collect_diagnostics, tab, width)


@router.get("/reports")
async def support_reports(person: Dict[str, Any] = Depends(_person)) -> Dict[str, Any]:
    reports = await asyncio.to_thread(sr.get_reports().list)
    return {"reports": [_public(r, person) for r in reports], "delivery": await asyncio.to_thread(_delivery)}


@router.post("/reports", status_code=201)
async def support_create_report(payload: Any = Body(...), person: Dict[str, Any] = Depends(_person)) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="The report is not valid.")
    diagnostics = await asyncio.to_thread(sr.collect_diagnostics, payload.get("tab"), payload.get("width"))
    try:
        report = await asyncio.to_thread(
            sr.get_reports().create, text=payload.get("text"), area=payload.get("area"),
            urgency=payload.get("urgency") or "normal", contact=payload.get("contact"),
            diagnostics=diagnostics, created_by=person.get("sub"))
    except sr.ReportError as exc:
        status = 429 if "Too many" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None
    sr.uploader.request("report", sr.REPORT_DELAY_S)
    return {"report": _public(report, person), "delivery": await asyncio.to_thread(_delivery)}
