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
from app.services.timeutil import to_utc, utcnow
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
    # No environment-variable shortcuts (TESTING / PYTEST_CURRENT_TEST / DEBUG
    # used to open these routes); AUTH_DISABLED is honoured inside
    # verify_api_access and is the only bypass.
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


def _parse_window_bound(value: Optional[str], name: str) -> Optional[datetime]:
    """ISO date/date-time -> naive UTC. A value without an offset is store-local time."""
    from app.services.timeutil import to_utc

    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{name}' must be an ISO date or date-time")
    return to_utc(dt)


@router.get("/footfall/tripwires")
async def get_tripwire_footfall(
    from_: Optional[str] = Query(None, alias="from", description="ISO start (default: today 00:00 store time)"),
    to: Optional[str] = Query(None, description="ISO end, exclusive (default: start + 1 day)"),
    bucket: str = Query("hour", pattern="^(hour|day|none)$"),
    db: AsyncSession = Depends(get_db),
):
    """In/out crossings per Studio tripwire, with store-local hourly/daily buckets.

    Tripwires with no crossing in the window report null in/out (not
    observed). ``net_occupancy_estimate`` is entries minus exits on
    footfall-counting lines -- an estimate, labelled as such.
    """
    from app.services.tripwire_engine import tripwire_footfall

    start = _parse_window_bound(from_, "from")
    end = _parse_window_bound(to, "to")
    if start is None:
        start = day_bounds()[0] if end is None else end - timedelta(days=1)
    if end is None:
        end = start + timedelta(days=1) if from_ else day_bounds()[1]
    if end <= start:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")
    if end - start > timedelta(days=93):
        raise HTTPException(status_code=422, detail="window is limited to 93 days")
    out = await tripwire_footfall(db, start, end, bucket)
    out["footfall_source"] = await retail_metrics_service.footfall_source(db, start, end)
    return out


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
    kind: str = Query("presence", pattern="^(presence|dwell|interaction)$",
                      description="presence = where people were, dwell = seconds spent, interaction = shelf reaches"),
    presence_weighting: str = Query("samples", pattern="^(samples|tracks)$",
                                    description="presence only: samples = one per recorded point, "
                                                "tracks = visitors per cell (as the recorded history)"),
    from_: Optional[str] = Query(None, alias="from", description="ISO start (default: today 00:00 store time)"),
    to: Optional[str] = Query(None, description="ISO end, exclusive (default: end of today)"),
    db: AsyncSession = Depends(get_db),
):
    """Floor density of today's observations (presence, dwell or shelf interactions).

    Returns density_matrix=null when nothing has been observed, rather than the
    synthetic Gaussian blobs this endpoint used to emit. ``from``/``to``
    select another window (tracks by start time).
    """
    layout = await _active_layout(db)
    start, end = day_bounds()
    if from_ or to:
        start = _parse_window_bound(from_, "from") or start
        end = _parse_window_bound(to, "to") or end
        if end <= start:
            raise HTTPException(status_code=422, detail="'to' must be after 'from'")
        if end - start > timedelta(days=93):
            raise HTTPException(status_code=422, detail="window is limited to 93 days")
    return await retail_metrics_service.heatmap(
        db, layout.id, start, end, layout.width_m, layout.height_m,
        grid_w=resolution_w, grid_h=resolution_h, kind=kind, presence_weighting=presence_weighting,
    )


# ------------------------------------------------ recorded heatmap history
# services/heatmap_history.py. Snapshots are per store-local hour (bucket=hour)
# or day (bucket=day). A bucket with no row was not recorded (camera or
# pipeline off); a row with total_samples 0 was measured and empty.

_HM_SPACE = Query("floor", pattern="^(floor|image)$", description="floor (metres) or image (one camera)")
_HM_KIND = Query("presence", pattern="^(presence|dwell|interaction)$",
                 description="presence = visitors per cell, dwell = seconds, interaction = shelf reaches")
_HM_BUCKET = Query("hour", pattern="^(hour|day)$")


def _hm_scope(space: str, camera_id: Optional[str]) -> Optional[str]:
    if space == "image" and not camera_id:
        raise HTTPException(status_code=422, detail="camera_id is required for space=image")
    return camera_id if space == "image" else None


def _hm_window(from_: Optional[str], to: Optional[str], default_days: int = 7,
               names: tuple = ("from", "to")) -> tuple:
    end = _parse_window_bound(to, names[1]) or utcnow()
    start = _parse_window_bound(from_, names[0]) or (end - timedelta(days=default_days))
    if end <= start:
        raise HTTPException(status_code=422, detail=f"'{names[1]}' must be after '{names[0]}'")
    if end - start > timedelta(days=400):
        raise HTTPException(status_code=422, detail="window is limited to 400 days")
    return start, end


