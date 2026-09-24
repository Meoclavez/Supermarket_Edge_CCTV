"""Tests for the observation pipeline and the metrics derived from it.

The central property under test is that nothing is fabricated: a metric with no
supporting observation must come back as None, and must never be substituted
with zero or with a plausible constant.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.services.inference_backend import (
    Detection,
    PersonDetector,
    _nms,
    _parse_class_filter,
    build_model_spec,
    decode_output,
)
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


# ------------------------------------------------------------ tracker (v2)
# ByteTrack-style association: Kalman prediction, low-confidence second
# stage, keypoints carried on the track.


def test_low_confidence_box_extends_a_track_but_never_starts_one():
    t = CentroidTracker("cam_test")
    for i in range(3):
        t.update([_det(100, 100, 150, 300, conf=0.9)], now=float(i))
    (tid,) = t.tracks.keys()
    hits = t.tracks[tid].hits
    # Partly occluded: the detector is unsure, but the box is where the
    # track is. The identity survives instead of accruing a miss.
    t.update([_det(102, 101, 152, 301, conf=0.3)], now=3.0)
    assert list(t.tracks) == [tid]
    assert t.tracks[tid].hits == hits + 1
    assert t.tracks[tid].misses == 0
    # A lone low-confidence box elsewhere is not a new shopper.
    t.update([_det(100, 100, 150, 300, conf=0.9), _det(600, 100, 650, 300, conf=0.3)], now=4.0)
    assert list(t.tracks) == [tid]


def test_kalman_prediction_bridges_a_gap_in_detections():
    """A walker missed for two frames is re-acquired where they should be.

    The box moves 20 px per frame and is 60 px wide, so after a two-frame gap
    the new box does not overlap the last seen one at all: an IoU tracker
    without motion prediction would start a new identity.
    """
    t = CentroidTracker("cam_test")
    x = 100.0
    for i in range(6):
        t.update([_det(x, 100, x + 60, 300)], now=float(i))
        x += 20.0
    (tid,) = t.tracks.keys()
    last = t.tracks[tid].bbox
    t.update([], now=6.0)
    t.update([], now=7.0)
    x += 40.0
    assert x >= last[2], "test setup: new box must not overlap the last one"
    t.update([_det(x, 100, x + 60, 300)], now=8.0)
    assert list(t.tracks) == [tid]
    assert t.tracks[tid].misses == 0


def test_track_carries_smoothed_keypoints():
    k1 = np.zeros((17, 3), dtype=np.float32)
    k1[:, 0], k1[:, 1], k1[:, 2] = 120.0, 200.0, 0.9
    k1[0, 2] = 0.1                                  # nose not visible
    k2 = k1.copy()
    k2[:, 0] = 130.0
    k2[0, 0] = 500.0                                # invisible point jumps

    t = CentroidTracker("cam_test")
    t.update([Detection(100, 100, 150, 300, 0.9, keypoints=k1)], now=0.0)
    t.update([Detection(101, 100, 151, 300, 0.9, keypoints=k2)], now=1.0)
    (track,) = t.tracks.values()
    assert track.keypoints.shape == (17, 3)
    assert track.keypoints_fresh
    # Visible in both: EMA between 120 and 130, weighted towards the new one.
    assert 120.0 < track.keypoints[9, 0] < 130.0
    assert track.keypoints[9, 0] > 125.0
    # Not visible: taken as observed, never blended.
    assert track.keypoints[0, 0] == 500.0

    t.update([], now=2.0)
    assert not track.keypoints_fresh


def test_tracker_exposes_contract_fields():
    t = CentroidTracker("cam_test")
    t.update([_det(100, 100, 150, 300)], now=0.0)
    (track,) = t.tracks.values()
    for name in ("track_id", "bbox", "keypoints", "foot_point", "floor_xy", "age", "hits", "confirmed"):
        assert hasattr(track, name)


# ---------------------------------------------------------------- detector

MODELS = Path(__file__).resolve().parent.parent / "models"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _image(name: str) -> np.ndarray:
    import cv2

    for d in (FIXTURES, Path(os.environ.get("POSE_TEST_IMAGES_DIR", "/nonexistent"))):
        p = d / name
        if p.exists():
            return cv2.imread(str(p))
    pytest.skip(f"test image {name} not available")


def test_nms_suppresses_overlapping_boxes():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105], [500, 500, 600, 600]], dtype=float)
    scores = np.array([0.9, 0.8, 0.7])
    keep = _nms(boxes, scores, 0.5)
    assert sorted(keep) == [0, 2]


def test_module_import_does_not_load_models():
    """Importing the detector or reading hardware status must not load a model."""
    code = (
        "import app.services.inference_backend as ib, app.services.hardware_detector as hd\n"
        "p = hd.current_hardware_profile()\n"
        "assert ib.person_detector.session is None, 'model loaded at import'\n"
        "assert not ib.person_detector.initialised\n"
        "assert p.inference_backend == 'not_initialised', p.inference_backend\n"
        "print('ok')\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr[-2000:]
    assert res.stdout.strip().endswith("ok")


def test_detector_without_model_reports_unavailable_and_returns_nothing():
    """The no-backend state must produce zero detections, never placeholders."""
    d = PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")
    assert not d.available
    status = d.initialise()
    assert not d.available
    assert d.last_error is not None and "not found" in d.last_error
    assert d.detect(np.zeros((480, 640, 3), dtype=np.uint8)) == []
    assert status["available"] is False
    assert status["backend"] == "unavailable"
    assert all(not a["ok"] for a in status["provider_attempts"])


def test_hailo_is_never_claimed_without_a_runner(monkeypatch, tmp_path):
    """A fitted Hailo device with no runner must not mark the detector available."""
    from app.config import settings

    fake_dev = tmp_path / "hailo0"
    fake_dev.write_text("")
    monkeypatch.setattr(settings, "HAILO_DEVICE", str(fake_dev))
    d = PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")
    status = d.initialise()
    assert status["hailo"]["device_present"] is True
    assert status["hailo"]["runner"] is False and status["hailo"]["selected"] is False
    assert status["backend"] != "hailo"
    assert status["available"] is False


# --------------------------------------------------- output layout decoding


class _IO:
    def __init__(self, name, shape, type_="tensor(float)"):
        self.name, self.shape, self.type = name, shape, type_


def test_model_spec_is_decided_from_metadata_and_shape():
    inp = [_IO("images", [1, 3, 640, 640])]
    pose = build_model_spec(Path("p.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                            inp, [_IO("output0", [1, 56, 8400])])
    assert (pose.task, pose.layout, pose.num_classes, pose.kpt_shape) == ("pose", "cf", 1, (17, 3))

    names80 = str({i: f"c{i}" for i in range(80)})
    det = build_model_spec(Path("d.onnx"), {"names": names80}, inp, [_IO("output0", [1, 84, 8400])])
    assert (det.task, det.layout, det.num_classes) == ("detect", "cf", 80)

    v5 = build_model_spec(Path("v5.onnx"), {}, inp, [_IO("output0", [1, 25200, 85])])
    assert (v5.task, v5.layout, v5.num_classes) == ("detect", "v5", 80)

    half = build_model_spec(Path("h.onnx"), {}, [_IO("images", [1, 3, 640, 640], "tensor(float16)")],
                            [_IO("output0", [1, 84, 8400])])
    assert half.input_dtype == np.float16

    # Anchor-major export of the same pose head.
    cl = build_model_spec(Path("p.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                          inp, [_IO("output0", [1, 8400, 56])])
    assert (cl.task, cl.layout) == ("pose", "cl")

    with pytest.raises(ValueError):
        # 60 channels cannot be 4 box + 1 class + 17x3 keypoints.
        build_model_spec(Path("bad.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                         inp, [_IO("output0", [1, 60, 8400])])


class _FakeSession:
    def __init__(self, raw):
        self.raw = raw

    def run(self, _outputs, feeds):
        (blob,) = feeds.values()
        assert blob.shape == (1, 3, 640, 640)
        return [self.raw]


def _pose_detector_with_output(raw) -> PersonDetector:
    d = PersonDetector(model_path="/nonexistent/pose.onnx", object_model_path="")
    d.spec = build_model_spec(Path("pose.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                              [_IO("images", [1, 3, 640, 640])], [_IO("output0", list(raw.shape))])
    d.session = _FakeSession(raw)
    d._initialised = True
    return d


def test_pose_decoder_maps_boxes_and_keypoints_back_through_the_letterbox():
    """Synthetic [1,56,N] tensor: one person, a duplicate, and a weak anchor.

    A 1280x720 frame letterboxes to 640x640 with scale 0.5 and 140 px of
    vertical padding, so network (x, y) maps to ((x - 0) / 0.5, (y - 140) / 0.5).
    """
    n = 8
    raw = np.zeros((1, 56, n), dtype=np.float32)

    def anchor(i, cx, cy, w, h, score, wrist=(300.0, 330.0)):
        raw[0, 0:4, i] = (cx, cy, w, h)
        raw[0, 4, i] = score
        k = np.zeros((17, 3), dtype=np.float32)
        k[:, 0], k[:, 1], k[:, 2] = cx, cy, 0.9
        k[0] = (cx, cy - 100, 0.1)                 # nose: not visible
        k[9] = (wrist[0], wrist[1], 0.95)          # left wrist
        raw[0, 5:, i] = k.reshape(-1)

    anchor(0, 320, 320, 100, 240, 0.90)
    anchor(1, 322, 321, 100, 240, 0.80)            # duplicate: NMS removes it
    anchor(2, 100, 300, 40, 100, 0.20)             # below threshold

    d = _pose_detector_with_output(raw)
    dets = d.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert len(dets) == 1
    det = dets[0]
    assert (det.x1, det.y1, det.x2, det.y2) == pytest.approx((540, 120, 740, 600))
    assert det.confidence == pytest.approx(0.9)
    assert det.keypoints.shape == (17, 3)
    assert tuple(det.keypoints[9]) == pytest.approx((600.0, 380.0, 0.95))
    assert det.keypoints[0, 2] == pytest.approx(0.1)
    # The status timings are measured from the call, not invented.
    assert d.status()["frames_inferred"] == 1
    assert d.status()["avg_infer_ms"] is not None


def test_detect_layout_decoder_keeps_only_people():
    """v8-style [1, 4+C, N]: a person anchor and a confidently-detected handbag."""
    raw = np.zeros((1, 84, 4), dtype=np.float32)
    raw[0, 0:4, 0] = (320, 320, 100, 240)
    raw[0, 4 + 0, 0] = 0.8                         # person
    raw[0, 0:4, 1] = (100, 300, 60, 150)
    raw[0, 4 + 26, 1] = 0.9                        # handbag
    names80 = str({i: f"c{i}" for i in range(80)})
    spec = build_model_spec(Path("d.onnx"), {"names": names80}, [_IO("images", [1, 3, 640, 640])],
                            [_IO("o", [1, 84, 4])])
    assert spec.layout == "cf"
    boxes, scores, cls, kpts = decode_output(raw, spec, 0.5, class_ids={0})
    assert len(boxes) == 1 and cls[0] == 0 and kpts is None
    boxes, scores, cls, _ = decode_output(raw, spec, 0.5, class_ids={26})
    assert list(cls) == [26]


def test_object_class_filter_accepts_names_and_ids():
    names = {0: "person", 24: "backpack", 26: "handbag", 67: "cell phone"}
    assert _parse_class_filter("backpack, 67, not-a-class, person", names) == {24, 67}


# ----------------------------------------------------- provider fallback


class _FakeOrt:
    """Minimal onnxruntime stand-in that mimics its *silent* provider fallback."""

    class SessionOptions:
        graph_optimization_level = None
        log_severity_level = 0

    class GraphOptimizationLevel:
        ORT_ENABLE_ALL = 99

    def __init__(self, broken: set[str]):
        self.broken = broken
        self.created: list[tuple[str, str]] = []

    def get_available_providers(self):
        return ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]

    def InferenceSession(self, path, sess_options=None, providers=()):
        names = [p[0] if isinstance(p, tuple) else p for p in providers]
        self.created.append((Path(path).name, names[0]))
        working = [n for n in names if n not in self.broken]
        if working != names:
            print(f"*** EP Error *** EP Error simulated failure of {names[0]} when using {names}")
        return _FakeOrtSession(working)


class _FakeOrtSession:
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return self._providers

    def get_modelmeta(self):
        class M:
            custom_metadata_map = {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"}
        return M()

    def get_inputs(self):
        return [_IO("images", [1, 3, 640, 640])]

    def get_outputs(self):
        return [_IO("output0", [1, 56, 8400])]


@pytest.fixture
def fake_models(monkeypatch, tmp_path):
    from app.config import settings

    for name in ("big-pose.onnx", "small-pose.onnx"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(settings, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "big-pose.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_CPU", "small-pose.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_PATH", "")
    monkeypatch.setattr(settings, "INFERENCE_DISABLED_PROVIDERS", "")
    return tmp_path


def test_tensorrt_fallback_is_recorded_and_cuda_is_used(fake_models):
    ort = _FakeOrt(broken={"TensorrtExecutionProvider"})
    d = PersonDetector(object_model_path="")
    d.available_providers = ort.get_available_providers()
    d._select(ort)
    assert d.provider == "cuda" and d.execution_provider == "CUDAExecutionProvider"
    assert d.model_path.name == "big-pose.onnx"
    trt, cuda = d.provider_attempts[0], d.provider_attempts[1]
    assert trt["provider"] == "tensorrt" and trt["ok"] is False
    assert "landed on CUDAExecutionProvider" in trt["reason"]
    assert "simulated failure" in trt["reason"]
    assert cuda == {"provider": "cuda", "ok": True, "model": "big-pose.onnx"}


def test_gpu_failure_falls_back_to_cpu_with_the_small_model(fake_models):
    ort = _FakeOrt(broken={"TensorrtExecutionProvider", "CUDAExecutionProvider"})
    d = PersonDetector(object_model_path="")
    d.available_providers = ort.get_available_providers()
    d._select(ort)
    assert d.provider == "cpu"
    assert d.model_path.name == "small-pose.onnx"
    assert [a["ok"] for a in d.provider_attempts] == [False, False, True]
    assert "landed on CPUExecutionProvider" in d.provider_attempts[1]["reason"]


def test_pose_model_path_pins_the_model(fake_models, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "POSE_MODEL_PATH", str(fake_models / "small-pose.onnx"))
    ort = _FakeOrt(broken={"TensorrtExecutionProvider"})
    d = PersonDetector(object_model_path="")
    d.available_providers = ort.get_available_providers()
    d._select(ort)
    assert d.provider == "cuda" and d.model_path.name == "small-pose.onnx"


# ------------------------------------------------ real models, real images


@pytest.fixture(scope="module")
def live_detector():
    """The application's detector, initialised exactly as the lifespan does."""
    from app.services.inference_backend import initialise_inference, person_detector

    status = initialise_inference()
    if not status["available"]:
        pytest.skip(f"no inference backend available: {status['error']}")
    return person_detector


