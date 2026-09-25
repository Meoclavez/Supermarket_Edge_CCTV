"""Unit tests for the live snapshot: worker -> runtime -> engine.live_snapshot().

No threads are started and no camera is opened. ``CameraWorker._analyse`` is
driven directly with a stubbed detector so the snapshot path is exercised
deterministically. The property under test is that a person appears on the
2D map only when the track is confirmed AND the camera has a homography;
otherwise the box is reported but no floor position is invented.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.config import settings
from app.services import live_analytics_engine as lae
from app.services.inference_backend import Detection
from app.services.live_analytics_engine import (
    CameraRuntime,
    CameraWorker,
    LiveAnalyticsEngine,
    render_overlay,
)
from app.services.tracking_service import floor_projector

CAM = "cam_unit_live_01"


def _skeleton() -> np.ndarray:
    """A standing figure inside the stub box (100,50)-(180,300)."""
    k = np.zeros((17, 3), dtype=np.float32)
    pts = {
        0: (140, 70), 1: (135, 65), 2: (145, 65), 3: (130, 68), 4: (150, 68),
        5: (120, 110), 6: (160, 110), 7: (112, 150), 8: (168, 150),
        9: (108, 190), 10: (172, 190), 11: (125, 190), 12: (155, 190),
        13: (125, 240), 14: (155, 240), 15: (125, 290), 16: (155, 290),
    }
    for i, (x, y) in pts.items():
        k[i] = (x, y, 0.95)
    return k


@pytest.fixture
def stub_detector(monkeypatch):
    """Always report one person (with a skeleton) at a fixed spot in a 640x480 frame."""
    calls = {"n": 0, "conf": []}

    def detect(frame, conf_threshold=None, iou_threshold=None, camera_id=None, **_kw):
        calls["n"] += 1
        calls["conf"].append(conf_threshold)
        calls["frame"] = frame
        return [Detection(x1=100.0, y1=50.0, x2=180.0, y2=300.0, confidence=0.91, keypoints=_skeleton())]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    return calls


class _Result:
    def __init__(self, interactions=None):
        self.interactions = interactions or []


class FakePoseAnalytics:
    """Records what the engine hands to pose_analytics."""

    def __init__(self):
        self.calls: list[dict] = []
        self.resets: list[str] = []
        self.interactions: list = []
        self.raise_with: Exception | None = None

    def observe(self, camera_id, ts, frame_bgr, tracks, objects):
        if self.raise_with is not None:
            raise self.raise_with
        self.calls.append({"camera_id": camera_id, "ts": ts, "shape": frame_bgr.shape,
                           "tracks": list(tracks), "objects": list(objects)})
        out, self.interactions = self.interactions, []
        return _Result(out)

    def reset_camera(self, camera_id):
        self.resets.append(camera_id)


@pytest.fixture(autouse=True)
def fake_pose(monkeypatch):
    """Isolate the engine from the real pose_analytics module."""
    fake = FakePoseAnalytics()
    monkeypatch.setitem(lae._pose_state, "module", fake)
    monkeypatch.setitem(lae._pose_state, "status", "ok")
    monkeypatch.setitem(lae._pose_state, "error", None)
    return fake


@pytest.fixture
def worker():
    engine = LiveAnalyticsEngine()
    rt = CameraRuntime(camera_id=CAM, name="Unit Cam", source="/dev/null")
    engine.runtimes[CAM] = rt
    w = CameraWorker(rt, engine)
    yield w, rt, engine
    floor_projector.set_homography(CAM, None)


def test_runtime_records_frame_size_and_flags():
    rt = CameraRuntime(camera_id=CAM, name="Unit Cam", source="/dev/null")
    d = rt.to_dict()
    assert d["frame_width"] is None and d["frame_height"] is None
    assert d["calibrated"] is False
    assert d["has_frame"] is False

    rt.put_frame(np.zeros((480, 640, 3), dtype=np.uint8))
    d = rt.to_dict()
    assert (d["frame_width"], d["frame_height"]) == (640, 480)
    assert d["has_frame"] is True

    # The snapshot is copied out, never shared.
    rt.set_tracks([{"track_id": "t1"}])
    got = rt.get_tracks()
    got.append({"track_id": "t2"})
    assert len(rt.get_tracks()) == 1


def test_live_snapshot_with_no_cameras_is_empty():
    engine = LiveAnalyticsEngine()
    snap = engine.live_snapshot()
    assert snap["running"] is False
    assert snap["persons"] == []
    assert snap["detections"] == []
    assert snap["persons_total"] == 0
    assert snap["uncalibrated_track_count"] == 0
    assert snap["timestamp"].endswith("Z")


def test_uncalibrated_camera_reports_boxes_but_no_persons(worker, stub_detector):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rt.put_frame(frame)

    # First pass: a brand-new track is tentative.
    w._analyse(frame, now=1000.0)
    tracks = rt.get_tracks()
    assert len(tracks) == 1
    assert tracks[0]["confirmed"] is (settings.TRACK_MIN_HITS <= 1)
    assert tracks[0]["x_m"] is None and tracks[0]["y_m"] is None
    assert (tracks[0]["x1"], tracks[0]["y1"], tracks[0]["x2"], tracks[0]["y2"]) == (100.0, 50.0, 180.0, 300.0)

    # Keep seeing it until it is confirmed.
    for i in range(settings.TRACK_MIN_HITS):
        w._analyse(frame, now=1001.0 + i)
    tracks = rt.get_tracks()
    assert tracks[0]["confirmed"] is True
    assert tracks[0]["hits"] >= settings.TRACK_MIN_HITS
    assert rt.live_track_count == 1

    snap = engine.live_snapshot()
    assert snap["persons"] == []                    # no homography -> no position
    assert snap["persons_total"] == 0
    assert snap["uncalibrated_track_count"] == 1
    assert len(snap["detections"]) == 1
    cam = snap["detections"][0]
    assert cam["camera_id"] == CAM
    assert cam["camera_name"] == "Unit Cam"
    assert cam["calibrated"] is False
    assert cam["live_tracks"] == 1
    assert cam["frame_width"] == 640
    box = cam["boxes"][0]
    assert box["confirmed"] is True
    assert box["confidence"] == 0.91
    assert set(box) == {"track_id", "x1", "y1", "x2", "y2", "confidence", "confirmed", "keypoints"}
    assert len(box["keypoints"]) == 17 and len(box["keypoints"][0]) == 3


def test_calibrated_camera_places_confirmed_track_on_floor(worker, stub_detector):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    rt.put_frame(frame)

    # 100 px == 1 m, no rotation: foot point (140, 300) -> (1.4, 3.0) m.
    floor_projector.set_homography(CAM, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    engine.set_zones([
        {"id": "zone_a", "category": "AISLE",
         "polygon": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 5}, {"x": 0, "y": 5}]},
    ])

    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=2000.0 + i)

    snap = engine.live_snapshot()
    assert snap["uncalibrated_track_count"] == 0
    assert snap["persons_total"] == 1
    person = snap["persons"][0]
    assert person["camera_id"] == CAM
    assert person["camera_name"] == "Unit Cam"
    assert person["x_m"] == pytest.approx(1.4)
    assert person["y_m"] == pytest.approx(3.0)
    assert person["zone_id"] == "zone_a"
    assert person["hits"] >= settings.TRACK_MIN_HITS
    assert person["confidence"] == 0.91
    assert person["age_seconds"] >= 0
    assert snap["detections"][0]["calibrated"] is True
    assert rt.to_dict()["calibrated"] is True


def test_track_lost_drops_out_of_snapshot(worker, stub_detector, monkeypatch):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=3000.0 + i)
    assert len(rt.get_tracks()) == 1

    # The person walks out of view: the detector sees nothing.
    monkeypatch.setattr(lae.person_detector, "detect", lambda f, **kw: [])
    for i in range(settings.TRACK_MAX_AGE_FRAMES + 2):
        w._analyse(frame, now=3100.0 + i)
    assert rt.get_tracks() == []
    assert rt.live_track_count == 0
    assert engine.live_snapshot()["detections"][0]["boxes"] == []


def test_render_overlay_draws_on_a_copy(worker, stub_detector):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=4000.0 + i)

    source = rt.get_frame() if rt.get_frame() is not None else frame.copy()
    out = render_overlay(source, rt)
    assert out.shape == frame.shape
    assert out.any(), "overlay drew nothing"
    # Something was drawn along the box edge, and the original stays black.
    assert out[50, 100:180].any()
    assert not frame.any()


# ---------------------------------------------------------- pose pipeline


def test_engine_asks_for_the_low_confidence_band(worker, stub_detector):
    w, rt, engine = worker
    w._analyse(np.zeros((480, 640, 3), dtype=np.uint8), now=5000.0)
    assert stub_detector["conf"] == [settings.TRACK_LOW_CONF_THRESHOLD]


def test_pose_analytics_sees_only_confirmed_tracks_with_keypoints(worker, stub_detector, fake_pose):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=5100.0 + i)
    assert len(fake_pose.calls) == settings.TRACK_MIN_HITS + 1
    assert fake_pose.calls[0]["tracks"] == [] or all(t.confirmed for t in fake_pose.calls[0]["tracks"])
    last = fake_pose.calls[-1]
    assert last["camera_id"] == CAM and last["shape"] == frame.shape
    (track,) = last["tracks"]
    assert track.confirmed and track.keypoints.shape == (17, 3)
    assert isinstance(last["objects"], list)


def test_interaction_marks_the_open_zone_visit(worker, stub_detector, fake_pose, monkeypatch):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 1.0)
    floor_projector.set_homography(CAM, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    engine.set_zones([
        {"id": "zone_a", "category": "AISLE",
         "polygon": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 5}, {"x": 0, "y": 5}]},
    ])
    for i in range(settings.TRACK_MIN_HITS + 3):
        w._analyse(frame, now=6000.0 + i)
    (track,) = w.tracker.tracks.values()
    assert track.open_visit_id is not None and not track.interacted

    fake_pose.interactions = [(track.track_id, "shelf_1")]
    w._analyse(frame, now=6010.0)
    assert track.interacted is True
    visits, _ = engine.drain()
    phases = [v.phase for v in visits]
    assert "interact" in phases
    interact = next(v for v in visits if v.phase == "interact")
    assert interact.visit_id == track.open_visit_id and interact.interacted is True

    # A second interaction in the same visit does not re-publish.
    fake_pose.interactions = [(track.track_id, "shelf_1")]
    w._analyse(frame, now=6011.0)
    assert all(v.phase != "interact" for v in engine.drain()[0])


def test_pose_analytics_failure_never_stops_the_pipeline(worker, stub_detector, fake_pose):
    w, rt, engine = worker
    fake_pose.raise_with = RuntimeError("boom")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=7000.0 + i)
    assert rt.live_track_count == 1


def test_missing_pose_analytics_module_is_tolerated(worker, stub_detector, monkeypatch):
    w, rt, engine = worker
    monkeypatch.setitem(lae._pose_state, "module", None)
    monkeypatch.setitem(lae._pose_state, "status", "unavailable")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=7100.0 + i)
    assert rt.live_track_count == 1
    assert engine.status()["pose_analytics"]["status"] == "unavailable"


def test_worker_stop_resets_pose_state(worker, fake_pose):
    w, rt, engine = worker
    w.stop()
    w.run()      # returns at once because stop is set; runs the shutdown path
    assert fake_pose.resets == [CAM]


def test_render_overlay_draws_the_skeleton(worker, stub_detector):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=8000.0 + i)
    out = render_overlay(frame.copy(), rt)
    # Left upper arm runs from the shoulder (120,110) to the elbow (112,150):
    # its midpoint is inside the box, away from the box edges and the label.
    assert out[130, 114:120].any(), "arm segment not drawn"
    # A wrist marker is drawn at (108,190).
    assert out[190, 108].any()


def test_live_snapshot_hides_keypoints_of_a_coasting_track(worker, stub_detector, monkeypatch):
    w, rt, engine = worker
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=9000.0 + i)
    monkeypatch.setattr(lae.person_detector, "detect", lambda f, **kw: [])
    w._analyse(frame, now=9010.0)
    (box,) = engine.live_snapshot()["detections"][0]["boxes"]
    assert box["keypoints"] is None


# ------------------------------------------------- per-camera feature flags


@pytest.fixture
def flags():
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    def set_flags(**kw):
        feature_manager.set_camera_features(CAM, CameraFeatureConfig(**kw))

    set_flags()
    yield set_flags
    feature_manager.remove_camera(CAM)


def _calibrate_with_zone(engine):
    floor_projector.set_homography(CAM, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    engine.set_zones([
        {"id": "zone_a", "category": "AISLE",
         "polygon": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 5}, {"x": 0, "y": 5}]},
    ])


def test_people_counting_off_records_no_visits_and_no_tracks(worker, stub_detector, flags, monkeypatch):
    w, rt, engine = worker
    flags(people_counting=False)
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 0.0)
    _calibrate_with_zone(engine)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 3):
        w._analyse(frame, now=10000.0 + i)
    (track,) = w.tracker.tracks.values()
    assert track.confirmed and track.current_zone_id is None and track.floor_points == []
    assert rt.to_dict()["analysis_flags"]["people_counting"] is False
    # Tracking still runs (overlay, shelf/theft analytics) ...
    assert rt.live_track_count == 1
    for t in w.tracker.flush_all():
        engine.close_track(t)
    # ... but nothing about footfall or dwell is written.
    assert engine.drain() == ([], [])


def test_switching_counting_off_mid_visit_closes_the_open_visit(worker, stub_detector, flags, monkeypatch):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 0.0)
    _calibrate_with_zone(engine)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(settings.TRACK_MIN_HITS + 2):
        w._analyse(frame, now=11000.0 + i)
    (track,) = w.tracker.tracks.values()
    visit_id = track.open_visit_id
    assert visit_id is not None
    engine.drain()

    flags(people_counting=False)
    w._analyse(frame, now=11100.0)
    visits, tracks = engine.drain()
    assert [(v.phase, v.visit_id) for v in visits] == [("close", visit_id)]
    assert track.current_zone_id is None and track.open_visit_id is None


def test_all_analysis_off_skips_inference(worker, stub_detector, flags):
    w, rt, engine = worker
    flags(people_counting=False, shelf_interaction=False, theft_detection=False)
    w._analyse(np.zeros((480, 640, 3), dtype=np.uint8), now=12000.0)
    assert stub_detector["n"] == 0
    assert rt.get_tracks() == [] and rt.live_track_count == 0


# ------------------------------------------------------------ privacy masks


@pytest.fixture
def masks():
    from app.services.ai_zone_service import ai_zone_service

    created: list[str] = []

    def add(points, mode="BLACKOUT", **extra):
        rec = ai_zone_service.add_exclusion({
            "id": f"mask_unit_{len(created)}", "name": "unit", "camera_id": CAM,
            "points": points, "mask_mode": mode, "enabled": True, **extra,
        })
        created.append(rec["id"])
        return rec

    yield add
    for mid in created:
        ai_zone_service.delete_exclusion(mid)


# Normalised square covering pixels x 320..640, y 0..240 of a 640x480 frame.
TOP_RIGHT = [{"x": 0.5, "y": 0.0}, {"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 0.5}, {"x": 0.5, "y": 0.5}]


def _textured(h=480, w=640):
    rng = np.random.default_rng(7)
    return rng.integers(40, 255, size=(h, w, 3), dtype=np.uint8)


def test_blackout_mask_applies_to_served_frames_not_to_analysis(worker, masks):
    w, rt, engine = worker
    masks(TOP_RIGHT, "BLACKOUT")
    frame = _textured()
    rt.put_frame(frame)

    shown = engine.get_frame(CAM)
    assert not shown[10:230, 330:630].any(), "masked region still visible"
    assert np.array_equal(shown[250:, :], frame[250:, :]), "unmasked area changed"
    assert np.array_equal(rt.get_raw_frame(), frame)
    assert np.array_equal(engine.get_raw_frame(CAM), frame)


@pytest.mark.parametrize("mode", ["BLUR", "MOSAIC", "COLOR"])
def test_other_privacy_modes_change_only_the_masked_area(worker, masks, mode):
    w, rt, engine = worker
    masks(TOP_RIGHT, mode, mask_color_bgr=(0, 0, 255))
    frame = _textured()
    rt.put_frame(frame)
    shown = rt.get_frame()
    inside, outside = shown[20:220, 340:620], shown[260:, :]
    assert np.abs(inside.astype(int) - frame[20:220, 340:620]).mean() > 20
    assert np.array_equal(outside, frame[260:, :])


def test_clip_frames_are_masked_detector_frame_is_not(worker, stub_detector, fake_pose, masks):
    from app.services.clip_recorder import clip_recorder_service

    w, rt, engine = worker
    masks(TOP_RIGHT, "BLACKOUT")
    frame = _textured()
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=13000.0 + i)
    assert np.array_equal(stub_detector["frame"], frame), "detector must see the raw frame"
    assert fake_pose.calls, "pose analytics was not called"

    w._push_clip_buffer(frame)
    clip = clip_recorder_service.get_or_create_buffer(CAM, fps=5).get_latest_frame_copy()
    assert clip is not None and not clip[10:230, 330:630].any()


def test_pose_analytics_receives_the_masked_frame(worker, stub_detector, masks, monkeypatch):
    seen = {}

    class Grab(FakePoseAnalytics):
        def observe(self, camera_id, ts, frame_bgr, tracks, objects):
            seen["frame"] = frame_bgr.copy()
            return _Result()

    monkeypatch.setitem(lae._pose_state, "module", Grab())
    w, rt, engine = worker
    masks(TOP_RIGHT, "BLACKOUT")
    w._analyse(_textured(), now=13500.0)
    assert not seen["frame"][10:230, 330:630].any()


def test_ai_ignore_drops_people_standing_inside_but_leaves_video(worker, stub_detector, masks):
    w, rt, engine = worker
    # The stub person's foot point is (140, 300): inside this normalised region.
    masks([{"x": 0.1, "y": 0.5}, {"x": 0.4, "y": 0.5}, {"x": 0.4, "y": 0.8}, {"x": 0.1, "y": 0.8}], "AI_IGNORE")
    frame = _textured()
    rt.put_frame(frame)
    for i in range(settings.TRACK_MIN_HITS + 1):
        w._analyse(frame, now=14000.0 + i)
    assert rt.get_tracks() == [] and rt.detections_last == 0
    assert np.array_equal(rt.get_frame(), frame), "AI_IGNORE must not alter the picture"


def test_malformed_mask_fails_closed(worker, masks):
    w, rt, engine = worker
    masks([{"x": 0.5, "y": 0.0}, {"x": 1.0}, {"x": 1.0, "y": 0.5}], "BLUR")
    rt.put_frame(_textured())
    assert not rt.get_frame().any(), "a broken mask must black the frame out, not leak it"


def test_snapshot_route_masks_and_draws_skeleton(worker, masks, monkeypatch):
    import cv2
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import cameras as cameras_module

    w, rt, engine = worker
    monkeypatch.setattr(
        cameras_module.person_detector, "detect",
        lambda f, **kw: [Detection(100.0, 50.0, 180.0, 300.0, 0.91, keypoints=_skeleton())],
    )
    masks(TOP_RIGHT, "BLACKOUT")
    frame = np.full((480, 640, 3), 255, dtype=np.uint8)
    rt.put_frame(frame)
    with TestClient(app) as client:
        # Registered after startup: the supervisor's first reconcile removes
        # runtimes for cameras that are not in the database.
        monkeypatch.setitem(lae.live_engine.runtimes, CAM, rt)
        plain = client.get(f"/api/v1/cameras/{CAM}/snapshot?annotate=false")
        annotated = client.get(f"/api/v1/cameras/{CAM}/snapshot?annotate=true")
    for res in (plain, annotated):
        assert res.status_code == 200, res.text
        assert res.headers["X-Frame-Source"] == "live"
        img = cv2.imdecode(np.frombuffer(res.content, np.uint8), cv2.IMREAD_COLOR)
        assert img[20:220, 340:620].max() < 40, "privacy mask missing from snapshot"
    img = cv2.imdecode(np.frombuffer(annotated.content, np.uint8), cv2.IMREAD_COLOR)
    # Arm segment between shoulder (120,110) and elbow (112,150) drawn in green-ish.
    px = img[130, 116].astype(int)
    assert px[0] < 100 and px[1] > 150, f"skeleton not drawn: {px}"   # BGR (0,255,157) on white
