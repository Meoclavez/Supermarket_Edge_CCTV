"""Reach lifecycle on frames without a fresh skeleton, and cross-track dedupe.

1. A shopper who steps out of view mid-reach (the tracker coasts, or stops
   reporting the track) used to keep the reach open until the track died
   (~30 s at 5 fps with detection every 5th frame). The occlusion grace now
   runs on frame time: the reach closes ``INTERACTION_OCCLUSION_GRACE_SEC``
   after the hand was last seen in the zone, and ``ended_at`` is that last
   sighting. A brief occlusion still yields one reach.
2. Two overlapping people: the pose model gave the front shopper's hand to the
   person behind as well. One physical reach must produce one row, on the
   track whose arm chain fits; two people reaching side by side keep two.

Skeletons come from the human-proportion builder in test_reach_mapping.
Persisted rows are captured at ``_enqueue`` (no database involved).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.pose_analytics import pose_analytics
from app.services.shelf_interaction_service import PointCoord, ProductShelfZone, shelf_interaction_service

from tests.test_reach_mapping import FRAME, MID, REST, figure, lerp_angles, track_ns

CAM = "cam_lifecycle"
ZONE = "lc_mid"
DT = 0.2                                     # 5 analysed frames per second
GRACE = settings.INTERACTION_OCCLUSION_GRACE_SEC


@pytest.fixture
def scene(monkeypatch):
    shelf_interaction_service.save_zone(ProductShelfZone(
        id=ZONE, camera_id=CAM, name="Coffee (eye level)", sku_id="SKU-LC", category="Coffee", price=9.0,
        points=[PointCoord(x=x, y=y) for x, y in ((0.50, 0.14), (0.734, 0.14), (0.734, 0.50), (0.50, 0.50))],
    ))
    pose_analytics.reset_camera(CAM)
    rows: list[dict] = []

    def capture(kind, payload):
        if kind == "interaction":
            rows.append(payload)

    monkeypatch.setattr(pose_analytics, "_enqueue", capture)
    yield rows
    pose_analytics.reset_camera(CAM)
    shelf_interaction_service.delete_zone(ZONE)


class Feed:
    """Feeds frames at DT; records live interactions and when rows appear."""

    def __init__(self, rows, t0=1_000_000.0):
        self.rows, self.t, self.live, self.row_times = rows, t0, [], []

    def frame(self, tracks):
        before = len(self.rows)
        res = pose_analytics.observe(CAM, self.t, FRAME, tracks, [])
        self.live.extend(res.interactions)
        self.row_times.extend([self.t] * (len(self.rows) - before))
        self.t += DT
        return self.t - DT


def _coasting(tid, kps):
    t = track_ns(tid, kps, None)
    t.keypoints_fresh = False
    return t


def _reach_until_hold(feed, tid="p1", hold=5, **kw):
    """Rest, raise the right hand into the zone, hold. Returns (first hold ts, last hold ts)."""
    for _ in range(3):
        feed.frame([track_ns(tid, figure(520, **kw), None)])
    for a in lerp_angles(REST, MID, 2):
        feed.frame([track_ns(tid, figure(520, right=a, **kw), None)])
    first = None
    last = None
    for _ in range(hold):
        last = feed.frame([track_ns(tid, figure(520, right=MID, **kw), None)])
        first = last if first is None else first
    return first, last


# --------------------------------------------------------------------------- 1. lifecycle

@pytest.mark.parametrize("how", ["coasting", "gone"])
def test_person_leaving_view_mid_reach_closes_it_after_the_grace(scene, how):
    feed = Feed(scene)
    first_hold, last_seen = _reach_until_hold(feed)
    assert feed.live == [("p1", ZONE)] and scene == []

    stale = figure(520, right=MID)
    for _ in range(int(4.0 / DT)):                    # 4 s out of view, far below the 10 s TTL
        feed.frame([_coasting("p1", stale)] if how == "coasting" else [])

    assert len(scene) == 1, scene
    rec = scene[0]
    assert rec["track_id"] == "p1" and rec["zone_id"] == ZONE and rec["hand"] == "right"
    # Closed once the grace ran out, not when the track died ...
    assert last_seen + GRACE < feed.row_times[0] <= last_seen + GRACE + DT + 1e-6
    # ... and dated to the last time the hand was seen in the zone.
    assert rec["ended_at"] == pytest.approx(last_seen)
    assert rec["started_at"] <= first_hold
    assert rec["duration_sec"] == pytest.approx(round(last_seen - rec["started_at"], 3))
    assert rec["duration_sec"] < 2.0
    # The track state itself is kept until the TTL (it may come back).
    assert "p1" in pose_analytics._cameras[CAM].tracks


def test_brief_occlusion_below_the_grace_is_still_one_reach(scene):
    feed = Feed(scene)
    first_hold, _ = _reach_until_hold(feed, hold=3)
    stale = figure(520, right=MID)
    for _ in range(3):                                # 0.6 s without a fresh skeleton (< 1 s grace)
        feed.frame([_coasting("p1", stale)])
    last_seen = None
    for _ in range(3):                                # the hand is back in the same zone
        last_seen = feed.frame([track_ns("p1", figure(520, right=MID), None)])
    for a in lerp_angles(MID, REST, 3):
        feed.frame([track_ns("p1", figure(520, right=a), None)])
    for _ in range(8):
        feed.frame([track_ns("p1", figure(520), None)])

    assert feed.live == [("p1", ZONE)]
    assert len(scene) == 1, scene
    rec = scene[0]
    assert rec["ended_at"] >= last_seen - 1e-6 and rec["started_at"] <= first_hold
    assert rec["duration_sec"] >= last_seen - first_hold - 1e-6   # spans the occlusion


def test_hand_back_after_the_grace_is_a_new_reach(scene):
    feed = Feed(scene)
    _reach_until_hold(feed, hold=3)
    stale = figure(520, right=MID)
    for _ in range(int((GRACE + 0.8) / DT)):
        feed.frame([_coasting("p1", stale)])
    assert len(scene) == 1
    for _ in range(4):
        feed.frame([track_ns("p1", figure(520, right=MID), None)])
    pose_analytics.reset_camera(CAM)
    assert len(scene) == 2 and feed.live == [("p1", ZONE), ("p1", ZONE)]


# --------------------------------------------------------------------------- 2. dedupe

def _front_and_behind(front_right, *, elbow_behind=None):
    """Front shopper (tall, x=520) and a smaller person behind whose skeleton
    was given the front shopper's right wrist (and optionally elbow)."""
    front = figure(520, right=front_right)
    behind_kw = dict(H=400.0, top=100.0)
    base = figure(450, **behind_kw)
    elbow = elbow_behind if elbow_behind is not None else (float(base[8, 0]), float(base[8, 1]))
    wrist = (float(front[10, 0]), float(front[10, 1]))
    behind = figure(450, right_pts=(elbow, wrist), **behind_kw)
    return front, behind