@pytest.fixture(scope="module")
def n_pose():
    path = MODELS / "yolo26n-pose.onnx"
    if not path.exists():
        pytest.skip("yolo26n-pose.onnx not present")
    d = PersonDetector(model_path=path, object_model_path="")
    if not d.initialise()["available"]:
        pytest.skip(f"yolo26n-pose could not be loaded: {d.last_error}")
    return d


def test_live_detector_reports_the_provider_it_really_runs(live_detector):
    s = live_detector.status()
    assert s["backend"] == "onnxruntime" and s["available"] is True
    ok = [a for a in s["provider_attempts"] if a["ok"]]
    assert len(ok) == 1 and ok[0]["provider"] == s["provider"]
    assert s["execution_provider"].lower().startswith(
        {"tensorrt": "tensorrt", "cuda": "cuda", "cpu": "cpu"}.get(s["provider"], "")
    )
    assert s["keypoints_supported"] is True and s["num_keypoints"] == 17
    assert s["warmup_ms"] is not None and s["warmup_ms"] > 0
    from app.config import settings

    if not settings.POSE_MODEL_PATH:
        accel = s["provider"] not in ("cpu", "openvino")
        ladder = settings.POSE_MODEL_LADDER_GPU if accel else settings.POSE_MODEL_LADDER_CPU
        allowed = [n.strip() for n in ladder.split(",") if n.strip()]
        allowed += [settings.POSE_MODEL_GPU if accel else settings.POSE_MODEL_CPU]
        # The ladder picks by measured latency; whatever it picked is reported.
        sel = s["model_selection"]
        assert s["model"] in allowed and sel["chosen"] == s["model"], (s["model"], sel)
        chosen = next(t for t in sel["tried"] if t["model"] == s["model"])
        assert chosen["steady_ms"] > 0
        if sel["reason"] != "no candidate fits the budget; fastest measured":
            assert chosen["steady_ms"] <= sel["budget_ms"]


