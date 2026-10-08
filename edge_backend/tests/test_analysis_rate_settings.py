"""Adjustable analysis rate: where the GPU budget goes, and the settings that steer it.

* inference_scheduler: weighted max-min split (analysis_priority), per-camera
  caps (max_analysis_fps), the site ceiling ANALYTICS_MAX_DETECT_FPS, quiet
  cameras at ANALYTICS_IDLE_DETECT_FPS, motion wake, night-watch boost intact;
* motion_gate: what counts as motion, and its cost per frame;
* site settings (group "analysis") applied live, DECODE_MAX_FPS reopening a
  running camera's stream, GET /api/v1/system/analysis-capacity.

Fake clock and fake detector; no GPU, camera or server.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.config import settings
from app.services import inference_scheduler as isch
from app.services import live_analytics_engine as lae
from app.services.inference_scheduler import InferenceScheduler, allocate_rates
from app.services.motion_gate import MotionGate
from tests.test_inference_scheduler import Clock, FakeDetector

STORE_MS = 10.9          # RTMO-s 640x640 on the store's RX 9060 XT (MIGraphX), measured
CAMS32 = [f"cam{i:02d}" for i in range(32)]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for key, value in {"ANALYTICS_SCHEDULER": True, "POSE_BUDGET_UTILISATION": 0.6,
                       "ANALYTICS_MIN_DETECT_FPS": 1.0, "ANALYTICS_TARGET_DETECT_FPS": 2.0,
                       "ANALYTICS_REFIT_DEBOUNCE_SEC": 30.0, "ANALYTICS_MAX_INFLIGHT": 4,
                       "ANALYTICS_DETECT_EVERY_N_FRAMES": 5, "RECORDING_FPS": 25,
                       "ANALYTICS_MAX_DETECT_FPS": 10.0, "ANALYTICS_IDLE_DETECT_FPS": 0.5,
                       "ANALYTICS_ACTIVE_HOLD_SEC": 4.0, "ANALYTICS_MOTION_WAKE": True,
                       "DECODE_MAX_FPS": 10.0, "POSE_REFINER": "off"}.items():
        monkeypatch.setattr(settings, key, value)


def _sched(ms: float = STORE_MS):
    clock = Clock()
    det = FakeDetector({"rtmo.onnx": ms}, refiner=False)
    return InferenceScheduler(detector=det, clock=clock, rng=lambda: 0.5, run_async=False), clock, det


def _settle(s: InferenceScheduler, clock: Clock, people: dict[str, int], fps: float = 10.0) -> None:
    """Every camera streams at ``fps``; ``people`` per camera after the activity hold."""
    for cam in people:
        if s.admit(cam, fps=fps, frame_index=1):
            s.done(cam)
    clock.t += float(settings.ANALYTICS_ACTIVE_HOLD_SEC) + 1.0
    for cam, n in people.items():
        if s.admit(cam, fps=fps, frame_index=100):
            s.done(cam)
        s.set_activity(cam, n)
    s.tick()


def allocation(util: float, busy: int, n: int = 32, ms: float = STORE_MS) -> dict:
    """The real scheduler's split for ``n`` cameras, ``busy`` of them with people."""
    settings.POSE_BUDGET_UTILISATION = util
    s, clock, _ = _sched(ms)
    cams = CAMS32[:n]
    _settle(s, clock, {c: (1 if i < busy else 0) for i, c in enumerate(cams)})
    rates = {c: s.camera_status(c)["detect_fps_allocated"] for c in cams}
    st = s.status()
    return {"budget": st["budget_frames_per_s"], "active_fps": rates[cams[0]],
            "quiet_fps": rates[cams[-1]] if busy < n else None,
            "total": round(sum(rates.values()), 1), "quiet": st["cameras_quiet"], "rates": rates}


# ------------------------------------------------------------ allocation maths


