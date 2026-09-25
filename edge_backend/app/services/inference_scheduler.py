"""Global inference budget: how often each camera is analysed, and by which model.

Analysing every Nth frame of every camera does not scale. Measured on an
RX 9060 XT with 33 cameras (32 NVR sub-streams at 25 fps): 33 x (25 / 5) =
165 pose inferences per second x 14 ms (YOLO26m-pose 960x544) is 2.3 s of GPU
work per second, so the GPU sat at 97 % busy, every camera queued behind the
others and detections went stale. The model had also been chosen once, at
start-up, for the cameras that existed then.

This module keeps the accelerator at ``POSE_BUDGET_UTILISATION`` of its
*measured* capacity and shares that fairly:

    cost_ms   device time per analysed frame, measured over the last 10 s
              (pose + refiner + the object model's share: every session.run
              on the accelerator is timed under one device lock), or predicted
              from warm-up timings until enough frames have been measured
    budget    = POSE_BUDGET_UTILISATION x 1000 / cost_ms   frames per second
    rates     max-min fair split of the budget over the cameras that are
              analysing, each within [ANALYTICS_MIN_DETECT_FPS,
              camera fps / ANALYTICS_DETECT_EVERY_N_FRAMES]

A camera worker asks ``admit()`` for every decoded frame. It is admitted when
its own schedule says a frame is due (a per-camera token schedule staggered
across cameras), at least N frames after its last one, and fewer than
``ANALYTICS_MAX_INFLIGHT`` analysed frames are in flight. A frame that is not
admitted is simply not inferred: the live view, the clip buffer and recording
carry on. The floor is a guarantee; when the floors alone exceed the budget
that is reported (``floor_bound``) and the model is re-fitted.

Re-fit. The levels, most accurate first, are the pose ladder on this provider
(``PersonDetector.pose_ladder()``), with the ``auto`` keypoint refiner only on
the top rung, so under load it is shed before the model is. The demand is what
every analysing camera would get at ``ANALYTICS_TARGET_DETECT_FPS``; a level
fits when ``demand x cost / 1000 <= POSE_BUDGET_UTILISATION``. When the current
level does not fit (by its measured cost), the scheduler steps down to the most
accurate cheaper level that does, or to the next one down to measure it; it
steps up only to a level already measured on this device that fits within
``UP_MARGIN`` of the target. A decision is acted on once it has held for
``ANALYTICS_REFIT_DEBOUNCE_SEC`` with an unchanged camera set, on a separate
thread; the detector swaps the model atomically while the old one serves (on
MIGraphX a cold model is compiled in a child process first).

Night watch (services/night_watch.py). A camera whose night watch is armed
and sees no motion is *held*: ``admit()`` refuses its frames and it takes no
share, so its part of the budget goes to the other cameras. When motion is
seen it is *boosted* for a few seconds to confirm a person: admitted at its
ceiling (fps / N) ahead of the fair share, which the other cameras split
what is left of the budget, with one in-flight slot above the cap so a busy
device cannot starve the confirmation.

Everything is reported in ``status()`` (``person_detector.status()
["load_control"]``) and per camera in ``camera_status()``: rates allocated and
measured, target and measured utilisation, the model / refiner in use and why.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

TICK_SEC = 1.0            # rates and measurements are refreshed this often
ACTIVE_SEC = 5.0          # a camera with no frame for this long leaves the share
WINDOW_SEC = 10.0         # utilisation / cost measurement window
MIN_WINDOW_FRAMES = 5     # analysed frames needed before the measured cost is used
UP_MARGIN = 0.8           # a larger level must fit within 80 % of the target
IDLE_FPS = 1.0            # a camera with every analysis feature off only re-reads its flags


def allocate_rates(budget: float, ceilings: dict[str, float], floor: float) -> dict[str, float]:
    """Max-min fair split of ``budget`` frames/s over cameras with rate ``ceilings``.

    Cameras whose ceiling is below the equal share get their ceiling and the
    rest is shared by the others. Every camera then gets at least
    ``min(floor, ceiling)``, which may take the total above the budget.
    """
    rates: dict[str, float] = {}
    remaining = max(0.0, float(budget))
    todo = sorted(ceilings.items(), key=lambda kv: kv[1])
    for i, (cam, ceiling) in enumerate(todo):
        share = remaining / (len(todo) - i)
        rates[cam] = min(ceiling, share)
        remaining -= rates[cam]
    for cam, ceiling in ceilings.items():
        rates[cam] = max(rates[cam], min(floor, ceiling))
    return rates


@dataclass(frozen=True)
class Level:
    """One model-ladder rung, with or without the keypoint refiner."""

    model: Path
    refiner: bool

    @property
    def label(self) -> str:
        return self.model.name + (" + refiner" if self.refiner else "")


def build_levels(ladder: list[Path], refiner_available: bool, refiner_mode: str) -> list[Level]:
    """Levels most accurate first. ``auto``: the refiner only on the top rung."""
    if not ladder:
        return []
    if refiner_available and refiner_mode == "auto":
        return [Level(ladder[0], True)] + [Level(p, False) for p in ladder]
    on = refiner_available and refiner_mode == "on"
    return [Level(p, on) for p in ladder]


def choose_level(costs: list[Optional[float]], current: int, demand: float, target: float,
                 up_margin: float = UP_MARGIN) -> tuple[int, str]:
    """The level to run for ``demand`` frames/s at ``target`` utilisation, and why.

    ``costs`` are device ms per analysed frame per level (None: not measured
    on this device yet). Never steps up to an unmeasured level.
    """
    def util(j: int) -> float:
        return demand * float(costs[j]) / 1000.0

    if costs[current] is not None and util(current) > target:
        for j in range(current + 1, len(costs)):
            if costs[j] is None:
                return j, "not measured on this device yet; loading it to measure"
            if util(j) <= target:
                return j, f"predicted {util(j):.0%} of the device"
        known = [j for j in range(current, len(costs)) if costs[j] is not None]
        best = min(known, key=lambda j: costs[j])
        return best, (f"nothing on the ladder fits; the cheapest measured needs {util(best):.0%}"
                      if best != current else f"already the cheapest level ({util(best):.0%} predicted)")
    for j in range(current):
        if costs[j] is not None and util(j) <= target * up_margin:
            return j, f"headroom: predicted {util(j):.0%} of the device"
    if costs[current] is None:
        return current, "current level not measured yet"
    return current, f"fits: predicted {util(current):.0%} of the device"


@dataclass
class _Camera:
    first_seen: float
    last_seen: float
    fps: float = 0.0
    # Frames between analyses for this camera; 0 = ANALYTICS_DETECT_EVERY_N_FRAMES.
    # Set when decoding already drops frames (GPU path, DECODE_MAX_FPS).
    every_n: int = 0
    idle: bool = False
    # Night watch: held = no inference at all; boosted until = confirmation burst.
    held: bool = False
    boost_until: float = 0.0
    rate: float = 0.0
    next_due: float = 0.0
    last_frame: int = -(10 ** 9)
    inflight: int = 0
    skipped_busy: int = 0
    admits: deque = field(default_factory=lambda: deque(maxlen=512))


class InferenceScheduler:
    """Shares the accelerator between cameras and re-fits the model to the load."""

    def __init__(self, detector=None, clock=time.monotonic, rng=random.random, run_async: bool = True):
        self._detector = detector
        self._clock = clock
        self._rng = rng
        self._async = run_async
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._cams: dict[str, _Camera] = {}
        self._inflight = 0
        self._skipped_busy = 0
        self._last_tick: Optional[float] = None
        # Measurement: (t, busy_ms, frames) samples for one (provider, model, refiner).
        self._samples: deque = deque()
        self._sample_key: Optional[tuple] = None
        self._metrics: Optional[dict] = None
        self._cost_ms: Optional[float] = None
        self._cost_source = "none"
        self._util: Optional[float] = None
        self._budget: Optional[float] = None
        self._floor_bound = False
        # Pose ms per rung measured on a provider; rungs that failed to load.
        self._rung_ms: dict[tuple[str, str], float] = {}
        self._bad: dict[tuple[str, str], str] = {}
        # Re-fit state.
        self._analysing: frozenset = frozenset()
        self._set_changed_at = clock()
        self._pending: Optional[dict] = None
        self._swap: dict = {"state": "idle"}
        self._last_refit_at: Optional[float] = None
        self._refits: deque = deque(maxlen=10)
        self._ladder_view: list[dict] = []
        self._level_note: Optional[str] = None

    @property
    def detector(self):
        if self._detector is None:
            from app.services.inference_backend import person_detector

            self._detector = person_detector
        return self._detector

    # ------------------------------------------------------------ workers

    @staticmethod
    def _every_n() -> int:
        return max(1, int(settings.ANALYTICS_DETECT_EVERY_N_FRAMES))

    def _ceiling(self, cam: _Camera) -> float:
        fps = cam.fps if cam.fps > 0 else float(settings.RECORDING_FPS)
        return max(fps, 0.1) / (cam.every_n or self._every_n())

    def admit(self, camera_id: str, fps: float = 0.0, frame_index: Optional[int] = None,
              every_n: Optional[int] = None) -> bool:
        """Should this camera analyse the frame it just decoded? Call ``done()`` after.

        ``every_n`` overrides ANALYTICS_DETECT_EVERY_N_FRAMES for a camera whose
        decoder already thins frames, so the ceiling stays native fps / N
        (25 / 5 = 5 fps) rather than delivered fps / N (10 / 5 = 2 fps).
        """
        now = self._clock()
        self._maybe_tick(now)
        every_n = max(1, int(every_n)) if every_n else self._every_n()
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                c = self._cams[camera_id] = _Camera(first_seen=now, last_seen=now)
                if fps > 0:
                    c.fps = fps
                c.every_n = every_n
                self._recompute_locked(now)
                # Stagger the cameras so their turns do not all fall on one frame.
                c.next_due = now + self._rng() / max(c.rate, 1e-3)
            c.last_seen = now
            if fps > 0:
                c.fps = fps
            c.every_n = every_n
            boosted = c.boost_until > now
            if c.held and not boosted:
                return False
            if not settings.ANALYTICS_SCHEDULER:
                ok = frame_index is None or frame_index % every_n == 0
                if ok:
                    c.admits.append(now)
                return ok
            if frame_index is not None and frame_index < c.last_frame:
                c.last_frame = -(10 ** 9)          # a new worker for this camera counts from 0
            if frame_index is not None and frame_index - c.last_frame < every_n:
                return False
            if now < c.next_due:
                return False
            if self._inflight >= max(1, int(settings.ANALYTICS_MAX_INFLIGHT)) + (1 if boosted else 0):
                c.skipped_busy += 1
                self._skipped_busy += 1
                return False
            interval = 1.0 / max(c.rate, 1e-3)
            # At most one interval of catch-up after a stall, so no burst.
            c.next_due = max(c.next_due, now - interval) + interval
            if frame_index is not None:
                c.last_frame = frame_index
            c.inflight += 1
            self._inflight += 1
            c.admits.append(now)
            return True

    def done(self, camera_id: str) -> None:
        """An admitted frame finished (or failed) analysis."""
        with self._lock:
            c = self._cams.get(camera_id)
            if c is not None and c.inflight > 0:
                c.inflight -= 1
                self._inflight = max(0, self._inflight - 1)

    def set_idle(self, camera_id: str, idle: bool) -> None:
        """Every analysis feature is off for this camera: it takes no share."""
        with self._lock:
            c = self._cams.get(camera_id)
            if c is not None and c.idle != bool(idle):
                c.idle = bool(idle)
                self._recompute_locked(self._clock())

    def hold(self, camera_id: str, held: bool) -> None:
        """Night watch armed, no motion: admit nothing for this camera and give its share away."""
        now = self._clock()
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                if not held:
                    return
                c = self._cams[camera_id] = _Camera(first_seen=now, last_seen=now)
            if c.held != bool(held):
                c.held = bool(held)
                if not held:
                    c.boost_until = 0.0
                self._recompute_locked(now)

    def boost(self, camera_id: str, seconds: float) -> None:
        """Night watch saw motion: admit this camera at its ceiling for ``seconds`` (0 ends it)."""
        now = self._clock()
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                c = self._cams[camera_id] = _Camera(first_seen=now, last_seen=now)
            starting = c.boost_until <= now
            c.boost_until = now + max(0.0, float(seconds)) if seconds > 0 else 0.0
            if starting and c.boost_until > now:
                c.next_due = now              # the first frame of the burst is due at once
            self._recompute_locked(now)

    def forget(self, camera_id: str) -> None:
        """The camera stopped or lost its stream: give its share to the others."""
        with self._lock:
            c = self._cams.pop(camera_id, None)
            if c is not None:
                self._inflight = max(0, self._inflight - c.inflight)
                self._recompute_locked(self._clock())

    # -------------------------------------------------------- measurement

    def _maybe_tick(self, now: float) -> None:
        if self._last_tick is not None and now - self._last_tick < TICK_SEC:
            return
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if self._last_tick is None or now - self._last_tick >= TICK_SEC:
                self._tick(now)
        finally:
            self._tick_lock.release()

    def tick(self, now: Optional[float] = None) -> None:
        """Measure, re-allocate rates, and decide on a re-fit (normally driven by admit())."""
        with self._tick_lock:
            self._tick(self._clock() if now is None else now)

    def _tick(self, now: float) -> None:
        self._last_tick = now
        det = self.detector
        metrics = det.load_metrics() if getattr(det, "available", False) else None
        self._measure(now, metrics)
        with self._lock:
            self._recompute_locked(now)
        if settings.ANALYTICS_SCHEDULER and metrics is not None:
            try:
                self._consider_refit(now, metrics)
            except Exception as e:  # noqa: BLE001 - never take a capture thread down
                logger.error(f"Inference re-fit check failed: {type(e).__name__}: {e}")

    def _extras_ms(self, m: dict) -> tuple[float, float]:
        """(object model ms per analysed frame, refiner ms per analysed frame)."""
        every = int(settings.OBJECT_DETECT_EVERY_N)
        obj = (float(m["object_ms"]) / every) if (m.get("objects_on_device") and m.get("object_ms")
                                                   and every > 0) else 0.0
        ref = m.get("refiner_frame_ms")
        if ref is None:
            ref = m.get("refiner_batch_ms") or 0.0   # one batch per frame until measured: conservative
        return obj, float(ref)

    def _pose_ms(self, model: Path, m: dict) -> Optional[float]:
        key = (m["provider"], model.name)
        if key in self._bad:
            return None
        if model.name == m.get("model") and m.get("pose_ms") is not None:
            return float(m["pose_ms"])
        if key in self._rung_ms:
            return self._rung_ms[key]
        for t in (getattr(self.detector, "selection", None) or {}).get("tried", []):
            if t.get("model") == model.name and t.get("steady_ms") is not None:
                return float(t["steady_ms"])
        return None

    def _level_cost(self, level: Level, m: dict) -> Optional[float]:
        pose = self._pose_ms(level.model, m)
        if pose is None:
            return None
        obj, ref = self._extras_ms(m)
        return round(pose + obj + (ref if level.refiner else 0.0), 2)

    def _measure(self, now: float, m: Optional[dict]) -> None:
        self._metrics = m
        if m is None:
            self._samples.clear()
            self._sample_key = None
            self._cost_ms, self._cost_source, self._util = None, "none", None
            return
        if m.get("pose_ms_measured") and m.get("model"):
            self._rung_ms[(m["provider"], m["model"])] = float(m["pose_ms"])
        key = (m["provider"], m.get("model"), bool(m.get("refiner_enabled")))
        if key != self._sample_key:
            # A different model / provider / refiner state costs something else.
            self._samples.clear()
            self._sample_key = key
        self._samples.append((now, float(m["busy_ms"]), int(m["frames"])))
        while len(self._samples) > 2 and now - self._samples[1][0] >= WINDOW_SEC:
            self._samples.popleft()
        t0, b0, f0 = self._samples[0]
        dt = now - t0
        busy, frames = float(m["busy_ms"]) - b0, int(m["frames"]) - f0
        self._util = busy / (dt * 1000.0) if dt >= 2.0 else None
        if frames >= MIN_WINDOW_FRAMES and busy > 0:
            self._cost_ms, self._cost_source = busy / frames, f"measured over {dt:.0f} s"
        else:
            current = Level(Path(m.get("model") or "none"), bool(m.get("refiner_enabled")))
            self._cost_ms = self._level_cost(current, m)
            self._cost_source = "predicted from warm-up timings" if self._cost_ms else "none"

    def _recompute_locked(self, now: float) -> None:
        live = {k: c for k, c in self._cams.items() if now - c.last_seen <= ACTIVE_SEC}
        for c in self._cams.values():
            if c.held and c.boost_until <= now:
                c.rate = 0.0
        analysing = {k: c for k, c in live.items() if not c.idle and (not c.held or c.boost_until > now)}
        boosted = {k: c for k, c in analysing.items() if c.boost_until > now}
        floor = max(0.0, float(settings.ANALYTICS_MIN_DETECT_FPS))
        for c in live.values():
            if c.idle and not c.held:
                c.rate = min(IDLE_FPS, self._ceiling(c))
        if not settings.ANALYTICS_SCHEDULER or not self._cost_ms:
            # Legacy rule, or nothing to measure against (no detector): the ceiling.
            for c in analysing.values():
                c.rate = self._ceiling(c)
            self._budget, self._floor_bound = None, False
            return
        self._budget = float(settings.POSE_BUDGET_UTILISATION) * 1000.0 / self._cost_ms
        # A confirmation burst runs at its ceiling; the others share the rest.
        for c in boosted.values():
            c.rate = self._ceiling(c)
        rest = max(0.0, self._budget - sum(c.rate for c in boosted.values()))
        others = {k: c for k, c in analysing.items() if k not in boosted}
        rates = allocate_rates(rest, {k: self._ceiling(c) for k, c in others.items()}, floor)
        for k, r in rates.items():
            others[k].rate = r
        self._floor_bound = (sum(rates.values()) + sum(c.rate for c in boosted.values())
                             > self._budget * 1.001)

    # ------------------------------------------------------------- re-fit

    def _consider_refit(self, now: float, m: dict) -> None:
        if self._swap.get("state") != "idle":
            return
        det = self.detector
        ladder = [p for p in det.pose_ladder() if (m["provider"], p.name) not in self._bad]
        levels = build_levels(ladder, bool(m.get("refiner_available")), det._refiner_mode())
        current = Level(Path(det.model_path), bool(m.get("refiner_enabled"))) if det.model_path else None
        if current not in levels:
            self._ladder_view, self._pending = [], None
            self._level_note = ("no re-fit: the running model is not on this provider's ladder"
                                if levels else "no re-fit: no ladder")
            return
        self._level_note = None
        cur = levels.index(current)
        costs = [self._level_cost(lv, m) for lv in levels]
        if self._cost_ms and self._cost_source.startswith("measured"):
            costs[cur] = round(self._cost_ms, 2)      # the truth for the running level

        with self._lock:
            live = {k: c for k, c in self._cams.items() if now - c.last_seen <= ACTIVE_SEC and not c.idle
                    and not (c.held and c.boost_until <= now)}
            want = max(float(settings.ANALYTICS_TARGET_DETECT_FPS), float(settings.ANALYTICS_MIN_DETECT_FPS))
            demand = sum(min(want, self._ceiling(c)) for c in live.values())
        names = frozenset(live)
        if names != self._analysing:
            self._analysing, self._set_changed_at = names, now
        target = float(settings.POSE_BUDGET_UTILISATION)
        self._ladder_view = [
            {"model": lv.model.name, "refiner": lv.refiner, "ms_per_frame": costs[j],
             "predicted_utilisation": round(demand * costs[j] / 1000.0, 3) if costs[j] is not None else None,
             "current": j == cur}
            for j, lv in enumerate(levels)
        ]
        if not live:
            self._pending = None
            return
        idx, why = choose_level(costs, cur, demand, target)
        if idx == cur:
            self._pending = None
            return
        if self._pending is None or self._pending["to"] != levels[idx]:
            self._pending = {"to": levels[idx], "since": now, "why": why}
        self._pending["why"] = why
        since = max(self._pending["since"], self._set_changed_at, self._last_refit_at or float("-inf"))
        if now - since < float(settings.ANALYTICS_REFIT_DEBOUNCE_SEC):
            return
        reason = (f"{len(live)} camera(s) analysing want {demand:.1f} frames/s "
                  f"(target {want:g}/s each) at {target:.0%} of the device; {levels[idx].label}: {why}")
        self._swap = {"state": "switching", "from": current.label, "to": levels[idx].label,
                      "since": time.time(), "reason": reason}
        args = (current, levels[idx], reason, m["provider"])
        if self._async:
            threading.Thread(target=self._refit, args=args, name="pose-refit", daemon=True).start()
        else:
            self._refit(*args)

    def _refit(self, frm: Level, to: Level, reason: str, provider: str) -> None:
        det = self.detector
        started = time.perf_counter()
        ok, error = True, None
        try:
            # The refiner is shed before the model changes and restored after.
            if frm.refiner and not to.refiner:
                det.set_refiner(False, f"auto: off under load ({reason})")
            if to.model != frm.model:
                def progress(state: str) -> None:
                    self._swap["state"] = state

                ok, info = det.switch_pose_model(to.model, f"re-fit: {reason}", progress=progress)
                if ok:
                    if info.get("steady_ms") is not None:
                        self._rung_ms[(provider, to.model.name)] = float(info["steady_ms"])
                else:
                    error = info.get("error") or "unknown error"
                    self._bad[(provider, to.model.name)] = error
            if ok and to.refiner and not frm.refiner:
                det.set_refiner(True, f"auto: on, the device has headroom ({reason})")
        except Exception as e:  # noqa: BLE001 - keep serving with what runs
            ok, error = False, f"{type(e).__name__}: {e}"
        seconds = round(time.perf_counter() - started, 1)
        entry = {"at": time.time(), "from": frm.label, "to": to.label, "reason": reason, "ok": ok,
                 "error": error, "seconds": seconds}
        if ok:
            logger.info(f"Inference re-fit: {frm.label} -> {to.label} in {seconds} s ({reason})")
        else:
            logger.error(f"Inference re-fit {frm.label} -> {to.label} failed after {seconds} s, "
                         f"keeping {det.model_path.name if det.model_path else 'the current model'}: {error}")
        with self._lock:
            self._refits.append(entry)
            self._swap = {"state": "idle"}
            self._pending = None
            self._last_refit_at = self._clock()
            self._samples.clear()
            self._sample_key = None

    # ------------------------------------------------------------- status

    def _measured_fps(self, c: _Camera, now: float) -> float:
        span = min(WINDOW_SEC, max(now - c.first_seen, 1e-6))
        return sum(1 for t in c.admits if now - t <= WINDOW_SEC) / span if span >= 1.0 else 0.0

    def camera_status(self, camera_id: str) -> Optional[dict]:
        now = self._clock()
        with self._lock:
            c = self._cams.get(camera_id)
            if c is None:
                return None
            return {
                "mode": "budget" if settings.ANALYTICS_SCHEDULER else "every_nth_frame",
                "detect_fps_allocated": round(c.rate, 2),
                "detect_fps_measured": round(self._measured_fps(c, now), 2),
                "ceiling_fps": round(self._ceiling(c), 2),
                "floor_fps": float(settings.ANALYTICS_MIN_DETECT_FPS),
                "idle": c.idle,
                # Night watch: no inference while held; boosted = confirming motion.
                "night_hold": c.held and c.boost_until <= now,
                "boosted": c.boost_until > now,
                "skipped_busy": c.skipped_busy,
            }

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            live = [c for c in self._cams.values() if now - c.last_seen <= ACTIVE_SEC]
            held = [c for c in live if c.held and c.boost_until <= now]
            analysing = [c for c in live if not c.idle and not any(c is h for h in held)]
            rates = [c.rate for c in analysing]
            measured = sum(self._measured_fps(c, now) for c in analysing)
            pending = dict(self._pending) if self._pending else None
            swap, refits = dict(self._swap), list(self._refits)
            inflight, skipped = self._inflight, self._skipped_busy
        if pending:
            to = pending.pop("to")
            pending.update(to=to.label, waiting_s=round(now - max(
                pending["since"], self._set_changed_at, self._last_refit_at or float("-inf")), 1))
            pending.pop("since", None)
        m = self._metrics or {}
        cost = self._cost_ms
        return {
            "mode": "budget" if settings.ANALYTICS_SCHEDULER else "every_nth_frame (ANALYTICS_SCHEDULER off)",
            "target_utilisation": float(settings.POSE_BUDGET_UTILISATION),
            # Share of wall time the device spent in our session.run calls.
            "measured_utilisation": round(self._util, 3) if self._util is not None else None,
            "device_ms_per_frame": round(cost, 2) if cost else None,
            "device_ms_source": self._cost_source,
            "pose_ms_per_inference": m.get("pose_ms"),
            "pose_ms_source": "measured (EWMA of real frames)" if m.get("pose_ms_measured") else "warm-up",
            "capacity_frames_per_s": round(1000.0 / cost, 1) if cost else None,
            "budget_frames_per_s": round(self._budget, 1) if self._budget is not None else None,
            "cameras_analysing": len(analysing),
            "cameras_idle": len(live) - len(analysing) - len(held),
            "cameras_night_watch_held": len(held),
            "allocated_frames_per_s": round(sum(rates), 1),
            "measured_frames_per_s": round(measured, 1),
            "per_camera_fps": ({"min": round(min(rates), 2), "max": round(max(rates), 2),
                                "mean": round(sum(rates) / len(rates), 2)} if rates else None),
            "floor_fps": float(settings.ANALYTICS_MIN_DETECT_FPS),
            "target_fps": float(settings.ANALYTICS_TARGET_DETECT_FPS),
            "ceiling": f"camera fps / {self._every_n()}",
            "floor_bound": self._floor_bound,
            "max_inflight": max(1, int(settings.ANALYTICS_MAX_INFLIGHT)),
            "inflight": inflight,
            "skipped_busy": skipped,
            "level": {"model": m.get("model"), "refiner": bool(m.get("refiner_enabled"))} if m else None,
            "ladder": list(self._ladder_view),
            "note": self._level_note,
            "unusable_models": {f"{p}:{n}": e for (p, n), e in self._bad.items()},
            "pending_refit": pending,
            "swap": swap,
            "refits": refits,
        }


inference_scheduler = InferenceScheduler()
