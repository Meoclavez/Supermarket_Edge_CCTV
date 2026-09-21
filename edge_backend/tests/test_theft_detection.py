"""Comprehensive Unit & Integration Tests for Loss Prevention & Theft Detection Engine."""

import pytest
import asyncio
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import engine, async_session_factory
from app.models.db_models import Base, SystemSetupModel, TheftIncidentModel
from app.models.schemas import (
    TheftType,
    TheftIncidentStatus,
)
from app.services.theft_detection_service import theft_detection_service


def insert_incident(**overrides) -> str:
    """Write a TheftIncidentModel row directly.

    The API no longer has a /simulate endpoint: an incident exists only because
    a detector (or, in tests, this helper) wrote one. Nothing here is served
    to a client as an observation.
    """
    import uuid

    fields = dict(
        id=f"theft_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}",
        theft_type="SHELF_SWEEPING",
        severity="HIGH",
        status=TheftIncidentStatus.ACTIVE.value,
        department="Test Department",
        camera_id="cam_test",
        zone_id="zone_test",
        timestamp=datetime.utcnow(),
        person_track_id="track_test",
        confidence=0.5,
        estimated_loss_value=0.0,
        items_involved=[{"sku": "TEST", "name": "test item", "price": 1.0, "qty": 1}],
        notes="inserted by test",
    )
    fields.update(overrides)

    async def _write():
        async with async_session_factory() as session:
            session.add(TheftIncidentModel(**fields))
            await session.commit()

    asyncio.run(_write())
    return fields["id"]


@pytest.fixture(scope="session", autouse=True)
def init_test_database():
    """Ensure database schema is created and initialized."""
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        async with async_session_factory() as session:
            stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed")
            res = await session.execute(stmt)
            if not res.scalar_one_or_none():
                session.add(SystemSetupModel(key="setup_completed", value="true"))
                await session.commit()
    asyncio.run(_init())


# ============================================================================
# 1. Detection Algorithm Unit Tests
# ============================================================================

