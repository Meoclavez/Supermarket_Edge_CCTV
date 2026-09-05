"""Unit tests for ML market predictor, stockout timelines, and LLM market reasoning agent."""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.market_predictor import market_predictor
from app.services.llm_market_agent import (
    llm_market_agent,
    check_ollama_status,
    select_best_model,
    is_embedding_model,
    LLMMarketReasoningAgent,
)


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


def test_select_best_model_priorities():
    """Unit test model selector priority order and embedding model filtering."""
    # 1. Filters embedding model
    embedding_only = [{"name": "mxbai-embed-large:latest", "details": {"family": "bert"}}]
    assert select_best_model(embedding_only) is None

    # 2. Priority 1: ornith-brain
    mixed_models = [
        {"name": "mxbai-embed-large:latest"},
        {"name": "lfm2.5:8b"},
        {"name": "ornith:9b"},
        {"name": "ornith-brain:latest"}
    ]
    assert select_best_model(mixed_models) == "ornith-brain:latest"

    # 3. Priority 2: ornith
    without_brain = [
        {"name": "mxbai-embed-large:latest"},
        {"name": "lfm2.5:8b"},
        {"name": "ornith:9b"}
    ]
    assert select_best_model(without_brain) == "ornith:9b"

    # 4. Priority 3: qwen
    qwen_mixed = [
        {"name": "lfm2.5:8b"},
        {"name": "qwen2.5:14b"},
        {"name": "llama3.2:3b"}
    ]
    assert select_best_model(qwen_mixed) == "qwen2.5:14b"

    # 5. Priority 4: llama
    llama_mixed = [
        {"name": "lfm2.5:8b"},
        {"name": "llama3.1:8b"}
    ]
    assert select_best_model(llama_mixed) == "llama3.1:8b"

    # 6. Priority 5: lfm
    lfm_only = [
        {"name": "mxbai-embed-large:latest"},
        {"name": "lfm2.5:8b"}
    ]
    assert select_best_model(lfm_only) == "lfm2.5:8b"

    # 7. Fallback to first non-embedding model
    other_models = [
        {"name": "mxbai-embed-large:latest"},
        {"name": "mistral:7b"}
    ]
    assert select_best_model(other_models) == "mistral:7b"


def test_check_ollama_status_live():
    """Verify live dynamic discovery on local Ollama service."""
    status_info = check_ollama_status()
    assert "status" in status_info
    assert "ollama_active" in status_info
    assert "available_models" in status_info
    assert "active_model" in status_info
    assert "warning" in status_info
    assert status_info["status"] in ("online", "offline")
    if status_info["status"] == "online":
        assert status_info["ollama_active"] is True
        assert status_info["active_model"] == "ornith-brain:latest"
        assert len(status_info["available_models"]) >= 3
        assert status_info["warning"] is None


def test_ollama_offline_fallback():
    """Verify clean fallback behavior when Ollama service is unreachable."""
    offline_status = check_ollama_status(ollama_url="http://localhost:59999")
    assert offline_status["status"] == "offline"
    assert offline_status["ollama_active"] is False
    assert offline_status["active_model"] is None
    assert offline_status["available_models"] == []
    assert "offline or model unavailable" in offline_status["warning"]


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
    # Use offline agent or timeout=0.01 for fast deterministic test
    offline_agent = LLMMarketReasoningAgent(ollama_url="http://localhost:59999")
    response = offline_agent.generate_optimizations("STORE-AU-3912", sample_stats, forecast)
    assert response.total_products_monitored == 1
    assert len(response.optimizations) >= 1
    assert response.model_used == "deterministic-edge-rules"
    assert response.ollama_status == "offline"
    assert response.warning is not None
    assert response.ollama_active is False
    
    # Should detect pricing / friction anomaly
    opt = response.optimizations[0]
    assert opt.category == "PRICING"
    assert opt.priority == "HIGH"
    assert "promotional" in opt.automated_recommendation.lower() or "price" in opt.automated_recommendation.lower()


def test_analytics_market_routes():
    client = TestClient(app)
    
    # 1. Test GET predictions
    res_pred = client.get("/api/v1/analytics/market/predictions?store_id=STORE-AU-3912")
    assert res_pred.status_code == 200
    data_pred = res_pred.json()
    assert "hourly_footfall_forecast" in data_pred
    assert "stockout_risks" in data_pred
    assert "tier_elasticity_simulations" in data_pred

    # 2. Test GET LLM status
    res_status = client.get("/api/v1/analytics/market/llm-status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert "status" in data_status
    assert "ollama_active" in data_status
    assert "active_model" in data_status
    assert "available_models" in data_status
    assert "warning" in data_status

    # 3. Test POST LLM optimize
    res_opt = client.post("/api/v1/analytics/market/llm-optimize?store_id=STORE-AU-3912")
    assert res_opt.status_code == 200
    data_opt = res_opt.json()
    assert "model_used" in data_opt
    assert "ollama_status" in data_opt
    assert "warning" in data_opt
    assert "optimizations" in data_opt
    assert len(data_opt["optimizations"]) > 0