def test_tensorrt_without_libraries_is_recorded_on_this_machine(live_detector):
    s = live_detector.status()
    if "TensorrtExecutionProvider" not in s["available_providers"]:
        pytest.skip("this onnxruntime build has no TensorRT provider")
    trt = next(a for a in s["provider_attempts"] if a["provider"] == "tensorrt")
    if trt["ok"]:
        assert s["provider"] == "tensorrt"
    else:
        assert trt["reason"] and s["provider"] != "tensorrt"


def _assert_plausible_skeleton(det: Detection, frame_shape, margin: float = 0.15):
    h, w = frame_shape[:2]
    k = det.keypoints
    assert k is not None and k.shape == (17, 3)
    assert np.all((k[:, 0] >= 0) & (k[:, 0] <= w) & (k[:, 1] >= 0) & (k[:, 1] <= h))
    assert np.all((k[:, 2] >= 0) & (k[:, 2] <= 1))
    bw, bh = det.x2 - det.x1, det.y2 - det.y1
    for i in (9, 10):  # wrists inside or near the person box
        x, y, v = k[i]
        if v >= 0.5:
            assert det.x1 - margin * bw <= x <= det.x2 + margin * bw
            assert det.y1 - margin * bh <= y <= det.y2 + margin * bh
    vis = k[:, 2] >= 0.5
    if vis[5] and vis[6] and vis[11] and vis[12]:
        assert (k[5, 1] + k[6, 1]) / 2 < (k[11, 1] + k[12, 1]) / 2, "shoulders below hips"