def test_weighted_split_and_caps():
    # 30 frames/s over: a high-priority camera (weight 2), two normal, one capped at 3 fps.
    rates = allocate_rates(30.0, {"hi": 10.0, "a": 10.0, "b": 10.0, "capped": 3.0}, floor=1.0,
                           weights={"hi": 2.0, "a": 1.0, "b": 1.0, "capped": 1.0})
    assert rates["capped"] == pytest.approx(3.0)
    # 27 left over weights 2:1:1 -> 13.5, 6.75, 6.75; the high one is capped at 10 -> 17 for a and b.
    assert rates["hi"] == pytest.approx(10.0)
    assert rates["a"] == rates["b"] == pytest.approx(8.5)
    assert sum(rates.values()) == pytest.approx(30.0)
    # Without weights it is the old equal max-min split.
    assert allocate_rates(10.0, {"slow": 0.4, "a": 5.0, "b": 5.0}, 1.0) == pytest.approx(
        {"slow": 0.4, "a": 4.8, "b": 4.8})


def test_low_priority_gets_half_and_floor_still_holds():
    rates = allocate_rates(18.0, {"lo": 10.0, "n1": 10.0, "n2": 10.0, "n3": 10.0, "n4": 10.0}, floor=1.0,
                           weights={"lo": 0.5})
    assert rates["lo"] == pytest.approx(2.0) and rates["n1"] == pytest.approx(4.0)
    # Starved budget: the floor (1 fps) is still guaranteed to everyone.
    tight = allocate_rates(2.0, {"lo": 10.0, "a": 10.0, "b": 10.0}, floor=1.0, weights={"lo": 0.5})
    assert all(r >= 1.0 for r in tight.values())


def test_store_allocation_32_cameras():
    """32 D1 cameras at 10.9 ms per frame: the busy cameras get the budget, quiet ones 0.5 fps."""
    a = allocation(0.6, 4)
    assert a["budget"] == pytest.approx(0.6 * 1000 / STORE_MS, abs=0.1)        # 55 frames/s
    assert a["quiet"] == 28 and a["quiet_fps"] == 0.5
    assert a["active_fps"] == 10.0                                             # at the ceiling (was 1.7)
    a = allocation(0.6, 8)
    assert a["active_fps"] == pytest.approx((0.6 * 1000 / STORE_MS - 24 * 0.5) / 8, abs=0.01)
    a = allocation(0.6, 16)
    assert a["active_fps"] == pytest.approx((0.6 * 1000 / STORE_MS - 16 * 0.5) / 16, abs=0.01)
    assert a["total"] <= a["budget"] + 0.1
    a = allocation(0.75, 8)
    assert a["active_fps"] == pytest.approx((0.75 * 1000 / STORE_MS - 24 * 0.5) / 8, abs=0.01)
    # Everyone busy: the old equal split, every camera above the floor.
    a = allocation(0.6, 32)
    assert a["quiet"] == 0 and a["active_fps"] == pytest.approx(0.6 * 1000 / STORE_MS / 32, abs=0.01)


def test_priority_and_camera_cap_in_the_scheduler():
    s, clock, _ = _sched()
    s.configure("cam00", priority="high")
    s.configure("cam01", max_fps=2.0)
    s.configure("cam02", priority="low")
    _settle(s, clock, {c: (1 if i < 12 else 0) for i, c in enumerate(CAMS32)})
    st = {c: s.camera_status(c) for c in CAMS32}
    budget = 0.6 * 1000 / STORE_MS
    rest = budget - 20 * 0.5 - 2.0                    # quiet cameras, then cam01 at its cap
    unit = rest / (2.0 + 0.5 + 9 * 1.0)               # weights: high 2, low 0.5, nine normal
    assert st["cam01"]["detect_fps_allocated"] == 2.0 and st["cam01"]["ceiling_fps"] == 2.0
    assert st["cam00"]["detect_fps_allocated"] == pytest.approx(2 * unit, abs=0.01)
    assert st["cam02"]["detect_fps_allocated"] == pytest.approx(0.5 * unit, abs=0.01)
    assert st["cam03"]["detect_fps_allocated"] == pytest.approx(unit, abs=0.01)
    assert st["cam00"]["priority"] == "high" and st["cam00"]["weight"] == 2.0
    assert st["cam20"]["quiet"] and st["cam20"]["detect_fps_allocated"] == 0.5


