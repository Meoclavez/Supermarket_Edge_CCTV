"""Motion gate on real store frames at night (services/motion_gate.py).

Regression for the first deploy of the motion gate (27f2347): with the store
closed and nobody in view, 17 of the 32 store cameras stayed "active"
(last motion 0-3 s ago), so the GPU budget was spread over empty cameras.
The causes, found by replaying the cameras' own frames through the gate:

* the burnt-in clock: its seconds digits change every second and a changed
  pixel is absorbed slowly (~4 s at 10 frames/s), so 5-12 thumbnail pixels
  (IPC overlay) or 2-5 (recorder overlay) were changed all the time;
* a lit sign cycling its pictures, and glass doors / a shop window lit by
  the street (on one camera half the picture dims by 10-40 %, smoothly,
  every ~35 s).

Fixture ``fixtures/motion_gate_store_night.npz``: 64 x 48 grey thumbnails
(``motion_gate._luma_small`` of the dashboard snapshots, ~0.8 frames/s,
store closed, nobody in view) of four store cameras, as a first frame plus
per-frame differences: ``<cam>_t`` seconds, ``<cam>_y0``, ``<cam>_dy``.
ch15: glass doors and street light; ch22: IPC clock overlay, a lit sign and
a TV; ch23: IPC clock and a shop window; ch24: IPC clock only. The tests
replay them at the box's decode rate (10 frames/s, the latest snapshot
repeated, thumbnail noise sigma 1 as measured on the real frames).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.services import motion_gate
from app.services.motion_gate import MotionGate

FIXTURE = Path(__file__).parent / "fixtures" / "motion_gate_store_night.npz"
CAMERAS = ("ch15", "ch22", "ch23", "ch24")
FPS = 10.0
HOLD = 4.0          # ANALYTICS_ACTIVE_HOLD_SEC
WARM = 60.0         # the flicker map learns the view first


@pytest.fixture(scope="module")
def store():
    data = np.load(FIXTURE)
    out = {}
    for cam in CAMERAS:
        frames = np.concatenate([data[f"{cam}_y0"][None].astype(np.int16), data[f"{cam}_dy"]]).cumsum(axis=0)
        out[cam] = (data[f"{cam}_t"].astype(np.float64), frames.astype(np.uint8))
    return out


def at_decode_rate(t, frames, seed=0, walker=None):
    """(time, thumbnail) at FPS: the latest real frame, sensor noise, an optional walker."""
    rng = np.random.default_rng(seed)
    k = 0
    for i in range(int(t[-1] * FPS) + 1):
        now = i / FPS
        while k + 1 < len(t) and t[k + 1] <= now:
            k += 1
        f = frames[k].astype(np.float32)
        if walker is not None:
            f = walker(f, now)
        yield now, np.clip(f + rng.normal(0.0, 1.0, f.shape), 0, 255).astype(np.uint8)


def replay(gate, t, frames, **kw):
    return [now for now, f in at_decode_rate(t, frames, **kw) if gate.update(f, now)]


def wakes_and_active(events, start, end):
    """Quiet-to-active transitions (ANALYTICS_ACTIVE_HOLD_SEC) and the active share of [start, end]."""
    wakes, until, active = 0, -1e9, 0.0
    for t in events:
        if t < start:
            until = t + HOLD
            continue
        if t >= until:
            wakes += 1
            active += min(t + HOLD, end) - t
        else:
            active += min(t + HOLD, end) - min(until, end)
        until = t + HOLD
    return wakes, active / (end - start)


@pytest.mark.parametrize("cam", CAMERAS)
def test_empty_store_camera_at_night_stays_quiet(store, cam):
    t, frames = store[cam]
    g = MotionGate(threshold=14, min_pixels=4, persist=2)
    events = replay(g, t, frames)
    wakes, active = wakes_and_active(events, WARM, t[-1])
    minutes = (t[-1] - WARM) / 60.0
    # Measured on this fixture: 0-0.5 wakes/min and 0-3 % active; with the
    # flicker map and the local brightness off (the first deploy's rules)
    # 1.5-2.5 wakes/min and 83-98 % active.
    assert wakes / minutes <= 1.0, (cam, wakes, minutes)
    assert active <= 0.10, (cam, active)


def test_the_fixture_reproduces_the_live_regression(store, monkeypatch):
    """Without the flicker map and the local brightness the same frames keep the cameras active."""
    monkeypatch.setattr(motion_gate, "FLICKER_DUTY", 2.0)
    monkeypatch.setattr(motion_gate, "FLICKER_ONSETS", 1e9)
    monkeypatch.setattr(motion_gate, "LOCAL_GAIN_WINDOW", 1)
    for cam in CAMERAS:
        t, frames = store[cam]
        events = replay(MotionGate(threshold=14, min_pixels=4, persist=2), t, frames)
        _, active = wakes_and_active(events, WARM, t[-1])
        assert active > 0.5, (cam, active)


@pytest.mark.parametrize("cam", ("ch22", "ch23", "ch24"))
def test_the_burnt_in_clock_is_learnt_as_flicker(store, cam):
    t, frames = store[cam]
    g = MotionGate(threshold=14, min_pixels=4, persist=2)
    replay(g, t, frames)
    mask = g.flicker_mask()
    # The IPC clock's minutes and seconds: thumbnail rows 2-3, columns 51-56.
    assert mask[2:4, 51:57].all()
    # Only small parts of the picture: the floor where people walk is not masked.
    assert mask.mean() < 0.08


def _walker(start: float, row: int, luma: float, height: int = 10, speed: float = 7.5,
            from_right: bool = False):
    """A person-shaped patch (head, arms and body, legs) crossing the thumbnail from ``start``.

    10 rows ~ a 120 px tall person on a D1 frame; 7.5 thumbnail px/s ~ 1.2 m/s.
    ``luma``: the clothes' grey level (the head is 120).
    """
    shape = np.zeros((height, 5), np.float32)
    shape[: height // 5, 1:4] = 120.0                      # head
    shape[height // 5: height * 3 // 5, :] = luma          # shoulders, arms, body
    shape[height * 3 // 5:, 1:4] = luma                    # legs

    def apply(f, now):
        if now < start:
            return f
        x = int(round((now - start) * speed)) - shape.shape[1]
        if from_right:
            x = f.shape[1] - x - shape.shape[1]
        for dy in range(height):
            for dx in range(shape.shape[1]):
                xx, yy = x + dx, row - height + dy
                if shape[dy, dx] and 0 <= xx < f.shape[1] and 0 <= yy < f.shape[0]:
                    f[yy, xx] = shape[dy, dx]
        return f

    return apply


@pytest.mark.parametrize("cam", CAMERAS)
@pytest.mark.parametrize("row, luma, from_right", [(40, 35.0, False), (30, 200.0, True), (46, 90.0, False)])
def test_a_person_walking_in_wakes_the_camera_at_once(store, cam, row, luma, from_right):
    """After the view is learnt, a walker wakes the camera within 0.5 s of stepping in."""
    t, frames = store[cam]
    start = float(t[-1]) - 20.0                            # 68-160 s learnt
    entered = start + 5 / 7.5                              # fully inside the picture
    g = MotionGate(threshold=14, min_pixels=4, persist=2)
    walker = _walker(start, row, luma, from_right=from_right)
    events = [now for now, f in at_decode_rate(t, frames, walker=walker, seed=3)
              if g.update(f, now) and now >= start]
    assert events, (cam, row, luma)
    assert events[0] - entered <= 0.5, (cam, row, luma, events[0] - entered)


def test_a_local_light_change_is_not_motion():
    """Half the picture dimmed smoothly by 10-40 % (light through a door): no motion."""
    rng = np.random.default_rng(5)
    base = rng.integers(60, 200, (48, 64)).astype(np.float32)
    g = MotionGate(threshold=14, min_pixels=4, persist=2)
    for i in range(50):
        assert not g.update(np.clip(base + rng.normal(0, 1, base.shape), 0, 255).astype(np.uint8), i / FPS)
    gain = np.ones_like(base)
    gain[:, :32] = np.linspace(0.6, 0.9, 32)[None, :]      # left half, a smooth gradient
    hits = [g.update(np.clip(base * gain + rng.normal(0, 1, base.shape), 0, 255).astype(np.uint8), 5.0 + i / FPS)
            for i in range(30)]
    assert not any(hits)
    assert g.lighting_events == 0                          # under LIGHTING_FRACTION: handled locally