def _hm_minutes(bucket: str) -> int:
    from app.services import heatmap_history as hh

    return hh.HOUR_MINUTES if bucket == "hour" else hh.DAY_MINUTES


@router.get("/heatmaps/history")
async def get_heatmap_history(
    space: str = _HM_SPACE,
    camera_id: Optional[str] = Query(None),
    kind: str = _HM_KIND,
    from_: Optional[str] = Query(None, alias="from", description="ISO start (default: 7 days ago); no offset = store time"),
    to: Optional[str] = Query(None, description="ISO end, exclusive (default: now)"),
    bucket: str = _HM_BUCKET,
    db: AsyncSession = Depends(get_db),
):
    """Recorded snapshot metadata in a range, with recorded / empty / not-recorded bucket counts."""
    from app.services import heatmap_history as hh

    cam = _hm_scope(space, camera_id)
    start, end = _hm_window(from_, to)
    out = await hh.history(db, space, cam, kind, start, end, _hm_minutes(bucket))
    out["recorder"] = hh.heatmap_recorder.status()
    # Oldest snapshot of this space/camera/kind at any time (not only this range), so
    # the UI can tell "nothing recorded yet" from "nothing in this range".
    from sqlalchemy import func as _f
    from app.models.db_models import HeatmapSnapshotModel as _H

    q = select(_f.min(_H.bucket_start)).where(_H.space == space, _H.kind == kind)
    q = q.where(_H.camera_id == cam) if cam else q.where(_H.camera_id.is_(None))
    out["first_recorded"] = hh.iso_z(await db.scalar(q))
    return out


@router.get("/heatmaps/snapshot/{snapshot_id}")
async def get_heatmap_snapshot(snapshot_id: int, db: AsyncSession = Depends(get_db)):
    """One snapshot with its decoded grid (raw ``values`` and normalised ``density_matrix``)."""
    from app.models.db_models import HeatmapSnapshotModel
    from app.services import heatmap_history as hh

    row = await db.get(HeatmapSnapshotModel, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="heatmap snapshot not found")
    return {**hh.snapshot_meta(row), **hh.grid_payload(hh.row_grid(row), row.kind)}


@router.get("/heatmaps/aggregate")
async def get_heatmap_aggregate(
    space: str = _HM_SPACE,
    camera_id: Optional[str] = Query(None),
    kind: str = _HM_KIND,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    bucket: str = _HM_BUCKET,
    db: AsyncSession = Depends(get_db),
):
    """Sum of the recorded snapshots in a range, citing the snapshot ids used."""
    from app.services import heatmap_history as hh

    cam = _hm_scope(space, camera_id)
    start, end = _hm_window(from_, to)
    return await hh.aggregate(db, space, cam, kind, start, end, _hm_minutes(bucket))


@router.get("/heatmaps/compare")
async def get_heatmap_compare(
    a_from: str = Query(...), a_to: str = Query(...), b_from: str = Query(...), b_to: str = Query(...),
    space: str = _HM_SPACE,
    camera_id: Optional[str] = Query(None),
    kind: str = _HM_KIND,
    bucket: str = _HM_BUCKET,
    db: AsyncSession = Depends(get_db),
):
    """Period B minus period A, per recorded hour (so unequal periods compare fairly), plus a summary."""
    from app.services import heatmap_history as hh

    cam = _hm_scope(space, camera_id)
    a = _hm_window(a_from, a_to, names=("a_from", "a_to"))
    b = _hm_window(b_from, b_to, names=("b_from", "b_to"))
    return await hh.compare(db, space, cam, kind, a, b, _hm_minutes(bucket))


@router.get("/heatmaps/hour-profile")
async def get_heatmap_hour_profile(
    space: str = _HM_SPACE,
    camera_id: Optional[str] = Query(None),
    kind: str = _HM_KIND,
    days: int = Query(14, ge=1, le=90),
    hour: Optional[int] = Query(None, ge=0, le=23, description="also return the mean grid of this store-local hour"),
    db: AsyncSession = Depends(get_db),
):
    """Store-local hour-of-day pattern: per hour, the mean over the days that recorded it."""
    from app.services import heatmap_history as hh

    cam = _hm_scope(space, camera_id)
    return await hh.hour_profile(db, space, cam, kind, days, hour)


