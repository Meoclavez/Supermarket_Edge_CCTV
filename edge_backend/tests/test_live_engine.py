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


@pytest.fixture
def stub_detector(monkeypatch):
    """Always report one person at a fixed spot in a 640x480 frame."""
    calls = {"n": 0}

    def detect(frame):
        calls["n"] += 1
        return [Detection(x1=100.0, y1=50.0, x2=180.0, y2=300.0, confidence=0.91)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    return calls


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
    assert set(box) == {"track_id", "x1", "y1", "x2", "y2", "confidence", "confirmed"}


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
    monkeypatch.setattr(lae.person_detector, "detect", lambda f: [])
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
