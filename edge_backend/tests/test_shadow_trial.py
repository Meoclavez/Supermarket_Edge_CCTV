"""Shadow pose-model trial (services/shadow_trial.py).

The trial must be shadow only and honest:

* it never delays or displaces a live inference: admission is paced by the
  shadow model's own measured cost, the device is taken only when no live run
  holds or waits for it, its time is not added to the live model's busy total,
  and the live scheduler admits exactly what it would without it;
* both models' people are judged by the same rules, and only counts are kept;
* review images are few, masked, capped and deleted after the trial;
* the endpoint needs a signed-in user and reports nothing before data exists.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.services import shadow_trial as st_mod
from app.services.inference_backend import DeviceGate, Detection, PersonDetector, build_model_spec
from app.services.inference_scheduler import InferenceScheduler
from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: F401  (fixture)
from app.services.shadow_trial import (
    ShadowTrial,
    StaticBoxes,
    in_window,
    match_boxes,
    parse_window,
    resolution_class,
    size_bucket,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _det(x1, y1, x2, y2, conf=0.9, kp_conf=0.8):
    k = np.zeros((17, 3), np.float32)
    k[:, 0] = np.linspace(x1, x2, 17)
    k[:, 1] = np.linspace(y1, y2, 17)
    k[:, 2] = kp_conf
    return Detection(x1, y1, x2, y2, confidence=conf, keypoints=k, pose_keypoints=k)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(settings, "SHADOW_SAMPLE_EVERY_SEC", 0.0)
    monkeypatch.setattr(settings, "SHADOW_SAMPLES_MAX", 200)
    monkeypatch.setattr(settings, "SHADOW_TRIAL_RETAIN_DAYS", 7.0)
    return tmp_path


@pytest.fixture
def detector():
    d = PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")
    d.provider = "migraphx"                     # an accelerator: runs go through the device gate
    return d


def _trial(detector, clock=None, wall=None):
    t = ShadowTrial(detector=detector, clock=clock or Clock(), wall=wall or Clock(1.8e9), start_threads=False)
    t.model_path = Path("rtmo-s-body7-640x640-static.onnx")
    t.model_sha256 = "ab" * 32
    return t


# ------------------------------------------------------------------ helpers


def test_units():
    assert [size_bucket(h) for h in (10, 40, 79.9, 80, 159, 160, 900)] == \
        ["<40", "40-80", "40-80", "80-160", "80-160", ">160", ">160"]
    assert resolution_class((352, 288)) == "CIF (<=288 lines)"
    assert resolution_class((704, 576)) == "D1 (<=576 lines)"
    assert resolution_class((1280, 720)) == "720p"
    assert resolution_class((3072, 2048)) == "above 1080p"
    a = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], np.float32)
    b = np.array([[101, 101, 111, 111], [1, 0, 11, 10], [50, 50, 60, 60]], np.float32)
    assert sorted(match_boxes(a, b)) == [(0, 1), (1, 0)]
    assert match_boxes(a, np.zeros((0, 4), np.float32)) == []
    from datetime import datetime

    w = parse_window("22:00-06:30")
    assert w == (1320, 390)
    assert in_window(w, datetime(2026, 9, 27, 23, 0)) and in_window(w, datetime(2026, 9, 28, 6, 0))
    assert not in_window(w, datetime(2026, 9, 27, 12, 0))
    assert parse_window("25:00-01:00") is None and in_window(None, datetime.now())


def test_static_boxes_flag_a_box_that_never_moves():
    s = StaticBoxes()
    fridge = np.array([[100, 100, 150, 300]], np.float32)
    assert s.observe(("c", "m"), fridge, 1000, 1000, 0.0, 10) == 0
    for t in range(60, 600, 60):
        s.observe(("c", "m"), fridge + 3, 1000, 1000, float(t), 10)   # jitter within 2 %
    assert s.observe(("c", "m"), fridge, 1000, 1000, 601.0, 10) == 1
    walker = fridge + 100
    assert s.observe(("c", "m"), walker, 1000, 1000, 602.0, 10) == 0


# ------------------------------------------------------------------ tallies + samples


def test_pair_tallies_count_agreement_by_size_and_keypoints(storage, detector):
    t = _trial(detector)
    frame = np.full((720, 1280, 3), 120, np.uint8)
    main = [_det(100, 100, 160, 200), _det(600, 300, 615, 330, kp_conf=0.3)]      # 100 px and 30 px tall
    shadow = [_det(102, 101, 161, 202, kp_conf=0.9), _det(900, 200, 1000, 500)]  # match + a 300 px one
    out = t.add_pair("cam_a", frame, main, shadow, 7.5, {"infer_ms": 9.0, "model": "yolo26m-pose-544x960.onnx"})
    assert (out["matched"], out["only_main"], out["only_shadow"]) == (1, 1, 1)
    s = t.status()
    tot = s["totals"]
    assert tot["pairs"] == 1 and tot["persons_main"] == 2 and tot["persons_shadow"] == 2
    assert tot["by_box_height_px"]["<40"]["only_main"] == 1
    assert tot["by_box_height_px"][">160"]["only_shadow"] == 1
    assert tot["by_box_height_px"]["80-160"]["matched"] == 1
    kp = tot["keypoints"]
    assert kp["shadow"]["keypoint_gate_pass_rate"] == 1.0
    assert kp["main"]["keypoint_gate_pass_rate"] == 0.5
    assert kp["matched_main"]["mean_keypoint_confidence"] == pytest.approx(0.8)
    assert tot["device_ms"] == {"main_mean": 9.0, "shadow_mean": 7.5, "shadow_max": 7.5}
    assert list(s["by_camera"]) == ["cam_a"] and "yolo26m-pose-544x960.onnx" in s["by_live_model"]
    assert s["by_resolution"]                     # a class even without a registered size
    # A disagreement leaves one review image, listed and served by name only.
    assert len(s["samples"]) == 1
    name = s["samples"][0]["name"]
    assert (storage / "shadow_trial" / name).is_file()
    assert t.sample_path(name) is not None
    assert t.sample_path("../stats.json") is None and t.sample_path("x.jpg") is None


def test_samples_have_privacy_masks_burned_in(storage, detector, monkeypatch):
    import cv2

    from app.services import privacy_mask

    calls = []

    def black(frame, camera_id, masks=None, inplace=False):
        calls.append(camera_id)
        return np.zeros_like(frame)

    monkeypatch.setattr(privacy_mask, "apply_privacy_masks", black)
    t = _trial(detector)
    frame = np.full((360, 640, 3), 200, np.uint8)
    t.add_pair("cam_m", frame, [_det(10, 10, 60, 200)], [], 5.0, {})
    img = cv2.imread(str(storage / "shadow_trial" / t.status()["samples"][0]["name"]))
    assert calls == ["cam_m"]
    # The picture area (below the label bar, away from the drawn box) is the masked frame.
    assert img[200:300, 400:600].max() < 30


def test_sample_count_cap_and_rate_limit(storage, detector, monkeypatch):
    monkeypatch.setattr(settings, "SHADOW_SAMPLES_MAX", 3)
    wall = Clock(1.8e9)
    t = _trial(detector, wall=wall)
    frame = np.full((200, 200, 3), 50, np.uint8)
    for i in range(5):
        wall.t += 10
        t.add_pair("cam_c", frame, [_det(10, 10, 60, 150)], [], 5.0, {})
    files = sorted(p.name for p in (storage / "shadow_trial").glob("*.jpg"))
    listed = sorted(s["name"] for s in t.status()["samples"])
    assert len(files) == 3 and files == listed
    # One per camera per SHADOW_SAMPLE_EVERY_SEC.
    monkeypatch.setattr(settings, "SHADOW_SAMPLE_EVERY_SEC", 3600.0)
    wall.t += 10
    t.add_pair("cam_c", frame, [_det(10, 10, 60, 150)], [], 5.0, {})
    assert len(list((storage / "shadow_trial").glob("*.jpg"))) == 3
    # Agreement never writes a sample.
    t.add_pair("cam_d", frame, [_det(10, 10, 60, 150)], [_det(10, 10, 60, 150)], 5.0, {})
    assert not any("cam_d" in p.name for p in (storage / "shadow_trial").glob("*.jpg"))


def test_samples_are_declared_to_the_evidence_limit(storage):
    from app.services.evidence_storage import KINDS_BY_NAME, EvidenceStorage

    kind = KINDS_BY_NAME["shadow_trial"]
    assert kind.directory() == storage / "shadow_trial" and kind.link == "none"
    d = storage / "shadow_trial"
    d.mkdir()
    (d / "20260101T000000_cam.jpg").write_bytes(b"x" * 1000)
    (d / "stats.json").write_text("{}")
    rep = EvidenceStorage().enforce()
    assert rep["by_kind"]["shadow_trial"]["files"] == 1 and rep["by_kind"]["shadow_trial"]["managed"]


def test_stats_survive_a_restart_of_the_same_trial_only(storage, detector):
    t = _trial(detector)
    t.add_pair("cam_a", np.zeros((100, 100, 3), np.uint8), [_det(1, 1, 40, 90)], [_det(1, 1, 40, 90)], 4.0, {})
    t.save()
    again = _trial(detector)
    again._restore()
    assert again.status()["totals"]["pairs"] == 1
    other = _trial(detector)
    other.model_sha256 = "cd" * 32                 # another model file: start afresh
    other._restore()
    assert other.status()["totals"] is None and not (storage / "shadow_trial" / "stats.json").exists()


def test_trial_files_are_deleted_retain_days_after_the_last_pair(storage, detector):
    wall = Clock(1.8e9)
    t = _trial(detector, wall=wall)
    t.add_pair("cam_a", np.zeros((100, 100, 3), np.uint8), [_det(1, 1, 40, 90)], [], 4.0, {})
    t.save()
    assert (storage / "shadow_trial").is_dir()
    wall.t += 6 * 86400
    assert t.housekeep() is False
    wall.t += 2 * 86400
    assert t.housekeep() is True
    assert not (storage / "shadow_trial").exists() and t.status()["totals"] is None
    # A fresh process with the trial switched off cleans up the same way.
    (storage / "shadow_trial").mkdir()
    (storage / "shadow_trial" / "stats.json").write_text('{"last_pair_at": 1000}')
    fresh = ShadowTrial(detector=detector, wall=Clock(1.8e9), start_threads=False)
    fresh.start()
    assert fresh.state == "off" and not (storage / "shadow_trial").exists()


def test_housekeeping_never_follows_a_symlink_out_of_storage(storage, detector, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere")
    victim = outside / "keep.jpg"
    victim.write_bytes(b"x")
    os.symlink(outside, storage / "shadow_trial")
    t = _trial(detector, wall=Clock(1.8e9 + 30 * 86400))
    t.last_pair_at = 1.8e9
    t.housekeep()
    assert victim.exists()


# ------------------------------------------------------------------ scheduling


class FakeRtmo:
    """Stands in for the shadow ONNX session: one person, fixed cost."""

    def __init__(self, ms=0.0, box=(100, 100, 200, 400)):
        self.ms, self.box, self.calls = ms, box, 0

    def run(self, _names, feeds):
        self.calls += 1
        if self.ms:
            time.sleep(self.ms / 1000.0)
        dets = np.zeros((1, 8, 5), np.float32)
        kps = np.zeros((1, 8, 17, 3), np.float32)
        dets[0, 0] = [*self.box, 0.9]
        kps[0, 0, :, 2] = 0.9
        kps[0, 0, :, 0], kps[0, 0, :, 1] = 150, np.linspace(110, 390, 17)
        return [dets, kps]


def _running(detector, session, clock=None):
    t = _trial(detector, clock=clock)
    io = lambda n, s: SimpleNamespace(name=n, shape=s, type="tensor(float)")  # noqa: E731
    t.spec = build_model_spec(t.model_path, {}, [io("input", [1, 3, 640, 640])],
                              [io("dets", [1, 8, 5]), io("keypoints", [1, 8, 17, 3])])
    t.session = session
    t.state = "running"
    return t


def test_offer_is_paced_by_the_shadow_models_own_cost(storage, detector, monkeypatch):
    monkeypatch.setattr(settings, "SHADOW_POSE_SHARE", 0.2)
    clock = Clock()
    t = _running(detector, FakeRtmo(), clock)
    frame = np.full((640, 640, 3), 100, np.uint8)
    assert t.offer("cam", frame, []) is True
    assert t.offer("cam", frame, []) is False                    # one waits at most
    job, t._job = t._job, None
    monkeypatch.setattr(t, "process", lambda j: 10.0)            # a 10 ms shadow run
    t.run_job(job)
    # 10 ms at a 20 % share: the next may start 40 ms later.
    assert t._next_allowed == pytest.approx(clock.t + 0.040)
    assert t.offer("cam", frame, []) is False
    clock.t += 0.041
    assert t.offer("cam", frame, []) is True
    sch = t.status()["scheduling"]
    assert (sch["admitted"], sch["dropped_budget"], sch["dropped_queue"]) == (2, 1, 1)


def test_offer_respects_camera_list_window_and_state(storage, detector, monkeypatch):
    t = _running(detector, FakeRtmo())
    frame = np.zeros((10, 10, 3), np.uint8)
    monkeypatch.setattr(settings, "SHADOW_POSE_CAMERAS", "cam_x")
    assert t.offer("cam_y", frame, []) is False
    monkeypatch.setattr(settings, "SHADOW_POSE_CAMERAS", "")
    monkeypatch.setattr(t, "_window_open", lambda: False)
    assert t.offer("cam_y", frame, []) is False
    monkeypatch.setattr(t, "_window_open", lambda: True)
    t.state = "loading"
    assert t.offer("cam_y", frame, []) is False


def test_device_gate_yields_to_live_runs():
    gate = DeviceGate()
    with gate:                                       # a live run on the device
        assert gate.try_low() is False
    assert gate.try_low() is True                    # idle: the trial may run
    gate.release_low()

    # A live run waiting for the device: the trial does not jump the queue.
    gate.lock.acquire()
    waiter = threading.Thread(target=lambda: gate.__enter__() and gate.__exit__())
    waiter.start()
    while gate.waiting == 0:
        time.sleep(0.001)
    gate.lock.release()                              # the waiter now gets it
    waiter.join()
    assert gate.low_priority_delays()["count"] == 0

    # A live run arriving during a trial run waits for that one run, and it is counted.
    assert gate.try_low() is True
    done = threading.Event()

    def live():
        with gate:
            done.set()

    th = threading.Thread(target=live)
    th.start()
    while gate.waiting == 0:
        time.sleep(0.001)
    assert gate.try_low() is False                   # a second trial run cannot start meanwhile
    time.sleep(0.02)
    gate.release_low()
    th.join()
    assert done.is_set()
    d = gate.low_priority_delays()
    assert d["count"] == 1 and d["max_ms"] >= 15


def test_shadow_never_waits_on_a_busy_device_and_adds_no_live_load(storage, detector, monkeypatch):
    monkeypatch.setattr(st_mod, "DEVICE_WAIT_SEC", 0.05)
    session = FakeRtmo()
    t = _running(detector, session)
    frame = np.full((640, 640, 3), 100, np.uint8)
    busy_before = detector.load_metrics()["busy_ms"]
    with detector.device_gate:                       # the live model holds the device throughout
        assert t.offer("cam", frame, []) is True
        job, t._job = t._job, None
        started = time.perf_counter()
        ms = t.run_job(job)
        waited = time.perf_counter() - started
    assert ms < 0 and session.calls == 0 and waited < 1.0
    assert t.status()["scheduling"]["dropped_device_busy"] == 1
    assert t._next_allowed > t._clock()              # backs off instead of copying frames for nothing
    # Idle device: it runs, and its time is not added to the live model's busy total.
    t._next_allowed = 0.0
    assert t.offer("cam", frame, [_det(100, 100, 200, 400)]) is True
    job, t._job = t._job, None
    assert t.run_job(job) >= 0 and session.calls == 1
    assert detector.load_metrics()["busy_ms"] == busy_before
    tot = t.status()["totals"]
    assert tot["pairs"] == 1 and tot["matched"] == 1


def test_live_scheduler_admits_the_same_frames_with_a_trial_running(storage, detector, monkeypatch):
    """The trial takes nothing from the live schedule: same admissions, same measured load."""
    monkeypatch.setattr(settings, "ANALYTICS_SCHEDULER", True)

    def admissions(with_trial: bool) -> list[bool]:
        clock = Clock(0.0)
        sch = InferenceScheduler(detector=SimpleNamespace(available=False), clock=clock, rng=lambda: 0.0,
                                 run_async=False)
        trial = _running(detector, FakeRtmo(), Clock(0.0)) if with_trial else None
        out = []
        frame = np.full((64, 64, 3), 100, np.uint8)
        for i in range(200):
            clock.t = i * 0.04
            ok = sch.admit("cam", fps=25.0, frame_index=i)
            out.append(ok)
            if ok:
                if trial is not None and trial.offer("cam", frame, []):
                    job, trial._job = trial._job, None
                    trial.run_job(job)
                sch.done("cam")
        return out

    assert admissions(True) == admissions(False)


# ------------------------------------------------------------------ status + endpoint


def test_status_before_any_data_has_no_figures(detector):
    t = ShadowTrial(detector=detector, start_threads=False)
    s = t.status()
    assert s["state"] == "off" and s["totals"] is None and s["samples"] == [] and s["shadow_only"] is True


def test_missing_model_file_fails_honestly(storage, detector, monkeypatch):
    monkeypatch.setattr(settings, "SHADOW_POSE_MODEL", "does-not-exist.onnx")
    t = ShadowTrial(detector=detector, start_threads=False)
    t.start()
    assert t.state == "loading"
    assert t._load() is False and t.state == "failed" and "not found" in t.reason


def test_endpoint_requires_sign_in_and_reports_the_trial(client):  # noqa: F811 - fixture
    assert client.get("/api/v1/system/shadow-trial").status_code == 401
    assert client.get("/api/v1/system/shadow-trial/samples/20260101T000000_cam.jpg").status_code == 401
    tok = _create_admin(client, _issued_code(client), password="correct horse 1").json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    r = client.get("/api/v1/system/shadow-trial", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] in ("off", "loading", "compiling", "running", "failed") and body["shadow_only"] is True
    assert client.get("/api/v1/system/shadow-trial/samples/..%2Fstats.json", headers=hdr).status_code == 404
    assert client.get("/api/v1/system/shadow-trial/samples/nope.jpg", headers=hdr).status_code == 404


MODELS = Path(__file__).resolve().parents[1] / "models"


@pytest.mark.skipif(not ((MODELS / "yolo26n-pose.onnx").exists()
                         and (MODELS / "rtmo-s-body7-640x640-static.onnx").exists()),
                    reason="yolo26n-pose.onnx / rtmo-s-body7-640x640-static.onnx not present")
def test_real_trial_end_to_end_on_cpu(storage, monkeypatch):
    """The real loader and worker threads: live YOLO26n-pose vs shadow RTMO-s on a bundled picture."""
    import cv2

    from app.services.inference_backend import person_threshold

    monkeypatch.setattr(settings, "INFERENCE_DISABLED_PROVIDERS", "tensorrt,cuda,migraphx,rocm,openvino,directml")
    monkeypatch.setattr(settings, "POSE_REFINER", "off")
    monkeypatch.setattr(settings, "SHADOW_POSE_MODEL", "rtmo-s-body7-640x640-static.onnx")
    monkeypatch.setattr(settings, "SHADOW_POSE_SHARE", 0.5)
    live = PersonDetector(model_path=MODELS / "yolo26n-pose.onnx", object_model_path="")
    assert live.initialise()["available"]
    trial = ShadowTrial(detector=live)
    trial.start()
    try:
        deadline = time.time() + 60
        while trial.state in ("loading", "compiling") and time.time() < deadline:
            time.sleep(0.1)
        assert trial.state == "running", trial.reason
        st = trial.status()
        assert st["provider"] == "cpu" and st["weights"].endswith(".pth") and st["model_sha256"]
        frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "bus.jpg"))
        people = [d for d in live.detect(frame, conf_threshold=settings.TRACK_LOW_CONF_THRESHOLD)
                  if d.confidence >= person_threshold(d)]
        assert people and trial.offer("cam_bus", frame, people)
        while trial.status()["totals"] is None and time.time() < deadline:
            time.sleep(0.05)
        tot = trial.status()["totals"]
        assert tot["pairs"] == 1 and tot["persons_main"] == len(people)
        assert tot["matched"] >= 3 and tot["persons_shadow"] >= 3        # the four people in bus.jpg
        assert tot["device_ms"]["shadow_mean"] > 0 and tot["device_ms"]["main_mean"] > 0
        assert live.load_metrics()["frames"] == 1                        # the trial ran no live inference
    finally:
        trial.stop()
