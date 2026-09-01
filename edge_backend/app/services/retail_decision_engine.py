"""Automated Decision & Reasoning Engine for Supermarket Edge AI CCTV.

Implements:
1. Retail Anomaly Detectors:
   - High-Interest / Low-Conversion Anomaly (Friction phi > 75% or Conversion gamma < 10%).
   - Chronic Dead-Zone Anomaly (Traffic < 25% of store average).
   - Checkout Queue Bottleneck Anomaly (Wait time > 4.5 min or queue length > 5 persons).
   - Shelf Stockout Anomaly (High reach rate + instant put-backs + zero sales).
   - Promotional Opportunity Detector (High attraction + high conversion).
2. Generative Decision Synthesizer:
   - Evaluates multi-camera store telemetry, funnels, and queue distributions.
   - Synthesizes prioritized, structured operational recommendations with severity,
     root cause hypotheses, direct staff action items, and estimated financial/operational impacts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence
from pydantic import BaseModel, Field

from .retail_analytics_service import (
    FunnelMetrics,
    QueueMetrics,
    LaneStatus,
    DemographicsReport,
)

logger = logging.getLogger("RetailDecisionEngine")


# ============================================================================
# 1. Decision & Anomaly Schemas
# ============================================================================

class RetailAnomalyType(str, Enum):
    HIGH_INTEREST_LOW_CONVERSION = "HIGH_INTEREST_LOW_CONVERSION"
    CHRONIC_DEAD_ZONE = "CHRONIC_DEAD_ZONE"
    CHECKOUT_BOTTLENECK = "CHECKOUT_BOTTLENECK"
    SHELF_STOCKOUT = "SHELF_STOCKOUT"
    PROMOTIONAL_OPPORTUNITY = "PROMOTIONAL_OPPORTUNITY"
    UNMANNED_ACTIVE_QUEUE = "UNMANNED_ACTIVE_QUEUE"


class RecommendationSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class RetailRecommendation(BaseModel):
    """Structured actionable recommendation produced by the decision engine."""
    id: str = Field(..., description="Unique recommendation ID")
    anomaly_type: RetailAnomalyType
    zone_id: str
    zone_name: str
    severity: RecommendationSeverity
    metric_evidence: Dict[str, Any] = Field(default_factory=dict)
    root_cause_hypothesis: str
    operational_actions: List[str] = Field(default_factory=list)
    estimated_impact: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ShelfInteractionSummary(BaseModel):
    """Aggregated interaction metrics for a specific shelf or SKU."""
    zone_id: str
    shelf_id: str
    product_category: str
    reach_count: int = 0
    instant_put_back_count: int = 0
    total_sales: int = 0
    avg_touch_duration_sec: float = 0.0


# ============================================================================
# 2. Decision & Reasoning Engine Implementation
# ============================================================================

class RetailDecisionEngine:
    """Automated reasoning engine that synthesizes operational interventions from retail AI vision metrics."""

    def __init__(
        self,
        friction_threshold_pct: float = 75.0,
        low_conversion_threshold_pct: float = 10.0,
        min_interactions_for_anomaly: int = 5,
        dead_zone_traffic_ratio_threshold: float = 0.25,
        queue_bottleneck_wait_sec: float = 270.0,  # 4.5 minutes
        queue_bottleneck_length: int = 5,
        stockout_putback_ratio_threshold: float = 0.70,
    ):
        self.friction_threshold = friction_threshold_pct
        self.low_conversion_threshold = low_conversion_threshold_pct
        self.min_interactions = min_interactions_for_anomaly
        self.dead_zone_ratio = dead_zone_traffic_ratio_threshold
        self.queue_wait_threshold = queue_bottleneck_wait_sec
        self.queue_length_threshold = queue_bottleneck_length
        self.stockout_putback_ratio = stockout_putback_ratio_threshold
        self._rec_counter = 1

    def _next_rec_id(self, prefix: str = "REC") -> str:
        rec_id = f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{self._rec_counter:04d}"
        self._rec_counter += 1
        return rec_id

    # ------------------------------------------------------------------------
    # Anomaly Detector 1: High-Interest / Low-Conversion
    # ------------------------------------------------------------------------
    def detect_high_interest_low_conversion(
        self,
        funnel: FunnelMetrics,
        zone_name: str = "",
        product_category: str = "General Grocery",
    ) -> Optional[RetailRecommendation]:
        """Detect when customers frequently engage with items but abandon without purchase.

        Trigger condition:
          - N_interact >= min_interactions
          - Friction Index phi > 75.0% OR Conversion Rate gamma < 10.0%
        """
        if funnel.interact_count < self.min_interactions:
            return None

        is_high_friction = funnel.friction_index >= self.friction_threshold
        is_low_conversion = funnel.conversion_rate <= self.low_conversion_threshold

        if not (is_high_friction or is_low_conversion):
            return None

        # Determine severity based on severity of friction
        if funnel.friction_index >= 85.0 or funnel.conversion_rate <= 5.0:
            severity = RecommendationSeverity.CRITICAL
        else:
            severity = RecommendationSeverity.HIGH

        z_name = zone_name or funnel.zone_id.replace("zone_", "").replace("_", " ").title()

        root_cause = (
            f"High shopper engagement ({funnel.interact_count} physical inspections) in '{z_name}' "
            f"has an abnormal Friction Index of {funnel.friction_index:.1f}% (Conversion: {funnel.conversion_rate:.1f}%). "
            f"This discrepancy typically indicates an on-shelf price discrepancy vs POS, missing price tags, "
            f"damaged packaging, or uncompetitive pricing relative to adjacent items."
        )

        actions = [
            f"Dispatch floor associate to {z_name} to audit shelf pricing tags against POS master pricing.",
            f"Inspect physical stock for damaged boxes, broken seals, or nearing expiration dates.",
            f"Evaluate competitor pricing on top SKU '{product_category}' or introduce an on-shelf promotional discount tag.",
            f"Ensure clear nutritional, sizing, or compatibility labels are visibly displayed.",
        ]

        impact = f"Recovers estimated +15% to +25% in lost sales (approx. ${funnel.interact_count * 12:.0f}/week potential uplift)."

        return RetailRecommendation(
            id=self._next_rec_id("REC_HILC"),
            anomaly_type=RetailAnomalyType.HIGH_INTEREST_LOW_CONVERSION,
            zone_id=funnel.zone_id,
            zone_name=z_name,
            severity=severity,
            metric_evidence={
                "pass_count": funnel.pass_count,
                "dwell_count": funnel.dwell_count,
                "interact_count": funnel.interact_count,
                "sales_count": funnel.sales_count,
                "friction_index": funnel.friction_index,
                "conversion_rate": funnel.conversion_rate,
                "product_category": product_category,
            },
            root_cause_hypothesis=root_cause,
            operational_actions=actions,
            estimated_impact=impact,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------------
    # Anomaly Detector 2: Chronic Dead-Zone Anomaly
    # ------------------------------------------------------------------------
    def detect_chronic_dead_zone(
        self,
        zone_funnel: FunnelMetrics,
        all_zone_funnels: Sequence[FunnelMetrics],
        zone_name: str = "",
    ) -> Optional[RetailRecommendation]:
        """Detect zones suffering from chronic foot-traffic deficit (< 25% of store average).

        Trigger condition:
          - zone_traffic < 0.25 * average_store_zone_traffic
        """
        if not all_zone_funnels:
            return None

        # Calculate average foot traffic across all active aisles/zones
        avg_pass = sum(f.pass_count for f in all_zone_funnels) / len(all_zone_funnels)
        if avg_pass <= 0:
            return None

        traffic_ratio = zone_funnel.pass_count / avg_pass
        if traffic_ratio >= self.dead_zone_ratio:
            return None

        z_name = zone_name or zone_funnel.zone_id.replace("zone_", "").replace("_", " ").title()
        severity = RecommendationSeverity.HIGH if traffic_ratio < 0.15 else RecommendationSeverity.MEDIUM

        root_cause = (
            f"Foot traffic in '{z_name}' ({zone_funnel.pass_count} shoppers) is only {traffic_ratio * 100:.1f}% "
            f"of the supermarket average ({avg_pass:.0f} shoppers/zone). This chronic traffic void indicates "
            f"poor aisle line-of-sight, obstructive promotional standees blocking the corridor, "
            f"sub-optimal category placement, or inadequate overhead illumination."
        )

        actions = [
            f"Remove bulky freestanding floor displays or pallet drops blocking the entrance to {z_name}.",
            f"Relocate high-demand staple categories (e.g. coffee, cooking oil, popular snacks) into {z_name} to generate pull-through traffic.",
            f"Install high-visibility overhead aisle navigation markers pointing toward {z_name}.",
            f"Introduce an eye-level 'Weekly Manager's Special' endcap at the approach to {z_name}.",
        ]

        impact = f"Balances store floor circulation; expected +30% to +50% foot traffic normalization."

        return RetailRecommendation(
            id=self._next_rec_id("REC_DEAD"),
            anomaly_type=RetailAnomalyType.CHRONIC_DEAD_ZONE,
            zone_id=zone_funnel.zone_id,
            zone_name=z_name,
            severity=severity,
            metric_evidence={
                "zone_pass_count": zone_funnel.pass_count,
                "store_avg_pass_count": round(avg_pass, 1),
                "traffic_ratio_pct": round(traffic_ratio * 100.0, 1),
            },
            root_cause_hypothesis=root_cause,
            operational_actions=actions,
            estimated_impact=impact,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------------
    # Anomaly Detector 3: Checkout Queue Bottleneck Anomaly
    # ------------------------------------------------------------------------
    def detect_checkout_bottleneck(
        self,
        queue_metric: QueueMetrics,
        checkout_name: str = "",
        all_lanes: Optional[Sequence[QueueMetrics]] = None,
    ) -> Optional[RetailRecommendation]:
        """Detect checkout queue congestion and customer wait time bottlenecks.

        Trigger condition:
          - Mean wait time > 4.5 minutes (270s) OR current queue length > 5 persons.
        """
        is_wait_time_high = queue_metric.mean_wait_time_sec >= self.queue_wait_threshold
        is_queue_long = queue_metric.current_queue_length >= self.queue_length_threshold

        if not (is_wait_time_high or is_queue_long):
            return None

        lane_label = checkout_name or queue_metric.checkout_id.replace("_", " ").title()

        if queue_metric.mean_wait_time_sec >= 360.0 or queue_metric.current_queue_length >= 7:
            severity = RecommendationSeverity.CRITICAL
        else:
            severity = RecommendationSeverity.HIGH

        closed_lanes = [l.checkout_id for l in (all_lanes or []) if not l.active_cashier or l.lane_status == LaneStatus.CLOSED]

        root_cause = (
            f"Severe checkout bottleneck detected at {lane_label}. Current queue length is "
            f"{queue_metric.current_queue_length} customers with an average wait time of "
            f"{queue_metric.mean_wait_time_sec / 60.0:.1f} minutes (P90: {queue_metric.p90_wait_time_sec / 60.0:.1f} min). "
            f"Current checkout throughput (mu = {queue_metric.service_rate_per_min:.1f} cust/min) is overwhelmed by queue surge."
        )

        actions = [
            f"Immediately open standby checkout register(s) ({', '.join(closed_lanes[:2]) if closed_lanes else 'Express Lane 4 & 5'}).",
            f"Deploy front-end floor associate to assist with bag packing and scan queue line-busting.",
            f"Direct eligible basket shoppers (< 10 items) to self-service checkout terminals.",
            f"Verify barcode scanner and EFT-POS payment terminal latency on {lane_label}.",
        ]

        impact = f"Reduces customer wait time by ~60% (from {queue_metric.mean_wait_time_sec / 60.0:.1f} min to < 2.0 min) and prevents checkout cart abandonment."

        return RetailRecommendation(
            id=self._next_rec_id("REC_QBNK"),
            anomaly_type=RetailAnomalyType.CHECKOUT_BOTTLENECK,
            zone_id=queue_metric.checkout_id,
            zone_name=lane_label,
            severity=severity,
            metric_evidence={
                "current_queue_length": queue_metric.current_queue_length,
                "mean_wait_time_sec": queue_metric.mean_wait_time_sec,
                "p90_wait_time_sec": queue_metric.p90_wait_time_sec,
                "service_rate_per_min": queue_metric.service_rate_per_min,
                "lane_status": queue_metric.lane_status.value,
            },
            root_cause_hypothesis=root_cause,
            operational_actions=actions,
            estimated_impact=impact,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------------
    # Anomaly Detector 4: Shelf Stockout Anomaly
    # ------------------------------------------------------------------------
    def detect_shelf_stockout(
        self,
        summary: ShelfInteractionSummary,
        zone_name: str = "",
    ) -> Optional[RetailRecommendation]:
        """Detect phantom or actual shelf stockouts indicated by rapid put-backs and zero sales.

        Trigger condition:
          - Reach count >= 3
          - Instant put-back ratio >= 70% AND total_sales == 0 (or touch duration < 3.0s)
        """
        if summary.reach_count < 3:
            return None

        putback_ratio = summary.instant_put_back_count / summary.reach_count if summary.reach_count > 0 else 0.0
        is_stockout_pattern = (putback_ratio >= self.stockout_putback_ratio and summary.total_sales == 0) or \
                              (summary.avg_touch_duration_sec < 3.0 and summary.total_sales == 0 and summary.reach_count >= 4)

        if not is_stockout_pattern:
            return None

        z_name = zone_name or summary.zone_id.replace("zone_", "").replace("_", " ").title()

        root_cause = (
            f"Suspected on-shelf stockout or empty facade at Shelf '{summary.shelf_id}' in {z_name} "
            f"({summary.product_category}). AI vision detected {summary.reach_count} customer reach interactions, "
            f"with {summary.instant_put_back_count} instant put-backs ({putback_ratio * 100:.0f}%) and 0 POS sales. "
            f"Customers are reaching into the display shelf, finding the specific variant/size missing, and immediately withdrawing."
        )

        actions = [
            f"Trigger emergency stock replenishment task for '{summary.product_category}' from backroom inventory.",
            f"Pull forward remaining stock from the back of shelf {summary.shelf_id} to ensure full facade visibility.",
            f"Verify inventory count in ERP system to reconcile phantom inventory discrepancies.",
            f"Place temporary out-of-stock tag with restock ETA or suggest adjacent substitute product.",
        ]

        impact = f"Recovers an estimated $300 to $800/day in preventable lost stockout revenue."

        return RetailRecommendation(
            id=self._next_rec_id("REC_STKO"),
            anomaly_type=RetailAnomalyType.SHELF_STOCKOUT,
            zone_id=summary.zone_id,
            zone_name=f"{z_name} - {summary.shelf_id}",
            severity=RecommendationSeverity.HIGH,
            metric_evidence={
                "shelf_id": summary.shelf_id,
                "product_category": summary.product_category,
                "reach_count": summary.reach_count,
                "instant_put_back_count": summary.instant_put_back_count,
                "putback_ratio_pct": round(putback_ratio * 100.0, 1),
                "total_sales": summary.total_sales,
                "avg_touch_duration_sec": round(summary.avg_touch_duration_sec, 2),
            },
            root_cause_hypothesis=root_cause,
            operational_actions=actions,
            estimated_impact=impact,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------------
    # Anomaly Detector 5: Promotional Opportunity Detector
    # ------------------------------------------------------------------------
    def detect_promotional_opportunity(
        self,
        funnel: FunnelMetrics,
        zone_name: str = "",
        product_category: str = "",
    ) -> Optional[RetailRecommendation]:
        """Identify high-attraction, high-converting star products ready for scaling."""
        if funnel.pass_count >= 20 and funnel.attraction_rate >= 50.0 and funnel.conversion_rate >= 35.0:
            z_name = zone_name or funnel.zone_id.replace("zone_", "").replace("_", " ").title()

            return RetailRecommendation(
                id=self._next_rec_id("REC_PROMO"),
                anomaly_type=RetailAnomalyType.PROMOTIONAL_OPPORTUNITY,
                zone_id=funnel.zone_id,
                zone_name=z_name,
                severity=RecommendationSeverity.LOW,
                metric_evidence={
                    "attraction_rate": funnel.attraction_rate,
                    "conversion_rate": funnel.conversion_rate,
                    "sales_count": funnel.sales_count,
                },
                root_cause_hypothesis=(
                    f"Exceptional product performance in '{z_name}': {funnel.attraction_rate:.1f}% attraction "
                    f"and {funnel.conversion_rate:.1f}% conversion rate. Category is outperforming store benchmark."
                ),
                operational_actions=[
                    f"Feature category '{product_category}' on prime front-of-store promotional endcap.",
                    f"Establish cross-merchandising bundle with adjacent beverage or snack category.",
                    f"Increase shelf facings from 2 to 4 to prevent midday stock depletion.",
                ],
                estimated_impact=f"Projected +35% total sales volume expansion.",
                timestamp=datetime.now(timezone.utc),
            )
        return None

    # ------------------------------------------------------------------------
    # Generative Decision Synthesizer
    # ------------------------------------------------------------------------
    def synthesize_store_recommendations(
        self,
        zone_funnels: Dict[str, FunnelMetrics],
        zone_names: Optional[Dict[str, str]] = None,
        queue_metrics: Optional[Sequence[QueueMetrics]] = None,
        shelf_summaries: Optional[Sequence[ShelfInteractionSummary]] = None,
        demographics: Optional[Dict[str, DemographicsReport]] = None,
    ) -> List[RetailRecommendation]:
        """Synthesize comprehensive, prioritized retail recommendations across the store."""
        recommendations: List[RetailRecommendation] = []
        names = zone_names or {}
        all_funnels = list(zone_funnels.values())

        # 1. Process zone conversion funnels
        for z_id, f_metric in zone_funnels.items():
            z_name = names.get(z_id, z_id.replace("zone_", "").replace("_", " ").title())

            # Check High-Interest Low-Conversion
            rec_hilc = self.detect_high_interest_low_conversion(f_metric, z_name)
            if rec_hilc:
                recommendations.append(rec_hilc)

            # Check Chronic Dead-Zone
            rec_dead = self.detect_chronic_dead_zone(f_metric, all_funnels, z_name)
            if rec_dead:
                recommendations.append(rec_dead)

            # Check Promotional Opportunity
            rec_promo = self.detect_promotional_opportunity(f_metric, z_name)
            if rec_promo:
                recommendations.append(rec_promo)

        # 2. Process checkout queues
        if queue_metrics:
            for q in queue_metrics:
                q_name = names.get(q.checkout_id, q.checkout_id.replace("_", " ").title())
                rec_q = self.detect_checkout_bottleneck(q, q_name, queue_metrics)
                if rec_q:
                    recommendations.append(rec_q)

        # 3. Process shelf stockouts
        if shelf_summaries:
            for s in shelf_summaries:
                z_name = names.get(s.zone_id, s.zone_id.replace("zone_", "").replace("_", " ").title())
                rec_stock = self.detect_shelf_stockout(s, z_name)
                if rec_stock:
                    recommendations.append(rec_stock)

        # Sort recommendations by severity: CRITICAL > HIGH > MEDIUM > LOW > INFO
        severity_order = {
            RecommendationSeverity.CRITICAL: 0,
            RecommendationSeverity.HIGH: 1,
            RecommendationSeverity.MEDIUM: 2,
            RecommendationSeverity.LOW: 3,
            RecommendationSeverity.INFO: 4,
        }
        recommendations.sort(key=lambda r: severity_order.get(r.severity, 99))

        return recommendations


# Global singleton instance
retail_decision_engine = RetailDecisionEngine()
