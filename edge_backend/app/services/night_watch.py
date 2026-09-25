"""Night watch: motion-gated person alerts on selected cameras at night.

Per camera, opt-in (``CameraFeatureConfig.night_watch``, off by default):
a schedule in store-local time (``SITE_TIMEZONE``, else the host's zone),
e.g. 22:00-06:00 on chosen days, optionally also whenever the camera's
measured lighting is ``low_light`` or ``ir`` (``low_light.lighting_monitor``).

While a camera is armed:

* The pose model does not run for it. ``inference_scheduler.hold`` refuses
  its frames and gives its share of the accelerator to the other cameras.
  People counting, shelf reaches, theft cues and restricted-area checks pause
  on that camera; night watch replaces them.
* A cheap motion check runs instead, ``NIGHT_WATCH_MOTION_FPS`` times a
  second, on a small grayscale copy (``NIGHT_WATCH_MOTION_WIDTH`` px wide):
  a running-average background, the difference after removing the global
  brightness shift (median), a threshold, a 3x3 opening against sensor noise
  and IR flicker, and the largest connected blob. A sample where most of the
  picture changes at once (IR switching, lights, a headlight sweep) is a
  *lighting event*: the background is re-learnt and nothing is reported.
  Motion needs a blob of at least the sensitivity's area on N consecutive
  samples. AI_IGNORE regions and privacy masks are excluded.
* Motion starts a confirmation burst: ``inference_scheduler.boost`` admits the
  camera at its ceiling for ``NIGHT_WATCH_CONFIRM_SEC``. A person detected
  (per-box thresholds, AI_IGNORE applied) near the moving region on
  ``NIGHT_WATCH_CONFIRM_HITS`` frames is an alert: ``NIGHT_INTRUSION``, HIGH,
  logged, on the dashboard and pushed to phones through ``alert_dispatcher``,
  with a privacy-masked evidence still (and a short clip from the pre-event
  buffer when ``NIGHT_WATCH_CLIP`` is on). A burst that confirms nobody is
  recorded as ``NIGHT_MOTION`` (INFO, dashboard list only: never pushed or
  broadcast on the alert websocket, which the phone app turns into notifications).
* Per camera, a cooldown after each event and at most
  ``NIGHT_WATCH_MAX_EVENTS_PER_HOUR`` events an hour; anything held back is
  counted (``suppressed``), never silently dropped.

Evidence (stills, optional clips) is the only thing written, under
``NIGHT_WATCH_EVIDENCE_DIR`` on this device, oldest first deleted above
``NIGHT_WATCH_EVIDENCE_MAX_MB``. No continuous recording.

Times are store-local and DST-correct: windows are built from wall-clock
times per date with ``zoneinfo`` (never a fixed offset), so 22:00-06:00 is
7 h on the night clocks go forward and 9 h on the night they go back. A wall
time that does not exist (the skipped hour) is read with the offset before
the change; one that happens twice means its first occurrence.

Status per camera: disarmed | armed_idle | motion | person_confirmed |
cooldown, or unavailable when the camera should be watched but sends no
picture (it is never reported armed then).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ARMED_STATES = ("armed_idle", "motion", "person_confirmed", "cooldown")

NORMAL = "normal"      # not armed: the usual pipeline
HOLD = "hold"          # armed: no inference for this frame
CONFIRM = "confirm"    # armed, motion: run person detection if the scheduler admits it

# Motion sensitivity: per-pixel difference threshold (0-255 after blur), the
# smallest moving blob as a share of the analysed picture, and the number of
# consecutive samples it must persist. At 320 px wide a medium blob is ~15x15
# px: a person ~60 px tall on a 352x288 sub-stream covers ~1 % of it.
SENSITIVITY = {
    "low": {"threshold": 30.0, "min_area": 0.010, "persist": 4},
    "medium": {"threshold": 22.0, "min_area": 0.004, "persist": 3},
    "high": {"threshold": 15.0, "min_area": 0.0015, "persist": 2},
}
# A median brightness shift beyond this (0-255) is a lighting change too.
LIGHT_SHIFT = 40.0
# Background learning rates per sample: still pixels / pixels that changed.
BG_ALPHA = 0.05
BG_ALPHA_CHANGED = 0.01
# How long "person_confirmed" is shown before the cooldown.
CONFIRMED_HOLD_SEC = 5.0
# An armed camera with no frame for this long is reported unavailable.
STALE_SEC = 5.0
# How often lighting is measured for ``when_dark`` while not armed.
DARK_CHECK_SEC = 2.0


# ===================================================================== time

def store_tz():
    """ZoneInfo for SITE_TIMEZONE, or None = the host's own zone."""
    from app.services.timeutil import site_tz

    return site_tz()


