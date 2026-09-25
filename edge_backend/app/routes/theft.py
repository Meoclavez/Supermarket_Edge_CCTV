"""Loss-prevention incident API.

Incidents are written by the live pose pipeline (``services/pose_analytics``)
and are *suspicious behaviour for staff review*, never a finding of theft.
Every response carries the rule that fired, the evidence bullets behind it and
a URL for the evidence image (person crop + full frame with the skeleton).

All endpoints use the same authentication as the camera snapshot endpoints
(``auth_service.verify_api_access``: bearer header, ``?token=`` or the
dashboard session cookie), so an ``<img>`` tag can load the evidence image.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import TheftIncidentModel
from app.models.schemas import (
    TheftIncident,
    TheftStatisticsResponse,
    TheftAcknowledgeRequest,
    TheftDispatchRequest,
    TheftResolveRequest,
    TheftOutcome,
    THEFT_ACTIONED_OUTCOMES,
    THEFT_OUTCOME_LABELS,
    THEFT_RECOVERY_OUTCOMES,
)
from app.services.actor import describe_actor
from app.services.auth_service import auth_service
from app.services.pose_analytics import pose_analytics
from app.services.theft_detection_service import RULE_LABELS, theft_detection_service

logger = logging.getLogger("TheftRoutes")

REVIEW_LABEL = "Suspicious behaviour for staff review"


def _bind_notification_loop() -> None:
    # Gives the pose pipeline's writer thread the app loop for async
    # notifications even if nothing called pose_analytics.start().
    pose_analytics.bind_loop()


router = APIRouter(
    prefix="/api/v1/theft",
    tags=["Loss Prevention & Theft Detection"],
    dependencies=[Depends(auth_service.verify_api_access), Depends(_bind_notification_loop)],
)


class TheftIncidentOut(TheftIncident):
    """An incident plus the rule, evidence and evidence-image URL."""

    rule: Optional[str] = None
    rule_label: Optional[str] = None
    evidence: Optional[List[str]] = None
    snapshot_url: Optional[str] = None
    review_label: str = REVIEW_LABEL
    outcome: Optional[str] = None
    outcome_label: Optional[str] = None
    recovered_value: Optional[float] = None
    resolved_by: Optional[str] = None
    dispatched_by: Optional[str] = None
    dispatched_at: Optional[datetime] = None
    # For the dashboard's "Watch camera now" link.
    studio_url: Optional[str] = None


class TheftIncidentOutList(BaseModel):
    status: str = "success"
    total: int
    incidents: List[TheftIncidentOut]


def _serialize(inc: TheftIncidentModel) -> TheftIncidentOut:
    out = TheftIncidentOut.model_validate(inc)
    out.rule = inc.rule or inc.theft_type
    out.rule_label = RULE_LABELS.get(out.rule or "", out.rule)
    out.evidence = list(inc.evidence or [])
    has_image = bool(inc.snapshot_path) and Path(inc.snapshot_path).is_file()
    out.snapshot_url = f"/api/v1/theft/incidents/{inc.id}/evidence" if has_image else None
    # The mobile app reads this field (relative URL, fetched with a bearer token).
    out.evidence_snapshot_url = out.snapshot_url
    if inc.status in ("RESOLVED", "FALSE_ALARM") and inc.resolution:
        out.outcome = inc.resolution
        out.outcome_label = THEFT_OUTCOME_LABELS.get(inc.resolution, inc.resolution.replace("_", " ").capitalize())
    out.studio_url = f"/dashboard/studio?camera_id={inc.camera_id}" if inc.camera_id else None
    return out


@router.get("/incidents", response_model=TheftIncidentOutList)
async def get_theft_incidents(
    status: Optional[str] = Query(None, description="Filter by status (ACTIVE, ACKNOWLEDGED, DISPATCHED, RESOLVED, FALSE_ALARM)"),
    severity: Optional[str] = Query(None, description="Filter by severity (HIGH, MEDIUM, LOW)"),
    department: Optional[str] = Query(None, description="Filter by department name"),
    rule: Optional[str] = Query(None, description="Filter by rule (CONCEALMENT, SHELF_SWEEPING, SUSPICIOUS_LOITERING, EXIT_WITHOUT_CHECKOUT, SWEETHEARTING)"),
    limit: int = Query(50, ge=1, le=200, description="Max incidents to retrieve"),
    db: AsyncSession = Depends(get_db),
):
    """Loss-prevention incidents, newest first, with rule, evidence and snapshot URL."""
    try:
        incidents = await theft_detection_service.get_incidents(
            db=db,
            status=status,
            severity=severity,
            department=department,
            limit=limit,
            rule=rule,
        )
        return TheftIncidentOutList(
            incidents=[_serialize(inc) for inc in incidents],
            total=len(incidents),
        )
    except Exception as e:
        logger.error(f"Error fetching theft incidents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/incidents/{incident_id}", response_model=TheftIncidentOut)
async def get_theft_incident(incident_id: str, db: AsyncSession = Depends(get_db)):
    """One incident with its rule, evidence bullets and snapshot URL."""
    inc = await db.get(TheftIncidentModel, incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(inc)


@router.get("/incidents/{incident_id}/evidence")
async def get_theft_incident_evidence(incident_id: str, db: AsyncSession = Depends(get_db)):
    """The evidence JPEG: caption band, full frame with skeleton and box, person crop."""
    inc = await db.get(TheftIncidentModel, incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    if not inc.snapshot_path:
        raise HTTPException(status_code=404, detail="No evidence image was recorded for this incident")
    if inc.evidence_expired_at is not None:
        raise HTTPException(status_code=410, detail=(
            f"Evidence expired: the image was deleted by the evidence storage limit on "
            f"{inc.evidence_expired_at:%Y-%m-%d %H:%M} UTC"))
    path = Path(inc.snapshot_path).resolve()
    # Serve only files inside the evidence directory, whatever the row says.
    root = pose_analytics.evidence_dir().resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Evidence image not found")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/statistics", response_model=TheftStatisticsResponse)
async def get_theft_statistics(
    db: AsyncSession = Depends(get_db),
):
    """Compute live aggregated theft metrics, today's incident count, and prevented loss estimate."""
    try:
        stats = await theft_detection_service.get_statistics(db=db)
        return stats
    except Exception as e:
        logger.error(f"Error calculating theft statistics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/outcomes")
