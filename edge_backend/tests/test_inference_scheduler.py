"""Global inference budget (app/services/inference_scheduler.py) and the
detector / capture changes it relies on.

Measured on the store box (33 ONLINE cameras, RX 9060 XT, MIGraphX): with every
5th frame of every camera inferred, 165 inferences/s x 14 ms kept the GPU at
97 % and the model had been chosen at start-up for the cameras that existed
then. These tests pin the rate maths, fairness, floor / ceiling, the re-fit
debounce and ladder steps, the refiner being shed first, honest status, and
the FFmpeg decode thread cap. Everything runs on a fake clock and fake
detector; no GPU, camera or server is touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services import inference_scheduler as isch
from app.services import live_analytics_engine as lae
from app.services.inference_scheduler import (
    InferenceScheduler,
    Level,
    allocate_rates,
    build_levels,
    choose_level,
)

BACKEND = Path(__file__).resolve().parent.parent

# Measured single-model latencies on the RX 9060 XT (fp32).
M544, S640, N640 = 14.0, 5.6, 2.7


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for key, value in {"ANALYTICS_SCHEDULER": True, "POSE_BUDGET_UTILISATION": 0.6,
                       "ANALYTICS_MIN_DETECT_FPS": 1.0, "ANALYTICS_TARGET_DETECT_FPS": 2.0,
                       "ANALYTICS_REFIT_DEBOUNCE_SEC": 30.0, "ANALYTICS_MAX_INFLIGHT": 4,
                       "ANALYTICS_DETECT_EVERY_N_FRAMES": 5, "RECORDING_FPS": 25,
                       "OBJECT_DETECT_EVERY_N": 3, "POSE_REFINER": "auto"}.items():
        monkeypatch.setattr(settings, key, value)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeDetector:
    """Costs like the real one: pose ms per rung + object share + refiner."""

    def __init__(self, ladder_ms: dict[str, float], refiner_ms: float = 3.0, object_ms: float = N640,
                 measured: tuple[str, ...] = (), refiner: bool = True, mode: str = "auto"):
        self.ms = dict(ladder_ms)
        self.ladder = [Path("/models") / n for n in ladder_ms]
        self.model_path = self.ladder[0]
        self.refiner_ms, self.object_ms = refiner_ms, object_ms
        self.refiner_available, self.refiner_enabled, self.mode = refiner, refiner, mode
        self.available, self.provider = True, "migraphx"
        self.busy, self.frames = 0.0, 0
        # Start-up measured these rungs (PersonDetector.selection["tried"]).
        self.selection = {"tried": [{"model": n, "steady_ms": self.ms[n]} for n in measured]}
        self.calls: list[tuple] = []
        self.fail: set[str] = set()

    def pose_ladder(self):
        return list(self.ladder)

    @staticmethod
    def _refiner_mode():
        return (settings.POSE_REFINER or "auto").lower()

    def cost(self) -> float:
        return self.ms[self.model_path.name] + self.object_ms / 3 + (self.refiner_ms if self.refiner_enabled else 0)

    def infer(self, n: int = 1) -> None:
        self.frames += n
        self.busy += n * self.cost()

    def load_metrics(self) -> dict:
        return {"busy_ms": self.busy, "frames": self.frames, "provider": self.provider,
                "model": self.model_path.name, "pose_ms": self.ms[self.model_path.name],
                "pose_ms_measured": self.frames > 0, "object_ms": self.object_ms, "objects_on_device": True,
                "refiner_available": self.refiner_available, "refiner_enabled": self.refiner_enabled,
                "refiner_frame_ms": self.refiner_ms if self.refiner_enabled and self.frames else None,
                "refiner_batch_ms": self.refiner_ms}

    def set_refiner(self, on, reason):
        self.calls.append(("refiner", on))
        self.refiner_enabled = on
        return True

    def switch_pose_model(self, path, reason, progress=None):
        self.calls.append(("switch", Path(path).name))
        if Path(path).name in self.fail:
            return False, {"error": "compile failed"}
        self.model_path = Path(path)
        return True, {"steady_ms": self.ms[self.model_path.name]}


def _sched(det, clock) -> InferenceScheduler:
    return InferenceScheduler(detector=det, clock=clock, rng=lambda: 0.5, run_async=False)


def simulate(s: InferenceScheduler, det: FakeDetector, clock: Clock, cams: list[str], seconds: float,
             fps: float = 25.0, frame: dict | None = None) -> dict[str, int]:
    """Every camera decodes ``fps`` frames/s; admitted frames cost device time."""
    frame = frame if frame is not None else {}
    admitted = {c: 0 for c in cams}
    step = 1.0 / fps
    for _ in range(int(round(seconds * fps))):
        clock.t += step
        for c in cams:
            frame[c] = frame.get(c, 0) + 1
            if s.admit(c, fps=fps, frame_index=frame[c]):
                admitted[c] += 1
                det.infer()
                s.done(c)
    return admitted


CAMS33 = [f"cam{i:02d}" for i in range(33)]


# ------------------------------------------------------------ rate maths


@pytest.mark.parametrize("ms,per_cam", [(M544, 0.6 * 1000 / M544 / 33), (S640, 0.6 * 1000 / S640 / 33),
                                        (N640, 5.0)])
def test_rates_for_33_cameras_follow_the_measured_latency(ms, per_cam):
    budget = 0.6 * 1000.0 / ms
    rates = allocate_rates(budget, {c: 5.0 for c in CAMS33}, floor=1.0)
    assert set(rates) == set(CAMS33)
    assert all(r == pytest.approx(per_cam) for r in rates.values())
    # 14 ms -> 1.30/s, 5.6 ms -> 3.25/s, 2.7 ms -> capped at the old every-5th-frame rate.
    assert sum(rates.values()) <= budget + 1e-9


def test_floor_is_guaranteed_even_over_budget():
    rates = allocate_rates(0.6 * 1000 / 40.0, {c: 5.0 for c in CAMS33}, floor=1.0)
    assert all(r == 1.0 for r in rates.values())          # 15/s budget, 33/s of floors


def test_slow_camera_keeps_its_ceiling_and_the_rest_share_what_it_leaves():
    rates = allocate_rates(10.0, {"slow": 0.4, "a": 5.0, "b": 5.0}, floor=1.0)
    assert rates["slow"] == pytest.approx(0.4)             # never above fps / N, even for the floor
    assert rates["a"] == rates["b"] == pytest.approx(4.8)


def test_admission_is_fair_and_holds_the_target_utilisation():
    clock = Clock()
    det = FakeDetector({"m.onnx": M544}, refiner=False)
    s = _sched(det, clock)
    got = simulate(s, det, clock, CAMS33, 60.0)
    per = [n / 60.0 for n in got.values()]
    expected = 0.6 * 1000 / det.cost() / 33               # 1.29/s each
    assert min(per) > expected * 0.85 and max(per) < expected * 1.15
    assert max(per) - min(per) <= 0.1                      # round-robin fair
    st = s.status()
    assert st["measured_utilisation"] == pytest.approx(0.6, abs=0.06)
    assert st["device_ms_source"].startswith("measured")
    assert st["cameras_analysing"] == 33 and not st["floor_bound"]


def test_a_camera_is_never_analysed_more_often_than_every_nth_frame():
    clock = Clock()
    det = FakeDetector({"n.onnx": 0.5}, refiner=False, object_ms=0.0)   # nearly free: ceiling binds
    s = _sched(det, clock)
    frames: dict = {}
    got = simulate(s, det, clock, ["a", "b"], 20.0, frame=frames)
    assert all(n <= 20 * 25 / 5 + 1 for n in got.values())
    assert all(n >= 20 * 25 / 5 * 0.9 for n in got.values())


def test_inflight_bound_skips_frames_instead_of_queueing():
    clock = Clock()
    det = FakeDetector({"m.onnx": 1.0}, refiner=False)
    s = _sched(det, clock)
    admitted = []
    for frame in range(1, 51):                             # 2 s of frames from every camera
        clock.t += 0.04
        for cam in CAMS33:
            if s.admit(cam, fps=25, frame_index=frame):    # nobody calls done(): all stay in flight
                admitted.append(cam)
    assert len(admitted) == settings.ANALYTICS_MAX_INFLIGHT
    assert s.status()["skipped_busy"] > 0 and s.status()["inflight"] == settings.ANALYTICS_MAX_INFLIGHT
    for cam in admitted:
        s.done(cam)
    assert s.status()["inflight"] == 0


def test_scheduler_off_keeps_the_every_nth_frame_rule(monkeypatch):
    monkeypatch.setattr(settings, "ANALYTICS_SCHEDULER", False)
    clock = Clock()
    det = FakeDetector({"m.onnx": M544}, refiner=False)
    s = _sched(det, clock)
    got = simulate(s, det, clock, CAMS33[:3], 10.0)
    assert all(n == 50 for n in got.values())                  # 250 frames / 5
    assert s.status()["mode"].startswith("every_nth_frame")
    assert det.calls == []


def test_idle_and_departed_cameras_give_their_share_back():
    clock = Clock()
    det = FakeDetector({"m.onnx": M544}, refiner=False)
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 5.0)
    shared = s.camera_status("cam00")["detect_fps_allocated"]
    for c in CAMS33[11:]:
        s.set_idle(c, True)
    assert s.camera_status("cam00")["detect_fps_allocated"] == pytest.approx(shared * 3, rel=0.05)
    assert s.camera_status("cam20")["idle"] is True
    for c in CAMS33[1:]:
        s.forget(c)
    assert s.camera_status("cam05") is None
    assert s.camera_status("cam00")["detect_fps_allocated"] == pytest.approx(5.0)   # alone: the ceiling


# ---------------------------------------------------------- ladder choice


def test_levels_put_the_auto_refiner_on_the_top_rung_only():
    ladder = [Path("m.onnx"), Path("s.onnx")]
    assert build_levels(ladder, True, "auto") == [Level(ladder[0], True), Level(ladder[0], False),
                                                   Level(ladder[1], False)]
    assert build_levels(ladder, True, "on") == [Level(p, True) for p in ladder]
    assert build_levels(ladder, False, "auto") == [Level(p, False) for p in ladder]


def test_choose_level_for_33_cameras():
    # (m544 + refiner), m544, s544 (~7 ms), s640: + 0.9 ms object share each.
    costs = [M544 + 0.9 + 3.0, M544 + 0.9, 7.0 + 0.9, S640 + 0.9]
    demand = 33 * 2.0
    idx, why = choose_level(costs, 0, demand, 0.6)
    assert idx == 2 and "52%" in why                           # 66/s x 7.9 ms
    assert choose_level(costs, 2, demand, 0.6)[0] == 2         # stays
    assert choose_level(costs, 2, 4 * 2.0, 0.6)[0] == 0        # 4 cameras: back to the top with refiner
    # Up only with a margin: level 1 at 55 % of the device is not taken from level 2.
    assert choose_level([None, 0.55 * 1000 / 66, 7.9], 2, 66, 0.6)[0] == 2
    # An unmeasured cheaper level is loaded to be measured; an unmeasured larger one is never tried.
    assert choose_level([20.0, None, 5.0], 0, 66, 0.6)[0] == 1
    assert choose_level([None, 5.0], 1, 4, 0.6)[0] == 1
    # Nothing fits: the cheapest measured.
    assert choose_level([30.0, 20.0, 12.0], 0, 66, 0.6)[0] == 2


# ------------------------------------------------------------------ re-fit


def _ladder_det(**kw) -> FakeDetector:
    return FakeDetector({"m544.onnx": M544, "s544.onnx": 7.0, "s640.onnx": S640, "n640.onnx": N640},
                        measured=("m544.onnx", "s544.onnx"), **kw)


def test_refit_waits_for_the_debounce_then_sheds_the_refiner_before_the_model():
    clock = Clock()
    det = _ladder_det()
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33[:4], 10.0)                  # 4 cameras: top rung + refiner fits
    assert det.calls == []
    simulate(s, det, clock, CAMS33, 25.0)                      # 33 cameras arrive
    assert det.calls == [], "re-fitted before ANALYTICS_REFIT_DEBOUNCE_SEC"
    assert s.status()["pending_refit"]["to"] == "s544.onnx"
    simulate(s, det, clock, CAMS33, 10.0)
    assert det.calls == [("refiner", False), ("switch", "s544.onnx")]
    st = s.status()
    assert st["refits"][-1]["ok"] and st["refits"][-1]["to"] == "s544.onnx"
    assert "33 camera(s)" in st["refits"][-1]["reason"]
    assert st["level"] == {"model": "s544.onnx", "refiner": False}
    # With the cheaper model every camera gets more than the floor, and the device stays near target.
    simulate(s, det, clock, CAMS33, 20.0)
    st = s.status()
    assert st["per_camera_fps"]["min"] > 2.0
    assert st["measured_utilisation"] == pytest.approx(0.6, abs=0.06)


def test_a_camera_joining_restarts_the_debounce():
    clock = Clock()
    det = _ladder_det()
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33[:30], 20.0)
    simulate(s, det, clock, CAMS33[:31], 20.0)                 # 40 s since the load arrived, 20 s since the change
    assert det.calls == []
    simulate(s, det, clock, CAMS33[:31], 12.0)
    assert ("switch", "s544.onnx") in det.calls


def test_steps_back_up_with_the_refiner_restored_last_when_cameras_leave():
    clock = Clock()
    det = _ladder_det()
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 40.0)
    assert det.model_path.name == "s544.onnx" and not det.refiner_enabled
    det.calls.clear()
    for c in CAMS33[4:]:
        s.forget(c)
    simulate(s, det, clock, CAMS33[:4], 40.0)
    assert det.calls == [("switch", "m544.onnx"), ("refiner", True)]
    assert s.status()["level"] == {"model": "m544.onnx", "refiner": True}


def test_overload_at_the_floor_steps_down_through_unmeasured_rungs():
    clock = Clock()
    det = FakeDetector({"m544.onnx": 60.0, "s544.onnx": 30.0, "s640.onnx": 9.0}, measured=("m544.onnx",),
                       refiner=False)
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 5.0)
    st = s.status()
    assert st["floor_bound"] and st["per_camera_fps"]["min"] == 1.0
    simulate(s, det, clock, CAMS33, 70.0)
    # s544 was unknown: loaded to measure it (30 ms, still too slow), then s640.
    assert det.calls == [("switch", "s544.onnx"), ("switch", "s640.onnx")]
    assert not s.status()["floor_bound"]


def test_failed_switch_keeps_the_model_and_is_not_retried():
    clock = Clock()
    det = _ladder_det(refiner=False)
    det.fail.add("s544.onnx")
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 35.0)
    assert det.calls == [("switch", "s544.onnx")] and det.model_path.name == "m544.onnx"
    st = s.status()
    assert not st["refits"][-1]["ok"] and "compile failed" in st["refits"][-1]["error"]
    assert "migraphx:s544.onnx" in st["unusable_models"]
    simulate(s, det, clock, CAMS33, 35.0)
    assert det.calls[-1] == ("switch", "s640.onnx")            # the next rung, not the broken one again


def test_pinned_model_only_adjusts_rates():
    clock = Clock()
    det = FakeDetector({"pinned.onnx": M544}, refiner=False)
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 60.0)
    assert det.calls == []
    assert s.status()["per_camera_fps"]["mean"] == pytest.approx(0.6 * 1000 / det.cost() / 33, rel=0.02)


def test_status_reports_what_runs_not_what_was_planned():
    clock = Clock()
    det = _ladder_det(refiner=False)
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33[:2], 12.0)
    st = s.status()
    assert st["level"] == {"model": "m544.onnx", "refiner": False}
    assert st["pose_ms_per_inference"] == M544 and st["pose_ms_source"].startswith("measured")
    assert st["device_ms_per_frame"] == pytest.approx(det.cost())
    assert st["capacity_frames_per_s"] == pytest.approx(1000 / det.cost(), rel=0.01)
    # Two cameras on an idle GPU: capped at their ceiling, measured below the target load.
    assert st["allocated_frames_per_s"] == pytest.approx(10.0)
    assert st["measured_frames_per_s"] == pytest.approx(10.0, rel=0.1)
    assert st["measured_utilisation"] < 0.6
    cam = s.camera_status("cam00")
    assert cam["detect_fps_allocated"] == 5.0 and cam["detect_fps_measured"] == pytest.approx(5.0, rel=0.1)
    assert [row["current"] for row in st["ladder"]].count(True) == 1


def test_no_detector_means_no_limit_and_no_refit():
    clock = Clock()
    det = _ladder_det()
    det.available = False
    s = _sched(det, clock)
    got = simulate(s, det, clock, CAMS33[:3], 10.0)
    assert all(n == 50 for n in got.values()) and det.calls == []
    assert s.status()["device_ms_per_frame"] is None


# --------------------------------------------------- the real PersonDetector


from tests.test_pose_extended_arm import _init_with, _TimedOrt, ladder  # noqa: E402,F401


def test_switch_pose_model_swaps_atomically_and_reports_why(ladder, monkeypatch):
    import app.services.inference_backend as ib

    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 500.0)
    ort = _TimedOrt({"huge.onnx": 4, "mid.onnx": 2, "base.onnx": 1, "cpu.onnx": 1})
    d = _init_with(ort)
    assert d.model_path.name == "huge.onnx"
    assert [p.name for p in d.pose_ladder()] == ["huge.onnx", "mid.onnx", "base.onnx", "cpu.onnx"]
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    frame = np.zeros((64, 64, 3), np.uint8)
    d.detect(frame)
    before = d.load_metrics()
    assert before["frames"] == 1 and before["busy_ms"] > 0 and before["pose_ms_measured"]

    # Cameras keep inferring while the switch loads and measures the new model.
    stop, errors = threading.Event(), []

    def camera():
        while not stop.is_set():
            try:
                d.detect(frame)
            except Exception as e:  # pragma: no cover - the assertion below reports it
                errors.append(e)

    t = threading.Thread(target=camera)
    t.start()
    ok, info = d.switch_pose_model(ladder / "mid.onnx", "re-fit: test")
    stop.set()
    t.join()
    assert ok and info["from"] == "huge.onnx" and info["steady_ms"] >= 2
    assert not errors
    st = d.status()
    assert st["model"] == "mid.onnx" and st["model_selection"]["chosen"] == "mid.onnx"
    assert st["model_selection"]["reason"] == "re-fit: test"
    assert any(t["model"] == "mid.onnx" for t in st["model_selection"]["tried"])
    assert ib.PersonDetector._is_accelerator(d.provider) and d._gate(d.provider) is d._device_lock
    assert d._gate("cpu") is not d._device_lock


def test_switch_is_abandoned_when_the_provider_changed(ladder, monkeypatch):
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 500.0)
    ort = _TimedOrt({"huge.onnx": 1, "mid.onnx": 1, "base.onnx": 1, "cpu.onnx": 1})
    d = _init_with(ort)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    real_warm_up = d._warm_up

    def warm_up_then_gpu_arrives(*a, **kw):
        out = real_warm_up(*a, **kw)
        d.execution_provider = "MIGraphXExecutionProvider"     # the background compile adopted the GPU
        return out

    monkeypatch.setattr(d, "_warm_up", warm_up_then_gpu_arrives)
    ok, info = d.switch_pose_model(ladder / "mid.onnx", "re-fit")
    assert not ok and "provider changed" in info["error"] and d.model_path.name == "huge.onnx"


def test_set_refiner_needs_a_loaded_session():
    from app.services.inference_backend import PersonDetector

    d = PersonDetector(object_model_path="")
    assert d.set_refiner(True, "x") is False and d.refiner_enabled is False
    d.refiner_session = object()
    assert d.set_refiner(True, "auto: on") and d.refiner_enabled
    assert d.set_refiner(False, "auto: off under load") and d.refiner_reason == "auto: off under load"


def test_startup_parks_the_auto_refiner_when_the_top_rung_does_not_fit(ladder, monkeypatch):
    from app.services.inference_backend import PersonDetector

    def fake_refiner(self, ort):
        self.refiner_session, self.refiner_enabled, self.refiner_batch_ms = object(), True, 0.5

    monkeypatch.setattr(PersonDetector, "_load_refiner", fake_refiner)
    monkeypatch.setattr(settings, "POSE_LATENCY_BUDGET_MS", 30.0)
    d = _init_with(_TimedOrt({"huge.onnx": 60, "mid.onnx": 10, "base.onnx": 5, "cpu.onnx": 5}))
    assert d.model_path.name == "mid.onnx"
    assert d.refiner_session is not None and d.refiner_enabled is False
    assert "below the top of the ladder" in d.status()["keypoint_refiner"]["reason"]


def test_global_detector_status_carries_the_load_control():
    from app.services.inference_backend import PersonDetector, person_detector

    lc = person_detector.status()["load_control"]
    assert lc["mode"] == "budget" and lc["target_utilisation"] == 0.6
    assert PersonDetector(object_model_path="").status()["load_control"] is None


def test_worker_admits_through_the_scheduler_and_releases_every_slot(monkeypatch):
    """The capture loop: every frame is offered, only admitted ones are analysed."""
    clock = Clock()
    det = FakeDetector({"m.onnx": M544}, refiner=False)
    sched = _sched(det, clock)
    monkeypatch.setattr(lae, "inference_scheduler", sched)

    class Cap:
        def __init__(self):
            self.n = 0

        def isOpened(self):
            return True

        def read(self):
            self.n += 1
            clock.t += 0.04
            if self.n > 250:
                w._stop.set()
            return True, np.zeros((288, 352, 3), np.uint8)

        def release(self):
            pass

    engine = lae.LiveAnalyticsEngine()
    rt = lae.CameraRuntime(camera_id="cam_sched", name="c", source="rtsp://10.0.0.1/x")
    w = lae.CameraWorker(rt, engine)
    analysed = []
    monkeypatch.setattr(w, "_preflight", lambda src: None)
    monkeypatch.setattr(w, "_open", lambda src=None: Cap())
    monkeypatch.setattr(w, "_push_clip_buffer", lambda f: None)
    monkeypatch.setattr(w, "_analyse", lambda f, now: analysed.append(now))
    w.run()
    assert 45 <= len(analysed) <= 51                     # 10 s at 25 fps, alone: every 5th frame
    assert sched.status()["inflight"] == 0
    assert sched.camera_status("cam_sched") is None      # forgotten when the worker stopped


# ------------------------------------------------------ FFmpeg decode threads


@pytest.mark.parametrize("w,h,n", [(352, 288, 1), (704, 576, 1), (1280, 720, 1), (1920, 1080, 2),
                                   (2560, 1440, 4), (3840, 2160, 4), (None, None, 2)])
def test_decode_threads_scale_with_resolution(monkeypatch, w, h, n):
    monkeypatch.setattr(settings, "CAMERA_DECODE_THREADS", 0)
    assert lae.decode_threads_for(w, h) == n


def test_decode_threads_can_be_pinned(monkeypatch):
    monkeypatch.setattr(settings, "CAMERA_DECODE_THREADS", 3)
    assert lae.decode_threads_for(3840, 2160) == 3 == lae.decode_threads_for(352, 288)


def test_capture_options_keep_transport_and_timeouts_and_no_ignored_threads_entry():
    opts = lae.ffmpeg_capture_options()
    assert opts.startswith("rtsp_transport;tcp|") and "rw_timeout;" in opts
    assert "threads" not in opts        # OpenCV does not pass it to the decoder (measured)


def test_open_passes_the_decode_thread_cap_per_capture(monkeypatch):
    import cv2

    seen = {}

    class VC:
        def __init__(self, src, api=None, params=None):
            seen["params"] = list(params or [])

        def set(self, *a):
            return True

    monkeypatch.setattr(settings, "CAMERA_DECODE_THREADS", 0)
    monkeypatch.setattr(cv2, "VideoCapture", VC)
    monkeypatch.setattr(lae, "_resolve_host_bounded", lambda *a: None)
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="c1", name="c", source="rtsp://10.9.9.9/sub"),
                         lae.LiveAnalyticsEngine())
    monkeypatch.setitem(lae._source_sizes, "rtsp://10.9.9.9/sub", (352, 288))
    w._open()
    p = seen["params"]
    assert p[p.index(cv2.CAP_PROP_N_THREADS) + 1] == 1 and w._decode_threads == 1
    lae._source_sizes.pop("rtsp://10.9.9.9/sub")
    w._open()
    assert seen["params"][seen["params"].index(cv2.CAP_PROP_N_THREADS) + 1] == lae.DECODE_THREADS_UNKNOWN


def test_a_4k_stream_is_reopened_once_with_more_threads(monkeypatch):
    monkeypatch.setattr(settings, "CAMERA_DECODE_THREADS", 0)
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="c4k", name="c", source="rtsp://10.9.9.8/main"),
                         lae.LiveAnalyticsEngine())
    opened = []

    class Cap:
        def isOpened(self):
            return True

        def release(self):
            pass

    def fake_open(src=None):
        opened.append(lae.decode_threads_for(*lae._source_sizes.get(src, (None, None))))
        w._decode_threads = opened[-1]
        return Cap()

    monkeypatch.setattr(w, "_open", fake_open)
    w._decode_threads = 2
    w._fit_decode_threads(Cap(), "rtsp://10.9.9.8/main", np.zeros((2160, 3840, 3), np.uint8))
    assert opened == [4]
    w._fit_decode_threads(Cap(), "rtsp://10.9.9.8/main", np.zeros((2160, 3840, 3), np.uint8))
    assert opened == [4]                                   # already enough: no second reopen
    small = lae.CameraWorker(lae.CameraRuntime(camera_id="cs", name="c", source="x"), lae.LiveAnalyticsEngine())
    small._decode_threads = 2
    cap = Cap()
    assert small._fit_decode_threads(cap, "rtsp://10.9.9.7/sub", np.zeros((288, 352, 3), np.uint8)) is cap
    lae._source_sizes.pop("rtsp://10.9.9.8/main", None)
    lae._source_sizes.pop("rtsp://10.9.9.7/sub", None)


_THREADS = r"""
import os, sys, cv2
params = [] if sys.argv[2] == "default" else [cv2.CAP_PROP_N_THREADS, int(sys.argv[2])]
base = len(os.listdir("/proc/self/task"))
cap = cv2.VideoCapture(sys.argv[1], cv2.CAP_FFMPEG, params)
ok = sum(cap.read()[0] for _ in range(10))
print(len(os.listdir("/proc/self/task")) - base, ok)
"""


@pytest.mark.skipif(not shutil.which("ffmpeg") or not Path("/proc/self/task").is_dir(),
                    reason="needs the ffmpeg CLI and /proc")
def test_real_decoder_honours_the_thread_cap(tmp_path):
    """A generated 352x288 H.264 clip: the cap really removes the per-CPU decode threads."""
    clip = tmp_path / "cif.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=352x288:rate=25",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=60)

    def extra_threads(mode: str) -> tuple[int, int]:
        out = subprocess.run([sys.executable, "-c", _THREADS, str(clip), mode], capture_output=True, text=True,
                             timeout=60, cwd=BACKEND)
        added, frames = out.stdout.split()
        return int(added), int(frames)

    capped, frames = extra_threads("1")
    assert frames == 10
    default, _ = extra_threads("default")
    assert capped <= 1
    if (os.cpu_count() or 1) >= 4:
        assert default > capped + 2, f"default decoder added {default} threads, capped {capped}"


def test_thinned_camera_keeps_the_native_rate_ceiling():
    """GPU decode delivers 10 of 25 fps: stride 2 keeps the ceiling at 5 fps, not 2."""
    from app.services.inference_scheduler import InferenceScheduler, _Camera

    s = InferenceScheduler.__new__(InferenceScheduler)
    thinned = _Camera(first_seen=0, last_seen=0, fps=10.0, every_n=2)
    native = _Camera(first_seen=0, last_seen=0, fps=25.0)
    assert s._ceiling(thinned) == 5.0
    assert s._ceiling(native) == 25.0 / max(1, int(__import__("app.config", fromlist=["settings"]).settings.ANALYTICS_DETECT_EVERY_N_FRAMES))
