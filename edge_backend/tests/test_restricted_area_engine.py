"""Restricted areas, high security: trigger logic on synthetic tracks.

Calibrated cameras (floor metres through a homography) and uncalibrated ones
(image pixels, body-height units) are both covered: entry and flicker
rejection, announced vs unannounced (with overstay), schedules and time zones
incl. DST, dwell, speed, pacing, zig-zag, crouch, reach-up, point of interest,
edge lingering, people count, wrong door, tamper (covered / turned / offline,
lighting is not tamper), still figures, sensitivity boost, tier routing with
cooldown and quiet-hours bypass, plan gating and the detection-to-dispatch
latency with a fake clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.config import settings
from app.services import restricted_areas as ra
from app.services.restricted_area_engine import (
    RestrictedAreaEngine,
    count_reversals,
    low_posture,
    net_speed,
    reaching_up,
    zigzag_turns,
)

CAM = "cam_ra_unit"
FW, FH = 1280, 720
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_area(triggers=None, *, floor=None, region=None, linked=False, arming=None, pois=None,
              announcements=None, manual=None, door_lines=None, security=None, area_id="ra_unit"):
    trig = ra.default_triggers()
    for tid, cfg in (triggers or {}).items():
        trig[tid].update(cfg)
    a = ra.CompiledArea(id=area_id, name="Cash office", enabled=True,
                        arming=arming or {"mode": "always"}, triggers=trig,
                        security=security or {"confidence_delta": 0.05}, pois=pois or [])
    if floor is not None:
        a.floor_polygon = floor
        a.floor_cameras[CAM] = 1.0
    if region is not None:
        a.regions[CAM] = [region]
    if linked:
        a.linked_cameras.append(CAM)
    a.announcements = announcements or []
    if manual:
        a.manual_state, a.manual_until = manual
    a.door_lines = {CAM: door_lines} if door_lines else {}
    return a


@pytest.fixture
def eng(monkeypatch):
    e = RestrictedAreaEngine()
    e.synchronous = True
    e.record_clips = False
    e.clock = Clock()
    e._figures_loaded = True       # no still-figure file from other tests
    monkeypatch.setattr(e, "_save_figures", lambda force=False: None)

    def install(*areas):
        snap = ra.Snapshot(areas=list(areas), by_camera={CAM: list(areas)},
                           linked_intrusion_ids=frozenset(a.legacy_intrusion_id for a in areas if a.legacy_intrusion_id))
        monkeypatch.setattr(ra.restricted_areas, "_snapshot", snap)
        e._secured_cache.clear()

    e.install = install
    return e


def trk(tid="1", bbox=(600, 300, 680, 600), floor=None, kp=None, static=False, pending=False, hits=5):
    return SimpleNamespace(track_id=tid, bbox=bbox, floor_xy=floor, keypoints=kp, keypoints_fresh=True,
                           is_static=static, is_pending_static=pending, hits=hits, misses=0)


def run(e, frames):
    """frames: list of (t, [tracks]); returns every alert queued (PendingAlert objects)."""
    for t, tracks in frames:
        e.clock.t = t
        e.evaluate(CAM, tracks, FW, FH, t)
    return list(e._alerts)


def triggers_of(alerts):
    return [a.trigger for a in alerts]


SQUARE = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
REGION = [(0.3, 0.2), (0.7, 0.2), (0.7, 0.95), (0.3, 0.95)]


# --------------------------------------------------------------------------- pure functions

def test_pure_kinematics_helpers():
    walk = [(i * 0.1, i * 0.14, 0.0) for i in range(11)]          # 1.4 m/s
    run_ = [(i * 0.1, i * 0.3, 0.0) for i in range(11)]           # 3 m/s
    assert abs(net_speed(walk, 0.6, 1.0) - 1.4) < 0.01
    assert abs(net_speed(run_, 0.6, 1.0) - 3.0) < 0.01
    assert net_speed(walk[:2], 0.6, 1.0) is None                  # too few samples
    pace = [(x, 0.0) for x in (0, 1, 2, 1, 0, 1, 2, 1, 0, 1, 2)]
    assert count_reversals(pace, 0.8) == 4
    assert count_reversals([(x * 0.5, 0.0) for x in range(10)], 0.8) == 0
    assert count_reversals([(0, 0), (0.3, 0), (0.1, 0), (0.35, 0)], 0.8) == 0   # jitter below a leg
    zig = [(i, (i % 2) * 1.0) for i in range(8)]
    assert zigzag_turns(zig, 0.4, 60) >= 4
    assert zigzag_turns([(i, 0.0) for i in range(8)], 0.4, 60) == 0


def _kp(points):
    kp = np.zeros((17, 3), np.float32)
    for i, (x, y) in points.items():
        kp[i] = (x, y, 0.9)
    return kp


STANDING = _kp({0: (640, 320), 5: (620, 360), 6: (660, 360), 11: (625, 460), 12: (655, 460),
                13: (625, 530), 14: (655, 530), 7: (610, 410), 8: (670, 410), 9: (610, 450), 10: (670, 450)})
CROUCHING = _kp({0: (640, 440), 5: (620, 470), 6: (660, 470), 11: (625, 560), 12: (655, 560),
                 13: (625, 575), 14: (655, 578)})
REACHING = _kp({0: (640, 320), 1: (635, 315), 2: (645, 315), 5: (620, 360), 6: (660, 360),
                11: (625, 460), 12: (655, 460), 7: (615, 330), 9: (615, 280), 8: (670, 410), 10: (670, 450)})


def test_pose_helpers():
    assert low_posture(STANDING) is False
    assert low_posture(CROUCHING) is True
    assert low_posture(None) is None
    assert reaching_up(REACHING) is True
    assert reaching_up(STANDING) is False


# --------------------------------------------------------------------------- entry

def test_entry_needs_minimum_presence_and_rejects_flicker(eng):
    eng.install(make_area(floor=SQUARE))
    inside = trk(floor=(2.0, 2.0))
    # One frame inside, then gone: a flicker never alerts.
    assert run(eng, [(T0, [inside]), (T0 + 0.1, [])]) == []
    # Inside for 0.4 s on 5 frames: not yet.
    eng.reset()
    frames = [(T0 + 10 + i * 0.1, [trk("2", floor=(2.0, 2.0))]) for i in range(5)]
    assert run(eng, frames) == []
    # At 0.5 s: Entry without notice, Critical.
    alerts = run(eng, [(T0 + 10.5, [trk("2", floor=(2.0, 2.0))])])
    assert triggers_of(alerts) == ["unannounced_entry"]
    a = alerts[0]
    assert a.tier == "critical" and a.data["ra_requires_outcome"] and a.data["ra_all_phones"]
    assert a.data["alert_tier"] == "critical" and a.data["ra_bypass_quiet"]
    assert a.title.startswith("Urgent: Entry without notice")
    # Never twice for the same person.
    assert len(run(eng, [(T0 + 11 + i * 0.2, [trk("2", floor=(2.0, 2.0))]) for i in range(10)])) == 1


def test_a_brief_exit_keeps_the_clock_a_long_one_resets_it(eng):
    eng.install(make_area(floor=SQUARE, triggers={"unannounced_entry": {"min_presence_sec": 2.0}}))
    frames = [(T0 + i * 0.2, [trk(floor=(2.0, 2.0))]) for i in range(6)]          # 0..1.0 s inside
    frames += [(T0 + 1.2, [trk(floor=(5.0, 2.0))])]                                # 0.2 s outside
    frames += [(T0 + 1.4 + i * 0.2, [trk(floor=(2.0, 2.0))]) for i in range(4)]   # back in to 2.0 s
    assert triggers_of(run(eng, frames)) == ["unannounced_entry"]
    eng.reset()
    frames = [(T0 + i * 0.2, [trk("9", floor=(2.0, 2.0))]) for i in range(6)]
    frames += [(T0 + 1.2 + i * 0.5, [trk("9", floor=(5.0, 2.0))]) for i in range(4)]   # 2 s outside
    frames += [(T0 + 3.4 + i * 0.2, [trk("9", floor=(2.0, 2.0))]) for i in range(6)]   # 1 s inside
    assert run(eng, frames) == []


def test_announced_entry_is_quiet_then_overstay_and_new_intruders_alert(eng):
    ann = ra.Announcement(id="ann1", area_id="ra_unit", user_id="u1", user_name="Sam", note=None, people=1,
                          started_at=T0 - 60, until=T0 + 120, ended_at=None)
    eng.install(make_area(floor=SQUARE, announcements=[ann]))
    # Inside during the announcement: nothing.
    assert run(eng, [(T0 + i, [trk("1", floor=(2.0, 2.0))]) for i in range(60)]) == []
    # The announcement ends at T0+120; the grace is 2 min: still nothing at T0+200.
    assert run(eng, [(T0 + 60 + i * 2, [trk("1", floor=(2.0, 2.0))]) for i in range(71)]) == []
    # After T0+240 (end + 2 min): "Stayed longer than announced", once.
    alerts = run(eng, [(T0 + 241 + i, [trk("1", floor=(2.0, 2.0))]) for i in range(5)])
    assert triggers_of(alerts) == ["announced_overstay"]
    assert alerts[0].tier == "alert" and alerts[0].data["announced_by"] == "Sam"
    # Someone entering after the announcement ended is not covered by its grace.
    eng.reset()
    alerts = run(eng, [(T0 + 125 + i * 0.25, [trk("7", floor=(1.0, 1.0))]) for i in range(4)])
    assert triggers_of(alerts) == ["unannounced_entry"]


def test_im_out_with_someone_still_inside_alerts_after_15_s(eng):
    ann = ra.Announcement(id="ann2", area_id="ra_unit", user_id="u1", user_name="Sam", note=None, people=1,
                          started_at=T0 - 60, until=T0 + 600, ended_at=T0 + 10)
    eng.install(make_area(floor=SQUARE, announcements=[ann]))
    assert run(eng, [(T0 + i * 0.5, [trk("1", floor=(2.0, 2.0))]) for i in range(49)]) == []    # to T0+24
    alerts = run(eng, [(T0 + 24.5 + i * 0.5, [trk("1", floor=(2.0, 2.0))]) for i in range(3)])
    assert triggers_of(alerts) == ["announced_overstay"]


def test_disarmed_area_raises_nothing(eng):
    eng.install(make_area(floor=SQUARE, manual=("disarmed", T0 + 3600)))
    assert run(eng, [(T0 + i * 0.2, [trk(floor=(2.0, 2.0))]) for i in range(30)]) == []
    assert eng.occupancy("ra_unit", T0 + 5.8)["people"] == 1     # occupancy is still shown


def test_uncalibrated_camera_region_entry(eng):
    eng.install(make_area(region=REGION))
    alerts = run(eng, [(T0 + i * 0.1, [trk(bbox=(600, 300, 680, 600))]) for i in range(7)])
    assert triggers_of(alerts) == ["unannounced_entry"]
    eng.reset()
    assert run(eng, [(T0 + i * 0.1, [trk(bbox=(100, 300, 180, 600))]) for i in range(10)]) == []   # outside


def test_linked_uncalibrated_camera_counts_its_whole_view(eng):
    eng.install(make_area(linked=True))
    assert triggers_of(run(eng, [(T0 + i * 0.1, [trk(bbox=(10, 10, 80, 300))]) for i in range(7)])) == ["unannounced_entry"]


# --------------------------------------------------------------------------- schedules

def test_schedule_time_zone_and_dst(monkeypatch):
    tz = ZoneInfo("Australia/Melbourne")
    arming = {"mode": "schedule", "schedule": [{"days": ["sat", "sun"], "from": "22:00", "to": "06:00"}],
              "schedule_mode": "armed_during", "timezone": "Australia/Melbourne"}
    # Sat 3 Oct 2026 23:30 AEST (the night clocks go forward at 02:00 to 03:00).
    sat = datetime(2026, 10, 3, 23, 30, tzinfo=tz).timestamp()
    assert ra.schedule_armed(arming, sat)
    sun_0230 = datetime(2026, 10, 4, 3, 30, tzinfo=tz).timestamp()     # AEDT now
    assert ra.schedule_armed(arming, sun_0230)
    sun_noon = datetime(2026, 10, 4, 12, 0, tzinfo=tz).timestamp()
    assert not ra.schedule_armed(arming, sun_noon)
    nxt = ra.next_schedule_change(arming, sat)
    assert datetime.fromtimestamp(nxt, tz).strftime("%a %H:%M") == "Sun 06:00"
    # 22:00 -> 06:00 is 7 h on the night the clocks go forward.
    start = datetime(2026, 10, 3, 22, 0, tzinfo=tz).timestamp()
    assert round((nxt - start) / 3600, 2) == 7.0
    # Armed outside the rows.
    outside = dict(arming, schedule_mode="armed_outside")
    assert ra.schedule_armed(outside, sun_noon) and not ra.schedule_armed(outside, sat)
    # Always.
    assert ra.schedule_armed({"mode": "always"}, sun_noon)


def test_schedule_and_manual_override_in_the_engine(eng):
    tz = ZoneInfo("Australia/Melbourne")
    arming = {"mode": "schedule", "schedule": [{"days": ra.DAY_KEYS, "from": "22:00", "to": "06:00"}],
              "timezone": "Australia/Melbourne"}
    day = datetime(2026, 11, 10, 12, 0, tzinfo=tz).timestamp()
    night = datetime(2026, 11, 10, 23, 0, tzinfo=tz).timestamp()
    eng.install(make_area(floor=SQUARE, arming=arming))
    assert run(eng, [(day + i * 0.2, [trk("d", floor=(2, 2))]) for i in range(10)]) == []
    assert triggers_of(run(eng, [(night + i * 0.2, [trk("n", floor=(2, 2))]) for i in range(10)])) == ["unannounced_entry"]
    # Armed by hand during the day.
    eng.reset()
    eng.install(make_area(floor=SQUARE, arming=arming, manual=("armed", day + 3600)))
    assert triggers_of(run(eng, [(day + i * 0.2, [trk("m", floor=(2, 2))]) for i in range(10)])) == ["unannounced_entry"]


# --------------------------------------------------------------------------- the other triggers

def test_dwell(eng):
    eng.install(make_area(floor=SQUARE, triggers={"unannounced_entry": {"enabled": False},
                                                   "dwell": {"max_seconds": 30}}))
    alerts = run(eng, [(T0 + i, [trk(floor=(2, 2))]) for i in range(32)])
    assert triggers_of(alerts) == ["dwell"] and alerts[0].tier == "watch"


def _only(tid, **cfg):
    trig = {k: {"enabled": False} for k in ra.TRIGGERS}
    trig[tid] = dict(cfg, enabled=True)
    return trig


def test_speed_calibrated_metres_per_second(eng):
    eng.install(make_area(floor=[(0, 0), (20, 0), (20, 4), (0, 4)], triggers=_only("speed")))
    assert run(eng, [(T0 + i * 0.1, [trk(floor=(1 + 0.14 * i, 2))]) for i in range(30)]) == []    # 1.4 m/s
    eng.reset()
    alerts = run(eng, [(T0 + i * 0.1, [trk("r", floor=(1 + 0.3 * i, 2))]) for i in range(12)])    # 3 m/s
    assert triggers_of(alerts) == ["speed"] and alerts[0].data["speed_unit"] == "m/s"
    assert 2.8 < alerts[0].data["speed"] < 3.2 and alerts[0].data["calibrated"] is True


def test_speed_uncalibrated_body_heights(eng):
    eng.install(make_area(region=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)], triggers=_only("speed")))
    # Box 300 px tall moving 120 px/s = 0.4 body heights/s: walking.
    assert run(eng, [(T0 + i * 0.1, [trk(bbox=(100 + 12 * i, 300, 180 + 12 * i, 600))]) for i in range(20)]) == []
    eng.reset()
    # 600 px/s = 2 body heights/s: running.
    alerts = run(eng, [(T0 + i * 0.1, [trk("r", bbox=(100 + 60 * i, 300, 180 + 60 * i, 600))]) for i in range(10)])
    assert triggers_of(alerts) == ["speed"]
    assert alerts[0].data["speed_unit"] == "bh/s" and alerts[0].data["calibrated"] is False
    assert "estimate" in alerts[0].body


def test_pacing(eng):
    eng.install(make_area(floor=[(0, 0), (10, 0), (10, 4), (0, 4)], triggers=_only("pacing")))
    xs = [2 + (i % 10) * 0.3 if (i // 10) % 2 == 0 else 5 - (i % 10) * 0.3 for i in range(60)]
    alerts = run(eng, [(T0 + i * 0.5, [trk(floor=(x, 2))]) for i, x in enumerate(xs)])
    assert triggers_of(alerts) == ["pacing"] and alerts[0].data["reversals"] >= 3
    eng.reset()
    assert run(eng, [(T0 + i * 0.5, [trk("s", floor=(1 + 0.1 * i, 2))]) for i in range(60)]) == []


def test_zigzag(eng):
    eng.install(make_area(floor=[(0, 0), (20, 0), (20, 6), (0, 6)], triggers=_only("erratic")))
    pts = [(1 + 0.5 * i, 2 + 0.8 * (i % 2)) for i in range(24)]
    alerts = run(eng, [(T0 + i * 0.4, [trk(floor=p)]) for i, p in enumerate(pts)])
    assert triggers_of(alerts) == ["erratic"]
    eng.reset()
    assert run(eng, [(T0 + i * 0.4, [trk("s", floor=(1 + 0.5 * i, 2))]) for i in range(24)]) == []


def test_crouch_and_reach_up(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("crouch", min_seconds=2.0)))
    assert run(eng, [(T0 + i * 0.5, [trk(floor=(2, 2), kp=STANDING)]) for i in range(10)]) == []
    alerts = run(eng, [(T0 + 5 + i * 0.5, [trk(floor=(2, 2), kp=CROUCHING)]) for i in range(6)])
    assert triggers_of(alerts) == ["crouch"]
    eng.reset()
    eng.install(make_area(floor=SQUARE, triggers=_only("reach_up", min_seconds=1.0)))
    assert triggers_of(run(eng, [(T0 + i * 0.25, [trk("u", floor=(2, 2), kp=REACHING)]) for i in range(6)])) == ["reach_up"]


def test_point_of_interest(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("poi_approach"),
                          pois=[{"id": "safe", "name": "Safe", "x": 3.5, "y": 3.5}]))
    assert run(eng, [(T0 + i * 0.5, [trk(floor=(1.0, 1.0))]) for i in range(6)]) == []
    alerts = run(eng, [(T0 + 3 + i * 0.5, [trk(floor=(3.0, 3.2))]) for i in range(4)])
    assert triggers_of(alerts) == ["poi_approach"] and alerts[0].data["poi_name"] == "Safe"
    # Uncalibrated: a point on the camera picture, in body heights.
    eng.reset()
    eng.install(make_area(region=REGION, triggers=_only("poi_approach"),
                          pois=[{"id": "cash", "name": "Cash drawer", "camera_id": CAM, "u": 0.5, "v": 0.85}]))
    alerts = run(eng, [(T0 + i * 0.5, [trk("c", bbox=(600, 312, 680, 612))]) for i in range(4)])
    assert triggers_of(alerts) == ["poi_approach"] and alerts[0].data["distance_unit"] == "bh"


def test_lingering_at_the_edge(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("boundary_linger", min_seconds=8.0)))
    alerts = run(eng, [(T0 + i, [trk(floor=(4.6, 2.0))]) for i in range(10)])     # 0.6 m outside
    assert triggers_of(alerts) == ["boundary_linger"] and alerts[0].tier == "watch"
    eng.reset()
    assert run(eng, [(T0 + i, [trk("f", floor=(7.0, 2.0))]) for i in range(12)]) == []   # 3 m away


def test_people_count_and_announced_people(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("people_count", max_people=1)))
    two = lambda: [trk("a", floor=(1, 1)), trk("b", bbox=(100, 300, 180, 600), floor=(3, 3))]  # noqa: E731
    assert run(eng, [(T0, two())]) == []                     # held 1 s first
    alerts = run(eng, [(T0 + 0.5, two()), (T0 + 1.1, two())])
    assert triggers_of(alerts) == ["people_count"] and alerts[0].data["people"] == 2
    ann = ra.Announcement(id="a2", area_id="ra_unit", user_id="u", user_name="Sam", note=None, people=2,
                          started_at=T0 - 10, until=T0 + 900, ended_at=None)
    eng.reset()
    eng.install(make_area(floor=SQUARE, triggers=_only("people_count", max_people=1), announcements=[ann]))
    assert run(eng, [(T0 + i * 0.5, two()) for i in range(6)]) == []


def test_entry_not_through_the_door(eng):
    door = {"door1": ((0.45, 0.5), (0.55, 0.5))}            # a short line in the middle of the picture
    eng.install(make_area(region=[(0.3, 0.55), (0.7, 0.55), (0.7, 1.0), (0.3, 1.0)],
                          triggers=_only("wrong_door", door_line_ids=["door1"]), door_lines=door))
    # Walks down through the door line into the area: fine.
    path = [(T0 + i * 0.2, [trk("ok", bbox=(600, 0 + 20 * i, 680, 300 + 20 * i))]) for i in range(10)]
    assert run(eng, path) == []
    eng.reset()
    # Comes in from the right edge without crossing the door line.
    path = [(T0 + i * 0.2, [trk("bad", bbox=(1100 - 60 * i, 400, 1180 - 60 * i, 650))]) for i in range(10)]
    alerts = run(eng, path)
    assert triggers_of(alerts) == ["wrong_door"]


# --------------------------------------------------------------------------- tamper

def _scene(seed=1):
    rng = np.random.default_rng(seed)
    return (rng.random((48, 64)) * 200 + 20).astype(np.uint8)


def test_tamper_covered_turned_and_offline_but_not_lighting(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("tamper")))
    scene = _scene()
    for i in range(8):
        assert eng.frame_health(CAM, scene, T0 + i * 1.01) is None
    # Lights dimmed by half: the same scene, not tampering.
    for i in range(30):
        assert eng.frame_health(CAM, (scene * 0.5).astype(np.uint8), T0 + 10 + i * 1.01) is None
    # Covered (uniform black) for 10 s.
    raised = [eng.frame_health(CAM, np.full((48, 64), 3, np.uint8), T0 + 50 + i * 1.01) for i in range(12)]
    assert "covered" in raised
    assert [a.data["tamper"] for a in eng._alerts] == ["covered"] and eng._alerts[0].tier == "critical"
    # Uncovered, then turned to another scene for 15 s: one alert per move.
    for i in range(3):
        eng.frame_health(CAM, scene, T0 + 70 + i * 1.01)
    eng._areas["ra_unit"].last_alert.clear()
    other = _scene(seed=7)
    raised = [eng.frame_health(CAM, other, T0 + 80 + i * 1.01) for i in range(25)]
    assert raised.count("moved") == 1
    # Offline: no frame for 20 s.
    eng._areas["ra_unit"].last_alert.clear()
    eng.clock.t = T0 + 200
    assert asyncio.run(eng.tick(T0 + 200)) == 1
    assert eng._alerts[-1].data["tamper"] == "offline"
    assert asyncio.run(eng.tick(T0 + 201)) == 0              # once until frames return


def test_tamper_only_while_armed(eng):
    eng.install(make_area(floor=SQUARE, triggers=_only("tamper"), manual=("disarmed", T0 + 9999)))
    scene = _scene()
    for i in range(8):
        eng.frame_health(CAM, scene, T0 + i * 1.01)
    raised = [eng.frame_health(CAM, np.zeros((48, 64), np.uint8), T0 + 20 + i * 1.01) for i in range(15)]
    assert set(raised) == {None} and asyncio.run(eng.tick(T0 + 500)) == 0


# --------------------------------------------------------------------------- static figures

def test_static_figure_does_not_hide_a_person_unless_learnt_outside_guarded_time(eng):
    eng.install(make_area(floor=SQUARE))
    still = lambda tid="p": trk(tid, floor=(2, 2), static=True)   # noqa: E731
    # Armed, never learnt: a still "person" inside counts.
    assert triggers_of(run(eng, [(T0 + i * 0.2, [still()]) for i in range(5)])) == ["unannounced_entry"]
    # Pending-static counts too.
    eng.reset()
    assert triggers_of(run(eng, [(T0 + i * 0.2, [trk("q", floor=(2, 2), pending=True)]) for i in range(5)])) == ["unannounced_entry"]
    # Learnt while disarmed, then armed: suppressed.
    eng.reset()
    eng._figures_loaded = True
    eng.install(make_area(floor=SQUARE, manual=("disarmed", T0 + 100)))
    run(eng, [(T0 + i, [still("poster")]) for i in range(3)])
    assert eng.figure_count("ra_unit") == 1
    eng._alerts.clear()
    eng.install(make_area(floor=SQUARE))
    assert run(eng, [(T0 + 200 + i * 0.2, [still("poster2")]) for i in range(10)]) == []
    assert eng.stats["suppressed_static"] > 0
    # A moving person at the same place is not a still track: alerts.
    assert triggers_of(run(eng, [(T0 + 300 + i * 0.2, [trk("walker", floor=(2, 2))]) for i in range(5)])) == ["unannounced_entry"]
    assert eng.forget_figures("ra_unit") == 1


# --------------------------------------------------------------------------- sensitivity, priority, plan

def test_sensitivity_boost_inside_armed_areas_only(eng):
    from app.services.inference_backend import Detection

    eng.install(make_area(region=REGION, security={"confidence_delta": 0.05}))
    thr = max(settings.PERSON_CONF_THRESHOLD, settings.TRACK_NEW_TRACK_THRESHOLD)
    inside = Detection(600, 300, 680, 600, thr - 0.03)
    outside = Detection(50, 300, 120, 600, thr - 0.03)
    weak = Detection(600, 300, 680, 600, thr - 0.2)
    assert eng.boost_detections(CAM, [inside, outside, weak], FW, FH, T0) == 1
    assert inside.confidence >= thr and outside.confidence < thr and weak.confidence < thr - 0.1
    eng.install(make_area(region=REGION, manual=("disarmed", T0 + 60)))
    d = Detection(600, 300, 680, 600, thr - 0.03)
    assert eng.boost_detections(CAM, [d], FW, FH, T0) == 0


def test_secured_camera_gets_a_larger_share_and_a_faster_idle_rate(monkeypatch):
    from app.services.inference_scheduler import InferenceScheduler

    for k, v in {"ANALYTICS_SCHEDULER": True, "ANALYTICS_IDLE_DETECT_FPS": 0.5, "ANALYTICS_ACTIVE_HOLD_SEC": 4.0,
                 "ANALYTICS_MAX_DETECT_FPS": 10.0, "RESTRICTED_MIN_ANALYSIS_FPS": 3.0}.items():
        monkeypatch.setattr(settings, k, v)
    clock = Clock(1000.0)
    s = InferenceScheduler(detector=None, clock=clock, run_async=False)
    for cam in ("a", "b"):
        s.admit(cam, fps=25.0)
        s.set_activity(cam, 0)
    clock.t += 5.0
    for cam in ("a", "b"):
        s.admit(cam, fps=25.0)
    s.reallocate()
    assert s._cams["a"].rate == pytest.approx(0.5)          # quiet: the idle rate
    s.set_secured("a", True)
    assert s._weight("a") >= settings.RESTRICTED_PRIORITY_WEIGHT > s._weight("b")
    assert s._cams["a"].rate == pytest.approx(3.0)          # armed area: never slower than 3 fps
    assert s._cams["b"].rate == pytest.approx(0.5)
    assert s.camera_status("a")["restricted_area"] is True
    s.set_secured("a", False)
    assert s._cams["a"].rate == pytest.approx(0.5)


def test_plan_off_means_no_evaluation(eng, monkeypatch):
    from app.services import entitlements

    eng.install(make_area(floor=SQUARE))
    monkeypatch.setattr(entitlements, "feature_safe", lambda k: k != "restricted_areas")
    assert run(eng, [(T0 + i * 0.2, [trk(floor=(2, 2))]) for i in range(10)]) == []
    assert not eng.camera_secured(CAM, T0)
    assert entitlements.alert_feature("RESTRICTED_AREA", {"ra_area_id": "x"}) == "restricted_areas"
    assert entitlements.alert_feature("RESTRICTED_AREA", {}) == "zone_alerts"


def test_tripwire_engine_leaves_upgraded_areas_to_the_restricted_engine(eng, monkeypatch):
    from app.services import tripwire_engine as tw_mod
    from app.services.restricted_area_engine import restricted_area_engine

    a = make_area(region=REGION)
    a.legacy_intrusion_id = "iz_old"
    eng.install(a)
    zones = {"tripwires": [], "intrusion_zones": [{"id": "iz_old", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
                                                  {"id": "iz_new", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}]}
    from app.services.ai_zone_service import ai_zone_service

    monkeypatch.setattr(ai_zone_service, "get_all_zones", lambda camera_id=None: zones)
    _w, areas = tw_mod.TripwireEngine._rules_for(CAM)
    assert [x["id"] for x in areas] == ["iz_new"]
    from app.services import entitlements

    monkeypatch.setattr(entitlements, "feature_safe", lambda k: k != "restricted_areas")
    _w, areas = tw_mod.TripwireEngine._rules_for(CAM)
    assert [x["id"] for x in areas] == ["iz_old", "iz_new"]        # plan off: the old engine again
    assert restricted_area_engine.handles_intrusion("iz_old") is False


# --------------------------------------------------------------------------- routing, cooldowns, latency

def test_alert_carries_tier_routing_and_bypasses_cooldown_and_quiet_hours(eng, monkeypatch):
    from app.services import alert_dispatcher as ad
    from app.services.pairing_service import prefs_filter_reason

    calls = []

    async def fake_dispatch(et, sev, title, body, data=None, **kw):
        calls.append((et, sev, data, kw))
        return {"ok": True}

    monkeypatch.setattr(ad.alert_dispatcher, "dispatch", fake_dispatch)
    monkeypatch.setattr(ra.restricted_areas, "audit_now", lambda *a, **k: asyncio.sleep(0))
    eng.install(make_area(floor=SQUARE))
    run(eng, [(T0 + i * 0.2, [trk(floor=(2, 2))]) for i in range(5)])
    eng.process_pending_alerts()
    (et, sev, data, kw), = calls
    assert et == "RESTRICTED_AREA" and sev == "HIGH" and kw.get("bypass_cooldown") is True
    assert data["alert_tier"] == "critical" and data["ra_trigger"] == "unannounced_entry"
    # The dispatcher's tier routing: Critical = banner, sound, push, escalation, repeats; cooldown bypassed.
    persist, broadcast, push, bypass, note = ad.AlertDispatcher._route_tier(dict(data), True, True, True, False)
    assert (persist, broadcast, push, bypass) == (True, True, True, True)
    # Quiet hours on a paired phone do not hold a restricted-area alert back.
    prefs = {"event_types": None, "min_severity": "INFO", "camera_ids": None,
             "quiet_hours": {"start": "00:00", "end": "23:59"}}
    assert prefs_filter_reason(prefs, "RESTRICTED_AREA", "HIGH", CAM) is not None
    assert prefs_filter_reason(prefs, "RESTRICTED_AREA", "HIGH", CAM, ignore_quiet_hours=True) is None


def test_web_push_restricted_alerts_go_to_every_phone_when_critical(monkeypatch):
    from app.services import push_alerts as pa

    roster = dict(pa.DEFAULT_ROSTER, first_priority=["u1", "u2"], backup=["u3"], configured=True)
    ok, why, track = pa.PushAlertService.qualifies({"event_type": "RESTRICTED_AREA", "ra_area_id": "a",
                                                    "alert_tier": "critical"}, roster)
    assert ok and track
    ok, why, _ = pa.PushAlertService.qualifies({"event_type": "RESTRICTED_AREA", "ra_area_id": "a",
                                                "alert_tier": "review"}, roster)
    assert not ok
    ok, why, _ = pa.PushAlertService.qualifies({"event_type": "RESTRICTED_AREA", "ra_area_id": "a",
                                                "alert_tier": "critical"}, dict(roster, restricted_alerts=False))
    assert not ok and "restricted" in why

    asked = {}

    async def subs_for(ids):
        asked["ids"] = list(ids)
        return [SimpleNamespace(id=f"s_{i}", endpoint="https://fcm.googleapis.com/x", p256dh="", auth="",
                                user_id=i, label=i, platform="android") for i in ids]

    async def send_all(self, subs, base, alert_id, data, tier, ack=True):
        return [{"subscription_id": s.id, "status": "sent"} for s in subs]

    async def nothing(*a, **k):
        return None

    async def accounts():
        return {u: {"id": u, "is_active": True, "role": "operator", "username": u, "display_name": u}
                for u in ("u1", "u2", "u3")}

    monkeypatch.setattr(pa, "load_roster", lambda: roster)
    monkeypatch.setattr(pa, "load_accounts", accounts)
    monkeypatch.setattr(pa, "subscriptions_for", subs_for)
    monkeypatch.setattr(pa.PushAlertService, "_send_all", send_all)
    monkeypatch.setattr(pa.PushAlertService, "_record_alert", nothing)
    svc = pa.PushAlertService()
    data = {"event_type": "RESTRICTED_AREA", "ra_area_id": "a", "alert_tier": "critical", "ra_all_phones": True,
            "camera_id": "c", "alert_id": "ra_x", "channels": {"push": True, "escalate": True, "repeat": True}}
    muted = SimpleNamespace(muted_until=datetime(2999, 1, 1))
    rep = asyncio.run(svc.on_alert("t", "b", data, camera=muted, bypass_cooldown=True))
    assert rep["all_phones"] and asked["ids"] == ["u1", "u2", "u3"] and rep["sent"] == 3
    # A second one at once is not held by the per-camera cooldown.
    rep = asyncio.run(svc.on_alert("t", "b", dict(data, alert_id="ra_y"), camera=muted, bypass_cooldown=True))
    assert rep["sent"] == 3


def test_detection_to_dispatch_latency_under_one_second_with_a_fake_clock(eng, monkeypatch):
    monkeypatch.setattr(ra.restricted_areas, "audit_now", lambda *a, **k: asyncio.sleep(0))
    sent = []

    async def dispatcher(et, sev, title, body, data):
        eng.clock.t += 0.12          # log + websocket + push take 120 ms on the fake clock
        sent.append(eng.clock.t)
        return {"ok": True}

    eng.dispatcher_override = dispatcher
    eng.install(make_area(floor=SQUARE))
    frames = [(T0 + i * 0.1, [trk(floor=(2, 2))]) for i in range(6)]
    alerts = run(eng, frames)
    assert len(alerts) == 1
    first_inside = T0
    eng.clock.t += 0.03              # the writer thread wakes
    eng.process_pending_alerts()
    assert eng.stats["latency_ms"] is not None and eng.stats["latency_ms"] < 1000
    assert sent[0] - first_inside < 1.0          # from the first frame inside to the dispatch
    assert eng.last_delivery["latency_ms"] == eng.stats["latency_ms"]


def test_test_alert_is_labelled_and_needs_no_outcome(eng):
    a = make_area(floor=SQUARE)
    eng.install(a)
    alert = eng.raise_test(a, {"name": "Alex"})
    assert alert.title.startswith("TEST ") and alert.data["test"] and not alert.data["ra_requires_outcome"]
    assert alert.tier == "critical" and "Alex" in alert.body


def test_trigger_validation_and_defaults():
    trig = ra.default_triggers()
    assert trig["unannounced_entry"]["tier"] == "critical" and trig["tamper"]["tier"] == "critical"
    assert trig["speed"]["tier"] == "alert" and trig["pacing"]["tier"] == "alert" and trig["dwell"]["tier"] == "watch"
    assert trig["unannounced_entry"]["min_presence_sec"] == 0.5 and trig["wrong_door"]["enabled"] is False
    with pytest.raises(ra.AreaError):
        ra.validate_triggers({"speed": {"max_mps": 99}})
    with pytest.raises(ra.AreaError):
        ra.validate_triggers({"speed": {"tier": "loud"}})
    with pytest.raises(ra.AreaError, match="door line"):
        ra.validate_triggers({"wrong_door": {"enabled": True}})
    with pytest.raises(ra.AreaError, match="Always armed"):
        ra.validate_arming({"mode": "schedule", "schedule": []})
    out = ra.validate_triggers({"speed": {"max_mps": 3.0, "enabled": False}})
    assert out["speed"]["max_mps"] == 3.0 and out["speed"]["enabled"] is False
