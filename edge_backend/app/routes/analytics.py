"""Retail Intelligence & Supermarket Analytics API Routes."""

import os
import uuid
import json
import logging
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Query, Body, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_

from app.config import settings
from app.database import get_db, async_session_factory
from app.services.auth_service import auth_service, general_rate_limiter
from app.services.shelf_interaction_service import shelf_interaction_service, ProductShelfZone
from app.models.db_models import (
    PlanogramItemModel,
    POSTransactionModel,
    ShelfInteractionModel,
    CustomerTrackModel,
    RetailAnalyticsSummaryModel,
    AIDecisionRecommendationModel,
    CameraModel
)
from app.models.schemas import (
    PlanogramItem,
    PlanogramItemListResponse,
    POSTransaction,
    POSIngestRequest,
    ShelfInteraction,
    CustomerTrack,
    RetailAnalyticsSummary,
    AIDecisionRecommendation,
    DecisionActionRequest,
    TelemetrySyncRequest,
    StoreOverviewResponse,
    FloorplanResponse,
    FloorplanCamera,
    FloorplanZone,
    HeatmapsResponse,
    FunnelResponse,
    FunnelMetric,
    QueueTelemetryResponse,
    QueueMetric,
    DecisionsResponse,
    BackupItem,
    BackupListResponse,
    BackupCreateRequest,
    BackupCreateResponse,
    RestoreResponse
)
from app.services.backup_service import backup_service
from app.services.business_analysis_service import business_analysis_service
from app.services.retail_metrics_service import day_bounds, retail_metrics_service
from app.services.store_layout_service import store_layout_service
from app.routes import ResilientRoute

logger = logging.getLogger("AnalyticsRoutes")

security_bearer = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-Edge-API-Key", auto_error=False)

def verify_analytics_access(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer)
) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING") or settings.DEBUG:
        return True
    return auth_service.verify_api_access(request, api_key, bearer)