async def list_theft_outcomes():
    """The outcomes a reviewer can record, for the resolve buttons."""
    return {
        "outcomes": [
            {
                "id": o.value,
                "label": THEFT_OUTCOME_LABELS[o.value],
                "counts_as_actioned": o.value in THEFT_ACTIONED_OUTCOMES,
                "accepts_recovered_value": o.value in THEFT_RECOVERY_OUTCOMES,
                "is_false_alarm": o is TheftOutcome.FALSE_ALARM,
            }
            for o in TheftOutcome
        ],
        "required": True,
    }


@router.post("/incidents/{incident_id}/acknowledge", response_model=TheftIncidentOut)
async def acknowledge_theft_incident(
    incident_id: str,
    request: Request,
    payload: Optional[TheftAcknowledgeRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Record who acknowledged the incident (the name given, else the signed-in operator)."""
    guard_id = (payload.guard_id if payload and payload.guard_id else None) or await describe_actor(request, db)
    incident = await theft_detection_service.acknowledge_incident(
        incident_id=incident_id,
        guard_id=guard_id,
        db=db,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(incident)


_RULE_EVENT_TYPES = {
    "CONCEALMENT": "CONCEALMENT",
    "SHELF_SWEEPING": "SHELF_SWEEP",
    "SUSPICIOUS_LOITERING": "LOITERING",
    "EXIT_WITHOUT_CHECKOUT": "EXIT_WITHOUT_CHECKOUT",
}


@router.post("/incidents/{incident_id}/dispatch", response_model=TheftIncidentOut)
async def dispatch_security_to_incident(
    incident_id: str,
    request: Request,
    payload: Optional[TheftDispatchRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Mark "staff sent": records who marked it and when, and alerts paired phones.

    Nothing is invented: no guard unit and no audio deterrent are recorded
    unless typed by the operator (``staff_name``), because this system cannot
    dispatch a unit or play audio. The loss-prevention alert goes out through
    ``alert_dispatcher`` and its real delivery report is stored on the incident
    (``dispatch_details.alert``) and returned.
    """
    payload = payload or TheftDispatchRequest()
    actor = await describe_actor(request, db)
    incident = await theft_detection_service.dispatch_security(
        incident_id=incident_id,
        dispatched_by=actor,
        staff_name=payload.staff_name,
        note=payload.note,
        db=db,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")

    if payload.notify_phones:
        from app.services.alert_dispatcher import alert_dispatcher

        rule = incident.rule or incident.theft_type or ""
        label = RULE_LABELS.get(rule, rule.replace("_", " ").capitalize() or "Loss-prevention alert")
        where = incident.camera_name or incident.camera_id
        sent = f" ({payload.staff_name.strip()})" if payload.staff_name and payload.staff_name.strip() else ""
        body = f"{actor} sent staff{sent} to check {where}. Suspicious behaviour for staff review."
        if payload.note and payload.note.strip():
            body += f" Note: {payload.note.strip()[:200]}"
        has_image = bool(incident.snapshot_path) and Path(incident.snapshot_path).is_file()
        try:
            report = await alert_dispatcher.dispatch(
                _RULE_EVENT_TYPES.get(rule, "THEFT_SUSPECTED"),
                incident.severity or "HIGH",
                f"Staff sent: {label} - {where}",
                body,
                {
                    "camera_id": incident.camera_id,
                    "incident_id": incident.id,
                    "kind": "staff_dispatch",
                    "dispatched_by": actor,
                    "snapshot_url": f"/api/v1/theft/incidents/{incident.id}/evidence" if has_image else "",
                },
                bypass_cooldown=True,
            )
            push = report.get("push") or {}
            summary = {
                "alert_id": report.get("alert_id"),
                "logged": report.get("logged"),
                "websocket_clients": report.get("websocket_clients"),
                "phones_paired": push.get("devices"),
                "phones_sent": push.get("sent"),
                "phones_failed": push.get("failed"),
                "skipped": push.get("skipped"),
            }
        except Exception as exc:  # the record stands even if the alert could not go out
            logger.error(f"Staff-sent alert for {incident_id} failed: {exc}")
            summary = {"error": str(exc)[:200]}
        await theft_detection_service.record_dispatch_alert(incident.id, summary, db)
        incident = await db.get(TheftIncidentModel, incident_id)
    return _serialize(incident)


@router.post("/incidents/{incident_id}/resolve", response_model=TheftIncidentOut)
async def resolve_theft_incident(
    incident_id: str,
    request: Request,
    payload: TheftResolveRequest = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """Close an incident with the reviewer's outcome. ``outcome`` is required (422 without it).

    Outcomes: see ``GET /api/v1/theft/outcomes``. ``resolution`` is accepted
    as the old name. ``recovered_value`` only with RECOVERED_GOODS or
    CUSTOMER_PAID. The operator who recorded it is stored as ``resolved_by``.
    """
    incident = await theft_detection_service.resolve_incident(
        incident_id=incident_id,
        resolution=payload.outcome.value,
        notes=payload.notes,
        db=db,
        resolved_by=await describe_actor(request, db),
        recovered_value=payload.recovered_value,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(incident)
