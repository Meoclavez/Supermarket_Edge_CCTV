"""Unit and integration tests for the retail analytics endpoints.

The suite this replaced asserted the *fabrications* the dashboard used to
serve: 32 seeded cameras, six hardcoded checkout registers, five canned "AI
decisions" and a planogram of SKUs nobody had ever scanned. Those constants
were deleted from the application. These tests pin the honest contract that
replaced them.
"""

import asyncio
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import app
from app.database import engine, async_session_factory
from app.models.db_models import (
    Base,
    CameraModel,
    PlanogramItemModel,
    POSTransactionModel,
    ShelfInteractionModel,
    CustomerTrackModel,
    RetailAnalyticsSummaryModel,
    AIDecisionRecommendationModel,
    StoreLayoutModel,
    StoreZoneModel,
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
from app.services.auth_service import auth_service
from app.services.retail_metrics_service import day_bounds, retail_metrics_service


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


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token(
        {"sub": "test_admin", "role": "admin", "type": "user_session"}
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------- 1. API Route Tests ----------------
#
# These endpoints used to answer with hardcoded constants: 32 cameras, six
# checkout registers, five "AI decisions", an invented planogram and a heatmap
# built from random noise. All of that was deleted. The assertions below pin the
# honest replacement: a metric that was never measured is reported as ``None``
# and never as ``0``, and every list reflects exactly what is in the database.


async def _db_count(model):
    """Count rows of ``model`` in the live test database."""
    async with async_session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


def db_count(model):
    return asyncio.run(_db_count(model))


OVERVIEW_KEYS = {
    "store_name", "timestamp", "window", "today_footfall", "active_shoppers_now",
    "avg_dwell_seconds", "avg_dwell_minutes", "daily_revenue", "transactions",
    "conversion_rate_pct", "zones_total", "zones_with_data", "top_zones",
    "coverage", "has_data", "pos_connected", "footfall_source",
}

# Which signal today's footfall came from (entrance tripwires win; see
# RetailMetricsService.footfall); null when nothing was observed.
FOOTFALL_SOURCES = {None, "tripwire", "zone_visits", "tracks"}

# Keys that only ever existed because they were fabricated.
OVERVIEW_REMOVED_KEYS = {
    "store_id", "conversion_rate", "conversion_rate_percent", "lost_sales_index_phi",
    "queue_avg_wait_minutes", "hot_zones", "total_cameras_active",
    "edge_uptime_percent", "active_ai_alerts", "queue_stats",
}


def test_analytics_overview_endpoint(client):
    response = client.get("/api/v1/analytics/overview")
    assert response.status_code == 200
    data = response.json()

    assert set(data) == OVERVIEW_KEYS
    assert OVERVIEW_REMOVED_KEYS.isdisjoint(data)

    assert isinstance(data["store_name"], str)
    assert set(data["window"]) == {"start", "end"}

    # Every headline metric is either a real measurement or an explicit None.
    for key in ("today_footfall", "avg_dwell_seconds", "avg_dwell_minutes",
                "daily_revenue", "transactions", "conversion_rate_pct"):
        assert data[key] is None or isinstance(data[key], (int, float)), key

    assert isinstance(data["active_shoppers_now"], int)
    assert isinstance(data["top_zones"], list)
    assert isinstance(data["has_data"], bool)
    assert isinstance(data["pos_connected"], bool)
    assert data["footfall_source"] in FOOTFALL_SOURCES
    # A figure always names its source, and no source means no figure.
    assert (data["today_footfall"] is None) == (data["footfall_source"] is None)
    assert set(data["coverage"]) == {
        "cameras_total", "cameras_calibrated", "cameras_uncalibrated",
        "floor_tracking_possible",
    }

    # zones_total must match the blueprint, not a decorative constant.
    assert data["zones_total"] == db_count(StoreZoneModel)
    assert data["zones_with_data"] <= data["zones_total"]


def test_unobserved_metrics_are_none_not_zero():
    """The point of the whole refactor.

    An empty store has *no measurement* of footfall, revenue or conversion. It
    does not have a footfall of zero -- that is a claim the system cannot make
    with no calibrated camera and no POS feed. This test runs against its own
    pristine database so it can never be satisfied by data another test left
    behind.
    """
    async def _run():
        tmp = Path(tempfile.mkdtemp(prefix="cctv_pristine_"))
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp / 'pristine.db'}")
        try:
            async with fresh_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
            async with factory() as session:
                layout = StoreLayoutModel(
                    id="layout_pristine", store_id="store_pristine",
                    name="Pristine", width_m=40.0, height_m=20.0, is_active=True,
                )
                session.add(layout)
                await session.commit()

                overview = await retail_metrics_service.overview(
                    session, layout.id, "Pristine Store"
                )

                assert overview["today_footfall"] is None
                assert overview["today_footfall"] != 0
                assert overview["daily_revenue"] is None
                assert overview["daily_revenue"] != 0
                assert overview["conversion_rate_pct"] is None
                assert overview["avg_dwell_seconds"] is None
                assert overview["avg_dwell_minutes"] is None
                assert overview["transactions"] is None
                assert overview["has_data"] is False
                assert overview["pos_connected"] is False

                # Counts of *configured objects* are genuinely zero -- the
                # system knows there are no zones. That is a measurement.
                assert overview["zones_total"] == 0
                assert overview["active_shoppers_now"] == 0

                start, end = day_bounds()
                funnel = await retail_metrics_service.funnel(session, layout.id, start, end)
                for stage in funnel["stages"]:
                    assert stage["count"] is None
                    assert stage["observed"] is False
                assert funnel["conversion_rate_pct"] is None
                assert funnel["revenue"] is None

                # No CHECKOUT zone has been drawn, so there is no lane to
                # report on -- not six registers with plausible wait times.
                lanes = await retail_metrics_service.checkout_queues(
                    session, layout.id, start, end
                )
                assert lanes == []
        finally:
            await fresh_engine.dispose()

    asyncio.run(_run())


def test_analytics_floorplan_endpoint(client):
    response = client.get("/api/v1/analytics/floorplan")
    assert response.status_code == 200
    data = response.json()

    # The blueprint is now expressed in metres and is operator-drawn, not a
    # literal array duplicated in Python and JavaScript.
    assert data["units"] == "metres"
    assert data["width_m"] > 0
    assert data["height_m"] > 0
    assert isinstance(data["layout_id"], str)
    assert isinstance(data["name"], str)
    assert "CHECKOUT" in data["zone_categories"]

    for gone in ("dimensions", "categories", "active_shoppers", "total_active_count"):
        assert gone not in data

    assert isinstance(data["zones"], list)
    assert len(data["zones"]) == db_count(StoreZoneModel)
    for zone in data["zones"]:
        assert set(zone) == {
            "id", "name", "category", "polygon", "color", "sku_id",
            "sort_order", "area_m2", "metrics",
        }
        assert all(set(p) == {"x", "y"} for p in zone["polygon"])

    assert isinstance(data["cameras"], list)
    assert len(data["cameras"]) == db_count(CameraModel)
    for cam in data["cameras"]:
        assert "position_2d" not in cam
        assert "fov_polygon" not in cam
        assert {"camera_id", "floor_x", "floor_y", "azimuth_deg",
                "fov_deg", "has_homography"} <= set(cam)

    assert isinstance(data["active_shoppers_now"], int)
    assert set(data["coverage"]) == {
        "cameras_total", "cameras_calibrated", "cameras_uncalibrated",
        "floor_tracking_possible",
    }


def test_analytics_heatmaps_endpoint(client):
    """With no trajectories the heatmap says so instead of rendering noise."""
    response = client.get("/api/v1/analytics/heatmaps?resolution_w=40&resolution_h=25")
    assert response.status_code == 200
    data = response.json()

    assert data["grid_width"] == 40
    assert data["grid_height"] == 25

    if data["observed"]:
        assert data["samples"] > 0
        assert len(data["density_matrix"]) == 25
        assert len(data["density_matrix"][0]) == 40
    else:
        assert data["density_matrix"] is None
        assert data["samples"] == 0
        assert isinstance(data["message"], str) and data["message"]


def test_analytics_funnels_endpoint(client):
    response = client.get("/api/v1/analytics/funnels")
    assert response.status_code == 200
    data = response.json()

    assert set(data) == {
        "stages", "attraction_rate_pct", "engagement_rate_pct",
        "conversion_rate_pct", "revenue", "lost_sales_index_pct",
        "pos_connected", "zones", "demographics",
    }
    # The old per-category funnel objects with invented impressions are gone.
    assert "funnels" not in data
    assert "total_lost_sales_estimated" not in data

    assert [s["stage"] for s in data["stages"]] == [
        "Passed", "Dwelled", "Engaged", "Converted"
    ]
    for stage in data["stages"]:
        assert set(stage) == {"stage", "count", "observed"}
        if stage["observed"]:
            assert isinstance(stage["count"], int)
        else:
            assert stage["count"] is None

    for rate in ("attraction_rate_pct", "engagement_rate_pct",
                 "conversion_rate_pct", "lost_sales_index_pct", "revenue"):
        assert data[rate] is None or isinstance(data[rate], (int, float)), rate

    assert isinstance(data["pos_connected"], bool)
    assert isinstance(data["zones"], list)

    # No demographic classifier ships with this deployment, so the endpoint
    # declares the gap rather than inventing age/gender splits.
    assert data["demographics"]["available"] is False
    assert isinstance(data["demographics"]["reason"], str)


def test_analytics_queues_endpoint(client, auth_headers):
    """Checkout lanes come from CHECKOUT zones -- not six hardcoded registers."""
    before = client.get("/api/v1/analytics/queues")
    assert before.status_code == 200
    base = before.json()
    assert isinstance(base["registers"], list)
    assert base["total_lanes"] == len(base["registers"])
    assert base["observed"] is False
    for gone in ("store_avg_wait_sec", "recommended_open_registers",
                 "overall_queue_sla_percent"):
        assert gone not in base
    assert not any(r["zone_id"].startswith("pos_") for r in base["registers"])

    # Draw a checkout lane and it appears; there is no other way for one to exist.
    created = client.post(
        "/api/v1/layout/zones",
        json={
            "name": "Lane Under Test",
            "category": "CHECKOUT",
            "polygon": [{"x": 1, "y": 1}, {"x": 4, "y": 1}, {"x": 4, "y": 4}, {"x": 1, "y": 4}],
            "color": "#ff0000",
        },
        headers=auth_headers,
    )
    assert created.status_code == 201
    zone_id = created.json()["id"]

    try:
        res = client.get("/api/v1/analytics/queues")
        assert res.status_code == 200
        data = res.json()
        assert data["total_lanes"] == len(base["registers"]) + 1

        lane = next(r for r in data["registers"] if r["zone_id"] == zone_id)
        assert lane["name"] == "Lane Under Test"
        # Nobody has queued yet, so wait time is unmeasured, not zero.
        assert lane["avg_wait_seconds"] is None
        assert lane["avg_wait_minutes"] is None
        assert lane["observed"] is False
        assert lane["status"] == "NO DATA"
        assert lane["queue_length_now"] == 0
    finally:
        deleted = client.delete(f"/api/v1/layout/zones/{zone_id}", headers=auth_headers)
        assert deleted.status_code == 200


def test_analytics_decisions_crud_and_filtering(client):
    """The recommendation feed is empty until the rule engine writes to it.

    There are no seeded ``dec_01..dec_05`` rows any more, so this test creates
    the row whose lifecycle it wants to exercise.
    """
    async def _insert():
        async with async_session_factory() as session:
            session.add(AIDecisionRecommendationModel(
                id="dec_crud_under_test",
                date=date.today().isoformat(),
                category="RESTOCKING",
                severity=DecisionSeverity.CRITICAL.value,
                zone="zone_under_test",
                finding="Shelf empty for 40 minutes",
                root_cause="Replenishment run missed",
                action_item="Restock from back room",
                status=DecisionStatus.PENDING.value,
            ))
            await session.commit()

    async def _delete():
        async with async_session_factory() as session:
            row = await session.get(AIDecisionRecommendationModel, "dec_crud_under_test")
            if row is not None:
                await session.delete(row)
                await session.commit()

    asyncio.run(_delete())
    asyncio.run(_insert())

    try:
        response = client.get("/api/v1/analytics/decisions")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        # The feed mirrors the table exactly -- nothing is added for display.
        assert data["total"] == db_count(AIDecisionRecommendationModel)
        assert data["total"] == len(data["decisions"])
        assert not any(d["id"].startswith("dec_0") for d in data["decisions"])

        mine = next(d for d in data["decisions"] if d["id"] == "dec_crud_under_test")
        assert mine["status"] == "PENDING"
        assert mine["severity"] == "CRITICAL"

        filter_res = client.get("/api/v1/analytics/decisions?status=PENDING")
        assert filter_res.status_code == 200
        filtered = filter_res.json()["decisions"]
        assert all(d["status"] == "PENDING" for d in filtered)
        assert any(d["id"] == "dec_crud_under_test" for d in filtered)

        action_res = client.post(
            "/api/v1/analytics/decisions/dec_crud_under_test/action",
            json={"status": "APPLIED", "notes": "Restock applied immediately"},
        )
        assert action_res.status_code == 200
        action_data = action_res.json()
        assert action_data["status"] == "success"
        assert action_data["decision"]["status"] == "APPLIED"

        # The mutation is persisted, not echoed back.
        recheck = client.get("/api/v1/analytics/decisions?status=APPLIED")
        assert any(d["id"] == "dec_crud_under_test" for d in recheck.json()["decisions"])

        invalid_res = client.post(
            "/api/v1/analytics/decisions/invalid_id_999/action",
            json={"status": "APPLIED"},
        )
        assert invalid_res.status_code == 404
    finally:
        asyncio.run(_delete())


def test_analytics_daily_report_json(client):
    response = client.get("/api/v1/analytics/report/daily?format=json")
    assert response.status_code == 200
    data = response.json()

    assert set(data) == {
        "report_title", "date", "generated_at", "data_available", "kpi_scorecard",
        "coverage", "funnel", "queues", "findings", "findings_count",
        "analysis_message", "executive_summary", "shelf_reach",
    }
    for gone in ("store_id", "generated_by", "overview", "decisions",
                 "total_lost_sales_estimated", "overall_queue_sla_percent"):
        assert gone not in data

    assert isinstance(data["data_available"], bool)
    assert set(data["kpi_scorecard"]) == {
        "total_footfall", "shoppers_now", "avg_dwell", "conversion", "daily_revenue", "shelf_reaches",
    }
    # An unmeasured KPI reads "Not observed" -- never "$0" or "0%".
    if not data["data_available"]:
        assert data["kpi_scorecard"]["total_footfall"] == "Not observed"
        assert data["kpi_scorecard"]["daily_revenue"] == "Not observed"
        assert data["kpi_scorecard"]["conversion"] == "Not observed"

    assert isinstance(data["findings"], list)
    assert data["findings_count"] == len(data["findings"])
    assert isinstance(data["analysis_message"], str)
    assert isinstance(data["executive_summary"], str) and data["executive_summary"]


def test_analytics_digest_alias_matches_daily_report(client):
    """/digest and /report/daily are the same report."""
    digest = client.get("/api/v1/analytics/digest?format=json")
    assert digest.status_code == 200
    daily = client.get("/api/v1/analytics/report/daily?format=json")
    assert set(digest.json()) == set(daily.json())


def test_analytics_daily_report_html(client):
    response = client.get("/api/v1/analytics/report/daily?format=html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Daily Intelligence Digest" in response.text
    assert "Total Footfall" in response.text
    assert "Findings" in response.text


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


def test_pos_ingest_stores_offset_timestamps_as_utc_and_rejects_garbage(client):
    import sqlite3
    from app.config import settings

    ok = client.post("/api/v1/analytics/pos/ingest", json={"transactions": [{
        "transaction_id": "tx_tz_1", "register_id": "pos_1", "sku_id": "SKU_TZ",
        "quantity": 1, "amount": 1.0, "timestamp": "2026-09-23T15:30:00+05:30",
    }]})
    assert ok.status_code == 200
    with sqlite3.connect(settings.DATABASE_PATH) as db:
        (stored,) = db.execute(
            "SELECT timestamp FROM pos_transactions WHERE transaction_id = 'tx_tz_1'"
        ).fetchone()
    assert stored.startswith("2026-09-23 10:00:00")

    bad = client.post("/api/v1/analytics/pos/ingest", json={"transactions": [{
        "transaction_id": "tx_tz_2", "register_id": "pos_1", "sku_id": "SKU_TZ",
        "quantity": 1, "amount": 1.0, "timestamp": "yesterday-ish",
    }]})
    assert bad.status_code == 422


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
    """No SKUs are invented: the planogram is whatever was imported, or empty."""
    response = client.get("/api/v1/analytics/planogram")
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == len(data["items"]) == db_count(PlanogramItemModel)
    if data["total"] == 0:
        assert data["items"] == []
        assert isinstance(data["message"], str) and data["message"]
    # The seeded demo catalogue is gone for good.
    assert not any(i["sku_id"] == "SKU_CHIPS_01" for i in data["items"])


def test_analytics_planogram_reflects_imported_skus(client):
    """An imported SKU shows up; nothing else does."""
    async def _insert():
        async with async_session_factory() as session:
            session.add(PlanogramItemModel(
                sku_id="SKU_UNDER_TEST",
                name="Test Oat Milk 1L",
                category="Dairy",
                shelf_zone_id="zone_under_test",
                price=4.25,
                facing_count=4,
            ))
            await session.commit()

    async def _delete():
        async with async_session_factory() as session:
            row = await session.get(PlanogramItemModel, "SKU_UNDER_TEST")
            if row is not None:
                await session.delete(row)
                await session.commit()

    asyncio.run(_delete())
    asyncio.run(_insert())
    try:
        data = client.get("/api/v1/analytics/planogram").json()
        assert data["total"] == db_count(PlanogramItemModel)
        item = next(i for i in data["items"] if i["sku_id"] == "SKU_UNDER_TEST")
        assert item["name"] == "Test Oat Milk 1L"
        assert item["price"] == 4.25
        assert item["facing_count"] == 4
    finally:
        asyncio.run(_delete())


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
