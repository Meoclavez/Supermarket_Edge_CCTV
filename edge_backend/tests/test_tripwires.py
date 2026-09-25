"""Tripwires (in/out counting) and restricted areas.

Covers the crossing maths (direction, jitter, touch-without-crossing, going
round the end), restricted-area schedules across midnight, dwell and cooldown,
the live-engine hook with synthetic tracks and a mocked dispatcher, the zones
API round trip with 422s, and the footfall endpoint.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services import live_analytics_engine as lae
from app.services import tripwire_engine as te
from app.services.ai_zone_service import ai_zone_service
from app.services.inference_backend import Detection
from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine
from app.services.tripwire_engine import (
    AreaTrackState,
    WireTrackState,
    schedule_active,
    side_distance,
    step_area,
    step_crossing,
    tripwire_engine,
)

CAM = "cam_unit_tripwire"

# Horizontal line across a 640x480 frame at y=240, from x=100 to x=540.
A, B = (100.0, 240.0), (540.0, 240.0)
BAND = 8.0


def _walk(points, band=BAND, confirm=2, repeat=1.0, t0=1000.0, dt=0.2, state=None, ends=True):
    """Feed foot points; return the sides of settled crossings (and the state).

    ``ends`` = the track then leaves the view, which settles a pending crossing.
    """
    st = state or WireTrackState()
    out = []
    for i, p in enumerate(points):
        r = step_crossing(st, p, A, B, band, t0 + i * dt, confirm_frames=confirm, min_repeat_sec=repeat)
        if r is not None:
            out.append(r[0])
    if ends and (r := te.settle_crossing(st)) is not None:
        out.append(r[0])
    return out, st


# ------------------------------------------------------------------ geometry

def test_side_distance_sign_is_screen_right_of_a_to_b():
    # A->B points right; image y grows downward, so "below" is the right-hand side.
    assert side_distance(300, 300, *A, *B) > 0
    assert side_distance(300, 200, *A, *B) < 0
    assert abs(side_distance(300, 250, *A, *B) - 10.0) < 1e-9


def test_straight_walk_counts_once_with_direction():
    down = [(300, y) for y in range(150, 340, 15)]      # above -> below: into the right side
    crossings, _ = _walk(down)
    assert crossings == [1]
    up = list(reversed(down))
    crossings, _ = _walk(up)
    assert crossings == [-1]


def test_first_observation_only_sets_the_side():
    # Appears already below the line and walks away: never seen crossing.
    crossings, st = _walk([(300, y) for y in range(260, 400, 20)])
    assert crossings == [] and st.side == 1


def test_jitter_on_the_line_never_counts():
    rng = np.random.default_rng(7)
    # Approach, then 60 frames of +-6 px noise around the line (inside the 8 px band), then back.
    pts = [(300, 200), (300, 220)] + [(300, 240 + float(rng.uniform(-6, 6))) for _ in range(60)] + [(300, 200)]
    crossings, _ = _walk(pts)
    assert crossings == []


def test_single_frame_spike_across_the_line_is_not_a_crossing():
    # One detection frame lands clearly on the other side (bad box), then it is back.
    pts = [(300, 200), (300, 210), (300, 270), (300, 210), (300, 205)]
    crossings, _ = _walk(pts, confirm=2)
    assert crossings == []


def test_touching_the_line_and_turning_back_is_not_a_crossing():
    pts = [(300, 150), (300, 200), (300, 236), (300, 244), (300, 238), (300, 200), (300, 150)]
    crossings, _ = _walk(pts)
    assert crossings == []


def test_walking_round_the_end_of_the_line_is_not_a_crossing():
    # Goes from above to below at x=600, beyond B (x=540).
    pts = [(600, 150), (600, 200), (600, 280), (600, 320), (600, 360)]
    crossings, st = _walk(pts)
    assert crossings == [] and st.side == 1


def test_quick_bounce_back_cancels_out():
    down = [(300, 180), (300, 200), (300, 280), (300, 300)]
    up = [(300, 200), (300, 180)]
    # Stepped over and straight back inside the settle window: net zero, not a lone "in".
    crossings, st = _walk(down + up, repeat=5.0, dt=0.2)
    assert crossings == [] and st.side == -1
    crossings, _ = _walk(down + up, repeat=0.1, dt=0.2)
    assert crossings == [1, -1]


def test_repeated_passes_stay_balanced():
    """Walking in and out over and over gives equal in and out counts."""
    lap = [(300, y) for y in range(150, 340, 15)] + [(300, y) for y in range(340, 150, -15)]
    crossings, _ = _walk(lap * 6, repeat=1.0, dt=0.2)
    assert crossings.count(1) == 6 and crossings.count(-1) == 6
    assert crossings == [1, -1] * 6


def test_crossing_settles_only_after_the_window_or_when_the_track_ends():
    st = WireTrackState()
    pts = [(300, 180), (300, 200), (300, 280), (300, 300)]
    got = [step_crossing(st, p, A, B, BAND, 10.0 + i * 0.2, confirm_frames=2, min_repeat_sec=1.0) for i, p in enumerate(pts)]
    assert got == [None] * 4 and st.tentative is not None and st.tentative[0] == 1
    crossed_at = st.tentative[1]
    r = step_crossing(st, (300, 305), A, B, BAND, crossed_at + 1.0, confirm_frames=2, min_repeat_sec=1.0)
    assert r == (1, crossed_at)                     # reported with the time it crossed
    assert te.settle_crossing(st) is None


def test_slow_walker_lingering_on_the_line_counts_once():
    pts = [(300, 200)] + [(300, 240)] * 20 + [(300, 280), (300, 300)]
    crossings, _ = _walk(pts)
    assert crossings == [1]


# ------------------------------------------------------------------ schedules

def _at(weekday: int, hh: int, mm: int = 0) -> datetime:
    # 2026-09-21 is a Monday.
    return datetime(2026, 9, 21, hh, mm) + timedelta(days=weekday)


def test_schedule_across_midnight():
    area = {"schedule": [{"days": ["fri"], "from": "22:00", "to": "07:00"}]}
    assert schedule_active(area, _at(4, 23)) is True          # Fri 23:00
    assert schedule_active(area, _at(5, 6, 59)) is True       # Sat 06:59 (carried over)
    assert schedule_active(area, _at(5, 7, 0)) is False       # Sat 07:00
    assert schedule_active(area, _at(4, 21, 59)) is False     # Fri 21:59
    assert schedule_active(area, _at(3, 23)) is False         # Thu 23:00
    assert schedule_active(area, _at(4, 3)) is False          # Fri 03:00 belongs to Thursday's night


def test_schedule_modes_and_empty_schedule():
    assert schedule_active({"schedule": []}, _at(0, 12)) is True
    allowed = {"schedule_mode": "allowed_during",
               "schedule": [{"days": ["mon", "tue", "wed", "thu", "fri"], "from": "06:00", "to": "09:00"}]}
    assert schedule_active(allowed, _at(0, 7)) is False       # deliveries window: allowed
    assert schedule_active(allowed, _at(0, 12)) is True       # restricted otherwise
    assert schedule_active(allowed, _at(6, 7)) is True        # Sunday is not in the window
    whole_day = {"schedule": [{"days": ["sun"], "from": "00:00", "to": "24:00"}]}
    assert schedule_active(whole_day, _at(6, 13)) is True and schedule_active(whole_day, _at(5, 13)) is False


def test_restricted_area_timezone(monkeypatch):
    area = {"schedule": [{"days": ["mon"], "from": "09:00", "to": "10:00"}], "timezone": "Australia/Melbourne"}
    # Mon 2026-09-21 09:30 AEST == Sun 23:30 UTC.
    ts = datetime(2026, 9, 20, 23, 30).replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
    assert schedule_active(area, te.local_time(ts, area["timezone"])) is True
    assert schedule_active(area, te.local_time(ts, "UTC")) is False


def test_dwell_grace_and_cooldown():
    st = AreaTrackState()
    fired = [t for t in range(0, 20) if step_area(st, True, True, 100.0 + t, min_dwell=5, cooldown=10, grace=1.5)]
    # Fires once the 5 s dwell is reached, then again only after the 10 s cooldown.
    assert fired == [5, 15]

    st = AreaTrackState()
    seq = [(0, True), (1, True), (2, False), (3, True), (4, True), (5, True)]   # 1 s boundary blip
    fired = [t for t, inside in seq if step_area(st, inside, True, 200.0 + t, min_dwell=4, cooldown=60, grace=1.5)]
    assert fired == [4]

    st = AreaTrackState()
    seq = [(0, True), (1, True), (2, False), (5, False), (6, True), (8, True), (10, True)]  # real exit resets
    fired = [t for t, inside in seq if step_area(st, inside, True, 300.0 + t, min_dwell=4, cooldown=60, grace=1.5)]
    assert fired == [10]


def test_inactive_schedule_never_fires():
    st = AreaTrackState()
    assert not any(step_area(st, True, False, 400.0 + t, min_dwell=0, cooldown=10) for t in range(10))


# --------------------------------------------------------- engine hook (live)

@pytest.fixture
def rules():
    created = []

    def add_wire(**kw):
        rec = {"id": f"tw_unit_{len(created)}", "name": "Front door", "camera_id": CAM,
               "x1": A[0] / 640, "y1": A[1] / 480, "x2": B[0] / 640, "y2": B[1] / 480,
               "in_side": "right", "counts_footfall": True, "alert_enabled": True,
               "alert_direction": "in", "severity": "WARNING", "enabled": True, **kw}
        created.append(("tw", ai_zone_service.add_tripwire(rec)["id"]))
        return rec

    def add_area(**kw):
        rec = {"id": f"iz_unit_{len(created)}", "name": "Stockroom", "camera_id": CAM,
               "points": [{"x": 0.0, "y": 0.75}, {"x": 1.0, "y": 0.75}, {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}],
               "schedule": [], "min_dwell_seconds": 1.0, "severity": "HIGH", "enabled": True, **kw}
        created.append(("iz", ai_zone_service.add_intrusion(rec)["id"]))
        return rec

    yield add_wire, add_area
    for kind, i in created:
        (ai_zone_service.delete_tripwire if kind == "tw" else ai_zone_service.delete_intrusion)(i)
    tripwire_engine.reset_camera(CAM)
    tripwire_engine.drain_events()


@pytest.fixture
def dispatched(monkeypatch):
    calls = []

    async def fake_dispatch(event_type, severity, title, body, data):
        calls.append({"event_type": event_type, "severity": severity, "title": title, "body": body, "data": data})
        return {"ok": True}

    monkeypatch.setattr(tripwire_engine, "synchronous", True)
    monkeypatch.setattr(tripwire_engine, "dispatcher_override", fake_dispatch)
    return calls


@pytest.fixture
def moving_person(monkeypatch):
    """Stub detector: one person whose foot point follows ``path`` frame by frame."""
    state = {"path": [], "i": 0}

    def detect(frame, conf_threshold=None, iou_threshold=None, camera_id=None, **_kw):
        i = min(state["i"], len(state["path"]) - 1)
        state["i"] += 1
        fx, fy = state["path"][i]
        return [Detection(x1=fx - 30, y1=fy - 160, x2=fx + 30, y2=fy, confidence=0.9)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    monkeypatch.setitem(lae._pose_state, "module", None)
    monkeypatch.setitem(lae._pose_state, "status", "unavailable")
    return state


def _worker():
    engine = LiveAnalyticsEngine()
    rt = CameraRuntime(camera_id=CAM, name="Entrance cam", source="/dev/null")
    engine.runtimes[CAM] = rt
    return CameraWorker(rt, engine), rt


def test_engine_hook_counts_crossings_and_dispatches_alerts(rules, dispatched, moving_person):
    add_wire, add_area = rules
    wire = add_wire()
    area = add_area()
    # Walk from the top of the frame down through the line and stop inside the stockroom area.
    moving_person["path"] = [(320, y) for y in range(170, 400, 12)] + [(320, 400)] * 15
    w, rt = _worker()
    frame = np.full((480, 640, 3), 90, dtype=np.uint8)
    for i in range(len(moving_person["path"])):
        w._analyse(frame, now=50000.0 + i * 0.2)

    events = tripwire_engine.drain_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["tripwire_id"] == wire["id"] and ev["direction"] == "in" and ev["camera_id"] == CAM
    assert ev["counts_footfall"] is True

    assert tripwire_engine.process_pending_alerts() >= 2
    kinds = {c["event_type"]: c for c in dispatched}
    assert set(kinds) == {"TRIPWIRE_ALERT", "RESTRICTED_AREA"}
    tw_alert, ra_alert = kinds["TRIPWIRE_ALERT"], kinds["RESTRICTED_AREA"]
    assert tw_alert["severity"] == "WARNING" and ra_alert["severity"] == "HIGH"
    for c, rid, name in ((tw_alert, wire["id"], "Front door"), (ra_alert, area["id"], "Stockroom")):
        d = c["data"]
        assert d["camera_id"] == CAM and d["zone_id"] == rid and d["zone_name"] == name
        assert d["track_id"] and d["event_type"] == c["event_type"]
        assert d["snapshot_url"] == f"/api/zones/alerts/{d['alert_id']}/snapshot"
        assert te.TripwireEngine.snapshot_path(d["alert_id"]) is not None
    assert tw_alert["data"]["tripwire_id"] == wire["id"] and tw_alert["data"]["direction"] == "in"
    # Only one restricted-area alert despite many frames inside (cooldown).
    assert sum(1 for c in dispatched if c["event_type"] == "RESTRICTED_AREA") == 1


def test_engine_hook_respects_people_counting_flag(rules, dispatched, moving_person):
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    add_wire, _ = rules
    add_wire(alert_enabled=False)
    feature_manager.set_camera_features(CAM, CameraFeatureConfig(people_counting=False))
    try:
        moving_person["path"] = [(320, y) for y in range(170, 330, 12)]
        w, _ = _worker()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        for i in range(len(moving_person["path"])):
            w._analyse(frame, now=60000.0 + i * 0.2)
    finally:
        feature_manager.remove_camera(CAM)
    assert tripwire_engine.drain_events() == []
    assert dispatched == []


def test_disabled_rules_and_inactive_schedule_do_nothing(rules, dispatched, moving_person):
    add_wire, add_area = rules
    add_wire(enabled=False)
    now = datetime.now()
    tomorrow = te.DAYS[(now.weekday() + 1) % 7]
    add_area(schedule=[{"days": [tomorrow], "from": "10:00", "to": "11:00"}])
    moving_person["path"] = [(320, y) for y in range(170, 400, 12)] + [(320, 400)] * 10
    w, _ = _worker()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    t0 = now.replace(hour=12, minute=0).timestamp()
    for i in range(len(moving_person["path"])):
        w._analyse(frame, now=t0 + i * 0.2)
    tripwire_engine.process_pending_alerts()
    assert tripwire_engine.drain_events() == [] and dispatched == []


def test_dispatch_falls_back_to_notification_service(monkeypatch):
    """Without alert_dispatcher the engine uses notify_loss_prevention with event_type/severity."""
    import builtins

    from app.services.notification_service import notification_service

    real_import = builtins.__import__

    def no_dispatcher(name, *args, **kwargs):
        if name == "app.services.alert_dispatcher":
            raise ImportError("not built yet")
        return real_import(name, *args, **kwargs)

    seen = {}

    async def fake_notify(title, body, data):
        seen.update(title=title, data=data)
        return {"logged": False}

    monkeypatch.setattr(builtins, "__import__", no_dispatcher)
    monkeypatch.setattr(notification_service, "notify_loss_prevention", fake_notify)
    fn = te.TripwireEngine()._dispatcher()
    asyncio.run(fn("RESTRICTED_AREA", "HIGH", "t", "b", {"camera_id": "c"}))
    assert seen["data"]["event_type"] == "RESTRICTED_AREA" and seen["data"]["severity"] == "HIGH"


# ------------------------------------------------------------------ API

@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_tripwire_api_round_trip_and_validation(client):
    body = {"name": "Main entrance", "camera_id": CAM, "x1": 0.1, "y1": 0.6, "x2": 0.9, "y2": 0.6,
            "in_side": "left", "alert_enabled": True, "alert_direction": "out", "severity": "high"}
    r = client.post("/api/zones/tripwire", json=body)
    assert r.status_code == 200, r.text
    tw = r.json()["tripwire"]
    tw_id = tw["id"]
    try:
        assert tw["in_side"] == "left" and tw["severity"] == "HIGH" and tw["alert_direction"] == "out"
        assert "in_count" not in tw
        listed = [t for t in client.get("/api/zones").json()["tripwires"] if t["id"] == tw_id]
        assert listed and listed[0]["counts_footfall"] is True

        r = client.patch(f"/api/zones/tripwire/{tw_id}", json={"in_side": "right", "alert_enabled": False, "name": "Door A"})
        assert r.status_code == 200 and r.json()["tripwire"]["in_side"] == "right"
        assert client.get("/api/zones").json()["tripwires"][[t["id"] for t in client.get("/api/zones").json()["tripwires"]].index(tw_id)]["name"] == "Door A"

        for bad in ({"in_side": "up"}, {"severity": "LOUD"}, {"x1": 1.5}, {"alert_direction": "sideways"},
                    {"x1": 0.5, "y1": 0.6, "x2": 0.505, "y2": 0.6}, {"bogus": 1}):
            assert client.patch(f"/api/zones/tripwire/{tw_id}", json=bad).status_code == 422, bad
        assert client.patch("/api/zones/tripwire/nope", json={"name": "x"}).status_code == 404
        for bad in ({**body, "x2": 2}, {**body, "camera_id": ""}, {k: v for k, v in body.items() if k != "x1"}):
            assert client.post("/api/zones/tripwire", json=bad).status_code == 422
        # Mobile path: line_start/line_end, legacy direction.
        r = client.post(f"/api/v1/cameras/{CAM}/zones", json={"zone_type": "TRIPWIRE", "name": "m",
                        "line_start": {"x": 0.2, "y": 0.2}, "line_end": {"x": 0.8, "y": 0.2}, "direction": "bToA"})
        assert r.status_code == 200 and r.json()["in_side"] == "left"
        cam_zones = client.get(f"/api/v1/cameras/{CAM}/zones").json()
        mobile = [z for z in cam_zones if z["id"] == r.json()["id"]][0]
        assert mobile["zone_type"] == "TRIPWIRE" and mobile["line_start"] == {"x": 0.2, "y": 0.2}
        assert client.delete(f"/api/v1/cameras/{CAM}/zones/{mobile['id']}").status_code == 200
    finally:
        assert client.delete(f"/api/zones/tripwire/{tw_id}").status_code == 200


def test_restricted_area_api_round_trip_and_validation(client):
    body = {"name": "Stockroom", "camera_id": CAM,
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.4, "y": 0.1}, {"x": 0.4, "y": 0.5}],
            "schedule": [{"days": ["fri", "sat"], "from": "22:00", "to": "07:00"}],
            "min_dwell_seconds": 4, "severity": "WARNING", "enabled": True}
    r = client.post("/api/zones/intrusion", json=body)
    assert r.status_code == 200, r.text
    iz = r.json()["intrusion_zone"]
    try:
        assert iz["schedule"] == [{"days": ["fri", "sat"], "from": "22:00", "to": "07:00"}]
        assert iz["min_dwell_seconds"] == 4 and iz["severity"] == "WARNING"
        r = client.patch(f"/api/zones/intrusion/{iz['id']}", json={
            "schedule": [{"days": ["mon"], "from": "09:00", "to": "17:00"}], "schedule_mode": "allowed_during",
            "enabled": False, "timezone": "Australia/Melbourne"})
        assert r.status_code == 200, r.text
        stored = [z for z in client.get("/api/zones").json()["intrusion_zones"] if z["id"] == iz["id"]][0]
        assert stored["schedule_mode"] == "allowed_during" and stored["enabled"] is False
        assert stored["timezone"] == "Australia/Melbourne" and stored["points"] == body["points"]
        for bad in ({"schedule": [{"days": ["xyz"], "from": "09:00", "to": "10:00"}]},
                    {"schedule": [{"days": ["mon"], "from": "9am", "to": "10:00"}]},
                    {"schedule": [{"days": [], "from": "09:00", "to": "10:00"}]},
                    {"min_dwell_seconds": -1}, {"severity": "EXTREME"}, {"timezone": "Mars/Olympus"},
                    {"points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}]}, {"schedule_mode": "sometimes"}):
            assert client.patch(f"/api/zones/intrusion/{iz['id']}", json=bad).status_code == 422, bad
        assert client.post("/api/zones/intrusion", json={**body, "points": [{"x": 1.2, "y": 0}] * 3}).status_code == 422
        # Mobile editor shape (polygon_points + dwell_time_seconds).
        r = client.post(f"/api/v1/cameras/{CAM}/zones", json={
            "zone_type": "RESTRICTED_ZONE", "name": "Cash office", "dwell_time_seconds": 2,
            "polygon_points": [{"x": 0.5, "y": 0.5}, {"x": 0.9, "y": 0.5}, {"x": 0.9, "y": 0.9}]})
        assert r.status_code == 200 and r.json()["min_dwell_seconds"] == 2
        mobile = [z for z in client.get(f"/api/v1/cameras/{CAM}/zones").json() if z["id"] == r.json()["id"]][0]
        assert mobile["zone_type"] == "RESTRICTED_ZONE" and len(mobile["polygon_points"]) == 3
        assert client.delete(f"/api/v1/cameras/{CAM}/zones/{mobile['id']}").status_code == 200
    finally:
        assert client.delete(f"/api/zones/intrusion/{iz['id']}").status_code == 200


def test_clear_privacy_masks_only_keeps_tripwires(client):
    r = client.post("/api/zones/tripwire", json={"camera_id": CAM, "x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5})
    tw_id = r.json()["tripwire"]["id"]
    try:
        assert client.post("/api/zones/clear?kinds=exclusion_masks").status_code == 200
        assert any(t["id"] == tw_id for t in client.get("/api/zones").json()["tripwires"])
        assert client.post("/api/zones/clear?kinds=bogus").status_code == 422
    finally:
        client.delete(f"/api/zones/tripwire/{tw_id}")


def test_alert_snapshot_endpoint_404(client):
    assert client.get("/api/zones/alerts/za_doesnotexist/snapshot").status_code == 404
    assert client.get("/api/zones/alerts/..%2F..%2Fetc/snapshot").status_code == 404


# ------------------------------------------------------------------ footfall

def test_footfall_endpoint_and_precedence(client, monkeypatch):
    from app.database import async_session_factory
    from app.models.db_models import TripwireEventModel

    # Stored ts is naive UTC; buckets are store-local. A half-hour zone proves
    # UTC minutes are folded into the right local hours.
    monkeypatch.setattr(settings, "SITE_TIMEZONE", "Asia/Kolkata")
    day = datetime(2031, 5, 6)             # isolated future day (UTC times below)

    def ev(tw, direction, h, m, counts=True):
        return {"tripwire_id": tw, "tripwire_name": tw.upper(), "camera_id": CAM, "track_id": f"t{h}{m}",
                "direction": direction, "ts": day.replace(hour=h, minute=m), "counts_footfall": counts}

    tripwire_engine.drain_events()
    tripwire_engine._pending_events.extend([
        ev("tw_door", "in", 9, 5), ev("tw_door", "in", 9, 40), ev("tw_door", "out", 10, 15),
        ev("tw_door", "in", 10, 20), ev("tw_stock", "in", 10, 30, counts=False),
    ])
    written = asyncio.run(te.flush_tripwire_events(async_session_factory))
    assert written == 5

    # Naive from/to are store-local: 2031-05-06 00:00 IST .. 2031-05-07 00:00 IST.
    r = client.get("/api/v1/analytics/footfall/tripwires",
                   params={"from": "2031-05-06", "to": "2031-05-07", "bucket": "hour"})
    assert r.status_code == 200, r.text
    data = r.json()
    lines = {t["tripwire_id"]: t for t in data["tripwires"]}
    door = lines["tw_door"]
    assert (door["in"], door["out"], door["net"]) == (3, 1, 2)
    # 09:05Z = 14:35 IST; 09:40Z, 10:15Z, 10:20Z = 15:10, 15:45, 15:50 IST.
    assert door["buckets"] == [{"start": "2031-05-06T14:00:00+05:30", "in": 1, "out": 0},
                               {"start": "2031-05-06T15:00:00+05:30", "in": 2, "out": 1}]
    assert data["from"] == "2031-05-06T00:00:00+05:30"
    days = client.get("/api/v1/analytics/footfall/tripwires",
                      params={"from": "2031-05-06", "to": "2031-05-07", "bucket": "day"}).json()
    assert {t["tripwire_id"]: t for t in days["tripwires"]}["tw_door"]["buckets"] == [
        {"start": "2031-05-06T00:00:00+05:30", "in": 3, "out": 1}]
    assert lines["tw_stock"]["counts_footfall"] is False
    # Totals and the occupancy estimate only use footfall-counting lines.
    assert data["totals"] == {"in": 3, "out": 1, "net": 2}
    assert data["net_occupancy_estimate"] == 2 and "estimate" in data["estimate_note"].lower()
    assert data["footfall_source"] == "tripwire"

    # Tripwire entries win over zone/track footfall for that window (no double count).
    from app.services.retail_metrics_service import retail_metrics_service

    async def ff():
        async with async_session_factory() as db:
            return await retail_metrics_service.footfall(db, day, day + timedelta(days=1))
    assert asyncio.run(ff()) == 3

    # An empty window reports null, not zero.
    empty = client.get("/api/v1/analytics/footfall/tripwires",
                       params={"from": "2031-01-01", "to": "2031-01-02"}).json()
    assert empty["observed"] is False and empty["totals"]["in"] is None and empty["net_occupancy_estimate"] is None

    assert client.get("/api/v1/analytics/footfall/tripwires", params={"bucket": "week"}).status_code == 422
    assert client.get("/api/v1/analytics/footfall/tripwires", params={"from": "garbage"}).status_code == 422
    assert client.get("/api/v1/analytics/footfall/tripwires",
                      params={"from": "2031-01-02", "to": "2031-01-01"}).status_code == 422

    async def cleanup():
        from sqlalchemy import delete
        async with async_session_factory() as db:
            await db.execute(delete(TripwireEventModel).where(TripwireEventModel.camera_id == CAM))
            await db.commit()
    asyncio.run(cleanup())


# ------------------------------------------------- timezones and track quality

def test_day_bounds_are_store_local_midnights_in_utc(monkeypatch):
    from app.services.retail_metrics_service import day_bounds

    monkeypatch.setattr(settings, "SITE_TIMEZONE", "Australia/Melbourne")
    # 23 Sep 2026 in Melbourne (AEST, +10:00) starts at 22 Sep 14:00 UTC.
    assert day_bounds(datetime(2026, 9, 23, 12, 0)) == (datetime(2026, 9, 22, 14, 0), datetime(2026, 9, 23, 14, 0))
    # DST starts 4 Oct 2026 in Melbourne: that local day is 23 hours long.
    start, end = day_bounds(datetime(2026, 10, 4, 12, 0))
    assert (end - start) == timedelta(hours=23)


def test_new_pipeline_writes_are_utc():
    from app.services.timeutil import utc_from_ts

    assert utc_from_ts(1_800_000_000.0) == datetime(2027, 1, 15, 8, 0, 0)   # independent of host TZ


def test_fragmented_tracks_do_not_inflate_footfall(monkeypatch):
    """Four people flickering into dozens of ~1 s tracks still count as four."""
    from app.database import async_session_factory
    from app.models.db_models import CustomerTrackModel
    from app.services.retail_metrics_service import retail_metrics_service

    monkeypatch.setattr(settings, "FOOTFALL_MIN_TRACK_SECONDS", 3.0)
    monkeypatch.setattr(settings, "FOOTFALL_MIN_TRACK_HITS", 3)
    day = datetime(2032, 2, 3, 10, 0)            # isolated UTC day, no zone visits
    rows = []
    for person in range(4):                     # the real people: long, well-matched tracks
        rows.append(CustomerTrackModel(id=f"ct_frag_real_{person}", track_id=f"real{person}", camera_id=CAM,
                                       start_time=day, end_time=day + timedelta(minutes=5), hits=900,
                                       trajectory_points=[]))
    for i in range(60):                         # the flicker: ~1 s fragments of the same people
        t = day + timedelta(seconds=5 * i)
        rows.append(CustomerTrackModel(id=f"ct_frag_{i}", track_id=f"frag{i}", camera_id=CAM,
                                       start_time=t, end_time=t + timedelta(seconds=1), hits=5,
                                       trajectory_points=[]))
    # Long but barely matched (a coasting ghost), and a pre-m0008 row with no hits.
    rows.append(CustomerTrackModel(id="ct_frag_ghost", track_id="ghost", camera_id=CAM, start_time=day,
                                   end_time=day + timedelta(seconds=30), hits=1, trajectory_points=[]))
    rows.append(CustomerTrackModel(id="ct_frag_legacy", track_id="legacy", camera_id=CAM, start_time=day,
                                   end_time=day + timedelta(seconds=40), hits=None, trajectory_points=[]))

    async def run():
        from sqlalchemy import delete
        async with async_session_factory() as db:
            db.add_all(rows)
            await db.commit()
            try:
                start, end = day.replace(hour=0), day.replace(hour=0) + timedelta(days=1)
                return (await retail_metrics_service.footfall(db, start, end),
                        await retail_metrics_service.footfall_source(db, start, end))
            finally:
                await db.execute(delete(CustomerTrackModel).where(CustomerTrackModel.id.like("ct_frag_%")))
                await db.commit()
    footfall, source = asyncio.run(run())
    assert footfall == 5 and source == "tracks"     # 4 real + the legacy row judged on duration


def test_m0008_converts_only_rows_written_in_local_time(monkeypatch):
    import sqlite3
    import time as _time

    from app.migrations.m0008_tripwire_events import convert_local_rows

    monkeypatch.setenv("TZ", "Asia/Kolkata")
    _time.tzset()
    try:
        c = sqlite3.connect(":memory:", isolation_level=None)
        c.executescript("""
            CREATE TABLE customer_tracks (id TEXT PRIMARY KEY, start_time DATETIME, end_time DATETIME, created_at DATETIME);
            CREATE TABLE zone_visits (id TEXT PRIMARY KEY, entered_at DATETIME, exited_at DATETIME, created_at DATETIME);
            CREATE TABLE theft_incidents (id TEXT PRIMARY KEY, timestamp DATETIME, created_at DATETIME);
            -- written local (IST = UTC+5:30), flushed 4 s later in UTC
            INSERT INTO customer_tracks VALUES ('local', '2026-09-20 15:30:00.000000', '2026-09-20 15:31:00.000000', '2026-09-20 10:01:04.000000');
            -- already UTC
            INSERT INTO customer_tracks VALUES ('utc', '2026-09-20 10:00:00.000000', '2026-09-20 10:01:00.000000', '2026-09-20 10:01:04.000000');
            INSERT INTO zone_visits VALUES ('open', '2026-09-20 15:30:00.000000', NULL, '2026-09-20 10:00:08.000000');
            INSERT INTO theft_incidents VALUES ('ti', '2026-09-20 15:30:00.000000', '2026-09-20 10:00:02.000000');
        """)
        report = convert_local_rows(c)
        assert report == {"customer_tracks": 1, "zone_visits": 1, "theft_incidents": 1}
        rows = dict((r[0], r[1:]) for r in c.execute("SELECT id, start_time, end_time FROM customer_tracks"))
        assert rows["local"] == ("2026-09-20 10:00:00.000000", "2026-09-20 10:01:00.000000")
        assert rows["utc"] == ("2026-09-20 10:00:00.000000", "2026-09-20 10:01:00.000000")
        assert c.execute("SELECT entered_at, exited_at FROM zone_visits").fetchone() == ("2026-09-20 10:00:00.000000", None)
        # Running again changes nothing: every row now matches its created_at.
        assert convert_local_rows(c) == {"customer_tracks": 0, "zone_visits": 0, "theft_incidents": 0}
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()