def test_site_ceiling_and_delivered_fps_bound_the_rate(monkeypatch):
    s, clock, _ = _sched(1.0)                          # a cheap model: the ceiling binds
    _settle(s, clock, {"a": 1})
    assert s.camera_status("a")["detect_fps_allocated"] == 10.0
    monkeypatch.setattr(settings, "ANALYTICS_MAX_DETECT_FPS", 4.0)
    s.reallocate()
    assert s.camera_status("a")["detect_fps_allocated"] == 4.0
    monkeypatch.setattr(settings, "ANALYTICS_MAX_DETECT_FPS", 30.0)
    s.reallocate()
    assert s.camera_status("a")["detect_fps_allocated"] == 10.0   # never above the 10 delivered fps


def test_ten_fps_camera_is_analysed_at_ten_fps():
    """No more every-5th-frame cap: a busy camera alone is analysed at its delivered rate."""
    s, clock, det = _sched(1.0)
    got = 0
    for f in range(1, 201):                            # 20 s at 10 fps
        clock.t += 0.1
        if s.admit("a", fps=10.0, frame_index=f):
            got += 1
            det.infer()
            s.done("a")
        s.set_activity("a", 1)
    assert got >= 190


def test_quiet_camera_runs_at_the_idle_rate_then_motion_wakes_it_at_once():
    s, clock, det = _sched()
    _settle(s, clock, {c: 0 for c in CAMS32[:4]})
    assert s.camera_status("cam00")["quiet"] and s.camera_status("cam00")["detect_fps_allocated"] == 0.5
    frame = {"n": 1000}

    def run(seconds: float) -> int:
        n = 0
        for _ in range(int(seconds * 10)):
            clock.t += 0.1
            frame["n"] += 1
            for c in CAMS32[:4]:
                if s.admit(c, fps=10.0, frame_index=frame["n"]):
                    s.done(c)
                    if c == "cam00":
                        n += 1
                    s.set_activity(c, 0)
        return n

    assert run(10.0) in (4, 5, 6)                     # 0.5 fps for 10 s
    # Motion: woken, and the very next frame is analysed.
    assert s.motion("cam00") is True
    assert s.camera_status("cam00")["quiet"] is False
    assert s.camera_status("cam00")["detect_fps_allocated"] == 10.0
    clock.t += 0.1
    frame["n"] += 1
    assert s.admit("cam00", fps=10.0, frame_index=frame["n"]) is True
    s.done("cam00")
    assert s.motion("cam00") is False                 # already awake: no second wake
    assert s.camera_status("cam00")["motion_wakes"] == 1
    # Nothing found and no more motion: quiet again after the hold.
    clock.t += float(settings.ANALYTICS_ACTIVE_HOLD_SEC) + 0.5
    s.admit("cam00", fps=10.0, frame_index=frame["n"] + 100)
    s.set_activity("cam00", 0)
    s.tick()
    assert s.camera_status("cam00")["quiet"] is True


def test_people_found_on_an_idle_frame_wake_the_camera():
    s, clock, _ = _sched()
    _settle(s, clock, {"a": 0, "b": 0})
    assert s.camera_status("a")["detect_fps_allocated"] == 0.5
    s.set_activity("a", 2)
    assert s.camera_status("a")["quiet"] is False and s.camera_status("a")["detect_fps_allocated"] == 10.0
    assert s.camera_status("a")["people"] == 2


def test_night_watch_boost_still_runs_at_the_ceiling_on_a_quiet_camera():
    s, clock, _ = _sched()
    _settle(s, clock, {c: 0 for c in CAMS32})
    s.hold("cam05", True)
    assert s.camera_status("cam05")["night_hold"]
    s.boost("cam05", 3.0)
    st = s.camera_status("cam05")
    assert st["boosted"] and st["detect_fps_allocated"] == st["ceiling_fps"] == 10.0
    clock.t += 0.01
    assert s.admit("cam05", fps=10.0, frame_index=500) is True


def test_camera_that_never_reports_activity_is_never_quiet():
    s, clock, _ = _sched()
    s.admit("legacy", fps=10.0, frame_index=1)
    clock.t += 30.0
    s.admit("legacy", fps=10.0, frame_index=2)
    s.tick()
    assert s.camera_status("legacy")["quiet"] is False and s.camera_status("legacy")["people"] is None


