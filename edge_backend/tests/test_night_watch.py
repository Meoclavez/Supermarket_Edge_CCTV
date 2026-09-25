"""Night watch (app/services/night_watch.py, routes/night_watch.py).

Schedules in store time across midnight and both 2026 Melbourne DST changes
(and with SITE_TIMEZONE unset, the host zone), the motion check on synthetic
sequences, the scheduler hold / confirmation boost, alert dispatch with
evidence, cooldown and cap, the settings API and the pipeline status fields.
No GPU, camera or server is used.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.config import settings
from app.models.schemas import NightWatchConfig
from app.services import live_analytics_engine as lae
from app.services import night_watch as nwm
from app.services.inference_backend import Detection
from app.services.night_watch import MotionDetector, NightWatch, in_schedule, next_transitions, time_info, windows
from tests.test_inference_scheduler import CAMS33, M544, Clock, FakeDetector, _sched, simulate

MEL = ZoneInfo("Australia/Melbourne")
UTC = timezone.utc


def mel(*a) -> float:
    return datetime(*a, tzinfo=MEL).timestamp()


def utc(*a) -> float:
    return datetime(*a, tzinfo=UTC).timestamp()


@pytest.fixture
def melbourne(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TIMEZONE", "Australia/Melbourne")


NIGHT = NightWatchConfig(enabled=True, start="22:00", end="06:00")


# =================================================================== schedule


def test_window_crosses_midnight(melbourne):
    assert in_schedule(NIGHT, mel(2026, 9, 25, 23, 30))
    assert in_schedule(NIGHT, mel(2026, 9, 26, 2, 13))
    assert in_schedule(NIGHT, mel(2026, 9, 26, 5, 59))
    assert not in_schedule(NIGHT, mel(2026, 9, 26, 6, 0))
    assert not in_schedule(NIGHT, mel(2026, 9, 25, 21, 59))
    assert not in_schedule(NIGHT, mel(2026, 9, 26, 13, 0))


def test_days_are_the_days_a_window_starts(melbourne):
    fri = NightWatchConfig(enabled=True, start="22:00", end="06:00", days=["fri"])
    assert in_schedule(fri, mel(2026, 9, 26, 2, 0))          # Saturday 02:00: Friday's night
    assert not in_schedule(fri, mel(2026, 9, 26, 23, 0))     # Saturday night: not selected
    assert not in_schedule(fri, mel(2026, 9, 24, 23, 0))     # Thursday night
    none = NightWatchConfig(enabled=True, days=[])
    assert not in_schedule(none, mel(2026, 9, 26, 2, 0))
    assert next_transitions(none, mel(2026, 9, 26, 2, 0)) == {"in_window": False, "next_arm": None,
                                                              "next_disarm": None}


def test_night_clocks_go_forward_is_seven_hours(melbourne):
    # 4 Oct 2026 02:00 AEST -> 03:00 AEDT.
    t = next_transitions(NIGHT, mel(2026, 10, 3, 21, 0))
    assert t["next_arm"] == utc(2026, 10, 3, 12, 0)               # 22:00 AEST
    assert t["next_disarm"] == utc(2026, 10, 3, 19, 0)            # 06:00 AEDT
    assert t["next_disarm"] - t["next_arm"] == 7 * 3600
    assert time_info(t["next_arm"])["label"] == "22:00 AEST"
    assert time_info(t["next_disarm"])["label"] == "06:00 AEDT"
    assert in_schedule(NIGHT, utc(2026, 10, 3, 18, 59))           # 05:59 AEDT
    assert not in_schedule(NIGHT, utc(2026, 10, 3, 19, 0))
    # The next night is in AEDT on both ends: 8 h again.
    later = next_transitions(NIGHT, utc(2026, 10, 3, 19, 30))
    assert later["next_disarm"] - later["next_arm"] == 8 * 3600
    assert time_info(later["next_arm"])["label"] == "22:00 AEDT"


def test_night_clocks_go_back_is_nine_hours(melbourne):
    # 5 Apr 2026 03:00 AEDT -> 02:00 AEST.
    t = next_transitions(NIGHT, mel(2026, 4, 4, 12, 0))
    assert t["next_arm"] == utc(2026, 4, 4, 11, 0)                # 22:00 AEDT
    assert t["next_disarm"] == utc(2026, 4, 4, 20, 0)             # 06:00 AEST
    assert t["next_disarm"] - t["next_arm"] == 9 * 3600
    # Both 02:30s of that night are inside the window.
    assert in_schedule(NIGHT, utc(2026, 4, 4, 15, 30)) and in_schedule(NIGHT, utc(2026, 4, 4, 16, 30))


def test_window_spanning_the_changes_uses_real_instants(melbourne):
    short = NightWatchConfig(enabled=True, start="01:30", end="03:30")
    # Forward: 02:00-02:59 does not exist; 01:30 AEST .. 03:30 AEDT is one real hour.
    (s, e), = [w for w in windows(short, mel(2026, 10, 4, 12, 0), 0, 0)]
    assert (s, e) == (utc(2026, 10, 3, 15, 30), utc(2026, 10, 3, 16, 30))
    # Back: 01:30 AEDT .. 03:30 AEST is three real hours (02:00-02:59 happens twice).
    (s, e), = [w for w in windows(short, mel(2026, 4, 5, 12, 0), 0, 0)]
    assert (s, e) == (utc(2026, 4, 4, 14, 30), utc(2026, 4, 4, 17, 30))
    # A start inside the skipped hour is read with the offset before the change.
    gap = NightWatchConfig(enabled=True, start="02:30", end="05:00")
    (s, _), = windows(gap, mel(2026, 10, 4, 12, 0), 0, 0)
    assert time_info(s)["label"] == "03:30 AEDT"


def test_every_day_all_day_never_disarms(melbourne):
    always = NightWatchConfig(enabled=True, start="00:00", end="00:00")
    t = next_transitions(always, mel(2026, 9, 26, 12, 0))
    assert t["in_window"] and t["next_disarm"] is None


def test_unset_site_timezone_uses_the_host_zone(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TIMEZONE", "")
    monkeypatch.setenv("TZ", "Australia/Melbourne")
    time.tzset()
    try:
        t = next_transitions(NIGHT, utc(2026, 10, 3, 11, 0))
        assert (t["next_arm"], t["next_disarm"]) == (utc(2026, 10, 3, 12, 0), utc(2026, 10, 3, 19, 0))
        assert time_info(t["next_disarm"])["label"] == "06:00 AEDT"
        z = nwm.zone_info(utc(2026, 9, 25, 16, 13))
        assert z["source"] == "host" and z["name"] == "Australia/Melbourne" and z["abbreviation"] == "AEST"
        monkeypatch.setattr(settings, "SITE_TIMEZONE", "Not/AZone")        # unknown: host zone too
        assert time_info(utc(2026, 9, 25, 16, 13))["label"] == "02:13 AEST"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_config_validation():
    cfg = NightWatchConfig(start="7:05", end="06:00", days=[0, "Tuesday", "sun", "mon"], sensitivity="HIGH")
    assert cfg.start == "07:05" and cfg.days == ["mon", "tue", "sun"] and cfg.sensitivity == "high"
    for bad in ({"start": "25:00"}, {"end": "xx"}, {"days": ["funday"]}, {"sensitivity": "max"},
                {"cooldown_sec": 1}):
        with pytest.raises(ValueError):
            NightWatchConfig(**bad)
    assert NightWatchConfig().enabled is False


# ===================================================================== motion

H, W = 288, 352
_rng = np.random.default_rng(7)
SCENE = np.clip(np.cumsum(_rng.normal(0, 6, (H, W, 3)), axis=1) % 180 + 40, 0, 255).astype(np.uint8)


def noisy(img: np.ndarray, sigma: float = 4.0, seed: int = 0) -> np.ndarray:
    r = np.random.default_rng(seed)
    return np.clip(img.astype(np.int16) + r.normal(0, sigma, img.shape), 0, 255).astype(np.uint8)


def with_blob(x: int, y: int, bw: int = 36, bh: int = 70, value: int = 15, seed: int = 0) -> np.ndarray:
    f = noisy(SCENE, seed=seed)
    f[y:y + bh, x:x + bw] = value
    return f


def run(md: MotionDetector, frames, masks=None):
    return [md.update(f, masks) for f in frames]


def test_static_noise_is_not_motion():
    md = MotionDetector("high")
    res = run(md, [noisy(SCENE, sigma=6, seed=i) for i in range(40)])
    assert res[0].warming
    assert not any(r.motion or r.hit or r.lighting for r in res)


def test_ir_switch_and_brightness_jumps_are_lighting_not_motion():
    md = MotionDetector("high")
    run(md, [noisy(SCENE, seed=i) for i in range(5)])
    # IR night mode: grayscale, brighter, different contrast.
    gray = SCENE.mean(axis=2, keepdims=True).repeat(3, axis=2)
    ir = np.clip(gray * 1.4 + 35, 0, 255).astype(np.uint8)
    res = run(md, [noisy(ir, seed=i) for i in range(20)])
    assert res[0].lighting
    assert not any(r.motion for r in res)
    # A moderate global change (auto exposure) is absorbed by the median shift.
    md2 = MotionDetector("medium")
    run(md2, [noisy(SCENE, seed=i) for i in range(5)])
    res = run(md2, [noisy(np.clip(SCENE.astype(np.int16) + 18, 0, 255).astype(np.uint8), seed=i)
                    for i in range(10)])
    assert not any(r.motion or r.hit for r in res)
    # Lights switched on: a big jump is a lighting event.
    res = run(md2, [noisy(np.clip(SCENE.astype(np.int16) + 70, 0, 255).astype(np.uint8), seed=i)
                    for i in range(10)])
    assert res[0].lighting and not any(r.motion for r in res)


def test_a_moving_blob_is_motion_after_persisting():
    md = MotionDetector("medium")
    run(md, [noisy(SCENE, seed=i) for i in range(5)])
    res = run(md, [with_blob(40 + 10 * i, 120, seed=i) for i in range(8)])
    persist = md.persist
    assert [r.motion for r in res[:persist - 1]] == [False] * (persist - 1)
    assert res[persist - 1].motion and all(r.motion for r in res[persist - 1:])
    x1, y1, x2, y2 = res[-1].box
    assert x1 < 40 + 10 * 7 + 36 and x2 > 40 + 10 * 7 and y1 < 190 and y2 > 120


def test_a_blob_below_the_minimum_area_is_not_motion():
    md = MotionDetector("low")
    run(md, [noisy(SCENE, seed=i) for i in range(5)])
    res = run(md, [with_blob(40 + 6 * i, 120, bw=8, bh=12, seed=i) for i in range(10)])
    assert not any(r.motion for r in res)


@pytest.mark.parametrize("mode", ["AI_IGNORE", "BLACKOUT"])
def test_motion_inside_an_ignore_region_or_privacy_mask_is_ignored(mode):
    mask = {"mask_mode": mode, "points": [{"x": 0.0, "y": 0.3}, {"x": 0.7, "y": 0.3},
                                          {"x": 0.7, "y": 0.8}, {"x": 0.0, "y": 0.8}]}
    md = MotionDetector("high")
    run(md, [noisy(SCENE, seed=i) for i in range(5)], [mask])
    res = run(md, [with_blob(40 + 10 * i, 120, seed=i) for i in range(10)], [mask])
    assert not any(r.motion for r in res)
    # The same movement outside the region is seen.
    res = run(md, [with_blob(40 + 10 * i, 10, bh=50, seed=i) for i in range(10)], [mask])
    assert any(r.motion for r in res)


def test_motion_cost_is_small_at_every_camera_size():
    for w, h in ((352, 288), (704, 576), (2560, 1440)):
        img = noisy(np.full((h, w, 3), 90, np.uint8))
        md = MotionDetector("medium")
        md.update(img)
        t0 = time.perf_counter()
        for _ in range(20):
            md.update(img)
        assert (time.perf_counter() - t0) / 20 < 0.03        # measured ~2 ms; generous for a loaded box


# ================================================================== scheduler


def test_held_cameras_get_nothing_and_the_others_share_rises():
    det, clock = FakeDetector({"m.onnx": M544}, refiner=False), Clock()
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 15.0)
    before = s.camera_status("cam00")["detect_fps_allocated"]
    held = CAMS33[:11]
    for c in held:
        s.hold(c, True)
    got = simulate(s, det, clock, CAMS33, 15.0)
    assert all(got[c] == 0 for c in held)
    after = s.camera_status("cam20")["detect_fps_allocated"]
    assert after > before * 1.3
    assert s.camera_status("cam00")["night_hold"] is True and s.status()["cameras_night_watch_held"] == 11
    for c in held:
        s.hold(c, False)
    got = simulate(s, det, clock, CAMS33, 10.0)
    assert all(got[c] > 0 for c in held)


def test_a_confirmation_boost_is_admitted_at_the_ceiling_then_held_again():
    det, clock = FakeDetector({"m.onnx": M544}, refiner=False), Clock()
    s = _sched(det, clock)
    simulate(s, det, clock, CAMS33, 15.0)
    s.hold("cam05", True)
    assert simulate(s, det, clock, CAMS33, 3.0)["cam05"] == 0
    s.boost("cam05", 4.0)
    got = simulate(s, det, clock, CAMS33, 4.0)
    assert got["cam05"] >= 4.0 * 5.0 * 0.8                # ceiling 25 fps / 5 = 5 per second
    clock.t += 0.05
    assert s.camera_status("cam05")["night_hold"] is True     # boost over
    assert simulate(s, det, clock, CAMS33, 3.0)["cam05"] == 0


# ============================================================== state machine


class ConfigStub:
    def __init__(self, cfg):
        self.cfg = cfg

    def __call__(self, camera_id):
        return self.cfg


@pytest.fixture
def watch(melbourne, monkeypatch, tmp_path):
    """A NightWatch on a fake clock, its own scheduler, and a recording dispatcher."""
    monkeypatch.setattr(settings, "NIGHT_WATCH_EVIDENCE_DIR", str(tmp_path / "nw"))
    monkeypatch.setattr(settings, "NIGHT_WATCH_MOTION_FPS", 4.0)
    monkeypatch.setattr(settings, "NIGHT_WATCH_CONFIRM_SEC", 4.0)
    monkeypatch.setattr(settings, "NIGHT_WATCH_CONFIRM_HITS", 2)
    monkeypatch.setattr(settings, "NIGHT_WATCH_MAX_EVENTS_PER_HOUR", 12)
    monkeypatch.setattr(settings, "NIGHT_WATCH_CLIP", False)
    det = FakeDetector({"m.onnx": M544}, refiner=False)
    clock = Clock(mel(2026, 9, 26, 2, 13))            # 02:13 AEST
    sched = _sched(det, clock)
    nw = NightWatch(scheduler=sched, clock=clock)
    nw.synchronous = True
    nw.config_override = ConfigStub(NightWatchConfig(enabled=True, start="22:00", end="06:00", cooldown_sec=120))
    nw.lighting_override = lambda cam: "day"
    calls = []

    async def dispatch(event_type, severity, title, body, data, push=True, broadcast=True):
        calls.append({"event_type": event_type, "severity": severity, "title": title, "body": body,
                      "data": data, "push": push, "broadcast": broadcast})
        return {"ok": True}

    nw.dispatcher_override = dispatch
    return nw, clock, sched, calls


def feed(nw, clock, frames, cam="cam_nw", dt=0.25):
    modes = []
    for f in frames:
        clock.t += dt
        modes.append(nw.step(cam, "Back door", f, masks=[]))
    return modes


def person_at(x, y, bw=36, bh=70, conf=0.8):
    return Detection(x1=float(x), y1=float(y), x2=float(x + bw), y2=float(y + bh), confidence=conf)


def test_armed_without_motion_runs_no_inference(watch):
    nw, clock, sched, _ = watch
    modes = feed(nw, clock, [noisy(SCENE, seed=i) for i in range(20)])
    assert set(modes) == {nwm.HOLD}
    assert nw.camera_status("cam_nw")["state"] == "armed_idle"
    assert not sched.admit("cam_nw", fps=25.0, frame_index=1000)
    assert sched.camera_status("cam_nw")["night_hold"] is True


def test_outside_the_window_is_the_normal_pipeline(watch):
    nw, clock, sched, _ = watch
    clock.t = mel(2026, 9, 26, 12, 0)
    assert set(feed(nw, clock, [noisy(SCENE, seed=i) for i in range(5)])) == {nwm.NORMAL}
    st = nw.camera_status("cam_nw")
    assert st["state"] == "disarmed" and st["armed"] is False
    assert st["next_arm"]["label"] == "22:00 AEST" and st["next_arm"]["date"] == "2026-09-26"


def test_when_dark_arms_outside_the_window(watch):
    nw, clock, _, _ = watch
    nw.config_override = ConfigStub(NightWatchConfig(enabled=True, start="22:00", end="06:00", when_dark=True))
    clock.t = mel(2026, 9, 26, 12, 0)
    nw.lighting_override = lambda cam: "ir"
    assert feed(nw, clock, [noisy(SCENE)])[-1] == nwm.HOLD
    assert nw.camera_status("cam_nw")["armed_by"] == "dark"
    nw.lighting_override = lambda cam: "day"
    assert feed(nw, clock, [noisy(SCENE)])[-1] == nwm.NORMAL


def _start_burst(nw, clock):
    feed(nw, clock, [noisy(SCENE, seed=i) for i in range(4)])
    modes = feed(nw, clock, [with_blob(40 + 10 * i, 120, seed=i) for i in range(6)])
    assert modes[-1] == nwm.CONFIRM
    return modes


def test_motion_then_person_is_a_high_alert_with_evidence_and_store_time(watch):
    nw, clock, sched, calls = watch
    _start_burst(nw, clock)
    assert nw.camera_status("cam_nw")["state"] == "motion"
    assert sched.admit("cam_nw", fps=25.0, frame_index=5000)        # confirmation admitted
    sched.done("cam_nw")
    frame = with_blob(100, 120)
    nw.confirm("cam_nw", frame, [person_at(100, 120)])
    assert not calls                                                # one hit is not enough
    nw.confirm("cam_nw", frame, [person_at(102, 121, conf=0.9)])
    assert nw.camera_status("cam_nw")["state"] == "person_confirmed"
    assert nw.process_pending() == 1
    (c,) = calls
    assert c["event_type"] == "NIGHT_INTRUSION" and c["severity"] == "HIGH" and c["push"] and c["broadcast"]
    assert c["title"] == "Person at night - Back door"
    assert "Back door" in c["body"] and "02:13 AEST" in c["body"]
    assert c["data"]["store_tz_abbr"] == "AEST" and c["data"]["confidence"] == 0.9
    assert c["data"]["timestamp"].endswith("Z") and c["data"]["store_timezone"] == "Australia/Melbourne"
    snap = c["data"]["snapshot_url"]
    assert snap == f"/api/v1/night-watch/evidence/{c['data']['alert_id']}.jpg"
    assert NightWatch.evidence_path(snap.rsplit("/", 1)[1]) is not None
    # Held again, then a cooldown: new motion starts no burst until it ends.
    assert sched.camera_status("cam_nw")["night_hold"] is True
    clock.t += 6
    feed(nw, clock, [noisy(SCENE, seed=i) for i in range(3)])
    assert nw.camera_status("cam_nw")["state"] == "cooldown"
    modes = feed(nw, clock, [with_blob(40 + 10 * i, 120, seed=i) for i in range(8)])
    assert nwm.CONFIRM not in modes
    clock.t += 120
    feed(nw, clock, [noisy(SCENE, seed=i) for i in range(6)])
    assert nw.camera_status("cam_nw")["state"] == "armed_idle"
    assert nwm.CONFIRM in feed(nw, clock, [with_blob(40 + 10 * i, 120, seed=i) for i in range(8)])


def test_a_person_away_from_the_movement_does_not_confirm(watch):
    nw, clock, _, calls = watch
    _start_burst(nw, clock)
    for _ in range(4):
        nw.confirm("cam_nw", noisy(SCENE), [person_at(300, 10)])       # a poster in the far corner
    assert nw.camera_status("cam_nw")["state"] == "motion" and not calls


def test_motion_without_a_person_is_a_low_event_not_pushed(watch):
    nw, clock, sched, calls = watch
    _start_burst(nw, clock)
    feed(nw, clock, [noisy(SCENE, seed=i) for i in range(20)])           # 5 s: the burst ends
    assert nw.process_pending() == 1
    (c,) = calls
    assert c["event_type"] == "NIGHT_MOTION" and c["severity"] == "INFO"
    assert c["push"] is False and c["broadcast"] is False            # listed on the dashboard only
    assert "no person was confirmed" in c["body"] and "AEST" in c["body"]
    assert c["data"]["snapshot_url"].endswith(".jpg")
    assert nw.camera_status("cam_nw")["state"] == "armed_idle"
    assert sched.camera_status("cam_nw")["night_hold"] is True


def test_events_are_capped_per_hour_and_counted(watch, monkeypatch):
    nw, clock, _, calls = watch
    monkeypatch.setattr(settings, "NIGHT_WATCH_MAX_EVENTS_PER_HOUR", 2)
    monkeypatch.setattr(settings, "NIGHT_WATCH_RECONFIRM_SEC", 0.0)
    nw.config_override = ConfigStub(NightWatchConfig(enabled=True, start="22:00", end="06:00", cooldown_sec=10))
    for _ in range(4):
        _start_burst(nw, clock)
        frame = with_blob(100, 120)
        nw.confirm("cam_nw", frame, [person_at(100, 120)])
        nw.confirm("cam_nw", frame, [person_at(100, 120)])
        clock.t += 20
        feed(nw, clock, [noisy(SCENE, seed=i) for i in range(4)])
    nw.process_pending()
    assert len(calls) == 2
    assert nw.camera_status("cam_nw")["counts"]["suppressed"] == 2


def test_an_offline_camera_is_never_reported_armed(watch):
    nw, clock, _, _ = watch
    feed(nw, clock, [noisy(SCENE)])
    assert nw.camera_status("cam_nw", streaming=False)["state"] == "unavailable"
    clock.t += 30                                                       # no frames since
    st = nw.camera_status("cam_nw")
    assert st["state"] == "unavailable" and st["armed"] is False and st["note"]


def test_evidence_is_capped_oldest_first(watch, monkeypatch, tmp_path):
    nw, _, _, _ = watch
    # The evidence limit only ever deletes inside STORAGE_DIR (evidence_storage.py).
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path)
    d = NightWatch.evidence_dir()
    for i in range(5):
        p = d / f"nw_{i:012x}.jpg"
        p.write_bytes(b"x" * 400_000)
        os.utime(p, (1000 + i, 1000 + i))
    monkeypatch.setattr(settings, "NIGHT_WATCH_EVIDENCE_MAX_MB", 1.0)
    assert nw.enforce_cap() == 3
    assert sorted(p.name for p in d.iterdir()) == ["nw_000000000003.jpg", "nw_000000000004.jpg"]


def test_dispatcher_can_log_and_broadcast_without_pushing(monkeypatch):
    import asyncio

    from app.services import alert_dispatcher as ad
    from app.services.notification_service import alert_hub

    async def no_camera(camera_id):
        return None

    async def no_ws(payload):
        return 0

    async def must_not_push(*a, **k):
        raise AssertionError("pushed")

    d = ad.AlertDispatcher()
    monkeypatch.setattr(d, "_get_camera", no_camera)
    monkeypatch.setattr(d, "_push", must_not_push)
    monkeypatch.setattr(alert_hub, "broadcast_event", no_ws)
    out = asyncio.run(d.dispatch("NIGHT_MOTION", "INFO", "t", "b", {"camera_id": "c"}, push=False))
    assert out["push"]["skipped"].startswith("not pushed") and out["push"]["sent"] == 0


# ============================================================== engine + API


def test_worker_holds_confirms_and_reports_status(melbourne, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "NIGHT_WATCH_EVIDENCE_DIR", str(tmp_path / "nw"))
    g = nwm.night_watch
    monkeypatch.setattr(g, "config_override", ConfigStub(NightWatchConfig(enabled=True, start="00:00",
                                                                           end="00:00")))
    monkeypatch.setattr(g, "lighting_override", lambda cam: "day")
    monkeypatch.setattr(g, "synchronous", True)
    calls = {"n": 0}

    def detect(frame, camera_id=None, **kw):
        calls["n"] += 1
        return [person_at(100, 120)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    engine = lae.LiveAnalyticsEngine()
    rt = lae.CameraRuntime(camera_id="cam_nw_worker", name="Worker cam", source="/dev/null")
    rt.status = "ONLINE"
    engine.runtimes[rt.camera_id] = rt
    w = lae.CameraWorker(rt, engine)
    try:
        now = time.time()
        for i in range(4):
            assert w._night_watch_step(noisy(SCENE, seed=i), now + 0.25 * i) == nwm.HOLD
        assert w._night_armed and calls["n"] == 0
        d = rt.to_dict()
        assert d["night_watch"]["state"] == "armed_idle" and d["night_watch"]["armed"] is True
        assert d["night_watch"]["next_disarm"] is None                  # every day, all day
        assert "lighting" in d
        # The worker's own confirmation path runs the detector with the camera id.
        g._w[rt.camera_id].state = "motion"
        g._w[rt.camera_id].confirm_until = time.time() + 5
        w._night_confirm(with_blob(100, 120), time.time())
        assert calls["n"] == 1 and rt.detections_last == 1
    finally:
        g.forget(rt.camera_id)
        g._w.pop(rt.camera_id, None)
        g.process_pending()
        from app.services.inference_scheduler import inference_scheduler

        inference_scheduler.forget(rt.camera_id)


def test_detections_last_uses_the_per_box_threshold(monkeypatch):
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD_DARK", 0.35)
    monkeypatch.setattr(settings, "PERSON_DARK_LUMA", 60)
    seen = {}

    def detect(frame, camera_id=None, **kw):
        seen["camera_id"] = camera_id
        return [Detection(10, 10, 50, 100, 0.40, luma=20.0),      # dark box: counts at 0.40
                Detection(200, 10, 240, 100, 0.40, luma=150.0),   # lit box: does not
                Detection(100, 10, 140, 100, 0.60, luma=150.0)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    monkeypatch.setitem(lae._pose_state, "module", None)
    engine = lae.LiveAnalyticsEngine()
    rt = lae.CameraRuntime(camera_id="cam_thr", name="Thr", source="/dev/null")
    engine.runtimes["cam_thr"] = rt
    lae.CameraWorker(rt, engine)._analyse(np.zeros((288, 352, 3), np.uint8), now=5000.0)
    assert seen["camera_id"] == "cam_thr" and rt.detections_last == 2


def test_lighting_is_in_the_camera_status():
    from app.services.low_light import lighting_monitor

    rt = lae.CameraRuntime(camera_id="cam_light", name="L", source="/dev/null")
    assert rt.to_dict()["lighting"] is None
    lighting_monitor.observe(np.full((120, 160, 3), 200, np.uint8), "cam_light")
    try:
        assert rt.to_dict()["lighting"]["lighting"] in ("day", "mixed", "low_light", "ir")
    finally:
        lighting_monitor.forget("cam_light")


CAM = "cam_night_api"


@pytest.fixture
def api(melbourne):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.main import app
    from app.models.db_models import Base
    from app.services.feature_manager import feature_manager

    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    client = TestClient(app)
    client.delete(f"/api/v1/cameras/{CAM}")
    feature_manager.remove_camera(CAM)
    r = client.put(f"/api/v1/cameras/{CAM}", json={"id": CAM, "name": "Back door", "location": "Dock"})
    assert r.status_code == 200, r.text
    yield client
    client.delete(f"/api/v1/cameras/{CAM}")
    feature_manager.remove_camera(CAM)


def test_settings_round_trip_and_survive_a_restart(api):
    from app.services.feature_manager import feature_manager

    got = api.get(f"/api/v1/cameras/{CAM}/night-watch").json()
    assert got["configured"] is False and got["config"]["enabled"] is False
    assert got["store_timezone"]["name"] == "Australia/Melbourne"
    assert got["store_timezone"]["abbreviation"] in ("AEST", "AEDT")
    assert got["status"]["state"] == "disarmed"

    body = {"enabled": True, "start": "21:30", "end": "05:45", "days": ["mon", "tue", "sat"],
            "when_dark": True, "sensitivity": "high", "cooldown_sec": 300}
    r = api.put(f"/api/v1/cameras/{CAM}/night-watch", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["config"] == body and r.json()["configured"] is True

    feature_manager.remove_camera(CAM)                   # cold cache, as after a restart
    cfg = feature_manager.get_setting(CAM, "night_watch")
    assert cfg.enabled and cfg.start == "21:30" and cfg.days == ["mon", "tue", "sat"]
    assert api.get(f"/api/v1/cameras/{CAM}/night-watch").json()["config"] == body

    # Clients that only know the toggles (older dashboard, phone app) keep it.
    api.put(f"/api/v1/cameras/{CAM}/features", json={"people_counting": True, "shelf_interaction": False,
                                                      "theft_detection": True})
    cam = api.get(f"/api/v1/cameras/{CAM}").json()
    api.put(f"/api/v1/cameras/{CAM}", json={**cam, "features": {"people_counting": True,
                                                                 "shelf_interaction": True,
                                                                 "theft_detection": True}})
    assert api.get(f"/api/v1/cameras/{CAM}/night-watch").json()["config"] == body

    assert api.put(f"/api/v1/cameras/{CAM}/night-watch", json={**body, "start": "24:10"}).status_code == 422
    assert api.put("/api/v1/cameras/nope/night-watch", json=body).status_code == 404

    summary = api.get("/api/v1/night-watch").json()
    row = next(c for c in summary["cameras"] if c["camera_id"] == CAM)
    # No worker runs in this test: honest, never "armed".
    assert row["state"] in ("disarmed", "unavailable") and row["armed"] is False
    assert summary["store_timezone"]["name"] == "Australia/Melbourne"


def test_events_list_gives_utc_and_store_time_and_serves_evidence(api, monkeypatch, tmp_path):
    import asyncio

    from app.database import async_session_factory
    from app.models.db_models import SecurityEventModel

    monkeypatch.setattr(settings, "NIGHT_WATCH_EVIDENCE_DIR", str(tmp_path / "nw"))
    (NightWatch.evidence_dir() / "nw_abcdef012345.jpg").write_bytes(b"\xff\xd8jpeg")

    async def add():
        async with async_session_factory() as s:
            s.add(SecurityEventModel(
                id="nw_abcdef012345", camera_id=CAM, camera_name="Back door", location="Dock",
                event_type="NIGHT_INTRUSION", severity="HIGH", confidence=0.9,
                timestamp=datetime(2026, 9, 25, 16, 13), snapshot_url="/api/v1/night-watch/evidence/nw_abcdef012345.jpg",
                metadata_json={"title": "Person at night - Back door", "body": "b", "confidence_measured": True},
                acknowledged=False))
            await s.commit()

    asyncio.run(add())
    try:
        ev = api.get("/api/v1/night-watch/events").json()["events"]
        e = next(x for x in ev if x["id"] == "nw_abcdef012345")
        assert e["at"]["utc"] == "2026-09-25T16:13:00Z" and e["at"]["label"] == "02:13 AEST"
        assert e["at"]["utc_offset_min"] == 600 and e["confidence"] == 0.9
        r = api.get(e["snapshot_url"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert api.get("/api/v1/night-watch/evidence/..%2Fsecret.jpg").status_code == 404
        assert api.get("/api/v1/night-watch/evidence/nw_nothere0000.jpg").status_code == 404
        # The generic alert log serves the new types too.
        assert any(x["id"] == "nw_abcdef012345" for x in api.get("/api/v1/events").json()["events"])
    finally:
        async def drop():
            from sqlalchemy import delete

            async with async_session_factory() as s:
                await s.execute(delete(SecurityEventModel).where(SecurityEventModel.id == "nw_abcdef012345"))
                await s.commit()

        asyncio.run(drop())
