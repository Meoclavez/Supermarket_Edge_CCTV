"""Static-figure filter: a poster or mannequin is not a moving human.

The tracker classifies each track "pending" / "moving" / "static" from joint
micro-motion normalised by torso length (tracking_service.ByteTracker). A
figure that never moves beyond detector jitter becomes static after the
camera's static time and is kept out of counts, paths, zone rules, theft
analytics and customer_tracks persistence, while staying on the overlay.
Real people standing still (cashier, someone reading a label) keep moving a
wrist or the head and must stay "moving".

All time is a fake clock passed as ``now``; nothing sleeps.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.config import settings
from app.services import live_analytics_engine as lae
from app.services.inference_backend import Detection
from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine
from app.services.tracking_service import ByteTracker, floor_projector

CAM = "cam_unit_static_01"
FPS = 5.0
DT = 1.0 / FPS
BOX = (100.0, 50.0, 180.0, 300.0)

# A standing figure inside BOX: shoulders y=110, hips y=190 -> torso 80 px,
# so the motion threshold (0.15 torso) is 12 px.
_POSE = {
    0: (140, 70), 1: (135, 65), 2: (145, 65), 3: (130, 68), 4: (150, 68),
    5: (120, 110), 6: (160, 110), 7: (112, 150), 8: (168, 150),
    9: (108, 190), 10: (172, 190), 11: (125, 190), 12: (155, 190),
    13: (125, 240), 14: (155, 240), 15: (125, 290), 16: (155, 290),
}
HEAD = (0, 1, 2, 3, 4)
RIGHT_ARM = (8, 10)


def skeleton(dx: float = 0.0, dy: float = 0.0, moves: dict | None = None) -> np.ndarray:
    k = np.zeros((17, 3), dtype=np.float32)
    for i, (x, y) in _POSE.items():
        mx, my = (moves or {}).get(i, (0.0, 0.0))
        k[i] = (x + dx + mx, y + dy + my, 0.95)
    return k


def det(rng=None, jitter: float = 0.0, dx: float = 0.0, dy: float = 0.0,
        moves: dict | None = None) -> Detection:
    """One detection; ``jitter`` px of uniform noise on every keypoint and box edge."""
    k = skeleton(dx, dy, moves)
    x1, y1, x2, y2 = BOX
    b = np.array([x1 + dx, y1 + dy, x2 + dx, y2 + dy], dtype=np.float64)
    if jitter and rng is not None:
        k[:, :2] += rng.uniform(-jitter, jitter, size=(17, 2)).astype(np.float32)
        b += rng.uniform(-jitter, jitter, size=4)
    return Detection(x1=float(b[0]), y1=float(b[1]), x2=float(b[2]), y2=float(b[3]),
                     confidence=0.91, keypoints=k)


def run_poster(tr: ByteTracker, t0: float, seconds: float, rng, jitter: float = 2.0) -> float:
    """Feed a jittering poster for ``seconds``; returns the next timestamp."""
    n = int(round(seconds * FPS))
    for i in range(n):
        tr.update([det(rng, jitter)], now=t0 + i * DT)
    return t0 + n * DT


def only_track(tr: ByteTracker):
    (t,) = tr.tracks.values()
    return t


@pytest.fixture
def rng():
    return np.random.default_rng(1234)


# ------------------------------------------------------------ tracker level


def test_threshold_defaults_are_documented_values():
    assert settings.STATIC_FIGURE_SECONDS == 60
    assert settings.STATIC_FIGURE_GRACE_SEC == 5
    assert settings.STATIC_MEMORY_TTL_SEC == 1800
    assert 0.05 < settings.STATIC_FIGURE_MOTION_FRAC < 0.3


def test_poster_with_detector_jitter_becomes_static_after_the_configured_time(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 1000.0, 59.0, rng)
    t = only_track(tr)
    assert t.confirmed and t.motion_state == "pending" and t.is_human
    run_poster(tr, now, 2.0, rng)
    t = only_track(tr)
    assert t.motion_state == "static" and t.is_static and not t.is_human
    assert t.static_since is not None
    assert len(tr.static_memory) == 1


def test_per_camera_seconds_override(rng):
    tr = ByteTracker(CAM)
    tr.configure_static(True, 10)
    run_poster(tr, 1000.0, 11.0, rng)
    assert only_track(tr).is_static


def test_still_standing_person_with_small_wrist_motion_stays_moving(rng):
    """A cashier at the till: body still, one hand moving ~14 px (~9 cm) now and then.

    Torso 80 px ~ 50 cm, so 14 px is about 9 cm, a small hand movement;
    the body sways by 2 px (below jitter level) the whole time.
    """
    tr = ByteTracker(CAM)
    for i in range(int(180 * FPS)):
        now = 2000.0 + i * DT
        el = now - 2000.0
        gesture = (el % 15.0) < 3.0 and el > 1.0               # a 3 s gesture every 15 s
        w = math.sin(math.pi * (el % 15.0) / 3.0) if gesture else 0.0
        moves = {10: (11.0 * w, 9.0 * w), 8: (4.0 * w, 3.0 * w)}
        sway = 2.0 * math.sin(2 * math.pi * el / 7.0)
        tr.update([det(rng, 1.5, dx=sway, moves=moves)], now=now)
        assert not only_track(tr).is_static, f"flagged static at {now - 2000.0:.1f}s"
    t = only_track(tr)
    assert t.motion_state == "moving" and t.is_human


def test_still_person_reading_a_label_with_occasional_head_turns_stays_moving(rng):
    """Head turns of ~15 px (0.19 torso) once every 20 s, held for a second."""
    tr = ByteTracker(CAM)
    for i in range(int(180 * FPS)):
        now = 3000.0 + i * DT
        turning = (now - 3000.0) % 20.0 < 1.0 and now - 3000.0 > 1.0
        moves = {j: (15.0, 0.0) for j in HEAD} if turning else None
        tr.update([det(rng, 1.5, moves=moves)], now=now)
        assert not only_track(tr).is_static
    assert only_track(tr).motion_state == "moving"


def test_single_frame_keypoint_glitch_is_not_motion(rng):
    tr = ByteTracker(CAM)
    now = 4000.0
    for i in range(int(61 * FPS)):
        # Every 3 s one wrist jumps 40 px for exactly one frame (a model glitch).
        glitch = i % 15 == 7
        tr.update([det(rng, 2.0, moves={9: (40.0, 0.0)} if glitch else None)], now=now + i * DT)
    assert only_track(tr).is_static


def _noisy_wrists(rng, extra: dict | None = None, sigma: float = 7.0) -> dict:
    """Wrists the pose model cannot pin down on a small, unchanging figure.

    Measured on the real RTMO model (noisy JPEG frames of a still photo, a
    150 px figure, torso 43 px): the wrists' frame-to-frame step had a median
    of 0.12-0.17 torso and a 75th percentile of 0.23-0.27 torso, far above
    the 0.15 torso motion threshold. Gaussian noise of 7 px per axis on this
    80 px torso reproduces that (step median ~0.15, 75th pct ~0.21 torso).
    """
    moves = dict(extra or {})
    for j in (9, 10):
        dx, dy = rng.normal(0.0, sigma, 2)
        mx, my = moves.get(j, (0.0, 0.0))
        moves[j] = (mx + float(dx), my + float(dy))
    return moves


def test_poster_with_noisy_wrists_becomes_static_and_stays_static(rng):
    """Flagged at ~60 s and never un-flagged.

    Until a joint has MOTION_JITTER_MIN_STEPS steps (1.6 s here) its floor
    is unknown, so one early event may restart the clock: hence 62 s.
    """
    tr = ByteTracker(CAM)
    first_static = None
    for i in range(int(180 * FPS)):
        now = 6000.0 + i * DT
        tr.update([det(rng, 1.5, moves=_noisy_wrists(rng))], now=now)
        t = only_track(tr)
        if first_static is None and t.is_static:
            first_static = now - 6000.0
        elif first_static is not None:
            assert t.is_static, f"un-flagged at {now - 6000.0:.1f}s"
    assert first_static is not None and 59.0 < first_static < 62.0, first_static


def test_noisy_wrists_are_motion_without_their_jitter_floor(rng, monkeypatch):
    """The same poster never goes static with the floor off: the floor is what fixes it."""
    from app.services import tracking_service

    monkeypatch.setattr(tracking_service, "MOTION_JITTER_K", 0.0)
    tr = ByteTracker(CAM)
    for i in range(int(90 * FPS)):
        tr.update([det(rng, 1.5, moves=_noisy_wrists(rng))], now=6500.0 + i * DT)
    assert not only_track(tr).is_static


def test_person_with_noisy_wrists_still_moves_with_head_turns(rng):
    """The floor is per joint: noisy wrists do not hide a 15 px (0.19 torso) head turn."""
    tr = ByteTracker(CAM)
    for i in range(int(180 * FPS)):
        now = 7000.0 + i * DT
        el = now - 7000.0
        turning = el % 20.0 < 1.0 and el > 1.0
        head = {j: (15.0, 0.0) for j in HEAD} if turning else None
        tr.update([det(rng, 1.5, moves=_noisy_wrists(rng, head))], now=now)
        assert not only_track(tr).is_static, f"flagged static at {el:.1f}s"
    assert only_track(tr).motion_state == "moving"


def test_recent_pose_ignores_a_single_frame_flip():
    from collections import deque

    from app.services.tracking_service import recent_pose

    still, flipped = skeleton(), skeleton(moves={9: (40.0, 0.0)})
    p = recent_pose(deque([still, flipped, still]))
    assert np.allclose(p[:, :2], still[:, :2])
    p = recent_pose(deque([still, flipped, flipped]))
    assert np.allclose(p[9, :2], flipped[9, :2])
    assert recent_pose(deque()) is None


def test_static_track_unflags_as_soon_as_it_moves(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 5000.0, 61.0, rng)
    t = only_track(tr)
    assert t.is_static
    # Someone was standing that still after all and now walks off.
    tr.update([det(rng, 1.0, dx=20.0)], now=now)
    assert t.is_static  # one frame is not yet motion
    tr.update([det(rng, 1.0, dx=30.0)], now=now + DT)
    assert t.motion_state == "moving" and t.is_human and t.static_since is None


def _retire(tr: ByteTracker, now: float) -> float:
    for _ in range(settings.TRACK_MAX_AGE_FRAMES + 2):
        tr.update([], now=now)
        now += DT
    assert not tr.tracks
    return now


def test_new_track_on_a_remembered_static_box_is_static_after_the_grace(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 6000.0, 61.0, rng)
    old = only_track(tr)
    assert old.is_static
    now = _retire(tr, now)          # the poster flickered out; its track ended
    finished = tr.drain_finished()
    assert [f.track_id for f in finished] == [old.track_id]

    now = run_poster(tr, now, 3.0, rng)
    t = only_track(tr)
    assert t.track_id != old.track_id and t.confirmed and t.motion_state == "pending"
    run_poster(tr, now, 3.0, rng)
    assert only_track(tr).is_static


def test_real_person_on_a_remembered_box_in_another_pose_needs_the_full_time(rng):
    """Same box as the poster, different posture: the memory does not shortcut it."""
    tr = ByteTracker(CAM)
    now = run_poster(tr, 6500.0, 61.0, rng)
    now = _retire(tr, now)
    other = {9: (25.0, -60.0), 10: (-25.0, -60.0), 7: (5.0, -30.0), 8: (-5.0, -30.0),
             0: (0.0, 20.0), 1: (0.0, 20.0), 2: (0.0, 20.0), 3: (0.0, 20.0), 4: (0.0, 20.0)}
    for i in range(int(20 * FPS)):
        tr.update([det(rng, 1.5, moves=other)], now=now + i * DT)
    assert not only_track(tr).is_static


def test_static_memory_expires_after_the_ttl(rng, monkeypatch):
    monkeypatch.setattr(settings, "STATIC_MEMORY_TTL_SEC", 600.0)
    tr = ByteTracker(CAM)
    now = run_poster(tr, 7000.0, 61.0, rng)
    now = _retire(tr, now)
    assert len(tr.static_memory) == 1
    now += 601.0
    tr.update([], now=now)
    assert tr.static_memory == []
    now = run_poster(tr, now, 10.0, rng)
    assert only_track(tr).motion_state == "pending"   # full time again, not the grace


def test_unobserved_gap_is_not_time_standing_still(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 7500.0, 30.0, rng)
    # 40 s with no observation (inference paused) and the track still alive.
    t = only_track(tr)
    tr.update([det(rng, 2.0)], now=now + 40.0)
    assert t.motion_state == "pending"


def test_per_camera_disable_never_flags_static(rng):
    tr = ByteTracker(CAM)
    tr.configure_static(False, None)
    run_poster(tr, 8000.0, 120.0, rng)
    t = only_track(tr)
    assert not t.is_static and t.is_human
    assert tr.static_memory == []


def test_disabling_reverts_a_static_track(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 8500.0, 61.0, rng)
    assert only_track(tr).is_static
    tr.configure_static(False, None)
    tr.update([det(rng, 2.0)], now=now)
    assert only_track(tr).is_human
    assert not tr.matches_static_memory(BOX, skeleton(), now)


def test_matches_static_memory_needs_the_same_box_and_pose(rng):
    tr = ByteTracker(CAM)
    now = run_poster(tr, 9000.0, 61.0, rng)
    assert tr.matches_static_memory(BOX, skeleton(), now)
    assert not tr.matches_static_memory((300.0, 50.0, 380.0, 300.0), skeleton(200.0), now)
    other = {9: (25.0, -60.0), 10: (-25.0, -60.0), 7: (5.0, -30.0), 8: (-5.0, -30.0),
             0: (0.0, 20.0), 1: (0.0, 20.0), 2: (0.0, 20.0), 3: (0.0, 20.0), 4: (0.0, 20.0)}
    assert not tr.matches_static_memory(BOX, skeleton(moves=other), now)


# ------------------------------------------------------------- engine level


class _FakePose:
    def __init__(self):
        self.seen: list[list[str]] = []

    def observe(self, camera_id, ts, frame_bgr, tracks):
        self.seen.append([t.track_id for t in tracks])

        class R:
            interactions = []
        return R()

    def reset_camera(self, camera_id):
        pass


@pytest.fixture
def fake_pose(monkeypatch):
    fake = _FakePose()
    monkeypatch.setitem(lae._pose_state, "module", fake)
    monkeypatch.setitem(lae._pose_state, "status", "ok")
    monkeypatch.setitem(lae._pose_state, "error", None)
    return fake


@pytest.fixture
def poster_detector(monkeypatch):
    rng = np.random.default_rng(99)

    def detect(frame, conf_threshold=None, iou_threshold=None, camera_id=None, **_kw):
        return [det(rng, 2.0)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)


@pytest.fixture
def features():
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    def set_features(**kw):
        # Before the schema knows the static keys, construct without them.
        known = {k: v for k, v in kw.items() if k in CameraFeatureConfig.model_fields}
        feature_manager.set_camera_features(CAM, CameraFeatureConfig(**known))
        return set(kw) - set(known)

    set_features()
    yield set_features
    feature_manager.remove_camera(CAM)


@pytest.fixture
def worker():
    engine = LiveAnalyticsEngine()
    rt = CameraRuntime(camera_id=CAM, name="Static Cam", source="/dev/null")
    engine.runtimes[CAM] = rt
    w = CameraWorker(rt, engine)
    yield w, rt, engine
    floor_projector.set_homography(CAM, None)


@pytest.fixture
def tripwires(monkeypatch):
    from app.services.tripwire_engine import tripwire_engine

    seen: list[list[str]] = []

    def evaluate(camera_id, tracks, w, h, now, frame=None, counting=True, camera_name=None):
        seen.append([t.track_id for t in tracks])

    monkeypatch.setattr(tripwire_engine, "evaluate", evaluate)
    return seen


def _calibrate_with_zone(engine):
    floor_projector.set_homography(CAM, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    engine.set_zones([
        {"id": "zone_a", "category": "AISLE",
         "polygon": [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 5}, {"x": 0, "y": 5}]},
    ])


def _analyse_for(w, t0: float, seconds: float) -> float:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    n = int(round(seconds * FPS))
    for i in range(n):
        w._analyse(frame, now=t0 + i * DT)
    return t0 + n * DT


def test_static_tracks_are_excluded_from_every_consumer(
        worker, poster_detector, fake_pose, tripwires, features, monkeypatch):
    w, rt, engine = worker
    missing = features(static_figure_seconds=10)
    if missing:  # schema without the per-camera key yet: use the server default
        monkeypatch.setattr(settings, "STATIC_FIGURE_SECONDS", 10.0)
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 0.0)
    _calibrate_with_zone(engine)

    now = _analyse_for(w, 20000.0, 5.0)
    t = next(iter(w.tracker.tracks.values()))
    assert t.motion_state == "pending" and rt.live_track_count == 1
    assert fake_pose.seen[-1] == [t.track_id] and tripwires[-1] == [t.track_id]
    # Pending and never moved: in the zone, but no visit is published yet.
    assert t.current_zone_id == "zone_a" and t.open_visit_id is None and not t.established
    visits, _ = engine.drain()
    assert visits == []
    points_before = len(t.path_points), len(t.floor_points)

    now = _analyse_for(w, now, 7.0)
    assert t.is_static
    # Not counted, not analysed, no zone rules, no further path/floor points.
    assert rt.live_track_count == 0
    assert fake_pose.seen[-1] == [] and tripwires[-1] == []
    assert len(t.path_points) >= points_before[0] and len(t.floor_points) >= points_before[1]
    frozen = len(t.path_points), len(t.floor_points)
    _analyse_for(w, now, 3.0)
    assert (len(t.path_points), len(t.floor_points)) == frozen
    # It left the zone when it turned static; no visit was ever written.
    visits, _ = engine.drain()
    assert visits == []
    assert t.current_zone_id is None

    # Still drawn on the overlay and listed in the snapshot as "static".
    (entry,) = rt.get_tracks()
    assert entry["motion_state"] == "static" and entry["confirmed"] is True
    snap = engine.live_snapshot()
    assert snap["persons"] == [] and snap["uncalibrated_track_count"] == 0
    assert snap["detections"][0]["boxes"][0]["motion_state"] == "static"

    # Never persisted as a customer track.
    for ft in w.tracker.flush_all():
        engine.close_track(ft, reason="track_lost", persist=True)
    _visits, tracks = engine.drain()
    assert tracks == []


def test_moving_people_are_still_persisted(worker, fake_pose, features, monkeypatch):
    w, rt, engine = worker
    rng = np.random.default_rng(5)
    clock = {"i": 0}

    def detect(frame, conf_threshold=None, iou_threshold=None, camera_id=None, **_kw):
        clock["i"] += 1
        return [det(rng, 1.0, dx=3.0 * clock["i"])]   # walking right, 15 px/s

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    _analyse_for(w, 21000.0, 20.0)
    t = next(iter(w.tracker.tracks.values()))
    assert t.motion_state == "moving" and rt.live_track_count == 1
    assert rt.get_tracks()[0]["motion_state"] == "moving"
    for ft in w.tracker.flush_all():
        engine.close_track(ft, reason="track_lost", persist=True)
    assert [x.track_id for x in engine.drain()[1]] == [t.track_id]


def test_per_camera_disable_keeps_the_poster_counted(worker, poster_detector, fake_pose, features, monkeypatch):
    w, rt, engine = worker
    missing = features(static_figure_filter=False)
    if missing:
        pytest.skip("schema has no static_figure_filter yet (Stream C)")
    _analyse_for(w, 22000.0, 90.0)
    t = next(iter(w.tracker.tracks.values()))
    assert not t.is_static and rt.live_track_count == 1
    assert rt.get_tracks()[0]["motion_state"] == "pending"


def test_render_overlay_draws_static_figures_grey(worker, poster_detector, fake_pose, features, monkeypatch):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "STATIC_FIGURE_SECONDS", 10.0)
    _analyse_for(w, 23000.0, 12.0)
    (entry,) = rt.get_tracks()
    assert entry["motion_state"] == "static"
    out = lae.render_overlay(np.zeros((480, 640, 3), dtype=np.uint8), rt)
    x1, y1, x2, y2 = (int(entry[k]) for k in ("x1", "y1", "x2", "y2"))
    # Left edge, below the label: the muted grey, not the confirmed green.
    assert tuple(int(v) for v in out[(y1 + y2) // 2, x1]) == (150, 150, 150)


def test_night_confirm_ignores_a_remembered_static_figure(worker, poster_detector, fake_pose, features,
                                                         monkeypatch):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "STATIC_FIGURE_SECONDS", 10.0)
    now = _analyse_for(w, 24000.0, 12.0)
    assert len(w.tracker.static_memory) == 1

    got: list[int] = []
    monkeypatch.setattr(lae.nw.night_watch, "confirm",
                        lambda cam, frame, dets, now: got.append(len(dets)))
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    w._night_confirm(frame, now + 600.0)
    assert got == [0] and rt.detections_last == 0

    # A person elsewhere in the frame is still confirmed.
    monkeypatch.setattr(lae.person_detector, "detect",
                        lambda frame, **kw: [det(dx=300.0)])
    w._night_confirm(frame, now + 601.0)
    assert got[-1] == 1
