"""Static-figure filter on the store's real cut-out (tracking_service).

Regression for 27f2347: the life-size cardboard cut-out on "Dahua NVR Ch 12"
(feet hidden behind a drinks cooler) was held static at ~1.7 analysed
frames/s after 9ffae53, but flipped moving/static again once the scheduler
gave the camera ~3 frames/s. The pose model's box bottom on it is unsure:
y2 scattered by 0.12 torso (90th percentile) and in ~2 % of frames jumped
0.9-1.4 torso down when the model "found" legs in the stand (knees and an
ankle at confidence 0.7-0.97 for those frames only), sometimes on two
analysed frames in a row. The box was compared on its centre and height,
so those frames restarted the 60 s stillness clock; more frames per second
meant more chances. Now the box is compared on its top-centre point and
width (``box_reference``), and a joint's motion counts only when it was
visible on its last 3 observations and on at least half of its recent ones
(``MOTION_JOINT_MIN_SEEN``).

Fixture ``fixtures/static_cutout_ch12.npz`` (store closed, nobody else in
view): ``live_box`` / ``live_kp`` / ``live_t``: 386 consecutive analysed
frames of the cut-out's track from the box's /api/v1/live/tracks (measured
box, tracker keypoints, pixels of the 704 x 576 frame, every 1-2 analysed
frames at 2.4-3.4 frames/s); ``rtmo_box`` / ``rtmo_kp`` / ``rtmo_conf``:
the same model (RTMO-s 640) run locally on 138 dashboard snapshots of it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.services import tracking_service
from app.services.inference_backend import Detection
from app.services.tracking_service import ByteTracker

FIXTURE = Path(__file__).parent / "fixtures" / "static_cutout_ch12.npz"
HEAD = (0, 1, 2, 3, 4)


@pytest.fixture(scope="module")
def cutout():
    data = np.load(FIXTURE)
    live = [(b, k, 0.7) for b, k in zip(data["live_box"], data["live_kp"])]
    rtmo = [(b, k, c) for b, k, c in zip(data["rtmo_box"], data["rtmo_kp"], data["rtmo_conf"])]
    return {"live": live, "rtmo": rtmo}


def detection(box, kp, conf, dx: float = 0.0, moves: dict | None = None) -> Detection:
    k = np.array(kp, dtype=np.float32)
    k[:, 0] += dx
    for j, (mx, my) in (moves or {}).items():
        k[j, 0] += mx
        k[j, 1] += my
    return Detection(x1=float(box[0]) + dx, y1=float(box[1]), x2=float(box[2]) + dx, y2=float(box[3]),
                     confidence=float(conf), keypoints=k)


def replay(seq, fps: float, seconds: float, edit=None):
    """Feed the real sequence in its own order (repeated) at ``fps``; motion state per frame."""
    tr = ByteTracker("cam_cutout")
    tr.set_frame_size(704, 576)
    states = []
    for i in range(int(seconds * fps)):
        now = 10_000.0 + i / fps
        box, kp, conf = seq[i % len(seq)]
        d = edit(now - 10_000.0, box, kp, conf) if edit else detection(box, kp, conf)
        tr.update([d], now=now)
        (t,) = [t for t in tr.tracks.values() if t.confirmed] or [None]
        states.append(None if t is None else t.motion_state)
    return states


def first_static_and_flips(states, fps):
    first = next((i for i, s in enumerate(states) if s == "static"), None)
    if first is None:
        return None, 0
    after = states[first:]
    return first / fps, sum(1 for a, b in zip(after, after[1:]) if a == "static" and b != "static")


@pytest.mark.parametrize("source", ("live", "rtmo"))
@pytest.mark.parametrize("fps", (1.7, 3.0, 5.0, 10.0))
def test_cutout_goes_static_and_stays_static_at_any_analysed_rate(cutout, source, fps):
    states = replay(cutout[source], fps, 300.0)
    first, flips = first_static_and_flips(states, fps)
    assert first is not None and first <= 61.0, (source, fps, first)
    assert flips == 0, (source, fps, flips)


def test_measuring_the_box_bottom_flips_the_cutout(cutout, monkeypatch):
    """The fixture reproduces the live flipping with the old box measure (centre and height)."""
    def centre_and_height(bbox):
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0), max(float(y2 - y1), 1.0)

    monkeypatch.setattr(tracking_service, "box_reference", centre_and_height)
    states = replay(cutout["live"], 3.0, 300.0)
    first, flips = first_static_and_flips(states, 3.0)
    assert first is not None and flips >= 2
    assert sum(s == "static" for s in states[int(61 * 3):]) / len(states[int(61 * 3):]) < 0.7


@pytest.mark.parametrize("fps", (1.7, 3.0, 10.0))
def test_a_person_with_the_cutouts_jitter_and_head_turns_stays_moving(cutout, fps):
    """Same real detector jitter, plus a 15 px (0.2 torso) head turn held 1 s every 20 s."""
    def edit(el, box, kp, conf):
        turning = el % 20.0 < 1.0 and el > 1.0
        return detection(box, kp, conf, moves={j: (15.0, 0.0) for j in HEAD} if turning else None)

    states = replay(cutout["live"], fps, 180.0, edit)
    assert "static" not in states
    assert states[-1] == "moving"


@pytest.mark.parametrize("fps", (1.7, 3.0, 10.0))
def test_a_person_with_the_cutouts_jitter_shifting_weight_stays_moving(cutout, fps):
    """Same real jitter; the whole body sways 12 px (0.16 torso) for 2 s every 25 s."""
    def edit(el, box, kp, conf):
        return detection(box, kp, conf, dx=12.0 if el % 25.0 < 2.0 and el > 1.0 else 0.0)

    states = replay(cutout["live"], fps, 180.0, edit)
    assert "static" not in states
    assert states[-1] == "moving"