def test_capacity_shape():
    s, clock, _ = _sched()
    s.configure("cam00", priority="high", max_fps=6.0)
    _settle(s, clock, {"cam00": 1, "cam01": 0})
    cap = s.capacity()
    assert cap["cost_ms"] == pytest.approx(STORE_MS) and cap["budget_per_sec"] == pytest.approx(55.0, abs=0.1)
    row = cap["cameras"]["cam00"]
    assert row["priority"] == "high" and row["max_fps"] == 6.0 and row["active"] is True
    assert row["allocated_fps"] == 6.0 and isinstance(row["measured_fps"], float)
    assert cap["cameras"]["cam01"]["active"] is False and cap["cameras"]["cam01"]["quiet"] is True
    assert cap["total_allocated"] == pytest.approx(6.5)


# ---------------------------------------------------------------- motion gate


def _scene(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, (576 // 16, 704 // 16), np.uint8)
    img = np.kron(base, np.ones((16, 16), np.uint8))           # textured shelves
    return np.dstack([img, img, img])


def _noisy(img: np.ndarray, rng) -> np.ndarray:
    return np.clip(img.astype(np.int16) + rng.normal(0, 4, img.shape).astype(np.int16), 0, 255).astype(np.uint8)


def test_motion_gate_sees_a_walker_not_noise_or_lights():
    rng = np.random.default_rng(7)
    scene = _scene()
    g = MotionGate(threshold=14, min_pixels=4, persist=2)
    # Sensor noise on a still picture: never motion.
    assert not any(g.update(_noisy(scene, rng)) for _ in range(50))
    # The camera re-exposes (every pixel +60): the median change is removed, not motion.
    brighter = np.clip(scene.astype(np.int16) + 60, 0, 255).astype(np.uint8)
    assert not any(g.update(_noisy(brighter, rng)) for _ in range(3))
    for _ in range(30):
        g.update(_noisy(scene, rng))
    # The lights come on (every pixel x1.4): the brightness is normalised, not motion.
    lit = np.clip(scene.astype(np.float32) * 1.4, 0, 255).astype(np.uint8)
    assert not any(g.update(_noisy(lit, rng)) for _ in range(5))
    # A picture that changes almost everywhere at once (IR switch): a lighting event.
    ir = 255 - lit
    assert not any(g.update(_noisy(ir, rng)) for _ in range(2))
    assert g.lighting_events >= 1
    for _ in range(3):
        g.update(_noisy(lit, rng))
    # A person-sized dark block (60 x 160 px) walking across: motion on the second frame.
    hits = []
    for i in range(6):
        f = lit.copy()
        x = 100 + 40 * i
        f[300:460, x:x + 60] = 20
        hits.append(g.update(_noisy(f, rng)))
    assert hits[0] is False and any(hits[1:3])


def test_motion_gate_ignores_ai_ignore_areas_and_works_on_nv12():
    from app.services.capture_backends import Frame
    import cv2

    rng = np.random.default_rng(3)
    scene = _scene(2)
    g = MotionGate(threshold=14, min_pixels=4, persist=1)
    # Ignore the left half (thumbnail pixels): a flickering screen there.
    g.set_ignore([np.array([[0, 0], [31, 0], [31, 47], [0, 47]])])

    def nv12(bgr):
        i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
        return Frame(nv12=i420, width=bgr.shape[1], height=bgr.shape[0])

    for _ in range(10):
        g.update(nv12(_noisy(scene, rng)))
    flicker = scene.copy()
    flicker[100:300, 50:250] = 255 - flicker[100:300, 50:250]
    assert not g.update(nv12(_noisy(flicker, rng)))
    walker = scene.copy()
    walker[300:460, 500:560] = 10
    assert g.update(nv12(_noisy(walker, rng)))


def test_motion_gate_cost_per_frame():
    """Report the gate's CPU cost on D1 frames (BGR and NV12); it must stay tiny."""
    from app.services.capture_backends import Frame
    import cv2

    rng = np.random.default_rng(0)
    bgr = _noisy(_scene(), rng)
    frames = {"bgr_704x576": bgr,
              "nv12_704x576": Frame(nv12=cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420), width=704, height=576)}
    out = {}
    for name, f in frames.items():
        g = MotionGate()
        for _ in range(30):
            g.update(f)
        n = 1000
        t0 = time.perf_counter()
        for _ in range(n):
            g.update(f)
        out[name] = (time.perf_counter() - t0) / n * 1e6
    print("motion gate us/frame:", {k: round(v, 1) for k, v in out.items()})
    # 32 cameras x 10 fps x this must be a small part of one core even on a busy test box.
    assert all(v < 1000 for v in out.values())


# ------------------------------------------------------------- the worker side


class _FakeCv:
    """Enough of cv2.VideoCapture for ThinnedCapture: a fake clock advances per grab."""

    def __init__(self, clock, opened: list):
        self.clock, self.released = clock, False
        opened.append(self)

    def get(self, prop):
        import cv2

        return 25.0 if prop == cv2.CAP_PROP_FPS else self.clock.t * 1000.0

    def grab(self):
        self.clock.t += 0.04
        return not self.released

    def retrieve(self):
        return True, np.zeros((288, 352, 3), np.uint8)

    def isOpened(self):
        return not self.released

    def set(self, *a):
        return True

    def release(self):
        self.released = True


def test_decode_fps_change_reopens_the_stream_and_keeps_tracks(monkeypatch):
    from app.services import capture_backends

    clock = Clock()

    class FakeTime:
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return clock.t

    monkeypatch.setattr(lae, "time", FakeTime())
    monkeypatch.setattr(capture_backends.time, "monotonic", lambda: clock.t)
    det = FakeDetector({"m.onnx": 1.0}, refiner=False)
    sched = InferenceScheduler(detector=det, clock=clock, rng=lambda: 0.5, run_async=False)
    monkeypatch.setattr(lae, "inference_scheduler", sched)
    engine = lae.LiveAnalyticsEngine()
    rt = lae.CameraRuntime(camera_id="cam_fps", name="c", source="rtsp://10.0.0.9/x")
    w = lae.CameraWorker(rt, engine)
    opened: list = []
    events: list = []

    def fake_open(src=None):
        events.append(("open", float(settings.DECODE_MAX_FPS)))
        cap = capture_backends.ThinnedCapture(_FakeCv(clock, opened), settings.DECODE_MAX_FPS)
        real_release = cap.release

        def release():
            events.append(("release", cap.max_fps))
            real_release()
            if len(opened) >= 2 and cap is not None and cap.max_fps == 5.0:
                w._stop.set()

        cap.release = release
        return cap

    closed = []
    monkeypatch.setattr(w, "_preflight", lambda src: None)
    monkeypatch.setattr(w, "_open", fake_open)
    monkeypatch.setattr(w, "_after_first_frame", lambda cap, src, frame: cap)
    monkeypatch.setattr(w, "_clip_wanted", lambda: False)
    monkeypatch.setattr(w, "_analyse", lambda f, now: None)
    monkeypatch.setattr(engine, "close_track", lambda t, reason="", persist=None: closed.append(reason))
    reads = {"n": 0}
    real_read = capture_backends.read_frame

    def read(cap):
        reads["n"] += 1
        if reads["n"] == 40:                       # ~4 s in: the operator lowers the decode rate
            monkeypatch.setattr(settings, "DECODE_MAX_FPS", 5.0)
        if reads["n"] > 400:
            w._stop.set()
        return real_read(cap)

    monkeypatch.setattr(capture_backends, "read_frame", read)
    w.run()
    assert events[0] == ("open", 10.0)
    # The old capture was released before the new one opened, at the new rate.
    assert events[1] == ("release", 10.0) and events[2] == ("open", 5.0)
    assert opened[0].released
    assert "frame_size_changed" not in closed           # same frame size: tracks are kept
    assert sum(1 for e in events if e[0] == "open") == 2


def test_worker_motion_wakes_a_quiet_camera(monkeypatch):
    s, clock, _ = _sched()
    monkeypatch.setattr(lae, "inference_scheduler", s)
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="cam_mw", name="c", source="x"), lae.LiveAnalyticsEngine())
    _settle(s, clock, {"cam_mw": 0})
    assert s.camera_status("cam_mw")["quiet"]
    scene = _scene()
    for _ in range(5):
        w._motion_step(scene, clock.t)
    moving = scene.copy()
    for i in range(3):
        moving = scene.copy()
        moving[300:460, 100 + 40 * i:160 + 40 * i] = 10
        w._motion_step(moving, clock.t)
    assert s.camera_status("cam_mw")["quiet"] is False and s.camera_status("cam_mw")["motion_wakes"] == 1
    # Switched off: the gate does not run (and so wakes nothing).
    monkeypatch.setattr(settings, "ANALYTICS_MOTION_WAKE", False)
    frames = w._motion_gate.frames
    w._motion_step(moving, clock.t)
    assert w._motion_gate.frames == frames