def test_bus_image_people_have_plausible_skeletons(n_pose):
    img = _image("bus.jpg")
    dets = n_pose.detect(img)
    assert len(dets) >= 3, [d.to_dict() for d in dets]
    for det in dets:
        _assert_plausible_skeleton(det, img.shape)
    # At least one pedestrian is fully visible: most joints are seen.
    assert max(int((d.keypoints[:, 2] >= 0.5).sum()) for d in dets) >= 14


def test_zidane_image_both_people_found_with_relaxed_cctv_gates(n_pose, monkeypatch):
    """Close-up broadcast framing breaks the ceiling-camera size gate.

    With the shipped gates only the standing figure passes (the other fills
    over half the frame); with the gates relaxed both people are found and
    both skeletons are anatomically consistent.
    """
    from app.config import settings

    img = _image("zidane.jpg")
    shipped = n_pose.detect(img)
    assert len(shipped) >= 1
    monkeypatch.setattr(settings, "PERSON_MAX_FRAME_FRACTION", 1.0)
    monkeypatch.setattr(settings, "PERSON_MIN_ASPECT_RATIO", 0.0)
    dets = n_pose.detect(img)
    assert len(dets) == 2, [d.to_dict() for d in dets]
    for det in dets:
        _assert_plausible_skeleton(det, img.shape)
        assert det.keypoints[0, 2] >= 0.5, "face should be visible in this image"


