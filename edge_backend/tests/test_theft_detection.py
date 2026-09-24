"""Loss-prevention rules (pure functions over evidence) and the incident API."""

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
from app.services.auth_service import auth_service
from app.services.theft_detection_service import (
    detect_concealment,
    detect_exit_without_checkout,
    detect_shelf_sweeping,
    detect_suspicious_loitering,
    detect_sweethearting,
    estimate_head_yaw,
    evidence_confidence,
    theft_detection_service,
)


def _auth_headers() -> dict:
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


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
    """The rules are pure functions over observed evidence; confidence is derived, never constant."""

    @staticmethod
    def _conceal_samples(hold=4, returned=False, after=12, dt=0.2, vis=0.9, target="pocket_band"):
        t = 0.0
        s = [{"t": t, "in_shelf": False, "in_conceal": False, "vis": vis, "reach_vis": vis}]
        for _ in range(hold):
            t += dt
            s.append({"t": t, "in_shelf": False, "in_conceal": True, "vis": vis, "target": target})
        for i in range(after):
            t += dt
            s.append({"t": t, "in_shelf": returned and i == 2, "in_conceal": False, "vis": vis})
        return s

    def test_concealment_positive(self):
        res = detect_concealment(self._conceal_samples(), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert res["detected"] is True
        assert res["rule"] == "CONCEALMENT"
        assert 0.0 < res["confidence"] <= 1.0
        assert res["no_return_verified"] is True
        assert len(res["evidence"]) >= 3

    def test_concealment_confidence_tracks_visibility(self):
        hi = detect_concealment(self._conceal_samples(vis=0.95), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        lo = detect_concealment(self._conceal_samples(vis=0.4), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert hi["detected"] and lo["detected"]
        assert hi["confidence"] > lo["confidence"]

    def test_concealment_into_bag_is_reported(self):
        res = detect_concealment(self._conceal_samples(target="bag"), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert res["detected"] and res["target"] == "bag"
        assert any("bag" in e for e in res["evidence"])

    def test_concealment_negative_returned_to_shelf(self):
        res = detect_concealment(self._conceal_samples(returned=True), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert res["detected"] is False
        assert res["state"] == "returned"

    def test_concealment_waits_for_no_return_window(self):
        res = detect_concealment(self._conceal_samples(after=3), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert res["detected"] is False and res["state"] == "held"
        ended = detect_concealment(self._conceal_samples(after=3), window_sec=4.0, min_hold_frames=3,
                                   no_return_sec=2.0, track_ended=True)
        assert ended["detected"] is True and ended["no_return_verified"] is False

    def test_concealment_negative_brief_touch(self):
        res = detect_concealment(self._conceal_samples(hold=2), window_sec=4.0, min_hold_frames=3, no_return_sec=2.0)
        assert res["detected"] is False

    def test_shelf_sweeping_positive(self):
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        reaches = [{"timestamp": t_base + timedelta(seconds=s), "zone_id": "liquor_a", "vis": 0.9}
                   for s in (0.0, 1.0, 2.2, 3.5)]
        res = detect_shelf_sweeping(reaches, window_sec=5.0, min_reaches=3)
        assert res["detected"] is True
        assert res["rule"] == "SHELF_SWEEPING"
        assert res["count"] == 4
        assert res["span_sec"] == pytest.approx(3.5)
        assert 0.0 < res["confidence"] <= 1.0

    def test_shelf_sweeping_negative_slow_picking(self):
        t_base = datetime(2026, 9, 5, 14, 0, 0)
        reaches = [{"timestamp": t_base + timedelta(seconds=s), "zone_id": "z", "vis": 0.9} for s in (0, 10, 20)]
        assert detect_shelf_sweeping(reaches, window_sec=5.0, min_reaches=3)["detected"] is False

    def test_shelf_sweeping_counts_per_zone(self):
        reaches = [{"timestamp": float(i), "zone_id": ("a" if i % 2 else "b"), "vis": 0.9} for i in range(4)]
        assert detect_shelf_sweeping(reaches, window_sec=5.0, min_reaches=3)["detected"] is False

    def test_head_yaw_proxy(self):
        kps = [[0, 0, 0]] * 17
        kps = [list(k) for k in kps]
        kps[0] = [100, 50, 0.9]; kps[3] = [90, 52, 0.9]; kps[4] = [110, 52, 0.9]
        assert abs(estimate_head_yaw(kps, 0.5)) < 0.05
        kps[0] = [108, 50, 0.9]
        assert estimate_head_yaw(kps, 0.5) > 0.5
        kps[4] = [110, 52, 0.1]   # right ear hidden: profile
        assert estimate_head_yaw(kps, 0.5) == 1.0
        kps[0] = [100, 50, 0.1]
        assert estimate_head_yaw(kps, 0.5) is None

    def test_loitering_requires_all_signals(self):
        base = dict(dwell_sec=90, reaches=3, head_turns=8, head_samples=200, visibility=0.8,
                    min_dwell_sec=60, min_reaches=2, min_head_turns=6)
        assert detect_suspicious_loitering(**base)["detected"] is True
        assert detect_suspicious_loitering(**{**base, "head_turns": 2})["detected"] is False
        assert detect_suspicious_loitering(**{**base, "reaches": 0})["detected"] is False
        assert detect_suspicious_loitering(**{**base, "dwell_sec": 30})["detected"] is False

    def test_exit_without_checkout(self):
        seq = [{"t": 0, "zone_id": "a", "category": "AISLE"}, {"t": 20, "zone_id": "x", "category": "EXIT"}]
        res = detect_exit_without_checkout(seq, first_interaction_t=5.0, interaction_count=2,
                                           interaction_vis=0.9, floor_coverage=1.0)
        assert res["detected"] is True and res["rule"] == "EXIT_WITHOUT_CHECKOUT"
        assert any("Single-camera" in e for e in res["evidence"])

    def test_exit_after_checkout_is_normal(self):
        seq = [{"t": 10, "zone_id": "c", "category": "CHECKOUT"}, {"t": 20, "zone_id": "x", "category": "ENTRANCE"}]
        res = detect_exit_without_checkout(seq, first_interaction_t=5.0, interaction_count=2,
                                           interaction_vis=0.9, floor_coverage=1.0)
        assert res["detected"] is False

    def test_exit_without_interaction_is_normal(self):
        seq = [{"t": 20, "zone_id": "x", "category": "EXIT"}]
        assert detect_exit_without_checkout(seq, first_interaction_t=None, interaction_count=0,
                                            interaction_vis=0.0, floor_coverage=1.0)["detected"] is False

    def test_sweethearting_positive(self):
        t_base = datetime(2026, 9, 5, 15, 0, 0)
        scans = [{"timestamp": t_base, "sku": "A"}, {"timestamp": t_base + timedelta(seconds=3), "sku": "B"}]
        passes = [
            {"id": "p1", "timestamp": t_base + timedelta(seconds=0.5), "vis": 0.9},
            {"id": "p2", "timestamp": t_base + timedelta(seconds=3.2), "vis": 0.9},
            {"id": "p3", "timestamp": t_base + timedelta(seconds=7.0), "vis": 0.9},
        ]
        res = detect_sweethearting(scans, passes, tolerance_sec=2.0)
        assert res["detected"] is True and res["evaluable"] is True
        assert res["unmatched_count"] == 1
        assert res["unmatched_passes"][0]["pass_id"] == "p3"
        assert 0.0 < res["confidence"] < 1.0

    def test_sweethearting_negative_all_scanned(self):
        t_base = datetime(2026, 9, 5, 15, 0, 0)
        scans = [{"timestamp": t_base}, {"timestamp": t_base + timedelta(seconds=2)}]
        passes = [{"timestamp": t_base + timedelta(seconds=0.2)}, {"timestamp": t_base + timedelta(seconds=2.1)}]
        res = detect_sweethearting(scans, passes, tolerance_sec=2.0)
        assert res["detected"] is False and res["unmatched_passes"] == []

    def test_sweethearting_not_evaluable_without_pos(self):
        res = detect_sweethearting([], [{"timestamp": 1.0}], tolerance_sec=2.0)
        assert res["detected"] is False and res["evaluable"] is False

    def test_evidence_confidence_is_derived(self):
        assert evidence_confidence(0.0, [1.0, 1.0]) == 0.0
        assert evidence_confidence(1.0, [0.0]) == 0.0
        assert evidence_confidence(0.8, [0.5, 1.0]) == pytest.approx(0.6)

# ============================================================================
# 2. Integration API Tests
# ============================================================================

class TestTheftAPIIntegration:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        self.client = TestClient(app, headers=_auth_headers())

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
        inc = data["incidents"][0]
        # Every incident carries its rule, evidence and review framing.
        assert inc["rule"] == "CONCEALMENT"
        assert isinstance(inc["evidence"], list)
        assert inc["review_label"] == "Suspicious behaviour for staff review"
        # No evidence image was recorded for a directly inserted row.
        assert inc["snapshot_url"] is None and inc["evidence_snapshot_url"] is None

    def test_evidence_endpoint_requires_auth(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "AUTH_DISABLED", False, raising=False)
        monkeypatch.setattr(settings, "DEBUG", False)
        incident_id = insert_incident(theft_type="CONCEALMENT", rule="CONCEALMENT", evidence=["e1"])
        anon = TestClient(app)
        assert anon.get(f"/api/v1/theft/incidents/{incident_id}/evidence").status_code == 401
        assert anon.get("/api/v1/theft/incidents").status_code == 401
        # Same credentials the camera snapshot endpoints accept: bearer header or ?token=
        token = _auth_headers()["Authorization"].split(" ", 1)[1]
        res = anon.get(f"/api/v1/theft/incidents/{incident_id}/evidence?token={token}")
        assert res.status_code == 404   # authorised, but no image recorded
        res = self.client.get(f"/api/v1/theft/incidents/{incident_id}/evidence")
        assert res.status_code == 404

    def test_evidence_endpoint_refuses_paths_outside_evidence_dir(self, tmp_path):
        outside = tmp_path / "secret.jpg"
        outside.write_bytes(b"\xff\xd8not really")
        incident_id = insert_incident(snapshot_path=str(outside))
        res = self.client.get(f"/api/v1/theft/incidents/{incident_id}/evidence")
        assert res.status_code == 404

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