# --------------------------------------------------------------- site settings


def _make_db(path):
    import sqlite3

    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE system_setup (key VARCHAR(128) PRIMARY KEY, value VARCHAR(4096) NOT NULL, "
              "updated_at DATETIME)")
    c.commit()
    c.close()
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services import site_settings as ss

    for spec in ss.SPECS:
        monkeypatch.setattr(settings, spec.key, getattr(settings, spec.key))
    db = _make_db(tmp_path / "site.db")
    st = ss.SiteSettingsStore(db_path_fn=lambda: db)
    st.load()
    return st


def test_analysis_settings_are_site_settings(store):
    from app.services import site_settings as ss

    keys = ss.GROUP_KEYS["analysis"]
    assert set(keys) == {"POSE_BUDGET_UTILISATION", "DECODE_MAX_FPS", "ANALYTICS_MAX_DETECT_FPS",
                         "ANALYTICS_IDLE_DETECT_FPS", "ANALYTICS_MOTION_WAKE"}
    d = store.describe()["settings"]
    assert d["DECODE_MAX_FPS"]["applies"] == "reconnect" and d["POSE_BUDGET_UTILISATION"]["applies"] == "live"
    assert d["POSE_BUDGET_UTILISATION"]["min"] == 0.2 and d["POSE_BUDGET_UTILISATION"]["max"] == 0.95
    assert d["ANALYTICS_MOTION_WAKE"]["type"] == "bool"
    for key, bad in (("POSE_BUDGET_UTILISATION", 0.99), ("DECODE_MAX_FPS", 1), ("DECODE_MAX_FPS", 26),
                     ("ANALYTICS_MAX_DETECT_FPS", 0), ("ANALYTICS_IDLE_DETECT_FPS", 6),
                     ("ANALYTICS_MOTION_WAKE", "maybe")):
        with pytest.raises(ValueError):
            ss.validate_one(key, bad)


