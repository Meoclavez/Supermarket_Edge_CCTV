"""Live tracks + behaviour for the client-side overlay (/api/v1/live/*) and the server overlay.

No threads, cameras or models: runtimes get synthetic track snapshots
(``CameraRuntime.set_tracks`` with the analysed frame size, exactly as
``CameraWorker._analyse`` publishes them) and pose analytics gets synthetic
``TrackState`` objects, so every level and label is deterministic.
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import live_analytics_engine as lae
from app.services import public_exposure
from app.services import theft_detection_service as rules
from app.services.inference_backend import Detection
from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine, render_overlay
from app.services.pose_analytics import HANDS, CameraState, HandState, PoseAnalytics, TrackState

CAM = "cam_live_tracks_01"
CAM2 = "cam_live_tracks_02"
W, H = 640, 480
NOW = 1_000_000.0
FLAGS_ON = {"people_counting": True, "shelf_interaction": True, "theft_detection": True}


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "THEFT_INCIDENT_COOLDOWN_SEC", 120.0)
    monkeypatch.setattr(settings, "THEFT_PATTERN_ENABLED", True)
    monkeypatch.setattr(settings, "THEFT_PATTERN_SCORE_THRESHOLD", 1.2)
    monkeypatch.setattr(settings, "THEFT_PATTERN_HEAD_TURNS", 6)
    monkeypatch.setattr(settings, "THEFT_PATTERN_HEAD_WINDOW_SEC", 30.0)
    lae._behaviour_cache.clear()
    yield
    lae._behaviour_cache.clear()


@pytest.fixture
def pose(monkeypatch):
    """A real PoseAnalytics instance (no observe() runs) wired in as the engine's pose module."""
    pa = PoseAnalytics()
    monkeypatch.setitem(lae._pose_state, "module", pa)
    monkeypatch.setitem(lae._pose_state, "status", "ok")
    monkeypatch.setitem(lae._pose_state, "error", None)
    return pa


@pytest.fixture
def client():
    # No context manager: the lifespan (pipeline supervisor) does not run, so
    # the runtimes registered by a test are not reconciled away.
    return TestClient(app)


def _kps(x0: float, y0: float) -> list:
    """17 COCO points of a standing figure whose box starts at (x0, y0), all visible."""
    rel = [(40, 20), (35, 15), (45, 15), (30, 18), (50, 18), (20, 60), (60, 60), (12, 100), (68, 100),
           (8, 140), (72, 140), (25, 140), (55, 140), (25, 190), (55, 190), (25, 240), (55, 240)]
    return [[x0 + dx, y0 + dy, 0.95] for dx, dy in rel]


def _entry(tid, x1, y1, x2, y2, *, fresh=True, last_seen=NOW, pred=None, confirmed=True,
           motion="moving", pending_static=False):
    """A track entry as CameraWorker._analyse writes it."""
    kps = _kps(x1, y1)
    return {
        "track_id": tid, "x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2),
        "confidence": 0.9, "hits": 10, "confirmed": confirmed, "motion_state": motion,
        "pending_static": pending_static, "age_seconds": 5.0,
        "keypoints": kps if fresh else None,
        "fresh": fresh, "last_seen": last_seen,
        "pred_box": None if fresh else list(pred),
        "kp_last": None if fresh else kps,
        "x_m": None, "y_m": None, "zone_id": None,
    }


def _runtime(monkeypatch, cam=CAM, name="Live Cam", flags=FLAGS_ON):
    rt = CameraRuntime(camera_id=cam, name=name, source="/dev/null")
    rt.analysis_flags = dict(flags)
    monkeypatch.setitem(lae.live_engine.runtimes, cam, rt)
    return rt


def _pose_cam(pa: PoseAnalytics, cam=CAM, *, theft=True, shelf=True, now=NOW) -> CameraState:
    c = pa._camera(cam)
    c.theft_on, c.interactions_on, c.last_ts = theft, shelf, now
    c.frame_size = (W, H)
    return c


def _track(c: CameraState, tid: str) -> TrackState:
    st = TrackState(
        track_id=tid, first_seen=NOW - 30, last_seen=NOW,
        hands={n: HandState(name=n, kp_index=i, trajectory=deque(maxlen=8)) for n, i in HANDS},
        skeletons=deque(maxlen=8),
    )
    c.tracks[tid] = st
    return st