def test_object_model_reports_only_configured_classes(live_detector):
    s = live_detector.status()["object_detection"]
    if not s["available"]:
        pytest.skip(f"object model unavailable: {s['error']}")
    objs = live_detector.detect_objects(_image("bus.jpg"))
    for o in objs:
        assert o.label in s["classes"]
        assert o.class_id != 0


def test_detector_output_stays_inside_the_frame(live_detector):
    frame = np.full((720, 1280, 3), 120, dtype=np.uint8)
    for det in live_detector.detect(frame, conf_threshold=0.25):
        assert 0 <= det.x1 < det.x2 <= 1280
        assert 0 <= det.y1 < det.y2 <= 720
        assert 0.0 <= det.confidence <= 1.0


# --------------------------------------------- detection plausibility gates
# These pin the fix for a detector that reported a "person" occupying 48% of
# the frame with a height/width ratio of 1.08 on a camera pointed at an empty
# floor. That false positive was recorded as a real shopper and was then
# indistinguishable from genuine data.


def _detector():
    # The gates need no model; a bare instance must not load anything.
    return PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")


def test_near_square_box_is_rejected_as_person():
    d = _detector()
    frame_area = 1280 * 720
    # 641x694 is the exact shape of the observed false positive.
    assert not d._is_plausible_person(0, 0, 641, 694, frame_area)


def test_near_square_box_with_a_coherent_torso_is_accepted():
    """A bending shopper is square-ish, but the pose head sees the body."""
    d = _detector()
    k = np.zeros((17, 3), dtype=np.float32)
    k[5] = (120, 150, 0.9)
    k[6] = (180, 150, 0.9)
    k[11] = (130, 230, 0.8)
    assert d._is_plausible_person(100, 100, 220, 240, 1280 * 720, k)
    k_bad = k.copy()
    k_bad[11, 1] = 120                              # hip above shoulders
    assert not d._is_plausible_person(100, 100, 220, 240, 1280 * 720, k_bad)
    assert not d._is_plausible_person(100, 100, 220, 240, 1280 * 720, None)


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


def test_empty_scene_produces_no_detections(live_detector):
    """A flat, featureless frame must yield nothing at the shipped settings."""
    frame = np.full((720, 1280, 3), 90, dtype=np.uint8)
    assert live_detector.detect(frame) == []
