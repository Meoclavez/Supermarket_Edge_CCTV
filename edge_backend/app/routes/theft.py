"""Loss Prevention & Theft Detection API Routes with SQLite DB Persistence."""

import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.schemas import (
    TheftIncident,
    TheftIncidentListResponse,
    TheftStatisticsResponse,
    TheftAcknowledgeRequest,
    TheftDispatchRequest,
    TheftResolveRequest,
    TheftSimulateRequest,
)
from app.services.theft_detection_service import theft_detection_service

logger = logging.getLogger("TheftRoutes")

router = APIRouter(
    prefix="/api/v1/theft",
    tags=["Loss Prevention & Theft Detection"],
)


@router.get("/incidents", response_model=TheftIncidentListResponse)
async def get_theft_incidents(
    status: Optional[str] = Query(None, description="Filter by status (ACTIVE, ACKNOWLEDGED, DISPATCHED, RESOLVED, FALSE_ALARM)"),
    severity: Optional[str] = Query(None, description="Filter by severity (CRITICAL, HIGH, MEDIUM, LOW)"),
    department: Optional[str] = Query(None, description="Filter by department name"),
    limit: int = Query(50, ge=1, le=200, description="Max incidents to retrieve"),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve filtered list of loss prevention & theft incidents from SQLite."""
    try:
        incidents = await theft_detection_service.get_incidents(
            db=db,
            status=status,
            severity=severity,
            department=department,
            limit=limit,
        )
        return TheftIncidentListResponse(
            incidents=[TheftIncident.model_validate(inc) for inc in incidents],
            total=len(incidents),
        )
    except Exception as e:
        logger.error(f"Error fetching theft incidents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/incidents/{incident_id}/acknowledge", response_model=TheftIncident)
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
    return TheftIncident.model_validate(incident)


@router.post("/incidents/{incident_id}/dispatch", response_model=TheftIncident)
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
    return TheftIncident.model_validate(incident)


@router.post("/incidents/{incident_id}/resolve", response_model=TheftIncident)
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
    return TheftIncident.model_validate(incident)


@router.post("/simulate", response_model=TheftIncident)
async def simulate_theft(
    payload: Optional[TheftSimulateRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a live simulation of a theft event with real SQLite database persistence."""
    try:
        theft_type = payload.theft_type if payload else "SHELF_SWEEPING"
        camera_id = (payload.camera_id if payload else None) or "cam_liquor_zone"
        department = (payload.department if payload else None) or "Liquor & Spirits"
        estimated_loss_value = payload.estimated_loss_value if payload else None

        incident = await theft_detection_service.simulate_theft_incident(
            theft_type=theft_type,
            camera_id=camera_id,
            department=department,
            estimated_loss_value=estimated_loss_value,
            db=db,
        )
        return TheftIncident.model_validate(incident)
    except Exception as e:
        logger.error(f"Error simulating theft incident: {e}")
        raise HTTPException(status_code=500, detail=str(e))