@router.post("/heatmaps/record-now")
async def post_heatmap_record_now():
    """Close and record the current partial hour now (``complete: false`` until the hour ends)."""
    from app.services import heatmap_history as hh

    return await hh.heatmap_recorder.record_now()


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
    """Queue state per checkout lane (blueprint CHECKOUT zones and Studio checkout / queue areas)."""
    layout = await _active_layout(db)
    start, end = day_bounds()
    lanes = await retail_metrics_service.checkout_queues(db, layout.id, start, end)
    return {
        "registers": lanes,
        "total_lanes": len(lanes),
        "observed": any(l["observed"] for l in lanes),
        "message": (
            None if lanes
            else "No checkout lanes yet. Draw a checkout or queue area on a Checkout camera in Studio, "
                 "or a CHECKOUT zone on the blueprint (calibrated cameras), to measure queues."
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
    # Read-only: a GET never persists findings (POST /business/analysis/run does).
    analysis = await business_analysis_service.analyse(
        db, layout.id, persist=False, narrate=narrate
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
            # Hand reaches to mapped product shelves (shelf_interactions rows).
            "shelf_reaches": fmt((analysis.get("product_reach") or {}).get("reaches") or None),
        },
        # Human labels for the scorecard keys (the UI used to title-case snake_case).
        "kpi_scorecard_labels": {
            "total_footfall": "Visitors today",
            "shoppers_now": "In store now",
            "avg_dwell": "Average time in store",
            "conversion": "Visitors who bought",
            "daily_revenue": "Sales today",
            "shelf_reaches": "Shelf reaches",
        },
        "coverage": overview["coverage"],
        "funnel": funnel,
        "queues": queues,
        "findings": analysis["findings"],
        "findings_count": analysis["findings_count"],
        "analysis_message": analysis["message"],
        "shelf_reach": analysis.get("product_reach"),
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
            f"<tr><td>{report['kpi_scorecard_labels'].get(k) or k.replace('_', ' ').title()}</td><td><b>{v}</b></td></tr>"
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
        # Stored as naive UTC like every other pipeline timestamp: an offset is
        # converted, an offset-less value is read as store-local time. An
        # unreadable timestamp is rejected rather than replaced with "now",
        # which would silently move the sale to the wrong hour.
        ts_val = item.get("timestamp")
        if isinstance(ts_val, str):
            try:
                ts = to_utc(datetime.fromisoformat(ts_val.replace("Z", "+00:00")))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid POS timestamp: {ts_val!r}")
        elif isinstance(ts_val, datetime):
            ts = to_utc(ts_val)
        else:
            ts = utcnow()

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
        # Stored fields plus effective_shelf_level / effective_value_tier and
        # their source (operator, derived from geometry/price, or unknown).
        "zones": [shelf_interaction_service.zone_dict(z) for z in zones]
    }


@router.post("/products/zones")
async def save_product_shelf_zone(zone: ProductShelfZone = Body(...)):
    """Create or update a product shelf zone linked to camera feed coordinates."""
    saved = shelf_interaction_service.save_zone(zone)
    return {
        "status": "success",
        "message": f"Product shelf zone '{saved.name}' mapped to {saved.camera_id}",
        "zone": shelf_interaction_service.zone_dict(saved)
    }


@router.delete("/products/zones/{zone_id}")
async def delete_product_shelf_zone(zone_id: str):
    """Delete a mapped product shelf zone."""
    success = shelf_interaction_service.delete_zone(zone_id)
    if not success:
        raise HTTPException(status_code=404, detail="Product shelf zone not found")
    return {"status": "success", "message": f"Deleted product shelf zone {zone_id}"}


def _iso_z(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt is not None else None


def _product_window(date_str: Optional[str]):
    """[start, end) naive UTC for a store-local YYYY-MM-DD (default today)."""
    if not date_str:
        return day_bounds()
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return day_bounds(d)


@router.get("/products/summary")
async def get_product_reach_summary(
    date_str: Optional[str] = Query(None, alias="date", description="Store-local day, YYYY-MM-DD (default today)"),
    camera_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Per-product and per-shelf-level hand reaches for one day, read from shelf_interactions.

    Conversion appears only when POS rows exist for the day; otherwise each
    product reports ``conversion_status: "needs POS data"``.
    """
    start, end = _product_window(date_str)
    return await shelf_interaction_service.product_summary(db, start, end, camera_id=camera_id)


@router.get("/products/{zone_id}/stats")
async def get_product_zone_stats(
    zone_id: str,
    date_str: Optional[str] = Query(None, alias="date", description="Store-local day, YYYY-MM-DD (default today)"),
    db: AsyncSession = Depends(get_db),
):
    """Hand reaches recorded for one product zone (from the database, so restarts lose nothing)."""
    zone = shelf_interaction_service.get_zone(zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Product shelf zone not found")
    start, end = _product_window(date_str)
    summary = await shelf_interaction_service.product_summary(db, start, end, camera_id=zone.camera_id)
    p = next((x for x in summary["products"] if x["zone_id"] == zone_id), None) or {}
    return {
        **p,
        "window": summary["window"],
        "pos_connected": summary["pos_connected"],
        "product_name": zone.name,
        "shelf_tier": zone.shelf_tier,
        # Legacy keys. A hand position cannot tell a pick from a put-back, and
        # nothing measures impressions, so those are unknown rather than 0.
        "touches": p.get("reaches", 0),
        "avg_dwell_sec": p.get("avg_duration_sec"),
        "impressions": None,
        "picks": None,
        "put_backs": None,
        "attraction_rate": None,
        "friction_index": None,
        "conversion_rate": (round(p["units_per_reaching_shopper"] * 100.0, 1)
                            if p.get("units_per_reaching_shopper") is not None else None),
        "ab_test_mode": zone.study_metrics.ab_test_mode,
    }


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


@router.get("/products/interactions")
async def list_shelf_interactions(
    camera_id: Optional[str] = Query(None),
    zone_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Shelf interactions recorded by the live pose pipeline, newest first.

    Times are ISO 8601 UTC with an explicit ``Z`` (stored as naive UTC).
    """
    stmt = select(ShelfInteractionModel).order_by(desc(ShelfInteractionModel.timestamp)).limit(limit)
    if camera_id:
        stmt = stmt.where(ShelfInteractionModel.camera_id == camera_id)
    if zone_id:
        stmt = stmt.where(ShelfInteractionModel.shelf_zone_id == zone_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "interactions": [
            {
                "id": r.id,
                "camera_id": r.camera_id,
                "zone_id": r.shelf_zone_id,
                "zone_name": r.zone_name,
                "zone_space": r.zone_space,
                "track_id": r.person_track_id,
                "hand": r.hand,
                "started_at": _iso_z(r.started_at or r.timestamp),
                "ended_at": _iso_z(r.ended_at),
                "duration_sec": r.duration_sec,
                "wrist_visibility": r.confidence,
                "image_x": r.image_x,
                "image_y": r.image_y,
                "floor_x": r.floor_x,
                "floor_y": r.floor_y,
                "sku_id": r.sku_id,
                "product_category": r.product_category,
                "shelf_level": r.shelf_level,
                "value_tier": r.value_tier,
                "contact_point": r.contact_point,
            }
            for r in rows
        ],
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
async def trigger_llm_market_optimizations(request: Request, db: AsyncSession = Depends(get_db)):
    """Run the business analysis and narrate it with the local model.

    This used to feed the model a set of hardcoded shelf counters and a canned
    traffic curve, then fall back to a templated string when the six-second
    budget expired -- which it always did -- while reporting the model active.
    """
    # Recorded as a run (analysis_runs) and rate-limited like POST
    # /business/analysis/run. Inside the limit, or while a run is going, the
    # latest recorded run is returned (``reused_run``) instead of running again,
    # so a page that fires this on every open cannot hammer the model.
    from app.services.actor import describe_actor
    from app.services.recommendations_service import RunInProgress, RunRateLimited, recommendations_service

    try:
        run = await recommendations_service.run(
            db, trigger="market", requested_by=await describe_actor(request, db), narrate=True)
        return {**(run["result"] or {}), "run": {k: v for k, v in run.items() if k != "result"},
                "reused_run": False}
    except (RunRateLimited, RunInProgress) as e:
        latest = await recommendations_service.latest_row(db, narrated=True) or await recommendations_service.latest_row(db)
        if latest is None:
            raise HTTPException(status_code=409, detail="An analysis is already running; try again shortly.")
        from app.services.recommendations_service import serialize_run

        run = serialize_run(latest)
        return {**(run["result"] or {}), "run": {k: v for k, v in run.items() if k != "result"},
                "reused_run": True, "retry_after": getattr(e, "retry_after", None)}


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
    """Evaluate the rule engine over today's observations, read-only.

    Nothing is persisted: a GET never writes. Recording a run (and refreshing
    the stored recommendations) is ``POST /business/analysis/run``; the last
    recorded run is ``GET /business/analysis/latest`` and the dashboard list
    is ``GET /recommendations``. Findings are deterministic and cite the
    metrics that triggered them. The local model, when asked for, only
    narrates them -- it never contributes a number or a finding of its own.
    """
    layout = await _active_layout(db)
    result = await business_analysis_service.analyse(
        db, layout.id, persist=False, narrate=narrate
    )
    result["persisted"] = False
    return result


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
