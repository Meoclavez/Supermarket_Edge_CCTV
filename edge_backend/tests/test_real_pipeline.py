"""Tests for the observation pipeline and the metrics derived from it.

The central property under test is that nothing is fabricated: a metric with no
supporting observation must come back as None, and must never be substituted
with zero or with a plausible constant.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.services.inference_backend import Detection, PersonDetector, _nms
from app.services.store_layout_service import (
    StoreLayoutError,
    point_in_polygon,
    polygon_area_m2,
    validate_polygon,
)
from app.services.tracking_service import CentroidTracker, FloorProjector


# ----------------------------------------------------------------- geometry


def test_polygon_area_is_shoelace():
    square = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 5}, {"x": 0, "y": 5}]
    assert polygon_area_m2(square) == 50.0


def test_point_in_polygon_respects_boundaries():
    square = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10}]
    assert point_in_polygon(5, 5, square)
    assert not point_in_polygon(15, 5, square)
    assert not point_in_polygon(-1, 5, square)


def test_point_in_concave_polygon():
    # An L-shape: the notch must not be counted as inside.
    l_shape = [
        {"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 4},
        {"x": 4, "y": 4}, {"x": 4, "y": 10}, {"x": 0, "y": 10},
    ]
    assert point_in_polygon(2, 8, l_shape)
    assert not point_in_polygon(8, 8, l_shape)


def test_validate_polygon_clamps_to_store_bounds():
    pts = validate_polygon([{"x": -5, "y": 2}, {"x": 99, "y": 2}, {"x": 5, "y": 99}], 50, 30)
    assert pts[0]["x"] == 0.0
    assert pts[1]["x"] == 50.0
    assert pts[2]["y"] == 30.0


def test_validate_polygon_rejects_degenerate_shape():
    with pytest.raises(StoreLayoutError):
        validate_polygon([{"x": 1, "y": 1}, {"x": 2, "y": 2}], 50, 30)


# ------------------------------------------------------------------ tracker


def _det(x1, y1, x2, y2, conf=0.9):
    return Detection(x1=x1, y1=y1, x2=x2, y2=y2, confidence=conf)


def test_tracker_keeps_identity_across_frames():
    t = CentroidTracker("cam_test")
    t.update([_det(100, 100, 150, 300)], now=0.0)
    first = list(t.tracks.keys())[0]
    # Small movement between frames must associate, not spawn a new identity.
    t.update([_det(108, 102, 158, 302)], now=0.2)
    assert list(t.tracks.keys()) == [first]
    assert t.tracks[first].hits == 2


def test_tracker_spawns_separate_identities_for_distant_detections():
    t = CentroidTracker("cam_test")
    t.update([_det(100, 100, 150, 300)], now=0.0)
    t.update([_det(100, 100, 150, 300), _det(600, 100, 650, 300)], now=0.2)
    assert len(t.tracks) == 2


def test_track_is_unconfirmed_until_seen_repeatedly():
    """A one-frame blip must not count as a shopper."""
    t = CentroidTracker("cam_test")
    t.update([_det(10, 10, 60, 200)], now=0.0)
    track = list(t.tracks.values())[0]
    assert not track.confirmed
    for i in range(1, 4):
        t.update([_det(10, 10, 60, 200)], now=i * 0.2)
    assert list(t.tracks.values())[0].confirmed


def test_foot_point_is_bottom_centre_not_box_centre():
    d = _det(100, 100, 200, 400)
    assert d.foot_point == (150.0, 400.0)
    assert d.center == (150.0, 250.0)


# --------------------------------------------------------------- projection


def test_homography_round_trips_known_correspondences():
    img = [(0, 720), (1280, 720), (1280, 400), (0, 400)]
    floor = [(2, 14), (18, 14), (18, 6), (2, 6)]
    H = FloorProjector.estimate_homography(img, floor)
    assert H is not None

    p = FloorProjector()
    p.set_homography("cam", H)
    for (u, v), (X, Y) in zip(img, floor):
        got = p.to_floor("cam", u, v)
        assert got is not None
        assert math.isclose(got[0], X, abs_tol=0.01)
        assert math.isclose(got[1], Y, abs_tol=0.01)


def test_uncalibrated_camera_yields_no_position():
    """An uncalibrated camera must decline to guess, not return the origin."""
    p = FloorProjector()
    assert p.to_floor("never_calibrated", 100, 200) is None
    assert not p.has_homography("never_calibrated")


def test_degenerate_correspondences_are_rejected():
    collinear_img = [(0, 0), (10, 10), (20, 20), (30, 30)]
    collinear_floor = [(0, 0), (1, 1), (2, 2), (3, 3)]
    assert FloorProjector.estimate_homography(collinear_img, collinear_floor) is None


def test_degenerate_matrix_is_not_installed():
    p = FloorProjector()
    p.set_homography("cam", [[0, 0, 0], [0, 0, 0], [0, 0, 1]])
    assert not p.has_homography("cam")


# ---------------------------------------------------------------- detector


def test_nms_suppresses_overlapping_boxes():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105], [500, 500, 600, 600]], dtype=float)
    scores = np.array([0.9, 0.8, 0.7])
    keep = _nms(boxes, scores, 0.5)
    assert sorted(keep) == [0, 2]


def test_detector_without_model_reports_unavailable_and_returns_nothing():
    """The no-backend state must produce zero detections, never placeholders."""
    d = PersonDetector(model_path="/nonexistent/model.onnx")
    assert not d.available
    assert d.last_error is not None
    assert d.detect(np.zeros((480, 640, 3), dtype=np.uint8)) == []
    assert d.status()["available"] is False


def test_detector_loads_bundled_model_and_reports_backend():
    from app.config import settings

    d = PersonDetector(settings.PERSON_MODEL_PATH)
    if not d.available:
        pytest.skip("no inference backend available in this environment")
    status = d.status()
    assert status["backend"] in ("onnxruntime", "hailo")
    assert status["provider"] != "none"


def test_detector_finds_person_in_synthetic_scene():
    """End-to-end sanity: the decode path must map boxes inside the frame."""
    from app.config import settings

    d = PersonDetector(settings.PERSON_MODEL_PATH)
    if not d.available:
        pytest.skip("no inference backend available in this environment")

    frame = np.full((720, 1280, 3), 120, dtype=np.uint8)
    dets = d.detect(frame, conf_threshold=0.25)
    for det in dets:
        assert 0 <= det.x1 < det.x2 <= 1280
        assert 0 <= det.y1 < det.y2 <= 720
        assert 0.0 <= det.confidence <= 1.0


# --------------------------------------------- detection plausibility gates
# These pin the fix for a detector that reported a "person" occupying 48% of
# the frame with a height/width ratio of 1.08 on a camera pointed at an empty
# floor. That false positive was recorded as a real shopper and was then
# indistinguishable from genuine data.


def _detector():
    from app.config import settings

    d = PersonDetector(settings.PERSON_MODEL_PATH)
    if not d.available:
        pytest.skip("no inference backend available in this environment")
    return d


def test_near_square_box_is_rejected_as_person():
    d = _detector()
    frame_area = 1280 * 720
    # 641x694 is the exact shape of the observed false positive.
    assert not d._is_plausible_person(0, 0, 641, 694, frame_area)


def test_upright_person_shaped_box_is_accepted():
    d = _detector()
    frame_area = 1280 * 720
    assert d._is_plausible_person(100, 100, 160, 260, frame_area)   # 60x160


def test_box_covering_most_of_the_frame_is_rejected():
    d = _detector()
    frame_area = 1280 * 720
    # Tall enough to pass the aspect test, but far too large to be one person.
    assert not d._is_plausible_person(0, 0, 500, 700, frame_area)


def test_tiny_box_is_rejected():
    d = _detector()
    assert not d._is_plausible_person(10, 10, 18, 30, 1280 * 720)


def test_default_confidence_threshold_is_not_permissive():
    """The shipped threshold must exclude the observed 0.408 false positive."""
    from app.config import settings

    assert settings.PERSON_CONF_THRESHOLD >= 0.45


def test_empty_scene_produces_no_detections():
    """A flat, featureless frame must yield nothing at the shipped settings."""
    d = _detector()
    frame = np.full((720, 1280, 3), 90, dtype=np.uint8)
    assert d.detect(frame) == []