class TestTheftDetectionAlgorithms:
    def test_shelf_sweeping_positive(self):
        """Test bulk sweep detection: 4 items picked within 3.5 seconds."""
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        interactions = [
            {"timestamp": t_base, "sku": "WHISKY_01", "name": "Single Malt", "zone": "Liquor Shelf A"},
            {"timestamp": t_base + timedelta(seconds=1.0), "sku": "WHISKY_02", "name": "Bourbon", "zone": "Liquor Shelf A"},
            {"timestamp": t_base + timedelta(seconds=2.2), "sku": "GIN_01", "name": "Gin", "zone": "Liquor Shelf A"},
            {"timestamp": t_base + timedelta(seconds=3.5), "sku": "VODKA_01", "name": "Vodka", "zone": "Liquor Shelf A"},
        ]

        res = theft_detection_service.detect_shelf_sweeping(interactions, window_sec=5.0, min_picks=3)
        assert res["detected"] is True
        assert res["theft_type"] == TheftType.SHELF_SWEEPING.value
        assert res["count"] == 4
        assert res["confidence"] >= 0.85
        assert len(res["window_interactions"]) == 4

    def test_shelf_sweeping_negative_below_min_picks(self):
        """Test insufficient picks (< 3) within window."""
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        interactions = [
            {"timestamp": t_base, "sku": "WHISKY_01"},
            {"timestamp": t_base + timedelta(seconds=2.0), "sku": "WHISKY_02"},
        ]
        res = theft_detection_service.detect_shelf_sweeping(interactions, window_sec=5.0, min_picks=3)
        assert res["detected"] is False
        assert res["count"] == 0

    def test_shelf_sweeping_negative_spread_out(self):
        """Test picks spread out over 20 seconds (> 5s window)."""
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        interactions = [
            {"timestamp": t_base, "sku": "WHISKY_01"},
            {"timestamp": t_base + timedelta(seconds=8.0), "sku": "WHISKY_02"},
            {"timestamp": t_base + timedelta(seconds=18.0), "sku": "WHISKY_03"},
        ]
        res = theft_detection_service.detect_shelf_sweeping(interactions, window_sec=5.0, min_picks=3)
        assert res["detected"] is False

    def test_concealment_positive(self):
        """Test concealment kinematics: wrist touches shelf ROI then directly touches body pocket."""
        shelf_roi = (0.2, 0.2, 0.4, 0.4)
        body_bbox = (0.45, 0.5, 0.7, 0.9)
        cart_bbox = (0.05, 0.6, 0.35, 0.95)

        # Trajectory: starts near shelf, touches shelf, moves to body torso/pocket, bypasses cart
        wrist_traj = [
            (0.15, 0.25),
            (0.25, 0.30),  # In shelf_roi
            (0.35, 0.40),
            (0.50, 0.60),  # In body_bbox
            (0.55, 0.65),  # In body_bbox
        ]

        res = theft_detection_service.detect_concealment(
            wrist_trajectory=wrist_traj,
            shelf_roi=shelf_roi,
            body_bbox=body_bbox,
            cart_bbox=cart_bbox,
        )
        assert res["detected"] is True
        assert res["theft_type"] == TheftType.CONCEALMENT.value
        assert res["confidence"] >= 0.90

    def test_concealment_negative_deposited_in_cart(self):
        """Test normal shopper motion: wrist touches shelf then deposits in cart."""
        shelf_roi = (0.2, 0.2, 0.4, 0.4)
        body_bbox = (0.45, 0.5, 0.7, 0.9)
        cart_bbox = (0.05, 0.6, 0.35, 0.95)

        # Trajectory: touches shelf then touches cart
        wrist_traj = [
            (0.25, 0.30),  # In shelf_roi
            (0.20, 0.50),
            (0.15, 0.70),  # In cart_bbox
        ]

        res = theft_detection_service.detect_concealment(
            wrist_trajectory=wrist_traj,
            shelf_roi=shelf_roi,
            body_bbox=body_bbox,
            cart_bbox=cart_bbox,
        )
        assert res["detected"] is False
        assert "cart" in res["reason"].lower()

    def test_sweethearting_positive(self):
        """Test cashier scanning bypass: 3 visual passes, only 1 valid barcode scan."""
        t_base = datetime(2026, 9, 5, 14, 0, 0)

        # Visual item passes
        passes = [
            {"id": "p1", "timestamp": t_base + timedelta(seconds=1.0), "item_description": "Milk 2L"},
            {"id": "p2", "timestamp": t_base + timedelta(seconds=5.0), "item_description": "Steak 500g"},
            {"id": "p3", "timestamp": t_base + timedelta(seconds=9.0), "item_description": "Salmon 400g"},
        ]

        # POS transactions recorded (only Milk scanned, Steak and Salmon bypassed)
        scans = [
            {"id": "tx1", "timestamp": t_base + timedelta(seconds=1.2), "sku": "MILK_2L"},
        ]

        res = theft_detection_service.detect_sweethearting(
            pos_transactions=scans,
            cashier_hand_passes=passes,
            tolerance_sec=2.0,
        )
        assert res["detected"] is True
        assert res["theft_type"] == TheftType.SWEETHEARTING.value
        assert res["unmatched_count"] == 2
        assert len(res["unmatched_passes"]) == 2
        assert res["unmatched_passes"][0]["pass_id"] == "p2"

    def test_sweethearting_negative_all_scanned(self):
        """Test cashier scanning compliant: all passes match scans within 1.5s."""
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        passes = [
            {"id": "p1", "timestamp": t_base + timedelta(seconds=2.0)},
            {"id": "p2", "timestamp": t_base + timedelta(seconds=6.0)},
        ]
        scans = [
            {"timestamp": t_base + timedelta(seconds=2.3)},
            {"timestamp": t_base + timedelta(seconds=5.8)},
        ]

        res = theft_detection_service.detect_sweethearting(scans, passes, tolerance_sec=2.0)
        assert res["detected"] is False
        assert res["unmatched_count"] == 0

    def test_pushout_exit_bypass_positive(self):
        """Test pushout: cart crosses exit boundary with 0.0s checkout dwell."""
        exit_polygon = [(0.0, 0.8), (0.3, 0.8), (0.3, 1.0), (0.0, 1.0)]
        track_traj = [
            {"x": 0.5, "y": 0.5, "timestamp": datetime(2026, 9, 5, 14, 0, 0)},
            {"x": 0.3, "y": 0.7, "timestamp": datetime(2026, 9, 5, 14, 0, 10)},
            {"x": 0.1, "y": 0.9, "timestamp": datetime(2026, 9, 5, 14, 0, 20)},  # Inside exit
        ]

        res = theft_detection_service.detect_pushout_exit_bypass(
            track_trajectory=track_traj,
            exit_zone_polygon=exit_polygon,
            checkout_visit_duration=0.0,
        )
        assert res["detected"] is True
        assert res["theft_type"] == TheftType.PUSHOUT_EXIT_BYPASS.value
        assert res["confidence"] >= 0.90

    def test_pushout_exit_bypass_negative_normal_checkout(self):
        """Test normal shopper: cart crosses exit after 45s dwell in checkout."""
        exit_box = (0.0, 0.8, 0.3, 1.0)
        track_traj = [{"x": 0.1, "y": 0.9, "timestamp": datetime.utcnow()}]

        res = theft_detection_service.detect_pushout_exit_bypass(
            track_trajectory=track_traj,
            exit_zone_polygon=exit_box,
            checkout_visit_duration=45.0,
        )
        assert res["detected"] is False


