"""Theft alert tiers end to end.

Synthetic keypoints (test inputs, never shown to a user) go through
``pose_analytics.observe()`` frame by frame as the live engine sends them:
a reach into a product area, the hand to the pocket band and held there,
at standard stock, at spirits and seen less clearly. Then: the ``theft_incidents`` row carries the tier, risk score, risk
factors and an evidence JPEG; the notification reaches ``alert_dispatcher``
with that tier's channels (phone and web push replaced by recorders); the
incident API lists it with the tier; ``/live/tracks`` shows the track at level
"alert" with the same tier.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import Base, TheftIncidentModel
from app.services import camera_roles as cr
from app.services import live_analytics_engine as lae
from app.services import push_alerts as pa_mod
from app.services import site_settings as ss
from app.services import theft_alert_policy as tp
from app.services.alert_dispatcher import alert_dispatcher
from app.services.live_analytics_engine import CameraRuntime
from app.services.pose_analytics import pose_analytics
from app.services.shelf_interaction_service import PointCoord, ProductShelfZone, shelf_interaction_service

from tests.test_pose_analytics import (AT_REST, DT, FRAME, IN_SHELF, MID_AIR, POCKET, ZONE_POINTS, H, W, Driver,
                                      skeleton, track)

CAM = "cam_tier_e2e"
TRACK = "trk_tier_e2e"


@pytest.fixture
def loop_thread():
    """The application event loop the pose writer schedules notifications on."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="e2e-loop", daemon=True)
    t.start()
    pose_analytics.bind_loop(loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()
    pose_analytics._loop = None


@pytest.fixture
def scene(monkeypatch):
    for spec in ss.SPECS:
        if spec.group == "theft_alerts":
            monkeypatch.setattr(settings, spec.key, ss.ENV_DEFAULTS[spec.key])
    monkeypatch.setattr(settings, "THEFT_HIGH_VALUE_CATEGORIES", "SPIRITS,WINE")
    monkeypatch.setattr(settings, "THEFT_HIGH_VALUE_MIN_PRICE", 0.0)
    monkeypatch.setattr(settings, "CAMERA_ALERT_COOLDOWN_SEC", 0)
    cr.role_cache.set_override({CAM: "aisle"})
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO cameras (id, name, location, rtsp_url, status, fps, resolution, is_ai_enabled, "
            "ai_models, dvr_enabled, dvr_retention_days, dvr_quota_gb, created_at, channel_number, department, "
            "floor_x, floor_y, floor_z, azimuth_deg, fov_deg) VALUES (?, 'Spirits aisle', 'Aisle 7', 'rtsp://x', "
            "'OFFLINE', 0, 'unknown', 1, '[]', 0, 7, 1.0, ?, 1, 'LIQUOR', 0, 0, 3, 0, 90)",
            (CAM, datetime.utcnow().isoformat(sep=" ")))
        conn.exec_driver_sql("UPDATE cameras SET muted_until = NULL WHERE id = ?", (CAM,))
    eng.dispose()

    # Phone channels replaced by recorders; the dispatcher itself is real.
    calls = {"push": [], "web": []}

    async def fake_push(title, body, data, camera, bypass_cooldown=False):
        calls["push"].append({"title": title, "data": dict(data), "bypass_cooldown": bypass_cooldown})
        return {"provider": "fcm_v1", "sent": 1, "skipped": None}

    async def fake_web(title, body, data, camera, bypass_cooldown=False):
        calls["web"].append({"title": title, "data": dict(data)})
        return {"provider": "web_push", "pushed": True}

    dispatched = []
    real_dispatch = alert_dispatcher.dispatch

    async def spy_dispatch(event_type, severity, title, body, data=None, **kw):
        report = await real_dispatch(event_type, severity, title, body, data, **kw)
        dispatched.append({"event_type": event_type, "severity": severity, "title": title, "data": dict(data or {}),
                           "report": report})
        return report

    monkeypatch.setattr(alert_dispatcher, "_push", fake_push)
    monkeypatch.setattr(pa_mod.push_alerts, "on_alert", fake_web)
    monkeypatch.setattr(alert_dispatcher, "dispatch", spy_dispatch)
    yield {"calls": calls, "dispatched": dispatched}
    shelf_interaction_service.delete_zone("pz_tier_e2e")
    cr.role_cache.set_override(None)
    pose_analytics.reset_camera(CAM)
    pose_analytics.flush_now()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class VisDriver(Driver):
    """Driver with a chosen keypoint visibility (how clearly the camera sees the person)."""

    def __init__(self, cam, tid, t0=1000.0, vis=0.9):
        super().__init__(cam, tid, t0)
        self.vis = vis

    def step(self, wrist, n=1, floor=None, head_side=0):
        for _ in range(n):
            res = pose_analytics.observe(self.cam, self.t, FRAME,
                                         [track(self.tid, skeleton(wrist, vis=self.vis, head_side=head_side), floor)])
            self.interactions.extend(res.interactions)
            self.incidents.extend(res.incidents)
            self.t += DT
        return self


def _conceal(tid, *, t0=1000.0, vis=0.9):
    d = VisDriver(CAM, tid, t0=t0, vis=vis)
    d.step(AT_REST, 2).step(IN_SHELF, 4).step(MID_AIR, 1).step(POCKET, 6).step(AT_REST, 10)
    assert [i["rule"] for i in d.incidents] == ["CONCEALMENT"]
    return d.incidents[0]


def _set_zone(category, price):
    shelf_interaction_service.save_zone(ProductShelfZone(
        id="pz_tier_e2e", camera_id=CAM, name="Shelf E2E", sku_id="SKU-E2E", category=category, price=price,
        points=[PointCoord(x=x, y=y) for x, y in ZONE_POINTS]))


def test_concealment_alert_end_to_end(scene, loop_thread, monkeypatch):
    """A clearly seen pocket concealment at standard stock: Alert, pushed, not escalated."""
    _set_zone("Snacks", 3.0)
    # 1. observe(): reach into the shelf, hand to the pocket band, held, not returned.
    raised = _conceal(TRACK)
    conf = raised["confidence"]
    expected = tp.assess("CONCEALMENT", conf, target="pocket",
                         zone={"id": "pz_tier_e2e", "name": "Shelf E2E", "category": "SNACKS", "price": 3.0,
                               "high_value": False, "value_tier": None}, role="aisle")
    # Keypoints seen at visibility 0.9 -> confidence ~0.76; x1.0 (standard
    # stock) x1.0 (pocket) -> Alert (0.55..0.80).
    assert 0.55 <= conf < 0.8, conf
    assert raised["alert_tier"] == expected["alert_tier"] == "alert"
    assert raised["risk_score"] == pytest.approx(conf, abs=1e-3) == expected["risk_score"]
    assert raised["severity"] == "HIGH"
    factors = {f["factor"]: f for f in raised["risk_factors"]}
    assert factors["case"]["value"] == 1.0 and "standard-value" in factors["place_standard_value"]["reason"]

    # 2. The row: tier, score, factors, evidence JPEG.
    assert pose_analytics.flush_now() >= 1
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        row = s.execute(select(TheftIncidentModel).where(TheftIncidentModel.id == raised["id"])).scalar_one()
        assert row.alert_tier == "alert" and row.severity == "HIGH"
        assert row.risk_score == pytest.approx(raised["risk_score"])
        assert [f["factor"] for f in row.risk_factors] == [f["factor"] for f in raised["risk_factors"]]
        assert row.snapshot_path and Path(row.snapshot_path).is_file()
        assert Path(row.snapshot_path).read_bytes()[:2] == b"\xff\xd8"
    eng.dispose()

    # 3. The dispatcher got the tier and its channels; phones were pushed, no escalation.
    assert _wait(lambda: scene["dispatched"]), "notification never reached the dispatcher"
    sent = scene["dispatched"][0]
    assert sent["data"]["incident_id"] == raised["id"] and sent["data"]["alert_tier"] == "alert"
    assert sent["severity"] == "HIGH" and sent["title"].startswith("Alert: Possible concealment")
    assert sent["data"]["channels"] == tp.channels("alert")
    assert sent["data"]["channels"]["push"] is True and sent["data"]["channels"]["escalate"] is False
    assert sent["report"]["logged"] is True and sent["report"]["alert_tier"] == "alert"
    assert len(scene["calls"]["push"]) == 1 and scene["calls"]["push"][0]["bypass_cooldown"] is False
    assert scene["calls"]["web"][0]["data"]["channels"]["escalate"] is False

    # 4. The incident API lists it under its tier.
    with TestClient(app) as client:
        res = client.get("/api/v1/theft/incidents?tier=alert&limit=200")
        assert res.status_code == 200, res.text
        mine = [i for i in res.json()["incidents"] if i["id"] == raised["id"]]
        assert len(mine) == 1
        inc = mine[0]
        assert inc["alert_tier"] == "alert" and inc["alert_tier_label"] == "Alert"
        assert inc["risk_score"] == pytest.approx(raised["risk_score"])
        assert inc["alert_channels"]["push"] is True and inc["snapshot_url"]
        assert inc["risk_factors"] and inc["risk_factors"][0]["factor"] == "confidence"
        assert not [i for i in client.get("/api/v1/theft/incidents?tier=review,watch&limit=200").json()["incidents"]
                    if i["id"] == raised["id"]]

        # 5. /live/tracks: the person is at level "alert" with the incident's tier.
        rt = CameraRuntime(camera_id=CAM, name="E2E aisle", source="/dev/null")
        rt.analysis_flags = {"people_counting": True, "shelf_interaction": True, "theft_detection": True}
        now = pose_analytics._cameras[CAM].last_ts
        kps = [[float(x), float(y), float(v)] for x, y, v in pose_analytics._cameras[CAM].tracks[TRACK].keypoints]
        rt.set_tracks([{
            "track_id": TRACK, "x1": 280.0, "y1": 90.0, "x2": 360.0, "y2": 440.0, "confidence": 0.9, "hits": 20,
            "confirmed": True, "motion_state": "moving", "pending_static": False, "age_seconds": 5.0,
            "keypoints": kps, "fresh": True, "last_seen": now, "pred_box": None, "kp_last": None,
            "x_m": None, "y_m": None, "zone_id": None,
        }], now, frame_size=(W, H))
        monkeypatch.setitem(lae.live_engine.runtimes, CAM, rt)
        monkeypatch.setitem(lae._pose_state, "module", pose_analytics)
        monkeypatch.setitem(lae._pose_state, "status", "ok")
        lae._behaviour_cache.clear()
        live = client.get(f"/api/v1/live/tracks?cameras={CAM}")
        assert live.status_code == 200, live.text
        cam = live.json()["cameras"][CAM]
        beh = next(t for t in cam["tracks"] if str(t["track_id"]) == TRACK)["behaviour"]
        assert beh["level"] == "alert" and beh["tier"] == "alert"
        assert beh["incident_id"] == raised["id"] and beh["incident_rule"] == "CONCEALMENT"
        assert beh["risk_score"] == pytest.approx(raised["risk_score"])
    lae._behaviour_cache.clear()


def test_same_concealment_at_spirits_is_critical_and_escalates(scene, loop_thread, monkeypatch):
    """High-value stock (x1.25) lifts the same evidence to Critical: pushed past the cooldown, escalated."""
    _set_zone("Spirits", 45.0)
    raised = _conceal(TRACK + "_hv", t0=3000.0)
    assert raised["alert_tier"] == "critical" and raised["severity"] == "HIGH"
    assert raised["risk_score"] == pytest.approx(min(1.0, raised["confidence"] * 1.25), abs=1e-3)
    assert "SPIRITS" in {f["factor"]: f for f in raised["risk_factors"]}["place_high_value"]["reason"]
    pose_analytics.flush_now()
    assert _wait(lambda: scene["dispatched"])
    sent = scene["dispatched"][0]
    assert sent["title"].startswith("Urgent: Possible concealment")
    assert sent["data"]["channels"]["escalate"] is True and sent["data"]["channels"]["repeat"] is True
    assert scene["calls"]["push"][0]["bypass_cooldown"] is True
    assert scene["calls"]["web"][0]["data"]["alert_tier"] == "critical"


def test_weakly_seen_concealment_is_watch_and_not_pushed(scene, loop_thread):
    """The same movement seen less clearly (visibility 0.6) at standard stock: Watch, dashboard only."""
    _set_zone("Snacks", 3.0)
    raised = _conceal(TRACK + "_dim", t0=5000.0, vis=0.6)
    assert 0.35 <= raised["confidence"] < 0.55, raised["confidence"]
    assert raised["alert_tier"] == "watch" and raised["severity"] == "MEDIUM"
    pose_analytics.flush_now()
    assert _wait(lambda: scene["dispatched"])
    sent = scene["dispatched"][0]
    assert sent["data"]["alert_tier"] == "watch" and sent["severity"] == "WARNING"
    assert sent["report"]["logged"] is True and "dashboard only" in sent["report"]["push"]["skipped"]
    assert scene["calls"]["push"] == [] and scene["calls"]["web"] == []
