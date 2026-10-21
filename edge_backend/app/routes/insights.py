"""Dashboard insight endpoints: hourly visitors, analysis runs, one recommendations list.

Same prefix, authentication and rate limiting as ``routes/analytics.py``; kept
in its own module so its handlers stay small and read-only unless they are
POSTs. Every GET here only reads.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import POSTransactionModel
from app.routes import ResilientRoute
from app.routes.analytics import verify_analytics_access
from app.services.actor import describe_actor
from app.services.auth_service import general_rate_limiter, operator_allowed, require_admin
from app.services.hourly_traffic import hourly_visitors
from app.services.insight_topics import insight_topic_service
from app.services.recommendations_service import RunInProgress, RunRateLimited, recommendations_service
from app.services.retail_metrics_service import day_bounds
from app.services.timeutil import to_local

logger = logging.getLogger("InsightsRoutes")

router = APIRouter(
    prefix="/api/v1/analytics",
    tags=["Retail Intelligence"],
    dependencies=[Depends(verify_analytics_access), Depends(general_rate_limiter)],
    route_class=ResilientRoute,
)


@router.get("/footfall/hourly")
async def get_hourly_visitors(
    date_str: Optional[str] = Query(None, alias="date", description="Store-local YYYY-MM-DD (default: today)"),
    db: AsyncSession = Depends(get_db),
):
    """Measured visitors per store-local hour, with yesterday and last week's same weekday.

    Hours without evidence of recording are ``no_data`` with null counts, never 0.
    See ``services/hourly_traffic.py`` for definitions.
    """
    d = None
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=422, detail="'date' must be YYYY-MM-DD")
    return await hourly_visitors(db, d)


class AnalysisRunRequest(BaseModel):
    narrate: bool = False


def _rate_limited(e: RunRateLimited) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(e.retry_after)},
        content={"detail": f"Analysis ran moments ago; try again in {e.retry_after}s.",
                 "retry_after": e.retry_after, "latest_run_id": e.latest_run_id},
    )


@router.post("/business/analysis/run")
async def run_business_analysis(
    request: Request,
    payload: Optional[AnalysisRunRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
):
    """Run the rule engine now (optionally narrated) and return the recorded run.

    Rate-limited: 429 with ``Retry-After`` inside the minimum interval, 409
    while another run is in progress.
    """
    try:
        return await recommendations_service.run(
            db, trigger="manual", requested_by=await describe_actor(request, db),
            narrate=bool(payload and payload.narrate))
    except RunRateLimited as e:
        return _rate_limited(e)
    except RunInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/business/analysis/latest")
async def get_latest_business_analysis(db: AsyncSession = Depends(get_db)):
    """The latest recorded run: result, generated_at and a summary of its inputs. Never runs one."""
    return await recommendations_service.latest(db)


@router.get("/recommendations")
async def get_recommendations(
    status: Optional[str] = Query(None, description="PENDING, REVIEWED, APPLIED, DISMISSED or INFO"),
    days: int = Query(7, ge=1, le=90, description="Findings from this many store days"),
    include_dismissed: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Every recommendation in one list (rule findings + latest AI summary). Read-only."""
    return await recommendations_service.consolidated(
        db, status=status, days=days, include_dismissed=include_dismissed)


# ------------------------------------------------------------ insight topics
# The daily findings above, grouped into recurring issues with one unified
# analysis each (services/insight_topics.py). Under /recommendations, so the
# fleet gate's business_insights rule covers them. Operators may handle
# topics (daily use); the bulk backfill is for owners/administrators.

_TOPIC_STATUSES = ("open", "acknowledged", "snoozed", "resolved")
_TOPIC_TRENDS = ("worsening", "recurring", "new", "stable", "improving")


class TopicSnoozeRequest(BaseModel):
    days: int = Field(7, ge=1, le=90)


class TopicActionDoneRequest(BaseModel):
    action_id: Optional[str] = Field(None, max_length=64)
    date: Optional[str] = Field(None, description="Store-local YYYY-MM-DD (default: today)")


class TopicSplitRequest(BaseModel):
    insight_id: str = Field(..., min_length=1, max_length=64)


class TopicMergeRequest(BaseModel):
    other_topic_id: str = Field(..., min_length=1, max_length=64)


class TopicReanalyseRequest(BaseModel):
    narrate: bool = False


