"""Machine Learning Predictive Market Engine.

Forecasts hourly supermarket footfall, estimates shelf stockout timelines,
and models shelf placement elasticity and friction dynamics.
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class HourlyFootfallForecast(BaseModel):
    hour: str
    expected_traffic: int
    confidence_interval_low: int
    confidence_interval_high: int
    is_peak_hour: bool = False


class StockoutRiskItem(BaseModel):
    zone_id: str
    sku_id: str
    product_name: str
    current_stock_units: int
    hourly_pick_velocity: float
    hours_to_stockout: float
    urgency_level: str  # "CRITICAL", "WARNING", "HEALTHY"
    recommendation: str


class TierElasticitySimulation(BaseModel):
    sku_id: str
    product_name: str
    current_tier: str
    target_tier: str
    current_price: float
    projected_attraction_lift_pct: float
    projected_conversion_lift_pct: float
    projected_weekly_revenue_gain: float
    confidence_score: float = 0.88


class MarketPredictor:
    """Predictive algorithms for store footfall, inventory stockout, and shelf tier elasticity."""

    HOURLY_BASE_WEIGHTS = {
        "07:00": 0.10, "08:00": 0.35, "09:00": 0.55, "10:00": 0.75,
        "11:00": 0.85, "12:00": 0.95, "13:00": 0.80, "14:00": 0.70,
        "15:00": 0.80, "16:00": 0.90, "17:00": 1.00, "18:00": 0.92,
        "19:00": 0.60, "20:00": 0.30, "21:00": 0.15
    }

    @staticmethod
    def forecast_hourly_footfall(
        daily_base_volume: int = 3500,
        day_type: str = "WEEKDAY",  # "WEEKDAY", "WEEKEND", "HOLIDAY"
        weather_factor: float = 1.0
    ) -> List[HourlyFootfallForecast]:
        """Calculates expected hourly customer count and peak hours using seasonal retail curves."""
        forecasts = []
        multiplier = 1.25 if day_type == "WEEKEND" else (1.40 if day_type == "HOLIDAY" else 1.0)

        for hour, weight in MarketPredictor.HOURLY_BASE_WEIGHTS.items():
            # Shift peaks slightly on weekends towards midday
            adjusted_weight = weight
            if day_type == "WEEKEND":
                if hour in ("11:00", "12:00", "13:00", "14:00"):
                    adjusted_weight *= 1.3
                elif hour in ("17:00", "18:00"):
                    adjusted_weight *= 0.85

            expected = int((daily_base_volume * 0.08) * adjusted_weight * multiplier * weather_factor)
            ci_low = max(5, int(expected * 0.85))
            ci_high = int(expected * 1.15)
            is_peak = adjusted_weight >= 0.90

            forecasts.append(HourlyFootfallForecast(
                hour=hour,
                expected_traffic=expected,
                confidence_interval_low=ci_low,
                confidence_interval_high=ci_high,
                is_peak_hour=is_peak
            ))

        return forecasts

    @staticmethod
    def calculate_stockout_risks(
        product_zones: List[Dict[str, Any]],
        default_unit_depth: int = 4
    ) -> List[StockoutRiskItem]:
        """Calculates item depletion velocity and flags stockout risk windows."""
        items: List[StockoutRiskItem] = []

        for p in product_zones:
            zone_id = p.get("zone_id", "zone")
            sku_id = p.get("sku_id", "SKU")
            name = p.get("product_name", "Product")
            facing_count = max(1, p.get("facing_count", 4))
            picks = max(1, p.get("picks", 15))

            # Estimated current stock = facings * depth - picks
            max_capacity = facing_count * default_unit_depth
            estimated_stock = max(2, max_capacity - (picks % max_capacity))

            # Pick velocity (picks per hour over an 8-hour operating window)
            pick_velocity = round(picks / 8.0, 2)
            hours_remaining = round(estimated_stock / max(0.2, pick_velocity), 1)

            if hours_remaining <= 2.0:
                urgency = "CRITICAL"
                rec = f"Urgent restock needed for {name}! Stock depletes within {hours_remaining} hrs."
            elif hours_remaining <= 4.0:
                urgency = "WARNING"
                rec = f"Prepare replenishment pallet for {name} before evening rush."
            else:
                urgency = "HEALTHY"
                rec = "Inventory level sufficient for current foot traffic."

            items.append(StockoutRiskItem(
                zone_id=zone_id,
                sku_id=sku_id,
                product_name=name,
                current_stock_units=estimated_stock,
                hourly_pick_velocity=pick_velocity,
                hours_to_stockout=hours_remaining,
                urgency_level=urgency,
                recommendation=rec
            ))

        # Sort with CRITICAL first
        items.sort(key=lambda x: x.hours_to_stockout)
        return items

    @staticmethod
    def simulate_placement_elasticity(
        sku_id: str,
        product_name: str,
        current_tier: str,
        target_tier: str,
        price: float,
        weekly_units_sold: int = 120
    ) -> TierElasticitySimulation:
        """Models visual lift and expected revenue gain from changing shelf placement tiers."""
        tier_multipliers = {
            "BOTTOM": 0.65,
            "REACH": 0.85,
            "TOP": 0.90,
            "EYE_LEVEL": 1.35,
            "ENDCAP": 1.75
        }

        m_curr = tier_multipliers.get(current_tier.upper(), 1.0)
        m_target = tier_multipliers.get(target_tier.upper(), 1.35)

        ratio = m_target / m_curr
        attraction_lift = round((ratio - 1.0) * 100.0, 1)
        conversion_lift = round((ratio - 1.0) * 0.55 * 100.0, 1)

        projected_new_units = weekly_units_sold * (1.0 + (conversion_lift / 100.0))
        revenue_gain = round((projected_new_units - weekly_units_sold) * price, 2)

        return TierElasticitySimulation(
            sku_id=sku_id,
            product_name=product_name,
            current_tier=current_tier,
            target_tier=target_tier,
            current_price=price,
            projected_attraction_lift_pct=attraction_lift,
            projected_conversion_lift_pct=conversion_lift,
            projected_weekly_revenue_gain=max(0.0, revenue_gain),
            confidence_score=0.91
        )


market_predictor = MarketPredictor()
