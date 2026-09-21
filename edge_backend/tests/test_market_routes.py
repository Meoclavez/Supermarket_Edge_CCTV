"""The market endpoints refuse to forecast without history.

They used to return a full day of hourly footfall, stockout timelines and
elasticity simulations for a store that had never recorded a single shopper,
all generated from a hand-authored weight table in ``market_predictor`` and a
canned "deterministic edge rules" list in ``llm_market_agent``. Both modules
are deleted; the endpoints now report how much history they have.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize("module", [
    "app.services.market_predictor",
    "app.services.llm_market_agent",
    "app.services.retail_decision_engine",
    "app.services.retail_analytics_service",
    "app.services.camera_network_manager",
])
def test_fabricating_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_analytics_market_routes():
    with TestClient(app) as client:
        _assert_market_routes(client)


def _assert_market_routes(client):
    res_pred = client.get("/api/v1/analytics/market/predictions?store_id=STORE-AU-3912")
    assert res_pred.status_code == 200
    data_pred = res_pred.json()
    assert set(data_pred) == {
        "sufficient_history", "days_observed", "days_required",
        "hourly_forecast", "message",
    }
    for gone in ("store_id", "day_type", "hourly_footfall_forecast",
                 "stockout_risks", "tier_elasticity_simulations"):
        assert gone not in data_pred

    assert isinstance(data_pred["sufficient_history"], bool)
    assert data_pred["days_required"] > 0
    assert data_pred["days_observed"] >= 0
    if data_pred["sufficient_history"]:
        assert len(data_pred["hourly_forecast"]) > 0
    else:
        assert data_pred["hourly_forecast"] == []
        assert isinstance(data_pred["message"], str) and data_pred["message"]
        assert data_pred["days_observed"] < data_pred["days_required"]

    res_status = client.get("/api/v1/analytics/market/llm-status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert {"ollama_active", "model", "generation_verified"} <= set(data_status)
    assert isinstance(data_status["ollama_active"], bool)
    assert isinstance(data_status["generation_verified"], bool)
    if not data_status["ollama_active"]:
        assert data_status["model"] is None
        assert data_status["generation_verified"] is False

    res_opt = client.post("/api/v1/analytics/market/llm-optimize?store_id=STORE-AU-3912")
    assert res_opt.status_code == 200
    data_opt = res_opt.json()
    assert {"generated_at", "findings", "findings_count", "zones_assessed",
            "zones_total", "sufficient_data", "message", "narrative"} <= set(data_opt)
    assert "optimizations" not in data_opt
    assert isinstance(data_opt["findings"], list)
    assert data_opt["findings_count"] == len(data_opt["findings"])
    assert data_opt["zones_assessed"] <= data_opt["zones_total"]
    if not data_opt["sufficient_data"]:
        assert data_opt["findings"] == []
        assert data_opt["findings_count"] == 0
        assert isinstance(data_opt["message"], str) and data_opt["message"]
        assert data_opt["narrative"]["summary"] is None
        assert isinstance(data_opt["narrative"]["reason"], str)