async def _topic_or_404(db: AsyncSession, topic_id: str):
    t = await insight_topic_service.get(db, topic_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Topic not found (it may have been merged).")
    return t


@router.get("/recommendations/topics")
async def list_insight_topics(
    status: Optional[str] = Query(None, description="open, acknowledged, snoozed or resolved"),
    zone: Optional[str] = Query(None, max_length=128, description="Blueprint zone id or area name"),
    trend: Optional[str] = Query(None, description="worsening, recurring, new, stable or improving"),
    include_resolved: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Recurring issues (grouped findings), most urgent first. Read-only."""
    if status and status.strip().lower() not in _TOPIC_STATUSES:
        raise HTTPException(status_code=422, detail=f"'status' must be one of {', '.join(_TOPIC_STATUSES)}")
    if trend and trend.strip().lower() not in _TOPIC_TRENDS:
        raise HTTPException(status_code=422, detail=f"'trend' must be one of {', '.join(_TOPIC_TRENDS)}")
    return await insight_topic_service.list_topics(db, status=status, zone=zone, trend=trend,
                                                   include_resolved=include_resolved)


@router.get("/recommendations/topics/{topic_id}")
async def get_insight_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    """One topic with its daily findings and the zone / product metric series. Read-only."""
    out = await insight_topic_service.detail(db, topic_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Topic not found (it may have been merged).")
    return out


async def _topic_status(db: AsyncSession, topic_id: str, status: str, days: int = 7) -> dict:
    t = await _topic_or_404(db, topic_id)
    await insight_topic_service.set_status(db, t, status, days=days)
    return {"status": "success", "topic": insight_topic_service.serialize(t)}


@router.post("/recommendations/topics/{topic_id}/acknowledge")
@operator_allowed
async def acknowledge_insight_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    """Seen it: off the open count; new findings still join it."""
    return await _topic_status(db, topic_id, "acknowledged")


@router.post("/recommendations/topics/{topic_id}/snooze")
@operator_allowed
async def snooze_insight_topic(topic_id: str, payload: Optional[TopicSnoozeRequest] = Body(None),
                               db: AsyncSession = Depends(get_db)):
    """Hide for N days (default 7); it reopens by itself afterwards."""
    return await _topic_status(db, topic_id, "snoozed", days=(payload.days if payload else 7))


@router.post("/recommendations/topics/{topic_id}/resolve")
@operator_allowed
async def resolve_insight_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    """Fixed. A finding on a later day reopens it and marks it as recurring."""
    return await _topic_status(db, topic_id, "resolved")


@router.post("/recommendations/topics/{topic_id}/reopen")
@operator_allowed
async def reopen_insight_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    return await _topic_status(db, topic_id, "open")


@router.post("/recommendations/topics/{topic_id}/action-done")
@operator_allowed
async def insight_topic_action_done(topic_id: str, payload: Optional[TopicActionDoneRequest] = Body(None),
                                    db: AsyncSession = Depends(get_db)):
    """Record that an action of the plan was done (date kept for the before/after comparison)."""
    t = await _topic_or_404(db, topic_id)
    on = None
    if payload and payload.date:
        try:
            on = date.fromisoformat(payload.date)
        except ValueError:
            raise HTTPException(status_code=422, detail="'date' must be YYYY-MM-DD")
    try:
        await insight_topic_service.mark_action_done(db, t, payload.action_id if payload else None, on)
    except KeyError:
        raise HTTPException(status_code=404, detail="That action is not in this topic's plan.")
    return {"status": "success", "topic": insight_topic_service.serialize(t)}


@router.post("/recommendations/topics/{topic_id}/split")
@operator_allowed
async def split_insight_topic(topic_id: str, payload: TopicSplitRequest = Body(...),
                              db: AsyncSession = Depends(get_db)):
    """Take one finding out of a wrongly grouped topic into a topic of its own."""
    t = await _topic_or_404(db, topic_id)
    try:
        new = await insight_topic_service.split(db, t, payload.insight_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="That finding is not in this topic.")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success", "topic": insight_topic_service.serialize(t),
            "new_topic": insight_topic_service.serialize(new)}


@router.post("/recommendations/topics/{topic_id}/merge")
@operator_allowed
async def merge_insight_topics(topic_id: str, payload: TopicMergeRequest = Body(...),
                               db: AsyncSession = Depends(get_db)):
    """Fold another topic into this one; its future findings join this topic."""
    t = await _topic_or_404(db, topic_id)
    try:
        await insight_topic_service.merge(db, t, payload.other_topic_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="The other topic was not found.")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success", "topic": insight_topic_service.serialize(t)}


@router.post("/recommendations/topics/{topic_id}/reanalyse")
@operator_allowed
async def reanalyse_insight_topic(topic_id: str, payload: Optional[TopicReanalyseRequest] = Body(None),
                                  db: AsyncSession = Depends(get_db)):
    """Recompute the unified analysis now; ``narrate`` asks the local model to word the summary."""
    t = await _topic_or_404(db, topic_id)
    await insight_topic_service.reanalyse(db, t, narrate=bool(payload and payload.narrate))
    return {"status": "success", "topic": insight_topic_service.serialize(t)}


@router.post("/recommendations/topics/backfill")
async def backfill_insight_topics(
    max_rows: int = Query(5000, ge=1, le=100000),
    _admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Group findings that have no topic yet (owner/administrator). Idempotent and batched.

    The same backfill runs by itself shortly after startup.
    """
    return await insight_topic_service.backfill(db, max_rows=max_rows)


@router.get("/pos/status")
async def get_pos_status(db: AsyncSession = Depends(get_db)):
    """Whether sales data is arriving, and how to connect it (for the "Sales data" tile)."""
    start, end = day_bounds()
    total = await db.scalar(select(func.count(POSTransactionModel.id))) or 0
    today = await db.scalar(select(func.count(POSTransactionModel.id)).where(
        and_(POSTransactionModel.timestamp >= start, POSTransactionModel.timestamp < end))) or 0
    last = await db.scalar(select(func.max(POSTransactionModel.timestamp)))
    registers = [r for (r,) in (await db.execute(
        select(POSTransactionModel.register_id).group_by(POSTransactionModel.register_id))).all() if r]
    return {
        "connected": bool(total),
        "transactions_total": int(total),
        "transactions_today": int(today),
        "last_transaction_at": to_local(last).isoformat() if last else None,
        "registers": sorted(registers),
        "ingest": {
            "method": "POST",
            "path": "/api/v1/analytics/pos/ingest",
            "auth": ("send the till key in the X-Edge-API-Key header (create it under Till key "
                     "below; the device's internal service key also still works)"),
            "header": "X-Edge-API-Key",
            "body_example": {"transactions": [{
                "transaction_id": "T-1001", "register_id": "LANE-1", "sku_id": "SKU123",
                "quantity": 1, "amount": 4.5, "timestamp": "2026-09-24T10:15:00+10:00"}]},
            "notes": ("Send each sale as it rings up (or in batches). Timestamps without an offset are "
                      "read as store-local time. Link a checkout camera to its register with the "
                      "camera's POS register setting so lanes get conversion figures."),
        },
    }
