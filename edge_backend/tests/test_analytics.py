"""Comprehensive unit and integration tests for Supermarket Retail Analytics & Intelligence."""

import pytest
import asyncio
from datetime import datetime, date, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import engine, async_session_factory
from app.models.db_models import (
    Base,
    PlanogramItemModel,
    POSTransactionModel,
    ShelfInteractionModel,
    CustomerTrackModel,
    RetailAnalyticsSummaryModel,
    AIDecisionRecommendationModel,
)
from app.models.schemas import (
    PlanogramItem,
    POSTransaction,
    ShelfInteraction,
    CustomerTrack,
    RetailAnalyticsSummary,
    AIDecisionRecommendation,
    DecisionStatus,
    DecisionSeverity,
    ShelfActionType,
)


@pytest.fixture(scope="module", autouse=True)
def setup_analytics_test_db():
    """Ensure all tables are created prior to running analytics tests."""
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_init())


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


# ---------------- 1. API Route Tests ----------------

def test_analytics_overview_endpoint(client):
    response = client.get("/api/v1/analytics/overview")
    assert response.status_code == 200
    data = response.json()
    
    assert data["store_id"] == "store_main"
    assert "store_name" in data
    assert "today_footfall" in data
    assert data["today_footfall"] >= 0
    assert "avg_dwell_minutes" in data
    assert "conversion_rate" in data
    assert "daily_revenue" in data
    assert "queue_stats" in data
    assert "avg_wait_sec" in data["queue_stats"]
    assert "hot_zones" in data
    assert len(data["hot_zones"]) > 0


def test_analytics_floorplan_endpoint(client):
    response = client.get("/api/v1/analytics/floorplan")
    assert response.status_code == 200
    data = response.json()

    assert data["store_id"] == "store_main"
    assert "dimensions" in data
    assert "cameras" in data
    assert len(data["cameras"]) > 0
    assert "zones" in data
    assert len(data["zones"]) > 0
    assert "categories" in data
    assert "active_shoppers" in data
    assert "total_active_count" in data


def test_analytics_heatmaps_endpoint(client):
    response = client.get("/api/v1/analytics/heatmaps?resolution_w=40&resolution_h=25")
    assert response.status_code == 200
    data = response.json()

    assert data["grid_width"] == 40
    assert data["grid_height"] == 25
    assert "density_matrix" in data
    assert len(data["density_matrix"]) == 25
    assert len(data["density_matrix"][0]) == 40
    assert "trajectory_flows" in data
    assert len(data["trajectory_flows"]) > 0
    assert "peak_hours" in data


def test_analytics_funnels_endpoint(client):
    response = client.get("/api/v1/analytics/funnels")
    assert response.status_code == 200
    data = response.json()

    assert "funnels" in data
    assert len(data["funnels"]) > 0
    assert "overall_conversion_rate" in data
    assert "total_lost_sales_estimated" in data
    assert "funnel_stages" in data
    assert len(data["funnel_stages"]) == 5
    
    # Check first funnel structure
    first_funnel = data["funnels"][0]
    assert "category" in first_funnel
    assert "shelf_zone_id" in first_funnel
    assert "impressions" in first_funnel
    assert "conversion_rate" in first_funnel
    assert "lost_sales_estimated" in first_funnel


def test_analytics_queues_endpoint(client):
    response = client.get("/api/v1/analytics/queues")
    assert response.status_code == 200
    data = response.json()

    assert "registers" in data
    assert len(data["registers"]) >= 5
    assert "store_avg_wait_sec" in data
    assert "recommended_open_registers" in data
    assert "overall_queue_sla_percent" in data
    
    # Verify at least one register has bottleneck telemetry
    reg2 = next((r for r in data["registers"] if r["register_id"] == "pos_2"), None)
    assert reg2 is not None
    assert reg2["status"] == "BUSY"
    assert reg2["bottleneck_alert"] is True


def test_analytics_decisions_crud_and_filtering(client):
    # 1. Get all decisions
    response = client.get("/api/v1/analytics/decisions")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["total"] > 0
    assert len(data["decisions"]) > 0

    first_decision = data["decisions"][0]
    decision_id = first_decision["id"]

    # 2. Filter by status
    filter_res = client.get("/api/v1/analytics/decisions?status=PENDING")
    assert filter_res.status_code == 200
    for d in filter_res.json()["decisions"]:
        assert d["status"] == "PENDING"

    # 3. Update decision action status
    action_res = client.post(
        f"/api/v1/analytics/decisions/{decision_id}/action",
        json={"status": "APPLIED", "notes": "Restock applied immediately"}
    )
    assert action_res.status_code == 200
    action_data = action_res.json()
    assert action_data["status"] == "success"
    assert action_data["decision"]["status"] == "APPLIED"

    # 4. Verify 404 for invalid decision id
    invalid_res = client.post(
        "/api/v1/analytics/decisions/invalid_id_999/action",
        json={"status": "APPLIED"}
    )
    assert invalid_res.status_code == 404