# ============================================================================
# 2. Integration API Tests
# ============================================================================

class TestTheftAPIIntegration:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        self.client = TestClient(app)

    def test_simulate_endpoint_is_gone(self):
        """POST /api/v1/theft/simulate no longer exists: incidents are never invented."""
        res = self.client.post("/api/v1/theft/simulate", json={"theft_type": "SHELF_SWEEPING"})
        assert res.status_code in (404, 405)

    def test_list_theft_incidents_endpoint(self):
        """Test GET /api/v1/theft/incidents with query filters."""
        # Ensure at least one incident exists
        insert_incident(theft_type="CONCEALMENT", department="Cosmetics")

        res = self.client.get("/api/v1/theft/incidents?department=Cosmetics&limit=10")
        assert res.status_code == 200
        data = res.json()
        assert "incidents" in data
        assert "total" in data
        assert data["total"] >= 1
        assert all(inc["department"] == "Cosmetics" for inc in data["incidents"])

    def test_theft_statistics_endpoint(self):
        """Test GET /api/v1/theft/statistics."""
        insert_incident(theft_type="CONCEALMENT", department="Cosmetics")
        res = self.client.get("/api/v1/theft/statistics")
        assert res.status_code == 200
        data = res.json()
        assert "active_incidents_count" in data
        assert "today_incidents_count" in data
        assert "prevented_loss_estimate" in data
        assert "by_department" in data
        assert "by_theft_type" in data
        assert data["active_incidents_count"] >= 1

    def test_theft_lifecycle_acknowledge_dispatch_resolve(self):
        """Test complete incident workflow: insert -> acknowledge -> dispatch -> resolve."""
        # 1. An incident written by a detector (stood in for by a direct insert)
        incident_id = insert_incident(
            theft_type="SWEETHEARTING",
            camera_id="cam_checkout_02",
            department="Front Checkouts",
            estimated_loss_value=75.0,
        )

        # 2. Acknowledge
        ack_res = self.client.post(f"/api/v1/theft/incidents/{incident_id}/acknowledge", json={"guard_id": "guard_mike_104"})
        assert ack_res.status_code == 200
        assert ack_res.json()["status"] == TheftIncidentStatus.ACKNOWLEDGED.value
        assert ack_res.json()["guard_id"] == "guard_mike_104"

        # 3. Dispatch security & audio greeting deterrent
        dsp_res = self.client.post(f"/api/v1/theft/incidents/{incident_id}/dispatch", json={
            "guard_unit": "Mobile Response Unit 2",
            "audio_deterrent": True,
            "announcement_type": "CUSTOMER_ASSISTANCE_GREETING",
        })
        assert dsp_res.status_code == 200
        assert dsp_res.json()["status"] == TheftIncidentStatus.DISPATCHED.value
        assert dsp_res.json()["dispatch_details"]["guard_unit"] == "Mobile Response Unit 2"
        assert dsp_res.json()["dispatch_details"]["audio_deterrent_triggered"] is True

        # 4. Resolve incident
        rsv_res = self.client.post(f"/api/v1/theft/incidents/{incident_id}/resolve", json={
            "resolution": "RECOVERED_GOODS",
            "notes": "Guard approached checkout lane; customer agreed to pay for unscanned items.",
        })
        assert rsv_res.status_code == 200
        assert rsv_res.json()["status"] == TheftIncidentStatus.RESOLVED.value
        assert rsv_res.json()["resolution"] == "RECOVERED_GOODS"
        assert "agreed to pay" in rsv_res.json()["notes"]