@pytest.mark.parametrize("borrowed", ["wrist_only", "wrist_and_elbow"])
def test_one_hand_claimed_by_two_overlapping_people_is_one_reach(scene, borrowed):
    feed = Feed(scene)
    front_elbow = None
    for i in range(3):
        f, b = _front_and_behind(REST)
        feed.frame([track_ns("front", f, None), track_ns("behind", b, None)])
    poses = list(lerp_angles(REST, MID, 2)) + [MID] * 6
    for a in poses:
        f0 = figure(520, right=a)
        front_elbow = (float(f0[8, 0]), float(f0[8, 1]))
        f, b = _front_and_behind(a, elbow_behind=front_elbow if borrowed == "wrist_and_elbow" else None)
        feed.frame([track_ns("front", f, None), track_ns("behind", b, None)])
    for a in lerp_angles(MID, REST, 3):
        f, b = _front_and_behind(a)
        feed.frame([track_ns("front", f, None), track_ns("behind", b, None)])
    for _ in range(8):
        f, b = _front_and_behind(REST)
        feed.frame([track_ns("front", f, None), track_ns("behind", b, None)])
    pose_analytics.reset_camera(CAM)

    assert [(r["track_id"], r["zone_id"]) for r in scene] == [("front", ZONE)], scene
    assert feed.live == [("front", ZONE)]


def test_dedupe_state_leaves_no_trace_on_the_suppressed_track(scene):
    feed = Feed(scene)
    for a in [REST] * 3 + list(lerp_angles(REST, MID, 2)) + [MID] * 4:
        f, b = _front_and_behind(a)
        feed.frame([track_ns("front", f, None), track_ns("behind", b, None)])
    cam = pose_analytics._cameras[CAM]
    behind, front = cam.tracks["behind"], cam.tracks["front"]
    assert front.interaction_count == 1 and len(front.reaches) == 1
    assert behind.interaction_count == 0 and len(behind.reaches) == 0
    assert behind.first_interaction_t is None
    assert behind.hands["right"].active_suppressed is True


def test_two_people_reaching_side_by_side_keep_both_reaches(scene):
    feed = Feed(scene)
    left_reach = (210.0, 210.0)                        # second shopper at x=1000 reaching back-left, lower
    for i in range(3):
        feed.frame([track_ns("a", figure(520), None), track_ns("b", figure(1000), None)])
    steps = list(zip(lerp_angles(REST, MID, 2), lerp_angles(REST, left_reach, 2)))
    steps += [(MID, left_reach)] * 6
    for ra, la in steps:
        feed.frame([track_ns("a", figure(520, right=ra), None), track_ns("b", figure(1000, left=la), None)])
    for ra, la in zip(lerp_angles(MID, REST, 3), lerp_angles(left_reach, REST, 3)):
        feed.frame([track_ns("a", figure(520, right=ra), None), track_ns("b", figure(1000, left=la), None)])
    for _ in range(8):
        feed.frame([track_ns("a", figure(520), None), track_ns("b", figure(1000), None)])
    pose_analytics.reset_camera(CAM)

    got = sorted((r["track_id"], r["hand"], r["zone_id"]) for r in scene)
    assert got == [("a", "right", ZONE), ("b", "left", ZONE)], scene
    assert sorted(feed.live) == [("a", ZONE), ("b", ZONE)]
