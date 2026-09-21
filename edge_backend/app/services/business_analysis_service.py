"""Business management analysis over observed retail metrics.

Two layers, deliberately separated:

1. **Deterministic rules.** Threshold checks over real zone metrics produce the
   findings. These are reproducible, explainable and cite the numbers that
   triggered them, which is what an operator needs in order to act.
2. **Local language model.** Ollama turns the findings into an executive
   narrative. It never invents a finding and never supplies a number; if the
   model is unreachable the findings still stand on their own.

Recommendations are persisted to ``ai_decision_recommendations`` so their
status survives a restart. Previously that table was populated with five
hand-written findings seeded on first boot, and the genuine rule engine that
should have filled it had no caller anywhere in the application.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import AIDecisionRecommendationModel
from app.services.retail_metrics_service import ZoneMetrics, day_bounds, retail_metrics_service

logger = logging.getLogger(__name__)

# Thresholds that define an exception worth an operator's attention. They are
# explicit and tunable rather than buried in the rule bodies.
DEAD_ZONE_TRAFFIC_RATIO = 0.25      # below a quarter of store-average traffic
HIGH_DWELL_LOW_ENGAGE_SEC = 45.0    # people linger this long...
LOW_ENGAGEMENT_PCT = 10.0           # ...but this few reach for product
QUEUE_WAIT_WARN_SECONDS = 270.0     # 4.5 minutes at a checkout
MIN_VISITS_FOR_CONFIDENCE = 5       # below this, the sample proves nothing

# Only zones where a shopper is expected to handle product can be judged on
# engagement. Dwelling at an entrance or a checkout without reaching for
# anything is normal behaviour, not a merchandising failure.
MERCHANDISING_CATEGORIES = ("AISLE", "DEPARTMENT", "SHELF")


class Finding:
    """One rule hit, with the evidence that produced it."""

    def __init__(
        self,
        *,
        category: str,
        severity: str,
        zone: str,
        finding: str,
        root_cause: str,
        action_item: str,
        evidence: dict[str, Any],
    ):
        self.category = category
        self.severity = severity
        self.zone = zone
        self.finding = finding
        self.root_cause = root_cause
        self.action_item = action_item
        self.evidence = evidence

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "zone": self.zone,
            "finding": self.finding,
            "root_cause": self.root_cause,
            "action_item": self.action_item,
            "evidence": self.evidence,
        }


def _detect(zones: list[ZoneMetrics], funnel: dict) -> list[Finding]:
    """Run every rule over the observed metrics.

    Only zones with enough visits to be meaningful are assessed; a zone with
    two recorded visits cannot support a claim about shopper behaviour.
    """
    findings: list[Finding] = []
    assessable = [z for z in zones if z.visits >= MIN_VISITS_FOR_CONFIDENCE]
    if not assessable:
        return findings

    # Engagement is only measurable once something reports shelf interactions.
    # Without that signal every zone reads as 0% engagement, which would make
    # the merchandising rule fire everywhere and mean nothing. Suppress the
    # rule entirely rather than emit a store full of false findings.
    engagement_tracked = any(z.interactions > 0 for z in zones)

    avg_visits = sum(z.visits for z in assessable) / len(assessable)

    for z in assessable:
        # Shoppers stop but do not engage: a merchandising or pricing problem.
        if (
            engagement_tracked
            and z.category in MERCHANDISING_CATEGORIES
            and z.avg_dwell_seconds
            and z.avg_dwell_seconds >= HIGH_DWELL_LOW_ENGAGE_SEC
            and z.engagement_rate is not None
            and z.engagement_rate < LOW_ENGAGEMENT_PCT
        ):
            findings.append(Finding(
                category="MERCHANDISING",
                severity="HIGH",
                zone=z.name,
                finding=(
                    f"Shoppers dwell {z.avg_dwell_seconds:.0f}s in {z.name} but only "
                    f"{z.engagement_rate:.1f}% reach for product."
                ),
                root_cause=(
                    "High attention with low pick-up usually indicates unclear pricing, "
                    "an out-of-stock facing, or a confusing planogram."
                ),
                action_item=(
                    f"Audit {z.name} facings and shelf-edge pricing; verify stock against the planogram."
                ),
                evidence={
                    "visits": z.visits,
                    "avg_dwell_seconds": z.avg_dwell_seconds,
                    "engagement_rate_pct": z.engagement_rate,
                },
            ))

        # Chronically bypassed aisle.
        if z.category in ("AISLE", "DEPARTMENT") and z.visits < avg_visits * DEAD_ZONE_TRAFFIC_RATIO:
            findings.append(Finding(
                category="STORE_LAYOUT",
                severity="MEDIUM",
                zone=z.name,
                finding=(
                    f"{z.name} saw {z.visits} visits against a store average of {avg_visits:.0f}."
                ),
                root_cause=(
                    "Traffic well below the store average suggests the aisle sits off the "
                    "dominant circulation path or its category signage is not visible."
                ),
                action_item=(
                    f"Reposition a destination category into {z.name} or add an aisle-end "
                    "signpost to draw the main traffic flow through it."
                ),
                evidence={"visits": z.visits, "store_avg_visits": round(avg_visits, 1)},
            ))

        # Checkout congestion.
        if (
            z.category == "CHECKOUT"
            and z.avg_dwell_seconds
            and z.avg_dwell_seconds > QUEUE_WAIT_WARN_SECONDS
        ):
            findings.append(Finding(
                category="STAFFING",
                severity="CRITICAL",
                zone=z.name,
                finding=(
                    f"Average wait at {z.name} is {z.avg_dwell_seconds / 60:.1f} minutes."
                ),
                root_cause="Lane throughput is below arrival rate for the observed period.",
                action_item=f"Open an additional lane covering {z.name} during this period.",
                evidence={
                    "avg_wait_seconds": z.avg_dwell_seconds,
                    "queue_now": z.occupancy_now,
                },
            ))

    # Store-wide conversion friction, only when POS is actually connected.
    lost = funnel.get("lost_sales_index_pct")
    if funnel.get("pos_connected") and lost is not None and lost >= 75.0:
        findings.append(Finding(
            category="LOSS_PREVENTION",
            severity="HIGH",
            zone="Store-wide",
            finding=f"{lost:.0f}% of shoppers who handled product did not complete a purchase.",
            root_cause=(
                "A large gap between engagement and transactions points to price resistance, "
                "checkout friction, or abandonment at the queue."
            ),
            action_item="Review pricing on high-engagement lines and checkout wait times together.",
            evidence={k: funnel.get(k) for k in
                      ("conversion_rate_pct", "engagement_rate_pct", "lost_sales_index_pct")},
        ))

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return findings


class BusinessAnalysisService:
    """Generates, narrates and persists store recommendations."""

    # ------------------------------------------------------------------ LLM

    def _ollama_models(self) -> list[dict]:
        try:
            req = urllib.request.Request(f"{settings.OLLAMA_BASE_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=2.5) as r:
                return json.loads(r.read().decode()).get("models", [])
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            return []

    def select_model(self) -> Optional[str]:
        """Pick a generative model, preferring ones that answer quickly.

        Reasoning-heavy models are deprioritised here: a model that needs more
        than a minute to write three sentences makes the analysis feel broken,
        and the findings it narrates are already complete without it.
        """
        models = [m.get("name", "") for m in self._ollama_models()]
        generative = [
            n for n in models
            if not any(t in n.lower() for t in ("embed", "bert", "bge", "nomic-embed"))
        ]
        if not generative:
            return None
        for preference in ("lfm", "qwen", "llama", "mistral", "phi", "gemma", "ornith"):
            for n in generative:
                if preference in n.lower():
                    return n
        return generative[0]

    def narrate(self, findings: list[Finding], overview: dict, timeout: float = 90.0) -> dict:
        """Ask the local model for an executive summary of the findings.

        The generation budget is generous because this runs on demand, not on
        the request path of a dashboard poll. The previous implementation
        allowed six seconds, which every locally-hosted model exceeded, so it
        silently served a templated string while reporting the model as active.
        """
        model = self.select_model()
        if not model:
            return {
                "summary": None,
                "model_used": None,
                "ollama_active": False,
                "reason": "No generative model is available from Ollama.",
            }

        if not findings:
            return {
                "summary": None,
                "model_used": None,
                "ollama_active": True,
                "reason": "No findings to summarise.",
            }

        facts = [
            {"zone": f.zone, "severity": f.severity, "finding": f.finding, "action": f.action_item}
            for f in findings[:6]
        ]
        prompt = (
            "You are a retail operations analyst. Below are findings measured by an "
            "in-store camera analytics system, with the store's headline numbers.\n\n"
            f"Store metrics: {json.dumps({k: overview.get(k) for k in ('today_footfall', 'active_shoppers_now', 'avg_dwell_minutes', 'conversion_rate_pct', 'daily_revenue')})}\n"
            f"Findings: {json.dumps(facts)}\n\n"
            "Write a 3-sentence executive summary for the store manager. State what is "
            "happening, why it matters commercially, and what to do first. "
            "Use only the numbers given above. Do not invent figures. Do not use markdown."
        )

        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            # Several local models interleave a <think> block before their
            # answer. Ollama honours this flag on models that support it, and
            # ignores it on those that do not; _strip_reasoning handles the rest.
            "think": False,
            # Generous enough that a reasoning model can finish its block and
            # still produce the answer. Truncating mid-thought yields output
            # that is all scaffolding and no summary.
            "options": {"temperature": 0.2, "num_predict": 700},
        }).encode()

        started = datetime.now()
        try:
            req = urllib.request.Request(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # An honest failure. The findings above are unaffected.
            return {
                "summary": None,
                "model_used": model,
                "ollama_active": False,
                "reason": f"Model '{model}' did not respond within {timeout:.0f}s ({e}).",
            }
        except json.JSONDecodeError as e:
            return {"summary": None, "model_used": model, "ollama_active": False,
                    "reason": f"Malformed response from Ollama: {e}"}

        text = self._strip_reasoning(body.get("response") or "")
        if not text:
            return {
                "summary": None,
                "model_used": model,
                "ollama_active": True,
                "reason": (
                    "Model produced only reasoning, no answer. Increase the token "
                    "budget or choose a non-reasoning model."
                ),
            }

        return {
            "summary": text,
            "model_used": model,
            "ollama_active": True,
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
            "reason": None,
        }

    @staticmethod
    def _strip_reasoning(raw: str) -> str:
        """Remove chain-of-thought scaffolding and return only the answer.

        Reasoning models wrap their deliberation in <think> tags. When the
        token budget runs out mid-thought the closing tag never arrives, so a
        naive paired-tag regex leaves the entire block intact -- which is how
        raw reasoning would otherwise reach the manager's dashboard.
        """
        import re

        text = (raw or "").strip()
        text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        # An unterminated block means everything after it is deliberation.
        if (m := re.search(r"<think>", text, re.IGNORECASE)) is not None:
            text = text[: m.start()]
        text = re.sub(r"</?think>", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    # -------------------------------------------------------------- analysis

    async def analyse(
        self, db: AsyncSession, layout_id: str, *, persist: bool = True, narrate: bool = False
    ) -> dict:
        """Produce findings from today's observations, optionally narrated."""
        start, end = day_bounds()
        zones = await retail_metrics_service.zone_metrics(db, layout_id, start, end)
        funnel = await retail_metrics_service.funnel(db, layout_id, start, end)
        overview = await retail_metrics_service.overview(db, layout_id, settings.STORE_NAME)

        assessable = sum(1 for z in zones if z.visits >= MIN_VISITS_FOR_CONFIDENCE)
        findings = _detect(zones, funnel)

        if persist and findings:
            await self._persist(db, findings)

        engagement_tracked = any(z.interactions > 0 for z in zones)
        result = {
            "generated_at": datetime.now().isoformat(),
            "findings": [f.to_dict() for f in findings],
            "findings_count": len(findings),
            "zones_assessed": assessable,
            "zones_total": len(zones),
            "engagement_tracking_active": engagement_tracked,
            "suppressed_rules": (
                [] if engagement_tracked
                else ["merchandising_engagement: no shelf-interaction source is reporting"]
            ),
            # Say plainly why an empty result is empty.
            "sufficient_data": assessable > 0,
            "message": (
                None if assessable
                else (
                    f"Not enough observations yet. A zone needs at least "
                    f"{MIN_VISITS_FOR_CONFIDENCE} recorded visits before it can be assessed."
                )
            ),
        }
        if narrate:
            result["narrative"] = self.narrate(findings, overview)
        return result

    async def _persist(self, db: AsyncSession, findings: list[Finding]) -> None:
        """Store today's findings, replacing any earlier run for the same day.

        Re-running the analysis should refresh the day's recommendations rather
        than accumulate near-duplicates, but an operator's status changes on a
        finding that still applies are preserved.
        """
        today = date.today().isoformat()
        existing = (
            await db.execute(
                select(AIDecisionRecommendationModel).where(
                    AIDecisionRecommendationModel.date == today
                )
            )
        ).scalars().all()
        by_key = {(r.zone, r.category): r for r in existing}

        seen: set[tuple[str, str]] = set()
        for f in findings:
            key = (f.zone, f.category)
            seen.add(key)
            if (row := by_key.get(key)) is not None:
                row.severity = f.severity
                row.finding = f.finding[:512]
                row.root_cause = f.root_cause[:512]
                row.action_item = f.action_item[:512]
                row.updated_at = datetime.utcnow()
                continue
            db.add(AIDecisionRecommendationModel(
                id=f"rec_{uuid.uuid4().hex[:12]}",
                date=today,
                category=f.category,
                severity=f.severity,
                zone=f.zone[:64],
                finding=f.finding[:512],
                root_cause=f.root_cause[:512],
                action_item=f.action_item[:512],
                status="PENDING",
            ))

        # Drop stale findings from earlier today that no longer hold, unless
        # someone has already acted on them.
        for key, row in by_key.items():
            if key not in seen and row.status == "PENDING":
                await db.delete(row)

        await db.commit()


business_analysis_service = BusinessAnalysisService()