def test_analytics_daily_report_json(client):
    response = client.get("/api/v1/analytics/report/daily?format=json")
    assert response.status_code == 200
    data = response.json()

    assert "report_title" in data
    assert "date" in data
    assert "store_id" in data
    assert "executive_summary" in data
    assert "kpi_scorecard" in data
    assert "overview" in data
    assert "funnels" in data
    assert "queues" in data
    assert "decisions" in data


def test_analytics_daily_report_html(client):
    response = client.get("/api/v1/analytics/report/daily?format=html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Executive Daily Intelligence Report" in response.text
    assert "Total Footfall" in response.text
    assert "High-Priority AI Action Items" in response.text


def test_analytics_pos_ingest(client):
    payload = {
        "transactions": [
            {
                "transaction_id": "tx_test_101",
                "register_id": "pos_1",
                "sku_id": "SKU_CHIPS_01",
                "quantity": 2,
                "amount": 9.00
            },
            {
                "transaction_id": "tx_test_102",
                "register_id": "pos_2",
                "sku_id": "SKU_MILK_01",
                "quantity": 1,
                "amount": 3.20
            }
        ]
    }
    response = client.post("/api/v1/analytics/pos/ingest", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["ingested_count"] == 2
    assert data["total_amount"] == 12.20
    assert "tx_test_101" in data["transaction_ids"]


def test_analytics_sync_endpoint(client):
    sync_payload = {
        "store_id": "store_main",
        "cloud_endpoint": "https://central-cloud.internal/api/v1/telemetry",
        "include_raw_tracks": False
    }
    response = client.post("/api/v1/analytics/sync", json=sync_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["store_id"] == "store_main"
    assert data["batch_id"].startswith("sync_")
    assert "synced_at" in data
    assert data["records_synced"] > 0


def test_analytics_planogram_endpoint(client):
    response = client.get("/api/v1/analytics/planogram")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert data["total"] > 0
    assert any(i["sku_id"] == "SKU_CHIPS_01" for i in data["items"])


# ---------------- 2. Database Models & Schema Tests ----------------

def test_planogram_model_and_schema():
    item = PlanogramItem(
        sku_id="SKU_COLA_01",
        name="Classic Cola 2L",
        category="Beverages",
        shelf_zone_id="zone_aisle_02",
        price=3.50,
        facing_count=6
    )
    assert item.sku_id == "SKU_COLA_01"
    assert item.price == 3.50
    assert item.facing_count == 6


def test_pos_transaction_schema():
    pos = POSTransaction(
        transaction_id="tx_999",
        register_id="pos_3",
        sku_id="SKU_BREAD_01",
        quantity=1,
        amount=4.00
    )
    assert pos.transaction_id == "tx_999"
    assert pos.register_id == "pos_3"
    assert pos.amount == 4.00


def test_shelf_interaction_schema():
    interaction = ShelfInteraction(
        camera_id="cam_aisle_03",
        shelf_zone_id="zone_aisle_03",
        person_track_id="trk_42",
        action_type="GRAB",
        duration_sec=3.5
    )
    assert interaction.camera_id == "cam_aisle_03"
    assert interaction.action_type == "GRAB"
    assert interaction.duration_sec == 3.5


def test_customer_track_schema():
    track = CustomerTrack(
        track_id="trk_888",
        camera_id="cam_entrance_main",
        trajectory_points=[{"x": 0.1, "y": 0.2, "timestamp": 12345}],
        age_group="25-34",
        gender="FEMALE",
        sentiment="POSITIVE"
    )
    assert track.track_id == "trk_888"
    assert track.age_group == "25-34"
    assert track.sentiment == "POSITIVE"


def test_retail_summary_schema():
    summary = RetailAnalyticsSummary(
        date="2026-09-01",
        store_id="store_main",
        total_footfall=3500,
        avg_dwell_time=19.2,
        zone_metrics={"zone_produce": {"dwell": 75}},
        lost_sales_alerts=[{"zone": "zone_aisle_03", "phi": 0.65}],
        recommendations=[{"id": "rec_1"}]
    )
    assert summary.total_footfall == 3500
    assert summary.avg_dwell_time == 19.2
    assert "zone_produce" in summary.zone_metrics


def test_ai_decision_schema_enums():
    decision = AIDecisionRecommendation(
        id="dec_test",
        date="2026-09-01",
        category="SAFETY",
        severity=DecisionSeverity.CRITICAL.value,
        zone="zone_aisle_02",
        finding="Spill detected",
        root_cause="Broken glass bottle",
        action_item="Deploy wet floor cone",
        status=DecisionStatus.PENDING.value
    )
    assert decision.severity == "CRITICAL"
    assert decision.status == "PENDING"
