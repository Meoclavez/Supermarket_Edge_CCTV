"""Pose analytics: synthetic keypoint tracks through observe().

The skeletons here are test inputs to the rule engine, never shown to a user.
Each scenario drives ``pose_analytics.observe()`` frame by frame exactly as
the live engine does, then checks what was raised and what was persisted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import Base, ShelfInteractionModel, TheftIncidentModel
from app.services.auth_service import auth_service
from app.services.pose_analytics import pose_analytics
from app.services.shelf_interaction_service import PointCoord, ProductShelfZone, shelf_interaction_service

W, H = 640, 480
DT = 0.2  # 5 analysed frames per second

# Product zone to the right of the shopper, in normalised image coordinates:
# pixels x 448..608, y 96..240.
ZONE_POINTS = [(0.70, 0.20), (0.95, 0.20), (0.95, 0.50), (0.70, 0.50)]

AT_REST = (345.0, 300.0)       # hand hanging by the hip (inside the rest box)
IN_SHELF = (520.0, 170.0)      # inside the product zone
MID_AIR = (420.0, 120.0)       # beside the head: neither shelf nor body
POCKET = (330.0, 250.0)        # waistband / pocket band


def skeleton(right_wrist=AT_REST, vis=0.9, head_side=0):
    kps = np.zeros((17, 3), dtype=np.float32)
    nose_x = 320.0 + head_side * 9.0
    kps[0] = (nose_x, 110, vis)                     # nose
    kps[1] = (315, 105, vis); kps[2] = (325, 105, vis)
    kps[3] = (310, 112, vis); kps[4] = (330, 112, vis)   # ears
    kps[5] = (300, 150, vis); kps[6] = (340, 150, vis)   # shoulders
    kps[7] = (295, 205, vis); kps[8] = (345, 205, vis)   # elbows
    kps[9] = (295, 300, vis)                             # left wrist at rest
    kps[10] = (right_wrist[0], right_wrist[1], vis)      # right wrist
    kps[11] = (305, 260, vis); kps[12] = (335, 260, vis) # hips
    kps[13] = (305, 350, vis); kps[14] = (335, 350, vis)
    kps[15] = (305, 430, vis); kps[16] = (335, 430, vis)
    return kps


def track(tid, kps, floor=None):
    return SimpleNamespace(track_id=tid, bbox=(280.0, 90.0, 360.0, 440.0), keypoints=kps,
                           confirmed=True, floor_xy=floor)


FRAME = np.full((H, W, 3), 90, dtype=np.uint8)


def _auth_headers() -> dict:
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


class Driver:
    def __init__(self, cam, tid, t0=1000.0):
        self.cam, self.tid, self.t = cam, tid, t0
        self.interactions = []
        self.incidents = []

    def step(self, wrist, n=1, floor=None, head_side=0):
        for _ in range(n):
            res = pose_analytics.observe(self.cam, self.t, FRAME, [track(self.tid, skeleton(wrist, head_side=head_side), floor)], [])
            self.interactions.extend(res.interactions)
            self.incidents.extend(res.incidents)
            self.t += DT
        return self


@pytest.fixture(scope="module", autouse=True)
def schema():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    yield


@pytest.fixture
def zone():
    def make(cam, category="Snacks", price=4.5, zid=None):
        z = ProductShelfZone(
            id=zid or f"pz_{cam}", camera_id=cam, name=f"Shelf {cam}", sku_id="SKU-T",
            category=category, price=price,
            points=[PointCoord(x=x, y=y) for x, y in ZONE_POINTS],
        )
        shelf_interaction_service.save_zone(z)
        made.append(z.id)
        return z
    made = []
    yield make
    for zid in made:
        shelf_interaction_service.delete_zone(zid)
    pose_analytics.set_floor_zones(None)
    pose_analytics.reset_all()
    pose_analytics.flush_now()


def _rows(model, **where):
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        stmt = select(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        rows = list(s.execute(stmt).scalars().all())
    eng.dispose()
    return rows


def test_reach_is_returned_and_persisted(zone):
    zone("cam_pa_reach")
    d = Driver("cam_pa_reach", "trk_reach")
    d.step(AT_REST, 2).step(IN_SHELF, 4).step(AT_REST, 4)

    assert d.interactions == [("trk_reach", "pz_cam_pa_reach")]
    pose_analytics.flush_now()
    rows = _rows(ShelfInteractionModel, camera_id="cam_pa_reach")
    assert len(rows) == 1
    r = rows[0]
    assert r.shelf_zone_id == "pz_cam_pa_reach"
    assert r.hand == "right"
    assert r.person_track_id == "trk_reach"
    assert r.started_at is not None and r.ended_at is not None and r.ended_at >= r.started_at
    assert r.duration_sec == pytest.approx(3 * DT, abs=1e-3)   # 4 frames in the zone
    assert r.confidence == pytest.approx(0.9, abs=1e-3)          # measured wrist visibility
    assert 0.7 <= r.image_x <= 0.95 and 0.2 <= r.image_y <= 0.5
    # A reach that went back to rest raises nothing.
    assert d.incidents == []


def test_wrist_at_rest_over_zone_is_not_a_reach(zone):
    """A zone drawn over the shelf behind a shopper must not count a hanging hand."""
    z = ProductShelfZone(id="pz_behind", camera_id="cam_pa_rest", name="Behind", sku_id="S", category="Snacks",
                         points=[PointCoord(x=0.4, y=0.5), PointCoord(x=0.7, y=0.5),
                                 PointCoord(x=0.7, y=0.8), PointCoord(x=0.4, y=0.8)])
    shelf_interaction_service.save_zone(z)
    try:
        d = Driver("cam_pa_rest", "trk_rest")
        d.step(AT_REST, 10)   # (345,300) is inside that polygon but in the body's rest box
        assert d.interactions == []
    finally:
        shelf_interaction_service.delete_zone("pz_behind")


def test_reach_then_concealment_creates_incident(zone):
    zone("cam_pa_conceal", category="Cosmetics", price=12.0)
    d = Driver("cam_pa_conceal", "trk_conceal")
    d.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 6).step(AT_REST, 10)

    assert len(d.interactions) == 1
    rules_fired = [i["rule"] for i in d.incidents]
    assert rules_fired == ["CONCEALMENT"]
    inc = d.incidents[0]
    assert 0.3 <= inc["confidence"] <= 1.0
    assert inc["confidence"] != 0.92                          # not the old constant
    assert any("waistband/pocket" in e for e in inc["evidence"])
    assert inc["estimated_loss_value"] == 12.0

    pose_analytics.flush_now()
    rows = _rows(TheftIncidentModel, camera_id="cam_pa_conceal")
    assert len(rows) == 1
    row = rows[0]
    assert row.rule == "CONCEALMENT" and row.theft_type == "CONCEALMENT"
    assert row.evidence and isinstance(row.evidence, list)
    assert "staff review" in row.evidence_summary
    assert row.snapshot_path and Path(row.snapshot_path).is_file()
    assert Path(row.snapshot_path).read_bytes()[:2] == b"\xff\xd8"   # JPEG
    assert row.evidence_snapshot_url == f"/api/v1/theft/incidents/{row.id}/evidence"


def test_reach_then_return_to_shelf_is_not_concealment(zone):
    zone("cam_pa_return")
    d = Driver("cam_pa_return", "trk_return")
    d.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 4).step(IN_SHELF, 4).step(AT_REST, 12)

    assert [i[1] for i in d.interactions] == ["pz_cam_pa_return", "pz_cam_pa_return"]
    assert d.incidents == []
    pose_analytics.flush_now()
    assert _rows(TheftIncidentModel, camera_id="cam_pa_return") == []


def test_low_visibility_wrist_does_not_count(zone):
    zone("cam_pa_vis")
    d = Driver("cam_pa_vis", "trk_vis")
    for _ in range(6):
        pose_analytics.observe("cam_pa_vis", d.t, FRAME, [track("trk_vis", skeleton(IN_SHELF, vis=0.2))], [])
        d.t += DT
    assert pose_analytics._cameras["cam_pa_vis"].tracks["trk_vis"].interaction_count == 0


def test_shelf_sweeping_triggers(zone):
    zone("cam_pa_sweep", price=3.0)
    d = Driver("cam_pa_sweep", "trk_sweep")
    for _ in range(5):
        d.step(IN_SHELF, 2).step(MID_AIR, 2)
    assert len(d.interactions) == 5
    rules_fired = [i["rule"] for i in d.incidents]
    assert rules_fired == ["SHELF_SWEEPING"]           # once, then cooldown
    inc = d.incidents[0]
    assert inc["confidence"] >= settings.THEFT_MIN_CONFIDENCE
    assert any("separate reaches" in e for e in inc["evidence"])


def _floor_zones():
    return [
        {"id": "fz_aisle", "category": "AISLE", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10}]},
        {"id": "fz_checkout", "category": "CHECKOUT", "polygon": [{"x": 10, "y": 0}, {"x": 12, "y": 0}, {"x": 12, "y": 10}, {"x": 10, "y": 10}]},
        {"id": "fz_exit", "category": "EXIT", "polygon": [{"x": 12, "y": 0}, {"x": 14, "y": 0}, {"x": 14, "y": 10}, {"x": 12, "y": 10}]},
    ]


def test_exit_without_checkout_triggers(zone):
    zone("cam_pa_exit")
    pose_analytics.set_floor_zones(_floor_zones())
    d = Driver("cam_pa_exit", "trk_exit")
    d.step(AT_REST, 2, floor=(5, 5)).step(IN_SHELF, 3, floor=(5, 5)).step(AT_REST, 3, floor=(5, 5))
    d.step(AT_REST, 3, floor=(13, 5))
    assert [i["rule"] for i in d.incidents] == ["EXIT_WITHOUT_CHECKOUT"]
    ev = d.incidents[0]["evidence"]
    assert any("Single-camera track" in e for e in ev)


def test_exit_after_checkout_is_normal(zone):
    zone("cam_pa_paid")
    pose_analytics.set_floor_zones(_floor_zones())
    d = Driver("cam_pa_paid", "trk_paid")
    d.step(AT_REST, 2, floor=(5, 5)).step(IN_SHELF, 3, floor=(5, 5)).step(AT_REST, 3, floor=(5, 5))
    d.step(AT_REST, 3, floor=(11, 5)).step(AT_REST, 3, floor=(13, 5))
    assert d.incidents == []


def test_exit_without_interaction_is_normal(zone):
    zone("cam_pa_browse")
    pose_analytics.set_floor_zones(_floor_zones())
    d = Driver("cam_pa_browse", "trk_browse")
    d.step(AT_REST, 5, floor=(5, 5)).step(AT_REST, 3, floor=(13, 5))
    assert d.incidents == []


def test_loitering_needs_dwell_reaches_and_head_turns(zone, monkeypatch):
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_DWELL_SEC", 6.0)
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_HEAD_TURNS", 4)
    zone("cam_pa_loiter", category="Alcohol", price=40.0)
    d = Driver("cam_pa_loiter", "trk_loiter")
    d.step(IN_SHELF, 2).step(AT_REST, 3).step(IN_SHELF, 2).step(AT_REST, 3)
    for i in range(14):   # glance left/right repeatedly
        d.step(AT_REST, 2, head_side=(1 if i % 2 == 0 else -1))
    rules_fired = [i["rule"] for i in d.incidents]
    assert "SUSPICIOUS_LOITERING" in rules_fired


def test_no_loitering_without_head_turns(zone, monkeypatch):
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_DWELL_SEC", 6.0)
    zone("cam_pa_calm", category="Alcohol", price=40.0)
    d = Driver("cam_pa_calm", "trk_calm")
    d.step(IN_SHELF, 2).step(AT_REST, 3).step(IN_SHELF, 2).step(AT_REST, 30)
    assert "SUSPICIOUS_LOITERING" not in [i["rule"] for i in d.incidents]


def test_reset_camera_persists_open_reach(zone):
    zone("cam_pa_reset")
    d = Driver("cam_pa_reset", "trk_reset")
    d.step(IN_SHELF, 3)
    pose_analytics.reset_camera("cam_pa_reset")
    pose_analytics.flush_now()
    assert len(_rows(ShelfInteractionModel, camera_id="cam_pa_reset")) == 1
    assert "cam_pa_reset" not in pose_analytics._cameras


def test_incident_api_serves_rule_evidence_and_image(zone):
    zone("cam_pa_api", category="Cosmetics", price=9.0)
    d = Driver("cam_pa_api", "trk_api")
    d.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 6).step(AT_REST, 10)
    assert [i["rule"] for i in d.incidents] == ["CONCEALMENT"]
    pose_analytics.flush_now()

    with TestClient(app, headers=_auth_headers()) as client:
        res = client.get("/api/v1/theft/incidents?rule=CONCEALMENT&limit=200")
        assert res.status_code == 200
        incs = [i for i in res.json()["incidents"] if i["camera_id"] == "cam_pa_api"]
        assert len(incs) == 1
        inc = incs[0]
        assert inc["rule"] == "CONCEALMENT"
        assert inc["evidence"] and inc["review_label"].startswith("Suspicious behaviour")
        assert inc["snapshot_url"] == inc["evidence_snapshot_url"] == f"/api/v1/theft/incidents/{inc['id']}/evidence"
        img = client.get(inc["snapshot_url"])
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/jpeg"
        assert img.content[:2] == b"\xff\xd8"
        one = client.get(f"/api/v1/theft/incidents/{inc['id']}")
        assert one.status_code == 200 and one.json()["rule"] == "CONCEALMENT"
        assert client.get("/api/v1/theft/incidents/nope/evidence").status_code == 404


def test_heatmap_interaction_kind_api():
    """kind=interaction bins shelf interactions at the shopper's floor position."""
    from datetime import datetime

    ids = [f"si_hm_{i}" for i in range(4)]
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    # Stored observation times are naive UTC (app/services/timeutil.py).
    now = datetime.utcnow()
    with Session(eng) as s:
        for i, (fx, fy) in enumerate([(5.2, 5.1), (5.3, 5.2), (20.0, 12.0), (None, None)]):
            s.add(ShelfInteractionModel(
                id=ids[i], camera_id="cam_hm", shelf_zone_id="pz_hm", timestamp=now,
                person_track_id=f"t{i}", action_type="REACH", duration_sec=0.8,
                hand="right", started_at=now, ended_at=now, confidence=0.9,
                floor_x=fx, floor_y=fy,
            ))
        s.commit()
    try:
        with TestClient(app, headers=_auth_headers()) as client:
            res = client.get("/api/v1/analytics/heatmaps?kind=interaction&resolution_w=50&resolution_h=30")
            assert res.status_code == 200
            hm = res.json()
            assert hm["kind"] == "interaction" and hm["unit"] == "interactions"
            assert hm["observed"] is True
            assert hm["samples"] >= 3
            assert hm["unplaced_interactions"] >= 1         # uncalibrated camera: not placed
            m = hm["density_matrix"]
            assert len(m) == 30 and len(m[0]) == 50
            assert max(max(r) for r in m) == 1.0
            # (5.2,5.1) and (5.3,5.2) share the 1 m cell (5,5) on a 50x30 m layout.
            assert m[5][5] == 1.0

            for kind in ("presence", "dwell"):
                r = client.get(f"/api/v1/analytics/heatmaps?kind={kind}")
                assert r.status_code == 200 and r.json()["kind"] == kind
            assert client.get("/api/v1/analytics/heatmaps?kind=bogus").status_code == 422

            lst = client.get("/api/v1/analytics/products/interactions?camera_id=cam_hm")
            assert lst.status_code == 200 and lst.json()["total"] == 4
    finally:
        with Session(eng) as s:
            for i in ids:
                row = s.get(ShelfInteractionModel, i)
                if row is not None:
                    s.delete(row)
            s.commit()
        eng.dispose()


