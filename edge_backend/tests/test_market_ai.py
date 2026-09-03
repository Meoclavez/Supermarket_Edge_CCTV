"""Unit tests for ML market predictor, stockout timelines, and LLM market reasoning agent."""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.market_predictor import market_predictor
from app.services.llm_market_agent import llm_market_agent


def test_hourly_footfall_forecast():
    forecast = market_predictor.forecast_hourly_footfall(daily_base_volume=3000, day_type="WEEKDAY")
    assert len(forecast) == len(market_predictor.HOURLY_BASE_WEIGHTS)
    
    # Peak hours should be flagged around 17:00
    peak_17 = next(f for f in forecast if f.hour == "17:00")
    assert peak_17.is_peak_hour is True
    assert peak_17.expected_traffic > 0
    assert peak_17.confidence_interval_low < peak_17.expected_traffic < peak_17.confidence_interval_high


def test_stockout_risk_estimator():
    test_zones = [
        {
            "zone_id": "zone_crisps",
            "sku_id": "SKU-CRISP-01",
            "product_name": "Artisan Potato Crisps",
            "facing_count": 2,
            "picks": 35  # High pick velocity
        },
        {
            "zone_id": "zone_beans",
            "sku_id": "SKU-BEAN-01",
            "product_name": "Canned Beans",
            "facing_count": 8,
            "picks": 5   # Low pick velocity
        }
    ]
    risks = market_predictor.calculate_stockout_risks(test_zones)
    assert len(risks) == 2
    # The crisps with high velocity should have shorter hours to stockout
    assert risks[0].sku_id == "SKU-CRISP-01"
    assert risks[0].hours_to_stockout <= risks[1].hours_to_stockout


def test_placement_elasticity_simulation():
    sim = market_predictor.simulate_placement_elasticity(
        sku_id="SKU-TEST-01",
        product_name="Granola Bar",
        current_tier="BOTTOM",
        target_tier="EYE_LEVEL",
        price=5.00,
        weekly_units_sold=100
    )
    assert sim.projected_attraction_lift_pct > 0
    assert sim.projected_conversion_lift_pct > 0
    assert sim.projected_weekly_revenue_gain > 0


def test_llm_market_reasoning_agent():
    sample_stats = [
        {
            "zone_id": "zone_granola",
            "sku_id": "SKU-GRANOLA-PREM",
            "product_name": "Premium Granola",
            "price": 14.50,
            "touches": 35,
            "picks": 5,
            "put_backs": 30,
            "friction_index": 85.7,
            "shelf_tier": "EYE_LEVEL",
            "avg_dwell_sec": 14.2
        }
    ]
    forecast = [{"hour": "17:00", "is_peak_hour": True}]
    response = llm_market_agent.generate_optimizations("STORE-AU-3912", sample_stats, forecast)
    assert response.total_products_monitored == 1
    assert len(response.optimizations) >= 1
    
    # Should detect pricing / friction anomaly
    opt = response.optimizations[0]
    assert opt.category == "PRICING"
    assert opt.priority == "HIGH"
    assert "promotional" in opt.automated_recommendation.lower() or "price" in opt.automated_recommendation.lower()


def test_analytics_market_routes():
    client = TestClient(app)
    
    # Test GET predictions
    res_pred = client.get("/api/v1/analytics/market/predictions?store_id=STORE-AU-3912")
    assert res_pred.status_code == 200
    data_pred = res_pred.json()
    assert "hourly_footfall_forecast" in data_pred
    assert "stockout_risks" in data_pred
    assert "tier_elasticity_simulations" in data_pred

    # Test POST LLM optimize
    res_opt = client.post("/api/v1/analytics/market/llm-optimize?store_id=STORE-AU-3912")
    assert res_opt.status_code == 200
    data_opt = res_opt.json()
    assert "optimizations" in data_opt
    assert len(data_opt["optimizations"]) > 0
