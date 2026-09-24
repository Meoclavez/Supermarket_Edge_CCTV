"""Business-analysis runs and the one consolidated recommendations list.

Generation and reading are separate:

* **Runs** (``run``) execute the deterministic rule engine
  (``business_analysis_service.analyse``, which persists the day's findings to
  ``ai_decision_recommendations``), optionally ask the local model to narrate
  them, and record an ``analysis_runs`` row with the inputs the run saw and
  its result. Runs happen only from POST endpoints (rate-limited) and from
  the in-process scheduler (``ANALYSIS_SCHEDULE_SEC``, default hourly).
* **Reads** (``latest``, ``consolidated``) never write. The dashboard shows
  one list built from the persisted rule findings (with the operator's
  status), plus the latest narrated summary, each tagged with its source,
  priority, evidence and status.

The language model never contributes a finding or a number: its summary is
listed as ``source: ai_summary`` with the findings it was given as evidence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import AIDecisionRecommendationModel, AnalysisRunModel
from app.services.timeutil import to_local, utcnow

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, None) or os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# Manual runs (POST) are refused with 429 inside this interval of the last one.
RUN_MIN_INTERVAL_SEC = _env_int("ANALYSIS_RUN_MIN_INTERVAL_SEC", 60)
# Scheduled rules-only run; 0 disables. The first run waits for startup to settle.
SCHEDULE_SEC = _env_int("ANALYSIS_SCHEDULE_SEC", 3600)
SCHEDULE_FIRST_DELAY_SEC = _env_int("ANALYSIS_SCHEDULE_FIRST_DELAY_SEC", 120)
KEEP_RUNS = 500

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
DECISION_STATUSES = ("PENDING", "REVIEWED", "APPLIED", "DISMISSED")
SOURCE_LABELS = {
    "business_rules": "Store analysis rule",
    "heatmap_trends": "Recorded heatmap trend",
    "product_reach": "Shelf reach rule",
    "ai_summary": "AI summary (local model)",
}


class RunRateLimited(Exception):
    def __init__(self, retry_after: float, latest_run_id: Optional[str]):
        super().__init__(f"analysis ran less than {RUN_MIN_INTERVAL_SEC}s ago")
        self.retry_after = max(1, int(retry_after + 0.999))
        self.latest_run_id = latest_run_id


class RunInProgress(Exception):
    pass


def _source_for(category: Optional[str], stored: Optional[str]) -> str:
    if stored:
        return stored
    cat = (category or "").upper()
    if cat.startswith("HEATMAP_"):
        return "heatmap_trends"
    return "business_rules"


def _iso_local(dt: Optional[datetime]) -> Optional[str]:
    return to_local(dt).isoformat() if dt is not None else None


def serialize_run(row: Optional[AnalysisRunModel], include_result: bool = True) -> Optional[dict]:
    if row is None:
        return None
    out = {
        "id": row.id,
        "trigger": row.run_trigger,
        "requested_by": row.requested_by,
        "generated_at": _iso_local(row.generated_at),
        "findings_count": row.findings_count,
        "narrated": bool(row.narrated),
        "inputs_summary": row.inputs_summary,
    }
    if include_result:
        out["result"] = row.result
    return out


class RecommendationsService:
    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._last_manual: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.next_run_at: Optional[datetime] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def reset(self) -> None:
        """Tests: forget the manual-run rate limit."""
        self._last_manual = None

    # ------------------------------------------------------------------ runs

    async def run(self, db: AsyncSession, *, trigger: str = "manual", requested_by: Optional[str] = None,
                  narrate: bool = False) -> dict:
        """Run the analysis now and record it. Raises RunRateLimited / RunInProgress."""
        from app.services.business_analysis_service import Finding, business_analysis_service
        from app.services.retail_metrics_service import retail_metrics_service
        from app.services.store_layout_service import store_layout_service

        manual = trigger != "schedule"
        if manual and self._last_manual is not None:
            elapsed = time.monotonic() - self._last_manual
            if elapsed < RUN_MIN_INTERVAL_SEC:
                latest = await self.latest_row(db)
                raise RunRateLimited(RUN_MIN_INTERVAL_SEC - elapsed, latest.id if latest else None)
        lock = self._get_lock()
        if lock.locked():
            raise RunInProgress("an analysis run is already in progress")
        async with lock:
            if manual:
                self._last_manual = time.monotonic()
            started = utcnow()
            layout = await store_layout_service.get_active_layout(db)
            result = await business_analysis_service.analyse(db, layout.id, persist=True, narrate=False)
            if narrate:
                # The model call is blocking HTTP with a long budget; keep it off the loop.
                overview = await retail_metrics_service.overview(db, layout.id, settings.STORE_NAME)
                findings = [Finding(**{k: f.get(k) for k in ("category", "severity", "zone", "finding",
                                                              "root_cause", "action_item", "evidence")})
                            for f in result["findings"]]
                hm = (result.get("heatmap_trends") or {}).get("summary")
                result["narrative"] = await asyncio.to_thread(
                    business_analysis_service.narrate, findings, overview, heatmap_summary=hm)
            await self._attach_evidence(db, result["findings"])
            inputs = await self._inputs_summary(db, started, result)
            row = AnalysisRunModel(
                id=f"run_{uuid.uuid4().hex[:12]}",
                run_trigger=trigger[:16],
                requested_by=(requested_by or None) and requested_by[:128],
                generated_at=utcnow(),
                findings_count=int(result.get("findings_count") or 0),
                narrated=bool(narrate),
                inputs_summary=inputs,
                result=_jsonable(result),
            )
            db.add(row)
            await db.commit()
            await self._prune(db)
            return serialize_run(row)

    async def _attach_evidence(self, db: AsyncSession, findings: list[dict]) -> None:
        """Store each finding's cited metrics on today's recommendation row."""
        if not findings:
            return
        today = date.today().isoformat()   # the key business_analysis_service._persist uses
        rows = (await db.execute(select(AIDecisionRecommendationModel).where(
            AIDecisionRecommendationModel.date == today))).scalars().all()
        by_key = {(r.zone, r.category): r for r in rows}
        changed = False
        for f in findings:
            row = by_key.get(((f.get("zone") or "")[:64], f.get("category")))
            if row is None:
                continue
            row.evidence = _jsonable(f.get("evidence"))
            row.source = row.source or _source_for(f.get("category"), None)
            changed = True
        if changed:
            await db.commit()

    async def _inputs_summary(self, db: AsyncSession, started: datetime, result: dict) -> dict:
        from app.services.retail_metrics_service import day_bounds, retail_metrics_service

        start, _ = day_bounds()
        try:
            footfall = await retail_metrics_service.footfall(db, start, started)
            source = await retail_metrics_service.footfall_source(db, start, started)
        except Exception:
            footfall, source = None, None
        reach = result.get("product_reach") or {}
        narrative = result.get("narrative") or {}
        return {
            "window": {"from": _iso_local(start), "to": _iso_local(started)},
            "footfall": footfall,
            "footfall_source": source,
            "zones_assessed": result.get("zones_assessed"),
            "zones_total": result.get("zones_total"),
            "engagement_tracking_active": result.get("engagement_tracking_active"),
            "shelf_reaches": reach.get("reaches"),
            "pos_connected": reach.get("pos_connected"),
            "heatmap_history_used": bool(result.get("heatmap_trends")),
            "suppressed_rules": len(result.get("suppressed_rules") or []),
            "sufficient_data": result.get("sufficient_data"),
            "narration_model": narrative.get("model_used"),
        }

    async def _prune(self, db: AsyncSession) -> None:
        ids = (await db.execute(select(AnalysisRunModel.id).order_by(desc(AnalysisRunModel.generated_at))
                                .offset(KEEP_RUNS))).scalars().all()
        if ids:
            await db.execute(delete(AnalysisRunModel).where(AnalysisRunModel.id.in_(list(ids))))
            await db.commit()

    # ----------------------------------------------------------------- reads

    async def latest_row(self, db: AsyncSession, narrated: Optional[bool] = None) -> Optional[AnalysisRunModel]:
        stmt = select(AnalysisRunModel).order_by(desc(AnalysisRunModel.generated_at)).limit(1)
        if narrated is not None:
            stmt = stmt.where(AnalysisRunModel.narrated.is_(narrated))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def latest(self, db: AsyncSession) -> dict:
        row = await self.latest_row(db)
        return {
            "run": serialize_run(row),
            "generated_at": _iso_local(row.generated_at) if row else None,
            "inputs_summary": row.inputs_summary if row else None,
            "schedule": self.schedule_info(),
            "message": None if row else (
                "No analysis has run yet. It runs automatically"
                + (f" every {SCHEDULE_SEC // 60} minutes" if SCHEDULE_SEC > 0 else " when started")
                + ", or start one with Refresh analysis."),
        }

    def schedule_info(self) -> dict:
        return {
            "interval_seconds": SCHEDULE_SEC if SCHEDULE_SEC > 0 else None,
            "next_run_at": _iso_local(self.next_run_at),
            "manual_min_interval_seconds": RUN_MIN_INTERVAL_SEC,
        }

    async def consolidated(self, db: AsyncSession, *, status: Optional[str] = None, days: int = 7,
                           include_dismissed: bool = False) -> dict:
        """One list: persisted rule findings (with status) + the latest AI summary."""
        since = (date.today() - timedelta(days=max(1, days) - 1)).isoformat()
        stmt = select(AIDecisionRecommendationModel).where(AIDecisionRecommendationModel.date >= since)
        want = (status or "").strip().upper() or None
        if want:
            stmt = stmt.where(AIDecisionRecommendationModel.status == want)
        elif not include_dismissed:
            stmt = stmt.where(AIDecisionRecommendationModel.status != "DISMISSED")
        rows = (await db.execute(stmt)).scalars().all()

        items: list[dict] = []
        for r in rows:
            source = _source_for(r.category, r.source)
            sev = (r.severity or "MEDIUM").upper()
            items.append({
                "id": r.id,
                "source": source,
                "source_label": SOURCE_LABELS.get(source, source),
                "category": r.category,
                "priority": sev.lower(),
                "priority_rank": SEVERITY_RANK.get(sev, 9),
                "zone": r.zone,
                "title": r.finding,
                "why": r.root_cause,
                "do": r.action_item,
                "evidence": r.evidence,
                "evidence_available": r.evidence is not None,
                "status": r.status,
                "date": r.date,
                "created_at": _iso_local(r.created_at),
                "updated_at": _iso_local(r.updated_at),
                "status_endpoint": f"/api/v1/analytics/decisions/{r.id}/action",
                "allowed_statuses": list(DECISION_STATUSES),
            })

        latest = await self.latest_row(db)
        narrated = await self.latest_row(db, narrated=True)
        summary = ((narrated.result or {}).get("narrative") or {}).get("summary") if narrated else None
        if (summary and narrated.generated_at >= utcnow() - timedelta(days=max(1, days))
                and want in (None, "INFO")):
            items.append({
                "id": f"summary_{narrated.id}",
                "source": "ai_summary",
                "source_label": SOURCE_LABELS["ai_summary"],
                "category": "SUMMARY",
                "priority": "info",
                "priority_rank": SEVERITY_RANK["INFO"],
                "zone": "Store-wide",
                "title": "Executive summary",
                "why": None,
                "do": summary,
                "evidence": {
                    "run_id": narrated.id,
                    "model": ((narrated.result or {}).get("narrative") or {}).get("model_used"),
                    "based_on_findings": [
                        {"zone": f.get("zone"), "category": f.get("category"), "finding": f.get("finding")}
                        for f in ((narrated.result or {}).get("findings") or [])[:6]
                    ],
                },
                "evidence_available": True,
                "status": "INFO",
                "date": to_local(narrated.generated_at).date().isoformat(),
                "created_at": _iso_local(narrated.generated_at),
                "updated_at": None,
                "status_endpoint": None,
                "allowed_statuses": [],
            })

        items.sort(key=lambda i: (i["status"] not in ("PENDING", "INFO"), i["priority_rank"],
                                  -(datetime.fromisoformat(i["created_at"]).timestamp() if i["created_at"] else 0)))
        by_source: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for i in items:
            by_source[i["source"]] = by_source.get(i["source"], 0) + 1
            by_status[i["status"]] = by_status.get(i["status"], 0) + 1

        if items:
            empty_reason = None
        elif latest is None:
            empty_reason = (await self.latest(db))["message"]
        else:
            msg = (latest.result or {}).get("message")
            empty_reason = msg or "The latest analysis found nothing that needs attention."
        return {
            "generated_at": _iso_local(latest.generated_at) if latest else None,
            "latest_run": serialize_run(latest, include_result=False),
            "schedule": self.schedule_info(),
            "items": items,
            "counts": {"total": len(items), "open": sum(1 for i in items if i["status"] == "PENDING"),
                       "by_source": by_source, "by_status": by_status},
            "empty_reason": empty_reason,
        }

    # ------------------------------------------------------------- scheduler

    async def start(self) -> None:
        if SCHEDULE_SEC <= 0 or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._loop(), name="analysis-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        self.next_run_at = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        from app.database import async_session_factory

        delay = float(SCHEDULE_FIRST_DELAY_SEC)
        while True:
            self.next_run_at = utcnow() + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            delay = float(SCHEDULE_SEC)
            try:
                async with async_session_factory() as db:
                    await self.run(db, trigger="schedule", requested_by="scheduler", narrate=False)
            except RunInProgress:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the scheduler die
                logger.warning(f"Scheduled analysis failed: {e}")


def _jsonable(value: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(value)


recommendations_service = RecommendationsService()