def _get(client, cams: str):
    res = client.get(f"/api/v1/live/tracks?cameras={cams}")
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------- /live/tracks


def test_tracks_are_normalised_with_freshness_velocity_and_keypoints(client, pose, monkeypatch):
    rt = _runtime(monkeypatch)
    # trk_a walks right 32 px in 0.5 s (64 px/s = 0.1 frame widths per second).
    rt.set_tracks([_entry("trk_a", 32, 48, 160, 432, last_seen=NOW - 0.5)], NOW - 0.5, frame_size=(W, H))
    rt.set_tracks([
        _entry("trk_a", 64, 48, 192, 432),
        # Coasting for 1.5 s: reported at the tracker's predicted box with its last pose.
        _entry("trk_b", 300, 96, 428, 480, fresh=False, last_seen=NOW - 1.5, pred=(320, 96, 448, 480)),
    ], NOW, frame_size=(W, H))

    body = _get(client, CAM)
    assert body["server_time"] > 0
    cam = body["cameras"][CAM]
    assert cam["seq"] == 2
    assert cam["analysed_at"] == pytest.approx(NOW)
    assert (cam["frame_width"], cam["frame_height"]) == (W, H)
    assert cam["analysis_fps"] is None or cam["analysis_fps"] > 0
    a, b = cam["tracks"]

    assert a["track_id"] == "trk_a" and a["confirmed"] is True and a["motion_state"] == "moving"
    assert a["box"] == [0.1, 0.1, 0.3, 0.9]
    assert a["fresh"] is True and a["age_sec"] == 0.0
    assert a["velocity"] == pytest.approx([0.1, 0.0], abs=1e-4)
    assert len(a["keypoints"]) == 17
    assert a["keypoints"][0] == [pytest.approx(104 / W, abs=1e-4), pytest.approx(68 / H, abs=1e-4), 0.95]
    assert all(0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0 for p in a["keypoints"])
    # No pose state for this camera yet: behaviour is not available, never invented.
    assert a["behaviour"] is None

    assert b["fresh"] is False and b["age_sec"] == pytest.approx(1.5)
    assert b["box"] == [0.5, 0.2, 0.7, 1.0]
    assert b["velocity"] is None          # one match only: unknown
    assert b["keypoints"][0][0] == pytest.approx(340 / W, abs=1e-4)


class _NoPose:
    """Pose analytics stand-in without live_state: behaviour is reported as not available."""

    def observe(self, camera_id, ts, frame_bgr, tracks):
        return type("R", (), {"interactions": []})()

    def reset_camera(self, camera_id):
        pass


def test_worker_publishes_fresh_coasting_and_velocity(monkeypatch):
    """End to end through CameraWorker._analyse: a walking person, then a missed frame."""
    monkeypatch.setitem(lae._pose_state, "module", _NoPose())
    monkeypatch.setitem(lae._pose_state, "status", "ok")
    engine = LiveAnalyticsEngine()
    rt = CameraRuntime(camera_id=CAM, name="Unit", source="/dev/null")
    engine.runtimes[CAM] = rt
    w = CameraWorker(rt, engine)
    n = {"i": 0}

    def walking(frame, **_kw):
        dx = 20.0 * n["i"]
        n["i"] += 1
        k = np.asarray(_kps(100 + dx, 50), dtype=np.float32)
        return [Detection(x1=100.0 + dx, y1=50.0, x2=180.0 + dx, y2=300.0, confidence=0.91, keypoints=k)]

    monkeypatch.setattr(lae.person_detector, "detect", walking)
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 3):
        w._analyse(frame, now=5000.0 + i)
    (t,) = engine.live_tracks([CAM])["cameras"][CAM]["tracks"]
    assert t["fresh"] is True and t["age_sec"] == 0.0 and t["keypoints"] is not None
    assert t["velocity"][0] == pytest.approx(20.0 / W, abs=2e-3)   # 20 px per 1 s frame

    monkeypatch.setattr(lae.person_detector, "detect", lambda f, **kw: [])
    w._analyse(frame, now=5000.0 + settings.TRACK_MIN_HITS + 3)
    cam = engine.live_tracks([CAM])["cameras"][CAM]
    (t,) = cam["tracks"]
    assert t["fresh"] is False and t["age_sec"] == pytest.approx(1.0)
    assert t["keypoints"] is not None, "last matched pose is kept for the client"
    assert t["velocity"] is not None
    assert cam["seq"] == settings.TRACK_MIN_HITS + 4
    # /layout/live keeps hiding a coasting skeleton (its box format is unchanged).
    (box,) = engine.live_snapshot()["detections"][0]["boxes"]
    assert box["keypoints"] is None and "kp_last" not in box