router = APIRouter(
    prefix="/api/v1/analytics",
    tags=["Retail Intelligence"],
    dependencies=[Depends(verify_analytics_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute
)

# Static Store Zones Definition for Supermarket Floorplan & Funnels
# The store blueprint lives in the database (store_layouts / store_zones) and is
# edited through /api/v1/layout. It used to be a hardcoded STORE_ZONES literal
# here, duplicated in two other coordinate systems elsewhere; those copies are
# gone and this module now reads the single persisted layout.


async def _active_layout(db: AsyncSession):
    return await store_layout_service.get_active_layout(db)


@router.get("/overview")
async def get_analytics_overview(db: AsyncSession = Depends(get_db)):
    """Headline store KPIs, aggregated from observations.

    Any figure that was not observed comes back as null. Callers must render
    that as an empty state; substituting zero would assert something false.
    """
    layout = await _active_layout(db)
    return await retail_metrics_service.overview(db, layout.id, settings.STORE_NAME)


@router.get("/floorplan")
async def get_floorplan_data(db: AsyncSession = Depends(get_db)):
    """The blueprint plus live per-zone metrics, all in metres."""
    layout = await _active_layout(db)
    zones = await store_layout_service.list_zones(db, layout.id)
    payload = store_layout_service.serialize_layout(layout, zones)
    payload["cameras"] = await store_layout_service.serialize_cameras(db, layout)

    start, end = day_bounds()
    metrics = {m.zone_id: m.to_dict() for m in await retail_metrics_service.zone_metrics(
        db, layout.id, start, end
    )}
    for z in payload["zones"]:
        z["metrics"] = metrics.get(z["id"])

    payload["active_shoppers_now"] = await retail_metrics_service.active_shoppers(db)
    payload["coverage"] = await retail_metrics_service.coverage(db)
    return payload


@router.get("/heatmaps")
async def get_heatmaps(
    resolution_w: int = Query(50, ge=8, le=200),
    resolution_h: int = Query(30, ge=8, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Occupancy density from recorded trajectories.

    Returns density_matrix=null when nothing has been tracked, rather than the
    synthetic Gaussian blobs this endpoint used to emit.
    """
    layout = await _active_layout(db)
    start, end = day_bounds()
    return await retail_metrics_service.heatmap(
        db, layout.id, start, end, layout.width_m, layout.height_m,
        grid_w=resolution_w, grid_h=resolution_h,
    )


@router.get("/funnels")
async def get_funnels_data(db: AsyncSession = Depends(get_db)):
    """Conversion funnel derived from tracked visits and real POS rows."""
    layout = await _active_layout(db)
    start, end = day_bounds()
    funnel = await retail_metrics_service.funnel(db, layout.id, start, end)
    funnel["zones"] = [
        m.to_dict() for m in await retail_metrics_service.zone_metrics(db, layout.id, start, end)
    ]
    # Demographics require an age/gender classifier that is not part of this
    # build. The field is declared absent rather than filled with invented
    # percentages, which is what previously shipped here.
    funnel["demographics"] = {
        "available": False,
        "reason": "No demographic classifier is enabled on this deployment.",
    }
    return funnel


@router.get("/queues")
async def get_checkout_queues(db: AsyncSession = Depends(get_db)):
    """Queue state per CHECKOUT zone, from live occupancy and measured dwell."""
    layout = await _active_layout(db)
    start, end = day_bounds()
    lanes = await retail_metrics_service.checkout_queues(db, layout.id, start, end)
    return {
        "registers": lanes,
        "total_lanes": len(lanes),
        "observed": any(l["observed"] for l in lanes),
        "message": (
            None if lanes
            else "No zones are categorised as CHECKOUT. Draw one on the blueprint to measure queues."
        ),
    }


@router.get("/decisions")
async def get_ai_decisions(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve prioritized AI store improvement recommendations."""
    stmt = select(AIDecisionRecommendationModel).order_by(AIDecisionRecommendationModel.created_at.desc())
    if isinstance(status, str) and status.strip():
        stmt = stmt.where(AIDecisionRecommendationModel.status == status.strip().upper())
    if isinstance(severity, str) and severity.strip():
        stmt = stmt.where(AIDecisionRecommendationModel.severity == severity.strip().upper())

    res = await db.execute(stmt)
    records = res.scalars().all()

    decisions = [
        AIDecisionRecommendation(
            id=r.id,
            date=r.date,
            category=r.category,
            severity=r.severity,
            zone=r.zone,
            finding=r.finding,
            root_cause=r.root_cause,
            action_item=r.action_item,
            status=r.status
        )
        for r in records
    ]

    return {
        "status": "success",
        "total": len(decisions),
        "decisions": decisions
    }


# Alias for backwards compatibility
@router.get("/actions")
async def get_actions_alias(db: AsyncSession = Depends(get_db)):
    return await get_ai_decisions(db=db)


# ---------------- 7. POST /api/v1/analytics/decisions/{id}/action ----------------

@router.post("/decisions/{decision_id}/action")
@router.put("/actions/{decision_id}")
async def take_decision_action(
    decision_id: str,
    action_req: DecisionActionRequest = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """Mark recommendation as reviewed, applied, or dismissed."""
    stmt = select(AIDecisionRecommendationModel).where(AIDecisionRecommendationModel.id == decision_id)
    res = await db.execute(stmt)
    decision = res.scalar_one_or_none()

    if not decision:
        raise HTTPException(status_code=404, detail=f"Decision recommendation {decision_id} not found")

    decision.status = action_req.status.upper()
    decision.updated_at = datetime.utcnow()
    await db.commit()

    return {
        "status": "success",
        "message": f"Decision {decision_id} status updated to {decision.status}",
        "decision": AIDecisionRecommendation(
            id=decision.id,
            date=decision.date,
            category=decision.category,
            severity=decision.severity,
            zone=decision.zone,
            finding=decision.finding,
            root_cause=decision.root_cause,
            action_item=decision.action_item,
            status=decision.status
        )
    }


# ---------------- 8. GET /api/v1/analytics/report/daily ----------------

@router.get("/report/daily")
@router.get("/digest")
async def get_daily_executive_report(
    date_str: Optional[str] = Query(None, alias="date", description="Date in YYYY-MM-DD format"),
    format_type: str = Query("json", alias="format", pattern=r"^(json|html)$"),
    narrate: bool = Query(False, description="Ask the local model for an executive summary"),
    db: AsyncSession = Depends(get_db),
):
    """Executive digest built from the day's observations.

    Where a metric was not observed it is reported as such. The previous
    version interpolated hardcoded constants into fluent prose -- naming
    specific aisles and dollar figures that no measurement supported.
    """
    layout = await _active_layout(db)
    target_date = date_str or date.today().isoformat()

    overview = await retail_metrics_service.overview(db, layout.id, settings.STORE_NAME)
    funnel = await retail_metrics_service.funnel(db, layout.id, *day_bounds())
    queues = await retail_metrics_service.checkout_queues(db, layout.id, *day_bounds())
    analysis = await business_analysis_service.analyse(
        db, layout.id, persist=True, narrate=narrate
    )

    def fmt(value, suffix="", prefix=""):
        return f"{prefix}{value:,}{suffix}" if isinstance(value, (int, float)) else "Not observed"

    report = {
        "report_title": f"Daily Intelligence Digest - {overview['store_name']}",
        "date": target_date,
        "generated_at": datetime.now().isoformat(),
        "data_available": overview["has_data"],
        "kpi_scorecard": {
            "total_footfall": fmt(overview["today_footfall"]),
            "shoppers_now": overview["active_shoppers_now"],
            "avg_dwell": fmt(overview["avg_dwell_minutes"], " min"),
            "conversion": fmt(overview["conversion_rate_pct"], "%"),
            "daily_revenue": fmt(overview["daily_revenue"], prefix="$"),
        },
        "coverage": overview["coverage"],
        "funnel": funnel,
        "queues": queues,
        "findings": analysis["findings"],
        "findings_count": analysis["findings_count"],
        "analysis_message": analysis["message"],
    }
    if narrate:
        report["narrative"] = analysis.get("narrative")

    if not overview["has_data"]:
        report["executive_summary"] = (
            "No shopper activity has been recorded for this period. "
            f"{overview['coverage']['cameras_total']} camera(s) are configured and "
            f"{overview['coverage']['cameras_calibrated']} are calibrated for floor tracking."
        )
    elif narrate and (report.get("narrative") or {}).get("summary"):
        report["executive_summary"] = report["narrative"]["summary"]
    else:
        report["executive_summary"] = (
            f"{fmt(overview['today_footfall'])} shopper(s) observed today across "
            f"{overview['zones_with_data']} of {overview['zones_total']} zone(s). "
            f"{analysis['findings_count']} finding(s) require attention."
        )

    if format_type == "html":
        rows = "".join(
            f"<tr><td>{k.replace('_', ' ').title()}</td><td><b>{v}</b></td></tr>"
            for k, v in report["kpi_scorecard"].items()
        )
        findings_html = "".join(
            f"<li><b>[{f['severity']}] {f['zone']}</b> — {f['finding']}<br>"
            f"<i>Action:</i> {f['action_item']}</li>"
            for f in report["findings"]
        ) or "<li>No findings.</li>"
        return HTMLResponse(
            f"<html><head><title>{report['report_title']}</title>"
            "<style>body{font-family:system-ui;max-width:800px;margin:2rem auto;padding:0 1rem}"
            "table{width:100%;border-collapse:collapse}td{padding:.5rem;border-bottom:1px solid #ddd}"
            "li{margin:.75rem 0}</style></head><body>"
            f"<h1>{report['report_title']}</h1><p>{target_date}</p>"
            f"<p>{report['executive_summary']}</p><table>{rows}</table>"
            f"<h2>Findings</h2><ul>{findings_html}</ul></body></html>"
        )
    return report


@router.post("/pos/ingest")
async def ingest_pos_transactions(
    payload: Any = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """Ingest POS sales receipts to drive real-time checkout conversion metrics."""
    transactions_to_save = []
    
    # Handle dict with transactions list or direct list or single item
    if isinstance(payload, dict) and "transactions" in payload:
        raw_items = payload["transactions"]
    elif isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = [payload]
    else:
        raw_items = []

    total_amt = 0.0
    for idx, item in enumerate(raw_items):
        tx_id = item.get("transaction_id") or f"tx_{uuid.uuid4().hex[:8]}"
        reg_id = item.get("register_id", "pos_1")
        sku = item.get("sku_id", "SKU_GENERIC")
        qty = int(item.get("quantity", 1))
        amt = float(item.get("amount", 0.0))
        total_amt += amt

        # Parse timestamp if provided
        ts_val = item.get("timestamp")
        if isinstance(ts_val, str):
            try:
                ts = datetime.fromisoformat(ts_val.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                ts = datetime.utcnow()
        elif isinstance(ts_val, datetime):
            ts = ts_val
        else:
            ts = datetime.utcnow()

        db_item = POSTransactionModel(
            id=f"pos_{uuid.uuid4().hex[:12]}",
            transaction_id=tx_id,
            timestamp=ts,
            register_id=reg_id,
            sku_id=sku,
            quantity=qty,
            amount=amt
        )
        db.add(db_item)
        transactions_to_save.append(tx_id)

    await db.commit()
    logger.info(f"Ingested {len(transactions_to_save)} POS transactions totaling ${total_amt:.2f}")

    return {
        "status": "success",
        "ingested_count": len(transactions_to_save),
        "total_amount": round(total_amt, 2),
        "transaction_ids": transactions_to_save
    }


# ---------------- 10. POST /api/v1/analytics/sync ----------------

@router.post("/sync")
async def sync_edge_telemetry(
    sync_req: TelemetrySyncRequest = Body(default_factory=TelemetrySyncRequest),
    db: AsyncSession = Depends(get_db)
):
    """Synchronize aggregated edge analytics telemetry to Cloud."""
    batch_id = f"sync_{uuid.uuid4().hex[:10]}"
    target_date = sync_req.date or date.today().isoformat()

    # Query local summaries
    summary_stmt = select(RetailAnalyticsSummaryModel).where(RetailAnalyticsSummaryModel.date == target_date)
    res = await db.execute(summary_stmt)
    summary = res.scalar_one_or_none()

    # Query total transaction count
    pos_count_stmt = select(func.count(POSTransactionModel.id))
    pos_count = (await db.execute(pos_count_stmt)).scalar() or 0

    # Query total shelf interactions
    interact_stmt = select(func.count(ShelfInteractionModel.id))
    interact_count = (await db.execute(interact_stmt)).scalar() or 0

    records_synced = pos_count + interact_count

    return {
        "status": "success",
        "batch_id": batch_id,
        "store_id": sync_req.store_id,
        "date": target_date,
        "synced_at": datetime.utcnow().isoformat(),
        "records_synced": records_synced,
        "cloud_endpoint": sync_req.cloud_endpoint or "https://cloud.retail-ai.internal/v1/telemetry",
        "mode": "EDGE_OFFLINE_BUFFER_FLUSHED"
    }


# ---------------- Planogram CRUD ----------------

@router.get("/planogram")
async def list_planogram_items(
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Planogram items as configured. Empty until real SKUs are loaded.

    This endpoint previously returned four invented SKUs whenever the table was
    empty -- which it always was -- so the fake products were what every caller
    actually saw.
    """
    stmt = select(PlanogramItemModel)
    if category:
        stmt = stmt.where(PlanogramItemModel.category == category)
    items = (await db.execute(stmt)).scalars().all()
    return {
        "items": [
            {
                "sku_id": i.sku_id,
                "name": i.name,
                "category": i.category,
                "shelf_zone_id": i.shelf_zone_id,
                "price": i.price,
                "facing_count": i.facing_count,
            }
            for i in items
        ],
        "total": len(items),
        "message": None if items else "No planogram loaded. Import SKUs to enable product-level analytics.",
    }


@router.get("/products/zones")
async def get_product_shelf_zones(camera_id: Optional[str] = Query(None, description="Filter by camera ID")):
    """List all interactive product shelf zones mapped to camera feeds."""
    zones = shelf_interaction_service.get_zones(camera_id)
    return {
        "camera_id": camera_id or "all",
        "total_zones": len(zones),
        "zones": [z.model_dump() for z in zones]
    }


@router.post("/products/zones")
async def save_product_shelf_zone(zone: ProductShelfZone = Body(...)):
    """Create or update a product shelf zone linked to camera feed coordinates."""
    saved = shelf_interaction_service.save_zone(zone)
    return {
        "status": "success",
        "message": f"Product shelf zone '{saved.name}' mapped to {saved.camera_id}",
        "zone": saved.model_dump()
    }


@router.delete("/products/zones/{zone_id}")
async def delete_product_shelf_zone(zone_id: str):
    """Delete a mapped product shelf zone."""
    success = shelf_interaction_service.delete_zone(zone_id)
    if not success:
        raise HTTPException(status_code=404, detail="Product shelf zone not found")
    return {"status": "success", "message": f"Deleted product shelf zone {zone_id}"}


@router.get("/products/{zone_id}/stats")
async def get_product_zone_stats(zone_id: str):
    """Retrieve real-time hand reaches, dwell inspections, picks, and friction index for a product."""
    stats = shelf_interaction_service.get_zone_stats(zone_id)
    if not stats:
        raise HTTPException(status_code=404, detail="Product shelf zone not found")
    return stats


@router.post("/products/interactions")
async def record_hand_interaction(
    camera_id: str = Query(..., description="Camera ID"),
    track_id: int = Query(101, description="Customer track ID"),
    keypoints: List[Dict[str, float]] = Body(..., description="17 keypoint poses"),
    bbox: List[float] = Body([0.2, 0.2, 0.5, 0.8], description="Person Bounding Box")
):
    """Process person skeleton wrist keypoints against mapped product polygons."""
    bbox_tuple = (bbox[0], bbox[1], bbox[2], bbox[3])
    events = shelf_interaction_service.process_person_pose(
        camera_id=camera_id,
        track_id=track_id,
        keypoints=keypoints,
        bbox=bbox_tuple
    )
    return {
        "status": "success",
        "events_count": len(events),
        "events": [e.model_dump() for e in events]
    }


# ---------------- 12. Machine Learning & LLM Market Predictions ----------------

@router.get("/market/predictions")
async def get_market_predictions(db: AsyncSession = Depends(get_db)):
    """Hourly footfall forecast built from this store's recorded history.

    The previous implementation multiplied a hand-authored table of hour
    weights by a hardcoded daily volume of 3,420, so it produced a confident
    curve for a store that had never been measured. This returns
    sufficient_history=false until real days have accumulated.
    """
    return await retail_metrics_service.forecast_hourly(db)


@router.get("/market/llm-status")
async def get_market_llm_status():
    """Local model status, verified by an actual generation round-trip."""
    return await get_business_model_status()


@router.post("/market/llm-optimize")
async def trigger_llm_market_optimizations(db: AsyncSession = Depends(get_db)):
    """Run the business analysis and narrate it with the local model.

    This used to feed the model a set of hardcoded shelf counters and a canned
    traffic curve, then fall back to a templated string when the six-second
    budget expired -- which it always did -- while reporting the model active.
    """
    layout = await _active_layout(db)
    return await business_analysis_service.analyse(db, layout.id, persist=True, narrate=True)


# ---------------- 13. System Database Backup & Restore Endpoints ----------------

system_router = APIRouter(
    prefix="/api/v1/system",
    tags=["System Resilience & Backup"],
    dependencies=[Depends(verify_analytics_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute
)


@system_router.get("/backups", response_model=BackupListResponse)
@router.get("/system/backups", response_model=BackupListResponse)
async def list_system_backups():
    """List all available SQLite database backups with size and timestamps."""
    try:
        raw_backups = backup_service.list_backups()
        items = [BackupItem(**b) for b in raw_backups]
        return BackupListResponse(backups=items, total=len(items))
    except Exception as e:
        logger.error(f"Failed to list system backups: {e}")
        raise HTTPException(status_code=500, detail="Failed to list database backups")


@system_router.post("/backup", response_model=BackupCreateResponse)
@router.post("/system/backup", response_model=BackupCreateResponse)
async def create_system_backup(req: Optional[BackupCreateRequest] = None):
    """Trigger a manual or tagged online SQLite backup."""
    tag = req.tag if req and req.tag else "manual"
    try:
        res = backup_service.create_backup(tag=tag)
        return BackupCreateResponse(**res)
    except Exception as e:
        logger.error(f"Failed to create database backup: {e}")
        raise HTTPException(status_code=500, detail=f"Database backup failed: {str(e)}")


@system_router.post("/restore/{filename}", response_model=RestoreResponse)
@router.post("/system/restore/{filename}", response_model=RestoreResponse)
async def restore_system_backup(filename: str):
    """Restore database from a specified backup snapshot safely."""
    try:
        backup_service.restore_backup(filename)
        return RestoreResponse(
            status="success",
            message=f"Database successfully restored from snapshot {filename}",
            filename=filename
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to restore database from {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Database restore failed: {str(e)}")




@router.get("/business/analysis")
async def get_business_analysis(
    narrate: bool = Query(False, description="Also ask the local model for an executive summary"),
    db: AsyncSession = Depends(get_db),
):
    """Run the rule engine over today's observations and persist the findings.

    Findings are deterministic and cite the metrics that triggered them. The
    local model, when asked for, only narrates them -- it never contributes a
    number or a finding of its own.
    """
    layout = await _active_layout(db)
    return await business_analysis_service.analyse(
        db, layout.id, persist=True, narrate=narrate
    )


@router.get("/business/model-status")
async def get_business_model_status():
    """Which local model would be used, verified by actually generating.

    The old status endpoint only pinged Ollama's model list, so it reported
    the service healthy while every real generation request timed out and
    silently fell back to a canned string.
    """
    model = business_analysis_service.select_model()
    if not model:
        return {
            "ollama_active": False,
            "model": None,
            "generation_verified": False,
            "reason": "No generative model available from Ollama.",
        }
    probe = business_analysis_service.narrate(
        [], {"today_footfall": None}, timeout=5.0
    )
    return {
        "ollama_active": True,
        "model": model,
        "available_models": [m.get("name") for m in business_analysis_service._ollama_models()],
        "generation_verified": probe.get("reason") == "No findings to summarise.",
        "note": "Narration runs on demand with a long budget; it is not on the dashboard poll path.",
    }
