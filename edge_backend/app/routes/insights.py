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
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.db_models import POSTransactionModel
from app.routes import ResilientRoute
from app.routes.analytics import verify_analytics_access
from app.services.actor import describe_actor
from app.services.auth_service import general_rate_limiter
from app.services.hourly_traffic import hourly_visitors
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
            "auth": "Bearer session token or X-Edge-API-Key header",
            "body_example": {"transactions": [{
                "transaction_id": "T-1001", "register_id": "LANE-1", "sku_id": "SKU123",
                "quantity": 1, "amount": 4.5, "timestamp": "2026-09-24T10:15:00+10:00"}]},
            "notes": ("Send each sale as it rings up (or in batches). Timestamps without an offset are "
                      "read as store-local time. Link a checkout camera to its register with the "
                      "camera's POS register setting so lanes get conversion figures."),
        },
    }