def test_unknown_cameras_are_omitted_and_ids_are_capped(client, pose, monkeypatch):
    rt = _runtime(monkeypatch)
    rt.set_tracks([_entry("trk_a", 64, 48, 192, 432)], NOW, frame_size=(W, H))
    body = _get(client, f"{CAM},no_such_camera,{CAM}")
    assert list(body["cameras"]) == [CAM]

    ok = ",".join([CAM] + [f"x{i}" for i in range(15)])
    assert client.get(f"/api/v1/live/tracks?cameras={ok}").status_code == 200
    too_many = ",".join(f"x{i}" for i in range(17))
    assert client.get(f"/api/v1/live/tracks?cameras={too_many}").status_code == 422
    assert client.get("/api/v1/live/tracks").status_code == 422


def test_behaviour_levels_normal_watch_alert(client, pose, monkeypatch):
    rt = _runtime(monkeypatch)
    rt.set_tracks([
        _entry("trk_n", 10, 30, 100, 300),
        _entry("trk_w", 120, 30, 220, 300),
        _entry("trk_s", 240, 30, 330, 300),
        _entry("trk_x", 350, 30, 440, 300),
    ], NOW, frame_size=(W, H))
    c = _pose_cam(pose)
    _track(c, "trk_n")

    w = _track(c, "trk_w")
    w.hands["right"].active_zone, w.hands["right"].active_started = "zone_spirits", NOW - 2
    c.zones = [type("Z", (), {"id": "zone_spirits", "name": "Spirits"})()]
    w.hands["left"].post_reach = [{"t": NOW - 1, "target": None}, {"t": NOW - 0.5, "target": "chest"},
                                  {"t": NOW, "target": "chest"}]
    w.head_turns.extend([NOW - 40] + [NOW - 20 + i for i in range(5)])   # 5 in the 30 s window

    s = _track(c, "trk_s")
    s.head_turns.extend(NOW - 25 + i for i in range(6))                 # 6 in the window: scanning
    s.pattern_score = 0.6                                                # = 0.5 x threshold 1.2

    x = _track(c, "trk_x")
    x.fired[rules.RULE_CONCEALMENT] = NOW - 10
    x.incident_ids[rules.RULE_CONCEALMENT] = ("theft_20260101_abc123", NOW - 10)
    x.fired[rules.RULE_SUSPICIOUS_LOITERING] = NOW - 500               # outside the cooldown

    by = {t["track_id"]: t["behaviour"] for t in _get(client, CAM)["cameras"][CAM]["tracks"]}
    n = by["trk_n"]
    assert n["level"] == "normal" and n["labels"] == [] and n["incident_id"] is None
    assert n["pattern_score"] is None and n["pattern_threshold"] == 1.2 and "since" not in n

    w = by["trk_w"]
    assert w["level"] == "watch"
    assert w["labels"] == ["Reaching: Spirits", "Hand at chest"]
    assert w["reaching_zone"] == "Spirits" and w["conceal"] == "chest" and w["head_turns"] == 5

    s = by["trk_s"]
    assert s["level"] == "watch"
    assert s["labels"] == ["Looking around (6)", "Behaviour score 0.60 of 1.20"]
    assert s["pattern_score"] == 0.6 and s["head_turns"] == 6

    x = by["trk_x"]
    assert x["level"] == "alert"
    assert x["labels"][0] == "Possible concealment"
    assert x["incident_id"] == "theft_20260101_abc123" and x["incident_rule"] == rules.RULE_CONCEALMENT

    # Below half the threshold the score alone is not a cue.
    s_state = pose._cameras[CAM].tracks["trk_s"]
    s_state.pattern_score, s_state.head_turns = 0.59, deque()
    lae._behaviour_cache.clear()
    by = {t["track_id"]: t["behaviour"] for t in _get(client, CAM)["cameras"][CAM]["tracks"]}
    assert by["trk_s"]["level"] == "normal" and by["trk_s"]["pattern_score"] == 0.59


