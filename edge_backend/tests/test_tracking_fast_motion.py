"""Fast movers and variable analysed rates (app/services/tracking_service.py).

The scheduler now analyses a camera anywhere from 0.5 to 10+ frames a second,
and the rate changes as people come and go. The tracker therefore works in
seconds: the Kalman filter predicts by the real time between analysed frames,
a coasting track's predicted box keeps moving (``Track.predicted_bbox`` /
``predict_bbox``), a confident detection that IoU cannot place is matched by
centre distance from the predicted centre within a bounded, time-scaled gate,
and a lost track ends after TRACK_MAX_AGE_SEC whatever the frame count.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import tracking_service as ts
from app.services.inference_backend import Detection
from app.services.tracking_service import CentroidTracker

W, H = 704, 576          # D1 sub-stream


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for key, value in {"TRACK_MIN_HITS": 3, "TRACK_MAX_AGE_FRAMES": 30, "TRACK_MAX_AGE_SEC": 5.0,
                       "TRACK_IOU_THRESHOLD": 0.3, "TRACK_LOW_IOU_THRESHOLD": 0.5,
                       "TRACK_TENTATIVE_MAX_MISSES": 2, "TRACK_CENTRE_GATE_SPEED": 2.0,
                       "TRACK_CENTRE_GATE_MAX": 1.5, "PERSON_CONF_THRESHOLD": 0.5,
                       "TRACK_NEW_TRACK_THRESHOLD": 0.5}.items():
        monkeypatch.setattr(settings, key, value)


def _det(cx: float, top: float, w: float = 90.0, h: float = 300.0, conf: float = 0.9) -> Detection:
    return Detection(x1=cx - w / 2, y1=top, x2=cx + w / 2, y2=top + h, confidence=conf)


def _tracker() -> CentroidTracker:
    t = CentroidTracker("cam_fast")
    t.configure_static(False)
    return t


def _centre(box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def test_walker_crossing_a_third_of_the_frame_per_analysed_frame_at_2fps_keeps_one_id():
    """A third of a D1 frame (235 px) per 0.5 s for a 300 px tall person is ~2.7 m/s.

    The boxes (90 px wide) never overlap from one analysed frame to the next,
    so IoU alone (ByteTrack) started a new identity on every frame and the
    person was never confirmed.
    """
    t = _tracker()
    step = W / 3.0
    xs = [45.0 + step * i for i in range(3)]
    ids = []
    for i, x in enumerate(xs):
        live = t.update([_det(x, 150)], now=100.0 + 0.5 * i)
        assert len(live) == 1
        ids.append(live[0].track_id)
    assert len(set(ids)) == 1
    tr = t.tracks[ids[0]]
    assert tr.confirmed and tr.hits == 3
    assert tr.velocity_px[0] > 200.0                       # px/s, toward the right


def test_two_fast_walkers_crossing_keep_their_own_ids():
    t = _tracker()
    step = W / 3.0
    a_ids, b_ids = [], []
    for i in range(3):
        now = 50.0 + 0.5 * i
        a = _det(60.0 + step * i, 60)                      # left -> right, upper band
        b = _det(W - 60.0 - step * i, 240)                 # right -> left, lower band
        t.update([a, b], now=now)
        for tr in t.tracks.values():
            cx = (tr.bbox[0] + tr.bbox[2]) / 2.0
            if tr.last_seen == now and abs(cx - (a.x1 + a.x2) / 2) < 1:
                a_ids.append(tr.track_id)
            elif tr.last_seen == now and abs(cx - (b.x1 + b.x2) / 2) < 1:
                b_ids.append(tr.track_id)
    assert len(a_ids) == len(b_ids) == 3
    assert len(set(a_ids)) == 1 and len(set(b_ids)) == 1 and a_ids[0] != b_ids[0]
    assert len(t.tracks) == 2


def test_variable_analysed_rate_predicts_by_real_time():
    """Detections 0.1-1.0 s apart (the scheduler changes the rate): one id, velocity in px/s."""
    t = _tracker()
    speed = 300.0                                          # px/s, 1.5 box heights/s
    times = [0.0, 0.1, 0.6, 0.85, 1.85, 2.0, 2.5, 2.6, 3.6]
    ids = set()
    for when in times:
        live = t.update([_det(60.0 + speed * when, 100, w=60, h=200)], now=10.0 + when)
        ids |= {tr.track_id for tr in live}
    assert len(ids) == 1
    (tr,) = t.tracks.values()
    assert tr.velocity_px[0] == pytest.approx(speed, rel=0.2)
    # The prediction for a frame 0.7 s later lands on the walker (IoU would match).
    x1, y1, x2, y2 = tr.predict_bbox(10.0 + 3.6 + 0.7)
    assert _centre((x1, y1, x2, y2))[0] == pytest.approx(60.0 + speed * 4.3, abs=25)


def test_same_walker_at_a_different_rate_gives_the_same_velocity():
    """Per-frame Kalman steps measured px per analysed frame: 0.1 s and 0.5 s apart differed 5x."""
    vel = []
    for dt in (0.1, 0.5):
        t = _tracker()
        speed = 200.0
        for i in range(16):
            t.update([_det(60.0 + speed * dt * i, 100, w=60, h=200)], now=i * dt)
        (tr,) = t.tracks.values()
        vel.append(tr.velocity_px[0])
    # Both in px/s (the faster rate converges over more, noisier steps).
    assert vel[0] == pytest.approx(200.0, rel=0.1) and vel[1] == pytest.approx(200.0, rel=0.1)


def test_coasting_track_predicted_box_moves_and_then_stops():
    t = _tracker()
    speed = 250.0
    for i in range(6):
        t.update([_det(80.0 + speed * 0.2 * i, 100, w=60, h=200)], now=i * 0.2)
    (tid,) = t.tracks
    tr = t.tracks[tid]
    last = tr.bbox
    t.update([], now=1.5)                                   # missed for 0.5 s
    assert tr.misses == 1 and tr.bbox == last               # bbox: the last measured box
    moved = _centre(tr.predicted_bbox)[0] - _centre(last)[0]
    assert moved == pytest.approx(speed * 0.5, rel=0.25)    # predicted_bbox: where it is now
    ahead = _centre(tr.predict_bbox(1.8))[0] - _centre(last)[0]
    assert ahead == pytest.approx(speed * 0.8, rel=0.25)    # between analysed frames
    # Extrapolation stops COAST_PREDICT_SEC after the track was last seen.
    far = _centre(tr.predict_bbox(1.0 + ts.COAST_PREDICT_SEC + 5.0))[0]
    stop = _centre(tr.predict_bbox(1.0 + ts.COAST_PREDICT_SEC))[0]
    assert far == pytest.approx(stop)
    # Re-acquired where the prediction said, by IoU, same identity.
    live = t.update([_det(80.0 + speed * 1.7, 100, w=60, h=200)], now=1.7)
    assert [x.track_id for x in live] == [tid] and tr.misses == 0


def test_lost_track_ends_after_max_age_seconds_at_a_low_rate():
    """30 missed frames at 0.5 analysed frames/s were a minute of coasting; now TRACK_MAX_AGE_SEC."""
    t = _tracker()
    for i in range(4):
        t.update([_det(200, 100)], now=float(i))
    (tid,) = t.tracks
    assert t.tracks[tid].confirmed
    t.update([], now=5.0)
    t.update([], now=7.0)                                   # 4 s unseen, 2 misses: still coasting
    assert tid in t.tracks
    t.update([], now=9.0)                                   # 6 s unseen > 5 s
    assert tid not in t.tracks
    assert [x.track_id for x in t.drain_finished()] == [tid]


def test_frame_bound_still_applies_at_a_high_rate(monkeypatch):
    monkeypatch.setattr(settings, "TRACK_MAX_AGE_SEC", 60.0)
    t = _tracker()
    for i in range(4):
        t.update([_det(200, 100)], now=i * 0.1)
    (tid,) = t.tracks
    for i in range(settings.TRACK_MAX_AGE_FRAMES):
        t.update([], now=0.4 + i * 0.1)
    assert tid in t.tracks
    t.update([], now=0.4 + settings.TRACK_MAX_AGE_FRAMES * 0.1)
    assert tid not in t.tracks


def test_centre_gate_does_not_hand_an_id_to_someone_far_away():
    """At 10 fps the gate is a quarter of a box height: a new person nearby gets a new id."""
    t = _tracker()
    for i in range(4):
        t.update([_det(200, 100)], now=i * 0.1)
    (tid,) = t.tracks
    # The first person vanishes; another appears 1.2 box heights away 0.1 s later.
    live = t.update([_det(200 + 1.2 * 300, 100)], now=0.4)
    assert tid in t.tracks and t.tracks[tid].misses == 1
    assert len({x.track_id for x in live}) == 2
    # Nor across a long gap: a track unseen for over a second is not centre-matched.
    t2 = _tracker()
    for i in range(4):
        t2.update([_det(200, 100)], now=i * 0.5)
    (tid2,) = t2.tracks
    t2.update([], now=2.0)
    t2.update([], now=2.6)
    live = t2.update([_det(200 + 0.8 * 300, 100)], now=3.2)
    assert t2.tracks[tid2].misses == 3


def test_centre_gate_needs_a_similar_size():
    t = _tracker()
    t.update([_det(100, 100)], now=0.0)
    (tid,) = t.tracks
    # 0.5 s later, 200 px away but half the height (a child next to a shelf, not the same person).
    t.update([_det(300, 250, w=45, h=150)], now=0.5)
    assert t.tracks[tid].misses == 1 and len(t.tracks) == 2


def test_slow_walker_is_still_matched_by_iou_as_before():
    t = _tracker()
    ids = set()
    for i in range(30):
        live = t.update([_det(100 + 3.0 * i, 100)], now=i * 0.1)   # 30 px/s at 10 fps
        ids |= {x.track_id for x in live}
    assert len(ids) == 1
    (tr,) = t.tracks.values()
    assert tr.velocity_px[0] == pytest.approx(30.0, rel=0.3)
