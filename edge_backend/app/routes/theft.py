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
)
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


@router.post("/incidents/{incident_id}/acknowledge", response_model=TheftIncidentOut)
async def acknowledge_theft_incident(
    incident_id: str,
    payload: Optional[TheftAcknowledgeRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Acknowledge a live theft incident by a security guard or operator."""
    guard_id = payload.guard_id if payload else "guard_01"
    incident = await theft_detection_service.acknowledge_incident(
        incident_id=incident_id,
        guard_id=guard_id,
        db=db,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(incident)


@router.post("/incidents/{incident_id}/dispatch", response_model=TheftIncidentOut)
async def dispatch_security_to_incident(
    incident_id: str,
    payload: Optional[TheftDispatchRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Dispatch security floor personnel or trigger active audio greeting deterrent in zone."""
    guard_unit = payload.guard_unit if payload else "Unit 1 - Floor Guard"
    audio_deterrent = payload.audio_deterrent if payload else True
    announcement_type = (payload.announcement_type if payload else None) or "CUSTOMER_ASSISTANCE_GREETING"

    incident = await theft_detection_service.dispatch_security(
        incident_id=incident_id,
        guard_unit=guard_unit,
        audio_deterrent=audio_deterrent,
        announcement_type=announcement_type,
        db=db,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(incident)


@router.post("/incidents/{incident_id}/resolve", response_model=TheftIncidentOut)
async def resolve_theft_incident(
    incident_id: str,
    payload: Optional[TheftResolveRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Resolve an incident (e.g. RECOVERED_GOODS, FALSE_ALARM, POLICE_DISPATCHED)."""
    resolution = payload.resolution if payload else "RECOVERED_GOODS"
    notes = payload.notes if payload else None

    incident = await theft_detection_service.resolve_incident(
        incident_id=incident_id,
        resolution=resolution,
        notes=notes,
        db=db,
    )
    if not incident:
        raise HTTPException(status_code=404, detail=f"Theft incident '{incident_id}' not found")
    return _serialize(incident)