def test_behaviour_is_null_when_theft_and_shelf_analytics_are_off(client, pose, monkeypatch):
    rt = _runtime(monkeypatch, flags={"people_counting": True, "shelf_interaction": False,
                                      "theft_detection": False})
    rt.set_tracks([_entry("trk_x", 350, 30, 440, 300)], NOW, frame_size=(W, H))
    c = _pose_cam(pose)
    _track(c, "trk_x").fired[rules.RULE_CONCEALMENT] = NOW - 1
    (t,) = _get(client, CAM)["cameras"][CAM]["tracks"]
    assert t["behaviour"] is None

    # Same when pose analytics itself recorded both analyses as off.
    rt.analysis_flags = dict(FLAGS_ON)
    c.theft_on = c.interactions_on = False
    lae._behaviour_cache.clear()
    (t,) = _get(client, CAM)["cameras"][CAM]["tracks"]
    assert t["behaviour"] is None


def test_shelf_only_camera_reports_reaches_but_never_alerts(client, pose, monkeypatch):
    rt = _runtime(monkeypatch, flags={"people_counting": True, "shelf_interaction": True,
                                      "theft_detection": False})
    rt.set_tracks([_entry("trk_x", 350, 30, 440, 300)], NOW, frame_size=(W, H))
    c = _pose_cam(pose, theft=False)
    st = _track(c, "trk_x")
    st.fired[rules.RULE_CONCEALMENT] = NOW - 1
    st.hands["left"].active_zone, st.hands["left"].active_started = "zone_x", NOW - 1
    st.pattern_score = 5.0
    (t,) = _get(client, CAM)["cameras"][CAM]["tracks"]
    b = t["behaviour"]
    assert b["level"] == "watch" and b["labels"] == ["Reaching: zone_x"]
    assert b["incident_id"] is None and b["pattern_score"] is None


def test_alert_references_the_queued_incident_id(pose, monkeypatch):
    from app.services.pose_analytics import ObserveResult

    monkeypatch.setattr(pose, "_ensure_writer", lambda: None)   # keep the incident queued
    c = _pose_cam(pose)
    st = _track(c, "trk_x")
    raised = pose._raise(c, st, rules.RULE_CONCEALMENT, {"confidence": 0.9, "evidence": []}, NOW - 3,
                         ObserveResult(), snapshot=None)
    assert raised
    (kind, incident), = pose._pending
    b = pose.live_state(CAM)["tracks"]["trk_x"]
    assert kind == "incident" and b["level"] == "alert"
    assert b["incident_id"] == incident["id"] and b["incident_rule"] == rules.RULE_CONCEALMENT
    assert b["since"] == pytest.approx(NOW - 3)
    pose._pending.clear()


def test_live_state_does_not_wait_for_a_busy_camera(pose):
    c = _pose_cam(pose)
    _track(c, "trk_a")
    with c.lock:                       # an observe() in progress
        assert pose.live_state(CAM, lock_timeout=0.01) is None
    assert pose.live_state(CAM)["tracks"]["trk_a"]["level"] == "normal"
    assert pose.live_state("never_seen") is None


# ---------------------------------------------------------- /live/behaviour