def test_analysis_settings_apply_live_and_reallocate(store, monkeypatch):
    calls = []
    monkeypatch.setattr(isch.inference_scheduler, "reallocate", lambda: calls.append(1))
    store.update({"ANALYTICS_MAX_DETECT_FPS": 6, "ANALYTICS_IDLE_DETECT_FPS": 1.0,
                  "POSE_BUDGET_UTILISATION": 0.75, "ANALYTICS_MOTION_WAKE": False, "DECODE_MAX_FPS": 8},
                 actor="test")
    assert settings.ANALYTICS_MAX_DETECT_FPS == 6.0 and settings.ANALYTICS_IDLE_DETECT_FPS == 1.0
    assert settings.POSE_BUDGET_UTILISATION == 0.75 and settings.ANALYTICS_MOTION_WAKE is False
    assert settings.DECODE_MAX_FPS == 8.0
    assert calls == [1]
    # A running scheduler picks the new budget share up on reallocate.
    s, clock, _ = _sched()
    _settle(s, clock, {c: 1 for c in CAMS32})
    assert s.status()["budget_frames_per_s"] == pytest.approx(0.75 * 1000 / STORE_MS, abs=0.1)
    store.reset("POSE_BUDGET_UTILISATION", actor="test")
    s.reallocate()
    assert s.status()["budget_frames_per_s"] == pytest.approx(
        float(settings.POSE_BUDGET_UTILISATION) * 1000 / STORE_MS, abs=0.1)