def test_feature_flags_gate_interactions_and_incidents(zone):
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    zone("cam_pa_flags", category="Cosmetics", price=5.0)
    feature_manager.set_camera_features("cam_pa_flags", CameraFeatureConfig(shelf_interaction=False, theft_detection=False))
    try:
        d = Driver("cam_pa_flags", "trk_flags")
        d.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 6).step(AT_REST, 12)
        assert d.interactions == [] and d.incidents == []

        # Theft on, interactions off: the rule still sees the reach, but no
        # interaction is reported or persisted.
        feature_manager.set_camera_features("cam_pa_flags", CameraFeatureConfig(shelf_interaction=False, theft_detection=True))
        d2 = Driver("cam_pa_flags", "trk_flags2", t0=5000.0)
        d2.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 6).step(AT_REST, 12)
        assert d2.interactions == []
        assert [i["rule"] for i in d2.incidents] == ["CONCEALMENT"]
        pose_analytics.flush_now()
        assert _rows(ShelfInteractionModel, camera_id="cam_pa_flags") == []
    finally:
        feature_manager.remove_camera("cam_pa_flags")


def test_interaction_track_ids_are_strings(zone):
    zone("cam_pa_ids")
    d = Driver("cam_pa_ids", 42)
    d.step(AT_REST, 1).step(IN_SHELF, 3)
    assert d.interactions == [("42", "pz_cam_pa_ids")]