def test_behaviour_feed_lists_watch_and_alert_newest_first(client, pose, monkeypatch):
    rt1 = _runtime(monkeypatch, CAM, "Aisle 1")
    rt2 = _runtime(monkeypatch, CAM2, "Aisle 2")
    rt1.set_tracks([_entry("trk_w1", 10, 30, 100, 300), _entry("trk_a1", 120, 30, 220, 300),
                    _entry("trk_n1", 240, 30, 330, 300)], NOW, frame_size=(W, H))
    rt2.set_tracks([_entry("trk_w2", 10, 30, 100, 300)], NOW, frame_size=(W, H))
    c1, c2 = _pose_cam(pose, CAM), _pose_cam(pose, CAM2)
    w1 = _track(c1, "trk_w1")
    w1.hands["left"].active_zone, w1.hands["left"].active_started = "z", NOW - 10
    a1 = _track(c1, "trk_a1")
    a1.fired[rules.RULE_SHELF_SWEEPING] = NOW - 1
    a1.incident_ids[rules.RULE_SHELF_SWEEPING] = ("theft_sweep", NOW - 1)
    _track(c1, "trk_n1")
    gone = _track(c1, "trk_gone")                     # pose still holds it; the tracker does not
    gone.fired[rules.RULE_CONCEALMENT] = NOW - 2
    w2 = _track(c2, "trk_w2")
    w2.head_turns.extend(NOW - 5 + 0.5 * i for i in range(6))

    res = client.get("/api/v1/live/behaviour")
    assert res.status_code == 200, res.text
    rows = res.json()["tracks"]
    assert [(r["camera_id"], r["track_id"], r["level"]) for r in rows] == [
        (CAM, "trk_a1", "alert"), (CAM2, "trk_w2", "watch"), (CAM, "trk_w1", "watch")]
    top = rows[0]
    assert top["camera_name"] == "Aisle 1" and top["incident_id"] == "theft_sweep"
    assert top["labels"] == ["Possible shelf sweeping"] and top["since"] == pytest.approx(NOW - 1)
    assert set(top) == {"camera_id", "camera_name", "track_id", "level", "labels", "pattern_score",
                        "incident_id", "since"}


def test_live_json_routes_pass_the_online_access_tunnel(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)   # remote access needs real auth
    for path in ("/api/v1/live/tracks", "/api/v1/live/behaviour"):
        assert not public_exposure.is_live_video_path(path)
        assert public_exposure.PublicExposureMiddleware._refusal(path, "GET") is None


# ------------------------------------------------------------ server overlay


def _px(img, y, x):
    return tuple(int(v) for v in img[y, x])


def test_overlay_draws_a_dashed_coasting_box_without_a_stale_skeleton(pose, monkeypatch):
    rt = _runtime(monkeypatch)
    # Last matched at x=100..228; predicted at x=320..448; 1.5 s since the match.
    rt.set_tracks([_entry("trk_b", 100, 100, 228, 400, fresh=False, last_seen=NOW - 1.5,
                          pred=(320, 100, 448, 400))], NOW, frame_size=(W, H))
    out = render_overlay(np.zeros((H, W, 3), dtype=np.uint8), rt)
    edge = out[150:390, 320]
    drawn = int((edge.max(axis=1) > 0).sum())
    assert 0 < drawn < len(edge), "predicted box edge must be dashed (drawn with gaps)"
    assert not out[150:390, 98:103].any(), "nothing at the last measured box"
    # The skeleton (moved with the box) would put the left shoulder at (340,160)
    # and the left upper arm through (334,180): not drawn after > 1 s.
    assert not out[155:200, 326:345].any(), "stale skeleton drawn"

    # Within 1 s of the match the last pose is still drawn, moved with the box.
    rt.set_tracks([_entry("trk_b", 100, 100, 228, 400, fresh=False, last_seen=NOW + 0.5,
                          pred=(320, 100, 448, 400))], NOW + 1.0, frame_size=(W, H))
    out = render_overlay(np.zeros((H, W, 3), dtype=np.uint8), rt)
    assert out[155:200, 326:345].any(), "short-coast skeleton missing"


@pytest.mark.parametrize("level,colour", [("watch", lae.WATCH_COLOUR), ("alert", lae.ALERT_COLOUR)])
def test_overlay_colours_tracks_by_behaviour_level(pose, monkeypatch, level, colour):
    rt = _runtime(monkeypatch)
    rt.set_tracks([_entry("trk_n", 40, 100, 160, 400), _entry("trk_c", 300, 100, 420, 400)],
                  NOW, frame_size=(W, H))
    c = _pose_cam(pose)
    _track(c, "trk_n")
    st = _track(c, "trk_c")
    if level == "alert":
        st.fired[rules.RULE_CONCEALMENT] = NOW - 5
    else:
        st.hands["right"].active_zone, st.hands["right"].active_started = "z", NOW - 1
    out = render_overlay(np.zeros((H, W, 3), dtype=np.uint8), rt)
    assert _px(out, 300, 300) == colour, "behaviour track box not in its level colour"
    assert _px(out, 300, 40) == (80, 220, 90), "normal confirmed track stays green"
    # The behaviour label is drawn under the box in the same colour.
    assert (out[402:425, 300:330].reshape(-1, 3) == np.array(colour)).all(axis=1).any()