def test_per_camera_rate_fields_validate_and_survive_an_older_client():
    from app.models.schemas import CameraFeatureConfig
    from app.routes.cameras import _NON_FLAG_SETTINGS, _keep_unsent_settings

    cfg = CameraFeatureConfig(analysis_priority="high", max_analysis_fps=7.5)
    assert cfg.analysis_priority == "high" and cfg.max_analysis_fps == 7.5
    for bad in ({"analysis_priority": "urgent"}, {"max_analysis_fps": 0.2}, {"max_analysis_fps": 31}):
        with pytest.raises(ValueError):
            CameraFeatureConfig(**bad)
    assert {"analysis_priority", "max_analysis_fps"} <= set(_NON_FLAG_SETTINGS)
    merged = CameraFeatureConfig(people_counting=False).model_dump()
    _keep_unsent_settings(merged, {"people_counting": False},
                          {"analysis_priority": "low", "max_analysis_fps": 3.0})
    assert merged["analysis_priority"] == "low" and merged["max_analysis_fps"] == 3.0


def test_worker_pushes_camera_rate_settings_to_the_scheduler(monkeypatch):
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    s, clock, _ = _sched()
    monkeypatch.setattr(lae, "inference_scheduler", s)
    feature_manager.set_camera_features("cam_cfg", CameraFeatureConfig(analysis_priority="low", max_analysis_fps=3))
    try:
        w = lae.CameraWorker(lae.CameraRuntime(camera_id="cam_cfg", name="c", source="x"), lae.LiveAnalyticsEngine())
        w._rate_settings()
        _settle(s, clock, {"cam_cfg": 1})
        st = s.camera_status("cam_cfg")
        assert st["priority"] == "low" and st["max_fps_setting"] == 3.0 and st["ceiling_fps"] == 3.0
    finally:
        feature_manager.remove_camera("cam_cfg")


def test_capacity_endpoint(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.models.schemas import CameraFeatureConfig
    from app.routes import system
    from app.services.auth_service import auth_service
    from app.services.feature_manager import feature_manager

    s, clock, _ = _sched()
    monkeypatch.setattr(isch, "inference_scheduler", s)
    engine = lae.LiveAnalyticsEngine()
    engine.runtimes["cap_a"] = lae.CameraRuntime(camera_id="cap_a", name="Aisle 1", source="x")
    engine.runtimes["cap_b"] = lae.CameraRuntime(camera_id="cap_b", name="Back door", source="x")
    engine.runtimes["cap_off"] = lae.CameraRuntime(camera_id="cap_off", name="Office", source="x")
    monkeypatch.setattr(system, "live_engine", engine)
    feature_manager.set_camera_features("cap_a", CameraFeatureConfig(analysis_priority="high"))
    feature_manager.set_camera_features("cap_off", CameraFeatureConfig(max_analysis_fps=4))
    s.configure("cap_a", priority="high")
    _settle(s, clock, {"cap_a": 2, "cap_b": 0})
    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[auth_service.verify_api_access] = lambda: None
    try:
        body = TestClient(app).get("/api/v1/system/analysis-capacity").json()
    finally:
        for c in ("cap_a", "cap_off"):
            feature_manager.remove_camera(c)
    assert body["cost_ms"] == pytest.approx(STORE_MS)
    assert body["budget_per_sec"] == pytest.approx(55.0, abs=0.1)
    rows = {r["camera_id"]: r for r in body["cameras"]}
    assert [r["name"] for r in body["cameras"]] == ["Aisle 1", "Back door", "Office"]
    for r in body["cameras"]:
        assert {"camera_id", "name", "priority", "max_fps", "allocated_fps", "measured_fps", "active"} <= set(r)
    assert rows["cap_a"]["priority"] == "high" and rows["cap_a"]["active"] is True
    assert rows["cap_a"]["allocated_fps"] == 10.0 and rows["cap_a"]["max_fps"] == 10.0
    assert rows["cap_b"]["active"] is False and rows["cap_b"]["allocated_fps"] == 0.5
    # Not streaming: nothing allocated, nothing measured (null, not 0), its own cap.
    assert rows["cap_off"]["allocated_fps"] == 0.0 and rows["cap_off"]["measured_fps"] is None
    assert rows["cap_off"]["max_fps"] == 4.0 and rows["cap_off"]["active"] is False
    assert body["total_allocated"] == pytest.approx(10.5)