def _host_zone_name() -> Optional[str]:
    import os

    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def local_dt(ts: float) -> datetime:
    """Epoch seconds -> aware datetime in the store's zone."""
    tz = store_tz()
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts).astimezone()


def zone_info(ts: Optional[float] = None) -> dict:
    """The store's zone: IANA name, where it came from, abbreviation and offset now."""
    ts = time.time() if ts is None else ts
    tz = store_tz()
    dt = local_dt(ts)
    off = dt.utcoffset() or timedelta(0)
    return {
        "name": (settings.SITE_TIMEZONE or "").strip() if tz is not None else _host_zone_name(),
        "source": "SITE_TIMEZONE" if tz is not None else "host",
        "abbreviation": dt.tzname(),
        "utc_offset_min": int(off.total_seconds() // 60),
    }


def time_info(ts: float) -> dict:
    """One instant for display: UTC, and store-local with the zone abbreviation."""
    dt = local_dt(ts)
    off = dt.utcoffset() or timedelta(0)
    return {
        "ts": round(float(ts), 3),
        "utc": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
        "local": dt.isoformat(),
        "date": dt.date().isoformat(),
        "weekday": DAY_KEYS[dt.weekday()],
        "hhmm": dt.strftime("%H:%M"),
        "abbr": dt.tzname(),
        "utc_offset_min": int(off.total_seconds() // 60),
        "label": f"{dt.strftime('%H:%M')} {dt.tzname()}",
    }


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def wall_ts(d: date, hhmm: str) -> float:
    """Epoch seconds of wall-clock ``hhmm`` on store-local date ``d`` (DST-aware)."""
    h, m = str(hhmm).split(":")[:2]
    naive = datetime.combine(d, dtime(int(h), int(m)))
    tz = store_tz()
    if tz is not None:
        return naive.replace(tzinfo=tz).timestamp()
    return naive.timestamp()   # host zone: mktime, per-date offset


def windows(cfg, around_ts: float, days_back: int = 1, days_ahead: int = 8) -> list[tuple[float, float]]:
    """The schedule's windows near ``around_ts`` as merged (start, end) epoch pairs."""
    days = set(getattr(cfg, "days", None) or [])
    if not days:
        return []
    d0 = local_dt(around_ts).date()
    overnight = _minutes(cfg.end) <= _minutes(cfg.start)
    out: list[tuple[float, float]] = []
    for k in range(-days_back, days_ahead + 1):
        d = d0 + timedelta(days=k)
        if DAY_KEYS[d.weekday()] not in days:
            continue
        s = wall_ts(d, cfg.start)
        e = wall_ts(d + timedelta(days=1) if overnight else d, cfg.end)
        if e > s:
            out.append((s, e))
    out.sort()
    merged: list[tuple[float, float]] = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def in_schedule(cfg, ts: float) -> bool:
    return any(s <= ts < e for s, e in windows(cfg, ts))


def next_transitions(cfg, ts: float) -> dict:
    """``{in_window, next_arm, next_disarm}`` (epoch seconds or None) by the schedule alone."""
    ws = windows(cfg, ts)
    for s, e in ws:
        if s <= ts < e:
            nxt = next((a for a, _ in ws if a > e), None)
            # Every day, all day: the window never closes within the horizon.
            return {"in_window": True, "next_disarm": e if e - ts < 7 * 86400 else None, "next_arm": nxt}
    for s, e in ws:
        if s > ts:
            return {"in_window": False, "next_arm": s, "next_disarm": e}
    return {"in_window": False, "next_arm": None, "next_disarm": None}


def window_label(cfg) -> str:
    return f"{cfg.start}–{cfg.end}"


# =================================================================== motion

@dataclass
class MotionResult:
    motion: bool = False            # a blob persisted for the sensitivity's N samples
    hit: bool = False               # this sample had a blob of the minimum area
    lighting: bool = False          # lighting event: background re-learnt, not motion
    warming: bool = False           # first sample: background being learnt
    changed_fraction: float = 0.0
    largest_fraction: float = 0.0
    shift: float = 0.0              # median brightness change vs the background
    box: Optional[tuple[float, float, float, float]] = None   # largest blob, frame pixels
    ms: float = 0.0


class MotionDetector:
    """Background-difference motion on a small grayscale copy of the frame."""

    def __init__(self, sensitivity: str = "medium", width: Optional[int] = None,
                 lighting_fraction: Optional[float] = None) -> None:
        self.width = int(width or settings.NIGHT_WATCH_MOTION_WIDTH)
        self.lighting_fraction = float(lighting_fraction if lighting_fraction is not None
                                       else settings.NIGHT_WATCH_LIGHTING_FRACTION)
        self.set_sensitivity(sensitivity)
        self._bg: Optional[np.ndarray] = None
        self._streak = 0
        self._mask_key = None
        self._valid: Optional[np.ndarray] = None
        self.last_small: Optional[np.ndarray] = None

    def set_sensitivity(self, sensitivity: str) -> None:
        self.sensitivity = sensitivity if sensitivity in SENSITIVITY else "medium"
        p = SENSITIVITY[self.sensitivity]
        self.threshold, self.min_area, self.persist = p["threshold"], p["min_area"], int(p["persist"])

    def reset(self) -> None:
        self._bg = None
        self._streak = 0

    def _small(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        import cv2

        h, w = frame.shape[:2]
        tw = max(16, min(self.width, w))
        th = max(1, int(round(h * tw / float(w))))
        # Decimate first (a strided view, no copy) to at most twice the
        # target, so INTER_AREA reads ~4x the output pixels, not the whole frame.
        step = max(1, w // (tw * 2))
        small = cv2.resize(frame[::step, ::step], (tw, th), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        return small, cv2.GaussianBlur(gray, (5, 5), 0)

    def _valid_mask(self, masks: Optional[list[dict]], sw: int, sh: int) -> Optional[np.ndarray]:
        """1 where motion counts, 0 inside AI_IGNORE regions and privacy masks."""
        import cv2

        if not masks:
            self._mask_key, self._valid = None, None
            return None
        key = (sw, sh, tuple(tuple((float(p["x"]), float(p["y"])) for p in m.get("points") or [])
                             for m in masks))
        if key == self._mask_key:
            return self._valid
        from app.services.privacy_mask import polygon_pixels

        valid = np.ones((sh, sw), np.uint8)
        for m in masks:
            try:
                cv2.fillPoly(valid, [polygon_pixels(m, sw, sh)], 0)
            except Exception:  # noqa: BLE001 - an unusable polygon excludes nothing
                continue
        self._mask_key, self._valid = key, valid
        return valid

    def update(self, frame: np.ndarray, masks: Optional[list[dict]] = None) -> MotionResult:
        import cv2

        t0 = time.perf_counter()
        small, gray = self._small(frame)
        self.last_small = small
        sh, sw = gray.shape[:2]
        g = gray.astype(np.float32)
        valid = self._valid_mask(masks, sw, sh)
        if self._bg is None or self._bg.shape != g.shape:
            self._bg, self._streak = g, 0
            return MotionResult(warming=True, ms=(time.perf_counter() - t0) * 1000.0)

        diff = g - self._bg
        sample = diff[::4, ::4]
        if valid is not None:
            sample = sample[valid[::4, ::4] > 0]
        shift = float(np.median(sample)) if sample.size else 0.0
        changed = (np.abs(diff - shift) > self.threshold).astype(np.uint8)
        if valid is not None:
            changed &= valid
        n_valid = float(valid.sum()) if valid is not None else float(sw * sh)
        if n_valid <= 0:
            return MotionResult(ms=(time.perf_counter() - t0) * 1000.0)
        frac = float(changed.sum()) / n_valid

        if frac > self.lighting_fraction or abs(shift) > LIGHT_SHIFT:
            # IR switch, lights, a headlight sweep: most of the picture changed
            # at once. Re-learn the background; never report it as motion.
            self._bg, self._streak = g, 0
            return MotionResult(lighting=True, changed_fraction=frac, shift=shift,
                                ms=(time.perf_counter() - t0) * 1000.0)

        opened = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
        largest, box = 0, None
        if n > 1:
            i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            largest = int(stats[i, cv2.CC_STAT_AREA])
            fh, fw = frame.shape[:2]
            sx, sy = fw / float(sw), fh / float(sh)
            x, y = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
            bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
            box = (x * sx, y * sy, (x + bw) * sx, (y + bh) * sy)
        lf = largest / n_valid
        hit = lf >= self.min_area
        self._streak = self._streak + 1 if hit else 0

        # Still pixels learn fast, changed ones slowly (a parked object is
        # absorbed in tens of seconds, a walking person is not).
        still = (1 - changed).astype(np.uint8)
        cv2.accumulateWeighted(g, self._bg, BG_ALPHA, mask=still)
        cv2.accumulateWeighted(g, self._bg, BG_ALPHA_CHANGED, mask=changed)
        return MotionResult(motion=self._streak >= self.persist, hit=hit, changed_fraction=frac,
                            largest_fraction=lf, shift=shift, box=box if hit else None,
                            ms=(time.perf_counter() - t0) * 1000.0)


# ================================================================== service

@dataclass
class _Pending:
    alert_id: str
    camera_id: str
    event_type: str
    severity: str
    title: str
    body: str
    data: dict
    push: bool
    frame: Optional[np.ndarray]
    bbox: Optional[tuple]
    caption: str
    subtitle: str
    keypoints: Any = None
    clip: bool = False


@dataclass
class _Watch:
    camera_id: str
    name: str
    detector: MotionDetector = field(default_factory=MotionDetector)
    armed: bool = False
    armed_by: Optional[str] = None
    state: str = "disarmed"
    since: float = 0.0
    last_frame_at: float = 0.0
    last_sample_at: float = 0.0
    last_dark_check: float = 0.0
    confirm_until: float = 0.0
    confirm_hits: int = 0
    motion_box: Optional[tuple] = None          # latest moving region (confirmation overlap)
    motion_frame: Optional[np.ndarray] = None   # the frame and region that started the burst
    motion_frame_box: Optional[tuple] = None
    motion_fraction: float = 0.0
    best: Optional[tuple] = None          # (confidence, bbox, frame) of the best person seen
    confirmed_until: float = 0.0
    cooldown_until: float = 0.0
    motion_quiet_until: float = 0.0
    reconfirm_after: float = 0.0
    events: deque = field(default_factory=lambda: deque(maxlen=512))
    ms: deque = field(default_factory=lambda: deque(maxlen=64))
    counts: dict = field(default_factory=lambda: {"samples": 0, "lighting_events": 0, "bursts": 0, "alerts": 0,
                                                  "motion_events": 0, "suppressed": 0})
    last_event: Optional[dict] = None
    last_motion: Optional[dict] = None


AsyncDispatch = Callable[..., Any]


class NightWatch:
    """Per-camera night-watch state machine, driven by the camera workers."""

    def __init__(self, scheduler=None, clock: Callable[[], float] = time.time) -> None:
        self._scheduler = scheduler
        self._clock = clock
        self._lock = threading.RLock()
        self._w: dict[str, _Watch] = {}
        self._pending: list[_Pending] = []
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Tests: deliver on the calling thread (process_pending) / inject a dispatcher.
        self.synchronous = False
        self.dispatcher_override: Optional[AsyncDispatch] = None
        # Tests: replace how the config and the lighting state are read.
        self.config_override: Optional[Callable[[str], Any]] = None
        self.lighting_override: Optional[Callable[[str], Optional[str]]] = None
        self.stats = {"delivered": 0, "errors": 0, "evidence_pruned": 0}
        self.last_delivery: Optional[dict] = None

    @property
    def scheduler(self):
        if self._scheduler is None:
            from app.services.inference_scheduler import inference_scheduler

            self._scheduler = inference_scheduler
        return self._scheduler

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    # ------------------------------------------------------------ inputs

    def config(self, camera_id: str):
        """The camera's NightWatchConfig, or None (never configured)."""
        if self.config_override is not None:
            return self.config_override(camera_id)
        try:
            from app.services.feature_manager import feature_manager

            return feature_manager.get_setting(camera_id, "night_watch")
        except Exception as e:  # noqa: BLE001 - an unreadable store means "not configured"
            logger.debug(f"night watch config for {camera_id} unreadable: {e}")
            return None

    def _lighting(self, camera_id: str) -> Optional[str]:
        if self.lighting_override is not None:
            return self.lighting_override(camera_id)
        try:
            from app.services.low_light import lighting_monitor

            st = lighting_monitor.camera_status(camera_id)
            return st["lighting"] if st else None
        except Exception:  # noqa: BLE001
            return None

    def _observe_lighting(self, camera_id: str, img: Optional[np.ndarray]) -> None:
        if img is None or self.lighting_override is not None:
            return
        try:
            from app.services.low_light import lighting_monitor

            lighting_monitor.observe(img, camera_id)
        except Exception:  # noqa: BLE001 - measuring must never stop the watch
            pass

    def armed_reason(self, cfg, camera_id: str, now: float) -> Optional[str]:
        """Why this camera is armed now ("schedule" / "dark"), or None."""
        if cfg is None or not cfg.enabled:
            return None
        if in_schedule(cfg, now):
            return "schedule"
        if cfg.when_dark and self._lighting(camera_id) in ("low_light", "ir"):
            return "dark"
        return None

    # ------------------------------------------------------------ worker API

    def _watch(self, camera_id: str, name: str) -> _Watch:
        w = self._w.get(camera_id)
        if w is None:
            w = self._w[camera_id] = _Watch(camera_id=camera_id, name=name)
        w.name = name or w.name
        return w

    def step(self, camera_id: str, name: str, frame: np.ndarray, now: Optional[float] = None,
             masks: Optional[list[dict]] = None) -> str:
        """Called by the camera worker for every decoded frame: NORMAL, HOLD or CONFIRM."""
        now = self._clock() if now is None else now
        cfg = self.config(camera_id)
        with self._lock:
            w = self._watch(camera_id, name)
            w.last_frame_at = now
        if cfg is not None and cfg.enabled and cfg.when_dark and not w.armed and now - w.last_dark_check >= DARK_CHECK_SEC:
            # Nothing else may be measuring this camera's lighting (analysis off).
            w.last_dark_check = now
            self._observe_lighting(camera_id, frame[::4, ::4])
        reason = self.armed_reason(cfg, camera_id, now)
        with self._lock:
            if reason is None:
                if w.armed:
                    self._disarm(w, now)
                return NORMAL
            if not w.armed:
                self._arm(w, reason, now)
            w.armed_by = reason
            w.detector.set_sensitivity(cfg.sensitivity)
            due = now - w.last_sample_at >= 1.0 / max(0.2, float(settings.NIGHT_WATCH_MOTION_FPS))
            if due:
                w.last_sample_at = now
        if due:
            if masks is None:
                from app.services.privacy_mask import camera_masks

                masks = camera_masks(camera_id)
            res = w.detector.update(frame, masks)
            self._observe_lighting(camera_id, w.detector.last_small)
            with self._lock:
                w.counts["samples"] += 1
                w.ms.append(res.ms)
                self._on_sample(w, cfg, res, frame, now)
        with self._lock:
            self._advance(w, cfg, now)
            return CONFIRM if w.state == "motion" and now < w.confirm_until else HOLD

    def confirm(self, camera_id: str, frame: np.ndarray, detections: list, now: Optional[float] = None) -> None:
        """Person detections (AI_IGNORE already applied) on a frame admitted for confirmation."""
        now = self._clock() if now is None else now
        cfg = self.config(camera_id)
        with self._lock:
            w = self._w.get(camera_id)
            if w is None or w.state != "motion" or cfg is None:
                return
            near = [d for d in detections if _near(d, w.motion_box, frame.shape)]
            if not near:
                return
            w.confirm_hits += 1
            top = max(near, key=lambda d: float(d.confidence))
            if w.best is None or float(top.confidence) > w.best[0]:
                w.best = (float(top.confidence), tuple(float(v) for v in top.bbox), frame.copy(),
                          getattr(top, "keypoints", None))
            if w.confirm_hits < max(1, int(settings.NIGHT_WATCH_CONFIRM_HITS)):
                return
            self.scheduler.boost(camera_id, 0)
            w.state, w.since = "person_confirmed", now
            w.confirmed_until = now + CONFIRMED_HOLD_SEC
            w.cooldown_until = now + float(cfg.cooldown_sec)
            conf, bbox, ev_frame, kps = w.best
            w.best, w.motion_frame = None, None
            self._emit(w, cfg, "person", now, ev_frame, bbox, conf, kps)

    def forget(self, camera_id: str) -> None:
        """The camera stopped or lost its stream: nothing is being watched."""
        with self._lock:
            w = self._w.get(camera_id)
            if w is not None:
                w.armed, w.armed_by, w.state = False, None, "disarmed"
                w.detector.reset()
                w.motion_frame = w.best = None

    # ------------------------------------------------------------ state machine

    def _arm(self, w: _Watch, reason: str, now: float) -> None:
        w.armed, w.armed_by, w.state, w.since = True, reason, "armed_idle", now
        w.detector.reset()
        w.confirm_until = w.cooldown_until = w.reconfirm_after = 0.0
        self.scheduler.hold(w.camera_id, True)
        logger.info(f"Night watch armed on {w.camera_id} ({reason}); pose model paused for this camera")

    def _disarm(self, w: _Watch, now: float) -> None:
        w.armed, w.armed_by, w.state, w.since = False, None, "disarmed", now
        w.detector.reset()
        w.motion_frame = w.best = None
        self.scheduler.hold(w.camera_id, False)
        logger.info(f"Night watch disarmed on {w.camera_id}; normal analysis resumes")

    def _on_sample(self, w: _Watch, cfg, res: MotionResult, frame: np.ndarray, now: float) -> None:
        if res.lighting:
            w.counts["lighting_events"] += 1
            return
        if res.hit:
            w.last_motion = {"at": now, "fraction": round(res.largest_fraction, 4)}
        if w.state == "motion":
            if res.hit and res.box is not None:
                w.motion_box = res.box
            return
        if w.state != "armed_idle" or not res.motion:
            return
        if now < w.reconfirm_after or now < w.cooldown_until:
            return
        w.state, w.since = "motion", now
        w.confirm_until = now + float(settings.NIGHT_WATCH_CONFIRM_SEC)
        w.confirm_hits, w.best = 0, None
        w.motion_box, w.motion_fraction = res.box, res.largest_fraction
        w.motion_frame, w.motion_frame_box = frame.copy(), res.box
        w.counts["bursts"] += 1
        self.scheduler.boost(w.camera_id, float(settings.NIGHT_WATCH_CONFIRM_SEC))

    def _advance(self, w: _Watch, cfg, now: float) -> None:
        if w.state == "motion" and now >= w.confirm_until:
            # The burst found nobody: a motion event (dashboard only).
            self.scheduler.boost(w.camera_id, 0)
            w.reconfirm_after = now + float(settings.NIGHT_WATCH_RECONFIRM_SEC)
            w.state, w.since = "armed_idle", now
            frame, box = w.motion_frame, w.motion_frame_box
            w.motion_frame = w.best = None
            if now < w.motion_quiet_until:
                w.counts["suppressed"] += 1
            else:
                w.motion_quiet_until = now + float(cfg.cooldown_sec)
                self._emit(w, cfg, "motion", now, frame, box, None, None)
        if w.state == "person_confirmed" and now >= w.confirmed_until:
            w.state, w.since = "cooldown", now
        if w.state == "cooldown" and now >= w.cooldown_until:
            w.state, w.since = "armed_idle", now

    # ------------------------------------------------------------ events

    def _emit(self, w: _Watch, cfg, kind: str, now: float, frame: Optional[np.ndarray],
              bbox: Optional[tuple], confidence: Optional[float], keypoints) -> Optional[str]:
        while w.events and now - w.events[0] > 3600.0:
            w.events.popleft()
        if len(w.events) >= max(1, int(settings.NIGHT_WATCH_MAX_EVENTS_PER_HOUR)):
            w.counts["suppressed"] += 1
            logger.warning(f"Night watch {kind} on {w.camera_id} not recorded: "
                           f"{settings.NIGHT_WATCH_MAX_EVENTS_PER_HOUR} events in the last hour")
            return None
        w.events.append(now)
        t = time_info(now)
        zone = zone_info(now)
        name = w.name or w.camera_id
        alert_id = f"nw_{uuid.uuid4().hex[:12]}"
        why = "dark picture" if w.armed_by == "dark" else f"night watch {window_label(cfg)}"
        if kind == "person":
            event_type, severity, push = "NIGHT_INTRUSION", "HIGH", True
            title = f"Person at night - {name}"
            body = f"{name}: a person was detected at {t['label']} ({why})."
            caption = f"NIGHT WATCH: person - {name}"
            w.counts["alerts"] += 1
        else:
            event_type, severity, push = "NIGHT_MOTION", "INFO", False
            title = f"Night motion - {name}"
            body = f"{name}: movement at {t['label']}; no person was confirmed ({why})."
            caption = f"NIGHT WATCH: motion - {name}"
            w.counts["motion_events"] += 1
        data = {
            "camera_id": w.camera_id, "camera_name": name, "alert_id": alert_id,
            "event_type": event_type, "severity": severity,
            "timestamp": t["utc"], "store_time": t["label"], "store_time_iso": t["local"],
            "store_timezone": zone["name"], "store_tz_abbr": t["abbr"], "store_utc_offset_min": t["utc_offset_min"],
            "armed_by": w.armed_by, "window": window_label(cfg), "motion_fraction": round(w.motion_fraction, 4),
            "night_watch": True,
        }
        if confidence is not None:
            data["confidence"] = round(float(confidence), 3)
        w.last_event = {"alert_id": alert_id, "event_type": event_type, "severity": severity, "at": t}
        p = _Pending(alert_id=alert_id, camera_id=w.camera_id, event_type=event_type, severity=severity,
                     title=title, body=body, data=data, push=push, frame=frame, bbox=bbox,
                     caption=caption, subtitle=f"{name}  {t['date']} {t['label']}", keypoints=keypoints,
                     clip=kind == "person" and bool(settings.NIGHT_WATCH_CLIP))
        with self._pending_lock:
            self._pending.append(p)
        if not self.synchronous:
            self._ensure_writer()
            self._wake.set()
        logger.info(f"Night watch {event_type} on {w.camera_id} at {t['label']} ({alert_id})")
        return alert_id

    # ------------------------------------------------------------ delivery (writer thread)

    @staticmethod
    def evidence_dir() -> Path:
        d = (Path(settings.NIGHT_WATCH_EVIDENCE_DIR) if settings.NIGHT_WATCH_EVIDENCE_DIR
             else Path(settings.STORAGE_DIR) / "night_watch")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def evidence_path(cls, name: str) -> Optional[Path]:
        stem, _, ext = name.partition(".")
        if ext not in ("jpg", "mp4") or not stem.startswith("nw_") or not stem.replace("_", "").isalnum():
            return None
        p = cls.evidence_dir() / name
        return p if p.is_file() else None

    def _write_still(self, p: _Pending) -> bool:
        if p.frame is None:
            return False
        import cv2

        from app.services.privacy_mask import apply_privacy_masks

        # Saved footage: privacy masks burned in.
        img = apply_privacy_masks(p.frame, p.camera_id)
        kps = p.keypoints
        try:
            from app.services.pose_analytics import render_evidence

            img = render_evidence(img, p.bbox, kps if isinstance(kps, np.ndarray) else None,
                                  p.caption, p.subtitle)
        except Exception as e:  # noqa: BLE001 - the plain masked frame with the box
            logger.debug(f"render_evidence unavailable for night watch ({e})")
            img = img.copy()
            if p.bbox:
                x1, y1, x2, y2 = (int(v) for v in p.bbox)
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 165, 255), 2)
        path = self.evidence_dir() / f"{p.alert_id}.jpg"
        ok = cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), int(settings.THEFT_EVIDENCE_JPEG_QUALITY)])
        if ok:
            from app.services.evidence_storage import note_evidence_written

            note_evidence_written(path)
        return bool(ok)

    def _write_clip(self, p: _Pending) -> bool:
        """Pre-event buffer + NIGHT_WATCH_CLIP_POST_SEC of the (masked) clip ring, as MP4."""
        from app.services.clip_recorder import clip_recorder_service

        buf = clip_recorder_service.buffers.get(p.camera_id)
        if buf is None:
            return False
        frames = buf.get_pre_event_frames()
        fps = max(1, int(buf.fps))
        end = time.monotonic() + max(0.0, float(settings.NIGHT_WATCH_CLIP_POST_SEC))
        last_t = buf.latest_frame_time
        while time.monotonic() < end:
            time.sleep(1.0 / fps)
            if buf.latest_frame_time != last_t:
                last_t = buf.latest_frame_time
                f = buf.get_latest_frame_copy()
                if f is not None:
                    frames.append(f)
        if not frames:
            return False
        path = self.evidence_dir() / f"{p.alert_id}.mp4"
        clip_recorder_service._mux_frames_to_mp4(frames, path, fps)
        if path.is_file():
            from app.services.evidence_storage import note_evidence_written

            note_evidence_written(path)
            return True
        return False

    def enforce_cap(self) -> int:
        """Delete the oldest night-watch evidence until it is within NIGHT_WATCH_EVIDENCE_MAX_MB.

        The sub-cap is enforced by the evidence storage manager, which also
        marks the alerts whose files went as "evidence expired" and refuses to
        touch a directory outside STORAGE_DIR or on a network share.
        """
        from app.services.evidence_storage import evidence_storage

        removed = evidence_storage.enforce_kind_cap("night_watch")
        if removed:
            self.stats["evidence_pruned"] += removed
        return removed

    def _dispatcher(self) -> AsyncDispatch:
        if self.dispatcher_override is not None:
            return self.dispatcher_override
        from app.services.alert_dispatcher import alert_dispatcher

        return alert_dispatcher.dispatch

    def _run(self, coro_fn) -> Any:
        loop = self._loop
        if loop is None:
            try:
                from app.services.tripwire_engine import tripwire_engine

                loop = tripwire_engine._loop
            except Exception:  # noqa: BLE001
                loop = None
        if loop is not None and not loop.is_closed() and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro_fn(), loop).result(timeout=30)
        if self.synchronous or self.dispatcher_override is not None:
            return asyncio.run(coro_fn())
        raise RuntimeError("no application event loop bound")

    def _deliver(self, p: _Pending) -> None:
        try:
            if self._write_still(p):
                url = f"/api/v1/night-watch/evidence/{p.alert_id}.jpg"
                p.data["snapshot_url"] = url
                p.data["evidence_snapshot_url"] = url
        except Exception as e:  # noqa: BLE001 - the alert still goes out, without a picture
            logger.error(f"night watch evidence for {p.alert_id} not saved: {e}")
        p.frame = p.keypoints = None
        dispatch = self._dispatcher()
        try:
            # A motion-only event is logged for the dashboard list and nothing
            # else: no push, and no websocket broadcast either (the phone app
            # turns every broadcast it receives into a notification).
            result = self._run(lambda: dispatch(p.event_type, p.severity, p.title, p.body, dict(p.data),
                                                push=p.push, broadcast=p.push))
            self.stats["delivered"] += 1
            self.last_delivery = {"alert_id": p.alert_id, "result": result}
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            self.last_delivery = {"alert_id": p.alert_id, "error": str(e)}
            logger.error(f"night watch {p.event_type} {p.alert_id} not delivered: {e}")
        if p.clip:
            try:
                if self._write_clip(p):
                    self._run(lambda: _set_clip_url(p.alert_id, f"/api/v1/night-watch/evidence/{p.alert_id}.mp4"))
            except Exception as e:  # noqa: BLE001
                logger.error(f"night watch clip for {p.alert_id} not saved: {e}")
        try:
            self.enforce_cap()
        except Exception as e:  # noqa: BLE001
            logger.error(f"night watch evidence cap check failed: {e}")

    def process_pending(self) -> int:
        with self._pending_lock:
            batch, self._pending = self._pending, []
        for p in batch:
            self._deliver(p)
        return len(batch)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._writer = threading.Thread(target=self._writer_loop, name="night-watch-writer", daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            try:
                self.process_pending()
            except Exception as e:  # noqa: BLE001 - keep the writer alive
                logger.error(f"night watch writer: {e}")

    # ------------------------------------------------------------ status

    def camera_status(self, camera_id: str, now: Optional[float] = None, streaming: Optional[bool] = None) -> dict:
        """Honest per-camera state; ``streaming`` False = the camera has no picture now."""
        now = self._clock() if now is None else now
        cfg = self.config(camera_id)
        with self._lock:
            w = self._w.get(camera_id)
            enabled = bool(cfg is not None and cfg.enabled)
            out: dict[str, Any] = {"enabled": enabled, "state": "disarmed", "armed": False, "armed_by": None,
                                   "note": None}
            if cfg is not None:
                out.update(window=window_label(cfg), days=list(cfg.days), when_dark=cfg.when_dark,
                           sensitivity=cfg.sensitivity, cooldown_sec=cfg.cooldown_sec)
            if not enabled:
                return out
            sched = next_transitions(cfg, now)
            out["in_window"] = sched["in_window"]
            out["next_arm"] = time_info(sched["next_arm"]) if sched["next_arm"] else None
            out["next_disarm"] = time_info(sched["next_disarm"]) if sched["next_disarm"] else None
            fresh = w is not None and now - w.last_frame_at <= STALE_SEC and streaming is not False
            if w is not None and w.armed and fresh:
                out.update(state=w.state, armed=True, armed_by=w.armed_by, since=time_info(w.since))
                if w.state == "cooldown":
                    out["cooldown_until"] = time_info(w.cooldown_until)
            elif sched["in_window"] or (w is not None and w.armed):
                out["state"] = "unavailable"
                out["note"] = "Should be watching now, but the camera is not sending a picture."
            if w is not None:
                ms = list(w.ms)
                out["motion"] = {
                    "samples": w.counts["samples"],
                    "ms_per_sample": round(sum(ms) / len(ms), 2) if ms else None,
                    "sample_fps": float(settings.NIGHT_WATCH_MOTION_FPS),
                    "last_motion": ({"at": time_info(w.last_motion["at"]), "fraction": w.last_motion["fraction"]}
                                    if w.last_motion else None),
                }
                out["counts"] = dict(w.counts)
                out["last_event"] = w.last_event
            try:
                from app.services.inference_backend import person_detector

                if person_detector.initialised and not person_detector.available:
                    out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                        "The person detector is not available: motion is recorded, but no person can be "
                        "confirmed, so no night alert is pushed.")
            except Exception:  # noqa: BLE001
                pass
            return out

    def status(self) -> dict:
        return {"stats": dict(self.stats), "last_delivery": self.last_delivery,
                "store_timezone": zone_info()}


def _near(det, box: Optional[tuple], shape) -> bool:
    """Whether a person box overlaps the moving region, grown by half its size (None = anywhere)."""
    if box is None:
        return True
    x1, y1, x2, y2 = box
    gx, gy = (x2 - x1) * 0.5, (y2 - y1) * 0.5
    ex1, ey1, ex2, ey2 = x1 - gx, y1 - gy, x2 + gx, y2 + gy
    dx1, dy1, dx2, dy2 = (float(v) for v in det.bbox)
    return dx1 < ex2 and dx2 > ex1 and dy1 < ey2 and dy2 > ey1


async def _set_clip_url(alert_id: str, url: str) -> None:
    from sqlalchemy import update

    from app.database import async_session_factory
    from app.models.db_models import SecurityEventModel

    async with async_session_factory() as session:
        await session.execute(update(SecurityEventModel).where(SecurityEventModel.id == alert_id)
                              .values(clip_url=url))
        await session.commit()


night_watch = NightWatch()
