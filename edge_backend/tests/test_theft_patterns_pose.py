"""Pose-only concealment targets and behaviour-pattern fusion, through observe().

Synthetic 17-keypoint skeletons drive ``pose_analytics.observe()`` frame by
frame exactly as the live engine does. They are test inputs only, never shown
to a user. The body here faces the camera with shoulders 60 px apart and a
110 px shoulder->hip torso (ratio 0.55, a frontal view).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine

from app.config import settings
from app.models.db_models import Base
from app.services import camera_roles as cr
from app.services import theft_detection_service as rules
from app.services.pose_analytics import pose_analytics
from app.services.shelf_interaction_service import PointCoord, ProductShelfZone, shelf_interaction_service

W, H = 640, 480
DT = 0.2  # 5 analysed frames per second
FRAME = np.full((H, W, 3), 90, dtype=np.uint8)
# Product zone to the right of the shopper: pixels x 448..608, y 96..240.
ZONE_POINTS = [(0.70, 0.20), (0.95, 0.20), (0.95, 0.50), (0.70, 0.50)]

IN_SHELF = (520.0, 170.0)
MID_AIR = (420.0, 120.0)          # beside the head: neither shelf nor body
POCKET = (330.0, 250.0)           # waistband
CHEST = (299.0, 175.0)            # right wrist on the LEFT side of the upper chest
FLANK = (362.0, 250.0)            # right wrist at its own hip, waist height, seen
HIDDEN = (366.0, 268.0)           # where a hidden wrist is guessed (visibility ~0)
RIGHT_REST = (352.0, 300.0)       # right arm hanging straight down
BASKET = (353.0, 285.0)           # right arm hanging straight, wrist just below the hip (basket handle)


def skeleton(rw=(352.0, 300.0), rw_vis=0.9, *, lw=(283.0, 290.0), lw_vis=0.9, head_side=0, face_vis=0.9,
             knee_vis=0.9, dx=0.0, shoulders=(290.0, 350.0)):
    k = np.zeros((17, 3), dtype=np.float32)
    nose_x = 320.0 + head_side * 9.0
    k[0] = (nose_x, 110, face_vis)
    k[1] = (315, 105, face_vis); k[2] = (325, 105, face_vis)          # eyes
    k[3] = (310, 112, 0.9); k[4] = (330, 112, 0.9)                    # ears
    k[5] = (shoulders[0], 150, 0.9); k[6] = (shoulders[1], 150, 0.9)  # L, R shoulder
    k[7] = (shoulders[0] - 5, 205, 0.9); k[8] = (shoulders[1] + 5, 205, 0.9)  # elbows
    k[9] = (lw[0], lw[1], lw_vis)
    k[10] = (rw[0], rw[1], rw_vis)
    k[11] = (300, 260, 0.9); k[12] = (340, 260, 0.9)                  # hips
    k[13] = (302, 350, knee_vis); k[14] = (338, 350, knee_vis)        # knees
    k[15] = (302, 430, 0.9); k[16] = (338, 430, 0.9)
    k[:, 0] += dx
    return k


class Driver:
    def __init__(self, cam, tid, t0=1000.0):
        self.cam, self.tid, self.t = cam, tid, t0
        self.interactions, self.incidents = [], []

    def step(self, rw, n=1, **kw):
        dx = kw.get("dx", 0.0)
        for _ in range(n):
            trk = SimpleNamespace(track_id=self.tid, bbox=(275.0 + dx, 90.0, 365.0 + dx, 440.0),
                                  keypoints=skeleton(rw, **kw), confirmed=True, floor_xy=None)
            res = pose_analytics.observe(self.cam, self.t, FRAME, [trk])
            self.interactions.extend(res.interactions)
            self.incidents.extend(res.incidents)
            self.t += DT
        return self

    def rules(self):
        return [i["rule"] for i in self.incidents]


@pytest.fixture(scope="module", autouse=True)
def schema():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    yield


@pytest.fixture
def zone():
    made = []

    def make(cam, category="Snacks", price=4.5):
        z = ProductShelfZone(id=f"pz_{cam}", camera_id=cam, name=f"Shelf {cam}", sku_id="SKU-P",
                             category=category, price=price,
                             points=[PointCoord(x=x, y=y) for x, y in ZONE_POINTS])
        shelf_interaction_service.save_zone(z)
        made.append(z.id)
        return z

    yield make
    for zid in made:
        shelf_interaction_service.delete_zone(zid)
    pose_analytics.reset_all()
    pose_analytics.flush_now()


def _reach(d, **kw):
    return d.step(RIGHT_REST, 2, **kw).step(IN_SHELF, 4, **kw)


# --------------------------------------------------------------------- chest

def test_chest_concealment_fires(zone):
    zone("cam_pp_chest", category="Cosmetics", price=15.0)
    d = Driver("cam_pp_chest", "trk_chest")
    _reach(d).step(MID_AIR, 1).step(CHEST, 4).step(RIGHT_REST, 14)
    assert d.rules() == ["CONCEALMENT"]
    inc = d.incidents[0]
    assert any("opposite side of the chest" in e for e in inc["evidence"])
    assert inc["confidence"] >= settings.THEFT_MIN_CONFIDENCE
    assert inc["estimated_loss_value"] == 15.0


def test_chest_two_handed_handling_is_not_concealment(zone):
    """Both hands at the chest: opening or reading a pack."""
    zone("cam_pp_read")
    d = Driver("cam_pp_read", "trk_read")
    _reach(d).step(MID_AIR, 1).step(CHEST, 4, lw=(312.0, 182.0)).step(RIGHT_REST, 14)
    assert d.incidents == []


def test_chest_long_hold_is_examining_not_concealment(zone):
    zone("cam_pp_exam")
    d = Driver("cam_pp_exam", "trk_exam")
    _reach(d).step(MID_AIR, 1).step(CHEST, 25).step(RIGHT_REST, 14)   # 5 s at the chest
    assert "CONCEALMENT" not in d.rules()


def test_chest_needs_a_frontal_view(zone):
    """Shoulders 20 px apart (profile): left/right crossing is not measurable."""
    zone("cam_pp_prof")
    d = Driver("cam_pp_prof", "trk_prof")
    sh = (310.0, 330.0)
    _reach(d, shoulders=sh).step(MID_AIR, 1, shoulders=sh).step((314.0, 175.0), 4, shoulders=sh)
    d.step(RIGHT_REST, 14, shoulders=sh)
    assert d.incidents == []


# ---------------------------------------------------------- behind the back

def _behind(d, hidden=6, **kw):
    _reach(d, **kw).step(FLANK, 1, **kw).step(HIDDEN, hidden, rw_vis=0.05, **kw)
    return d.step(RIGHT_REST, 14, **kw)


def test_behind_back_after_reach_fires(zone):
    zone("cam_pp_back", category="Razors", price=22.0)
    d = _behind(Driver("cam_pp_back", "trk_back"))
    assert d.rules() == ["CONCEALMENT"]
    inc = d.incidents[0]
    assert any("out of sight behind the body" in e for e in inc["evidence"])
    assert any("confidence is scaled" in e for e in inc["evidence"])
    assert settings.THEFT_MIN_CONFIDENCE <= inc["confidence"] < 0.8


def test_behind_back_brief_occlusion_does_not_fire(zone):
    """Hidden for 0.4 s (an arm swing), shorter than THEFT_OCCLUDED_MIN_HOLD_SEC."""
    zone("cam_pp_swing")
    d = _behind(Driver("cam_pp_swing", "trk_swing"), hidden=2)
    assert d.incidents == []


def test_behind_back_needs_lower_body_visible(zone):
    """Knees hidden: a trolley or display in front could hide the hand."""
    zone("cam_pp_trolley")
    d = _behind(Driver("cam_pp_trolley", "trk_trolley"), knee_vis=0.1)
    assert d.incidents == []


def test_behind_back_needs_the_face_towards_the_camera(zone):
    """From behind, a hand in front of the body (into a trolley) is hidden too."""
    zone("cam_pp_away")
    d = _behind(Driver("cam_pp_away", "trk_away"), face_vis=0.05)
    assert d.incidents == []


def test_wrist_vanishing_at_the_shelf_is_not_behind_back(zone):
    """Never seen leaving the shelf: the hand may be hidden by products."""
    zone("cam_pp_vanish")
    d = Driver("cam_pp_vanish", "trk_vanish")
    _reach(d).step(HIDDEN, 8, rw_vis=0.05).step(RIGHT_REST, 14)
    assert d.incidents == []


def test_other_person_overlapping_blocks_behind_back(zone):
    zone("cam_pp_crowd")
    cam, t = "cam_pp_crowd", 1000.0
    incidents = []

    def frame(rw, rw_vis=0.9):
        nonlocal t
        a = SimpleNamespace(track_id="trk_a", bbox=(275.0, 90.0, 365.0, 440.0),
                            keypoints=skeleton(rw, rw_vis), confirmed=True, floor_xy=None)
        b = SimpleNamespace(track_id="trk_b", bbox=(330.0, 100.0, 420.0, 450.0),
                            keypoints=skeleton((402.0, 300.0), dx=60.0), confirmed=True, floor_xy=None)
        incidents.extend(pose_analytics.observe(cam, t, FRAME, [a, b]).incidents)
        t += DT

    for p, n, v in ((RIGHT_REST, 2, 0.9), (IN_SHELF, 4, 0.9), (FLANK, 1, 0.9), (HIDDEN, 6, 0.05),
                    (RIGHT_REST, 14, 0.9)):
        for _ in range(n):
            frame(p, v)
    assert [i for i in incidents if i["track_id"] == "trk_a"] == []


# ------------------------------------------------------------ false alarms

def test_hand_hanging_at_side_with_basket_does_not_fire(zone):
    zone("cam_pp_basket", category="Cosmetics", price=8.0)
    d = Driver("cam_pp_basket", "trk_basket")
    _reach(d).step(MID_AIR, 1).step(BASKET, 20)
    assert d.incidents == []


def test_basket_position_would_be_pocket_band_without_the_hanging_arm_filter(zone, monkeypatch):
    """The same frames fire when the straight-arm filter is disabled: it is what prevents the alarm."""
    monkeypatch.setattr(settings, "THEFT_HANGING_ARM_STRAIGHT_RATIO", 1.01)
    zone("cam_pp_basket2", category="Cosmetics", price=8.0)
    d = Driver("cam_pp_basket2", "trk_basket2")
    _reach(d).step(MID_AIR, 1).step(BASKET, 20)
    assert d.rules() == ["CONCEALMENT"]


def test_pocket_concealment_still_fires(zone):
    zone("cam_pp_pocket", category="Cosmetics", price=8.0)
    d = Driver("cam_pp_pocket", "trk_pocket")
    _reach(d).step(MID_AIR, 1).step(POCKET, 6).step(RIGHT_REST, 12)
    assert d.rules() == ["CONCEALMENT"]
    assert any("waistband/pocket" in e for e in d.incidents[0]["evidence"])


def test_walking_past_does_not_fire(zone):
    """Hips moving ~2 torso lengths/s; the swinging hand crosses the zone and hides behind the hip."""
    zone("cam_pp_walk")
    d = Driver("cam_pp_walk", "trk_walk")
    for i in range(20):
        dx = -300.0 + i * 38.0                        # 38 px per 0.2 s frame = 1.7 torso lengths/s
        if i % 4 in (0, 1):
            d.step((IN_SHELF[0] - dx, IN_SHELF[1]), 1, dx=dx)   # swinging hand over the shelf zone
        elif i % 4 == 2:
            d.step(FLANK, 1, dx=dx)
        else:
            d.step(HIDDEN, 1, dx=dx, rw_vis=0.05)
    assert d.incidents == []
    assert d.interactions == []


def test_static_track_is_ignored(zone):
    zone("cam_pp_static")
    trk = SimpleNamespace(track_id="trk_poster", bbox=(275.0, 90.0, 365.0, 440.0), keypoints=skeleton(IN_SHELF),
                          confirmed=True, floor_xy=None, motion_state="static")
    for i in range(10):
        res = pose_analytics.observe("cam_pp_static", 1000.0 + i * DT, FRAME, [trk])
        assert res.interactions == [] and res.incidents == []
    assert "trk_poster" not in pose_analytics._cameras["cam_pp_static"].tracks


# ------------------------------------------------------- behaviour pattern

def _browse_suspiciously(d, seconds=15.0):
    """By high-value stock: 3 pickups within 10 s (one short of sweeping), none put
    back, glancing left/right throughout (a head turn every 0.4 s)."""
    for i in range(int(round(seconds / DT))):
        t = i * DT
        in_reach = any(s - 1e-6 <= t < s + 0.4 - 1e-6 for s in (0.0, 5.0, 9.4))
        d.step(IN_SHELF if in_reach else RIGHT_REST, 1, head_side=(1 if (i // 2) % 2 == 0 else -1))
    return d


def test_behaviour_pattern_fires_from_several_weak_cues(zone, monkeypatch):
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_DWELL_SEC", 20.0)   # dwell cue at 10 s
    zone("cam_pp_pattern", category="Alcohol", price=40.0)
    d = _browse_suspiciously(Driver("cam_pp_pattern", "trk_pattern"))
    assert d.rules() == ["BEHAVIOUR_PATTERN"]
    inc = d.incidents[0]
    assert inc["confidence"] >= settings.THEFT_MIN_CONFIDENCE
    ev = " | ".join(inc["evidence"])
    for label in ("Several quick reaches", "Frequent head turning", "Long stay by high-value",
                  "picked up and not put back"):
        assert label in ev
    assert "No single loss-prevention rule fired" in ev
    # Cues were consumed and the cooldown holds: no second alert for the same evidence.
    d.step(RIGHT_REST, 20, head_side=1)
    assert d.rules() == ["BEHAVIOUR_PATTERN"]


def test_behaviour_pattern_needs_two_cue_types(zone):
    """Head scanning alone, however long, is one cue type."""
    zone("cam_pp_scan", category="Snacks")
    d = Driver("cam_pp_scan", "trk_scan")
    for i in range(100):
        d.step(RIGHT_REST, 2, head_side=(1 if i % 2 == 0 else -1))
    assert d.incidents == []


def test_no_pattern_after_a_single_rule_fired(zone, monkeypatch):
    # Dwell cue at 20 s (reached), loitering rule at 40 s (not reached).
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_DWELL_SEC", 40.0)
    zone("cam_pp_once", category="Alcohol", price=40.0)
    d = Driver("cam_pp_once", "trk_once")
    _reach(d).step(MID_AIR, 1).step(POCKET, 6).step(RIGHT_REST, 12)
    assert d.rules() == ["CONCEALMENT"]
    d.step(RIGHT_REST, 50)                    # 10 s: the later reaches are no sweep with the first
    _browse_suspiciously(d)
    assert d.rules() == ["CONCEALMENT"]


def test_pattern_disabled_by_config(zone, monkeypatch):
    monkeypatch.setattr(settings, "THEFT_LOITER_MIN_DWELL_SEC", 20.0)
    monkeypatch.setattr(settings, "THEFT_PATTERN_ENABLED", False)
    zone("cam_pp_off", category="Alcohol", price=40.0)
    d = _browse_suspiciously(Driver("cam_pp_off", "trk_off"))
    assert d.incidents == []


def test_pattern_threshold_scales_with_role_sensitivity():
    base = cr.scaled_theft_thresholds(1.0)
    assert base["pattern_score_threshold"] == pytest.approx(settings.THEFT_PATTERN_SCORE_THRESHOLD)
    hv = cr.scaled_theft_thresholds(cr.theft_sensitivity("high_value"))
    assert 0.6 <= hv["pattern_score_threshold"] < base["pattern_score_threshold"]
    assert cr.scaled_theft_thresholds(cr.SENSITIVITY_MAX)["pattern_score_threshold"] >= 0.6


def test_pattern_weights_are_parsed_from_config():
    w = rules.parse_cue_map(settings.THEFT_PATTERN_WEIGHTS)
    assert set(w) == set(rules.PATTERN_CUE_LABELS)
    assert rules.parse_cue_map("a:0.5, b:x, :1, c:-1, d:0.2") == {"a": 0.5, "d": 0.2}
