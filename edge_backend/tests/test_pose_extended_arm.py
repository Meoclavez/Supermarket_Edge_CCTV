"""Extended / raised arm handling: plausibility gate, keypoint smoothing,
latency-budget model selection and the optional top-down refiner.

Background: a shopper reaching to a top shelf was not recognised. Measured on
609 COCO val2017 raised/extended-arm persons, the causes were (1) the aspect
gate rejecting wide upper-body boxes whose hips are hidden, (2) the keypoint
EMA delaying a fast wrist lift by one or two detection frames, and (3) small
models at 640 px missing far wrists. These tests pin the fixes.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services.inference_backend import Detection, PersonDetector, _has_coherent_torso
from app.services.tracking_service import ARM_JOINTS, ByteTracker, smooth_keypoints

MODELS = Path(__file__).resolve().parent.parent / "models"


def _kp(points: dict[int, tuple[float, float, float]]) -> np.ndarray:
    k = np.zeros((17, 3), dtype=np.float32)
    for i, v in points.items():
        k[i] = v
    return k


# A shopper seen from the waist up (hips behind the shelf edge), right arm
# stretched up and out to a top shelf: box 260 wide x 240 tall.
REACH_UPPER_BODY = _kp({
    0: (150, 60, 0.95), 1: (145, 55, 0.9), 2: (155, 55, 0.9),
    5: (120, 100, 0.95), 6: (180, 100, 0.95),
    7: (110, 150, 0.9), 9: (112, 195, 0.85),          # left arm hanging
    8: (240, 60, 0.9), 10: (330, 20, 0.8),            # right arm up and out
})


def _detector():
    return PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")


# ------------------------------------------------------------------ the gate


def test_wide_upper_body_reach_passes_the_gate():
    d = _detector()
    # 260 x 240: h/w = 0.92 < PERSON_MIN_ASPECT_RATIO, no hip visible.
    assert (240 / 260) < settings.PERSON_MIN_ASPECT_RATIO
    assert d._is_plausible_person(90, 0, 350, 240, 1920 * 1080, REACH_UPPER_BODY)


def test_upper_body_rule_needs_head_above_shoulders_and_an_elbow():
    no_elbow = REACH_UPPER_BODY.copy()
    no_elbow[[7, 8], 2] = 0.1
    assert not _has_coherent_torso(no_elbow)
    head_below = REACH_UPPER_BODY.copy()
    head_below[[0, 1, 2], 1] = 140                     # "head" under the shoulder line
    assert not _has_coherent_torso(head_below)
    no_head = REACH_UPPER_BODY.copy()
    no_head[[0, 1, 2], 2] = 0.2
    assert not _has_coherent_torso(no_head)


def test_hips_above_shoulders_are_still_rejected_even_with_a_head():
    k = REACH_UPPER_BODY.copy()
    k[11] = (130, 40, 0.9)                             # a "hip" above the shoulders
    assert not _has_coherent_torso(k)


def test_low_confidence_texture_keypoints_do_not_open_the_gate():
    k = REACH_UPPER_BODY.copy()
    k[:, 2] = 0.3                                      # what shelving/floor texture yields
    d = _detector()
    assert not d._is_plausible_person(90, 0, 350, 240, 1920 * 1080, k)


# ------------------------------------------------------------- smoothing


def test_fast_wrist_lift_is_not_delayed_by_smoothing():
    prev = REACH_UPPER_BODY.copy()
    prev[10] = (185, 190, 0.9)                         # right wrist at the hip
    new = REACH_UPPER_BODY.copy()                      # ... now above the head
    out = smooth_keypoints(prev, new, alpha=0.6, scale=240.0, arm_alpha=0.8)
    assert np.allclose(out[10, :2], new[10, :2], atol=1e-3)


def test_small_jitter_is_still_smoothed():
    prev = REACH_UPPER_BODY.copy()
    new = REACH_UPPER_BODY.copy()
    new[:, 0] += 2.0                                   # 2 px on a 240 px person: jitter
    out = smooth_keypoints(prev, new, alpha=0.6, scale=240.0, arm_alpha=0.8)
    assert out[0, 0] == pytest.approx(prev[0, 0] + 0.6 * 2.0, abs=1e-3)    # head: alpha
    assert out[10, 0] == pytest.approx(prev[10, 0] + 0.8 * 2.0, abs=1e-3)  # wrist: arm alpha
    assert set(ARM_JOINTS) == {7, 8, 9, 10}


def test_a_wrist_that_disappears_is_not_carried_forward():
    prev = REACH_UPPER_BODY.copy()
    new = REACH_UPPER_BODY.copy()
    new[10, 2] = 0.1
    out = smooth_keypoints(prev, new, alpha=0.6, scale=240.0, arm_alpha=0.8)
    assert out[10, 2] == pytest.approx(0.1)
    assert np.allclose(out[10, :2], new[10, :2])


def test_old_call_signature_still_works():
    prev = REACH_UPPER_BODY.copy()
    new = REACH_UPPER_BODY.copy()
    new[:, 1] += 50
    out = smooth_keypoints(prev, new, 0.6)
    assert out[0, 1] == pytest.approx(prev[0, 1] + 0.6 * 50, abs=1e-3)


def test_tracker_follows_a_reach_frame_by_frame():
    """Box grows as the arm goes up; the tracked wrist is where the detector saw it."""
    tr = ByteTracker("cam_reach")
    rest = REACH_UPPER_BODY.copy()
    rest[8] = (185, 150, 0.9)
    rest[10] = (185, 195, 0.9)
    frames = []
    for i in range(3):                                 # standing still, arm down
        frames.append((Detection(100, 40, 220, 400, 0.9, keypoints=rest.copy()), rest[10, :2]))
    up = rest.copy()
    up[:, 1] += 40                                     # box keeps its top: shift body down
    up[8] = (240, 100, 0.9)
    up[10] = (330, 45, 0.85)                           # wrist lifted to the top shelf
    for i in range(3):
        frames.append((Detection(100, 30, 340, 400, 0.9, keypoints=up.copy()), up[10, :2]))
    for n, (det, wrist) in enumerate(frames):
        live = tr.update([det], now=n * 0.2)
        assert len(live) == 1
        t = live[0]
        assert t.keypoints_fresh
        # The first lifted frame already has the wrist within 2 px of the detection.
        assert np.hypot(*(t.keypoints[10, :2] - wrist)) < 2.0, (n, t.keypoints[10], wrist)
    assert live[0].confirmed


# ---------------------------------------------------- budget model selection


def test_latency_budget_scales_with_camera_count(monkeypatch):
    d = _detector()
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 0.0)
    monkeypatch.setattr(settings, "POSE_BUDGET_UTILISATION", 0.6)
    monkeypatch.setattr(settings, "RECORDING_FPS", 25)
    monkeypatch.setattr(settings, "ANALYTICS_DETECT_EVERY_N_FRAMES", 5)
    monkeypatch.setattr(settings, "POSE_BUDGET_STREAMS", 4)
    # Without the scheduler every camera is analysed at 25 / 5 = 5 fps.
    monkeypatch.setattr(settings, "ANALYTICS_SCHEDULER", False)
    b4, src = d.latency_budget()
    assert b4 == pytest.approx(30.0) and "4 stream" in src
    monkeypatch.setattr(settings, "POSE_BUDGET_STREAMS", 32)
    assert d.latency_budget()[0] == pytest.approx(5.0)         # clamped floor (3.75 ms)
    # With it, the model is fitted for ANALYTICS_TARGET_DETECT_FPS per camera,
    # the rate the run-time re-fit uses too.
    monkeypatch.setattr(settings, "ANALYTICS_SCHEDULER", True)
    monkeypatch.setattr(settings, "ANALYTICS_TARGET_DETECT_FPS", 2.0)
    monkeypatch.setattr(settings, "ANALYTICS_MIN_DETECT_FPS", 1.0)
    monkeypatch.setattr(settings, "POSE_BUDGET_STREAMS", 33)
    b33, src = d.latency_budget()
    assert b33 == pytest.approx(9.1) and "x 2 fps" in src    # 600 ms / (33 x 2)
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 42.0)
    assert d.latency_budget() == (42.0, "POSE_LATENCY_BUDGET_MS")


class _IO:
    def __init__(self, name, shape, type_="tensor(float)"):
        self.name, self.shape, self.type = name, shape, type_


class _TimedSession:
    """A fake ORT session whose run() takes a model-specific time."""

    def __init__(self, path, ms, ep):
        self.path, self.ms, self.ep = Path(path), ms, ep

    def get_providers(self):
        return [self.ep, "CPUExecutionProvider"]

    def get_modelmeta(self):
        class M:
            custom_metadata_map = {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"}
        return M()

    def get_inputs(self):
        return [_IO("images", [1, 3, 64, 64])]

    def get_outputs(self):
        return [_IO("output0", [1, 56, 84])]

    def run(self, _names, _feed):
        time.sleep(self.ms / 1000.0)
        return [np.zeros((1, 56, 84), np.float32)]


class _TimedOrt:
    class SessionOptions:
        graph_optimization_level = None
        log_severity_level = 0

    class GraphOptimizationLevel:
        ORT_ENABLE_ALL = 99

    def __init__(self, ms_by_name):
        self.ms = ms_by_name

    def get_available_providers(self):
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def InferenceSession(self, path, sess_options=None, providers=()):
        ep = providers[0][0] if isinstance(providers[0], tuple) else providers[0]
        return _TimedSession(path, self.ms[Path(path).name], ep)


@pytest.fixture
def ladder(monkeypatch, tmp_path):
    for n in ("huge.onnx", "mid.onnx", "base.onnx", "cpu.onnx"):
        (tmp_path / n).write_bytes(b"")
    monkeypatch.setattr(settings, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(settings, "POSE_MODEL_PATH", "")
    monkeypatch.setattr(settings, "POSE_MODEL_LADDER_GPU", "huge.onnx,mid.onnx,missing.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "base.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_CPU", "cpu.onnx")
    monkeypatch.setattr(settings, "INFERENCE_DISABLED_PROVIDERS", "")
    monkeypatch.setattr(settings, "INFERENCE_WARMUP_RUNS", 1)
    monkeypatch.setattr(settings, "OBJECT_DETECT_EVERY_N", 0)
    return tmp_path


def _init_with(ort):
    d = PersonDetector(object_model_path="")
    d.available_providers = ort.get_available_providers()
    d._select(ort)
    d._finish_loading(ort)
    return d


def test_ladder_keeps_the_most_accurate_model_that_fits(ladder, monkeypatch):
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 60.0)
    d = _init_with(_TimedOrt({"huge.onnx": 120, "mid.onnx": 30, "base.onnx": 5, "cpu.onnx": 5}))
    assert d.provider == "cuda"
    assert d.model_path.name == "mid.onnx"
    sel = d.status()["model_selection"]
    assert sel["budget_ms"] == 60.0 and sel["chosen"] == "mid.onnx"
    assert [t["model"] for t in sel["tried"]] == ["huge.onnx", "mid.onnx"]
    assert sel["tried"][0]["steady_ms"] > 60 >= sel["tried"][1]["steady_ms"]


def test_ladder_takes_the_top_model_when_the_budget_allows(ladder, monkeypatch):
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 500.0)
    d = _init_with(_TimedOrt({"huge.onnx": 20, "mid.onnx": 10, "base.onnx": 5, "cpu.onnx": 5}))
    assert d.model_path.name == "huge.onnx"
    assert len(d.selection["tried"]) == 1


def test_ladder_falls_back_to_the_fastest_when_nothing_fits(ladder, monkeypatch):
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 5.0)
    d = _init_with(_TimedOrt({"huge.onnx": 60, "mid.onnx": 40, "base.onnx": 15, "cpu.onnx": 25}))
    assert d.model_path.name == "base.onnx"
    assert d.selection["reason"].startswith("no candidate fits")


def test_pinned_model_is_never_swapped(ladder, monkeypatch):
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 5.0)
    monkeypatch.setattr(settings, "POSE_MODEL_PATH", str(ladder / "huge.onnx"))
    d = _init_with(_TimedOrt({"huge.onnx": 30, "mid.onnx": 10, "base.onnx": 5, "cpu.onnx": 5}))
    assert d.model_path.name == "huge.onnx"
    assert d.selection["reason"] == "pinned by POSE_MODEL_PATH"


def test_ladder_ends_with_the_shipped_floor_models():
    """Missing ladder files are skipped, so the shipped floor models remain the fallback."""
    d = _detector()
    d._forced_model = None
    for accel, primary in ((True, settings.POSE_MODEL_GPU), (False, settings.POSE_MODEL_CPU)):
        names = [p.name for p in d._model_candidates(accel)]
        assert len(names) == len(set(names))
        assert {settings.POSE_MODEL_GPU, settings.POSE_MODEL_CPU} <= set(names)
        ladder = settings.POSE_MODEL_LADDER_GPU if accel else settings.POSE_MODEL_LADDER_CPU
        extra = [n.strip() for n in ladder.split(",") if n.strip() and n.strip() != primary]
        assert names[: len(extra)] == extra                 # most accurate first


# ------------------------------------------------ top-down refiner (real models)


def test_refiner_crop_and_decode_round_trip():
    """A SimCC peak at the crop centre maps back to the box centre."""
    from app.services.inference_backend import refiner_crop, refiner_decode

    frame = np.zeros((720, 1280, 3), np.uint8)
    crop, meta = refiner_crop(frame, (600, 200, 680, 440), (256, 192))
    assert crop.shape == (256, 192, 3)
    cx, cy, bw, bh = meta
    assert (cx, cy) == (640, 320) and bw / bh == pytest.approx(192 / 256)
    sx = np.zeros((1, 17, 384), np.float32)
    sy = np.zeros((1, 17, 512), np.float32)
    sx[0, :, 192] = 0.9          # x = 96 px in the crop = its centre
    sy[0, :, 256] = 0.7          # y = 128 px
    k = refiner_decode(sx, sy, [meta], (256, 192), 1280, 720)
    assert k.shape == (1, 17, 3)
    assert np.allclose(k[0, :, 0], 640) and np.allclose(k[0, :, 1], 320)
    assert np.allclose(k[0, :, 2], 0.7)      # min of the two peaks


@pytest.fixture(scope="module")
def refined_detector():
    pose, ref = MODELS / "yolo26s-pose.onnx", MODELS / settings.POSE_REFINER_MODEL
    if not pose.exists() or not ref.exists():
        pytest.skip("pose or refiner model not present (scripts/fetch_models.py)")
    old = settings.POSE_REFINER
    settings.POSE_REFINER = "on"
    try:
        d = PersonDetector(model_path=pose, object_model_path="")
        st = d.initialise()
    finally:
        settings.POSE_REFINER = old
    if not st["available"]:
        pytest.skip(f"pose model could not be loaded: {d.last_error}")
    if not st["keypoint_refiner"]["enabled"]:
        pytest.skip(f"refiner not enabled: {st['keypoint_refiner']['reason']}")
    return d


def test_refiner_keeps_skeletons_on_the_people(refined_detector):
    import cv2

    img = cv2.imread(str(Path(__file__).resolve().parent / "fixtures" / "bus.jpg"))
    dets = refined_detector.detect(img)
    assert len(dets) >= 3
    h, w = img.shape[:2]
    for d in dets:
        k = d.keypoints
        assert k.shape == (17, 3)
        assert np.all((k[:, 2] >= 0) & (k[:, 2] <= 1))
        assert np.all((k[:, 0] >= 0) & (k[:, 0] <= w) & (k[:, 1] >= 0) & (k[:, 1] <= h))
        bw, bh = d.x2 - d.x1, d.y2 - d.y1
        for i in (5, 6, 9, 10):
            if k[i, 2] >= 0.5:
                assert d.x1 - 0.2 * bw <= k[i, 0] <= d.x2 + 0.2 * bw
                assert d.y1 - 0.2 * bh <= k[i, 1] <= d.y2 + 0.2 * bh
    st = refined_detector.status()["keypoint_refiner"]
    assert st["enabled"] and st["avg_refine_ms"] is not None and st["avg_refine_ms"] > 0
