"""Shadow pose-model trial: a second pose model judged on the live model's frames.

A developer experiment for deciding whether to replace the live pose model
(``SHADOW_POSE_MODEL``, e.g. the RTMO-s static export). It is *shadow only*:
nothing it finds reaches tracking, counting, heatmaps, pose analytics or
alerts. What it keeps is a paired, measured comparison.

Pairing. After the live model has analysed a frame (``LiveCameraWorker``
calls ``offer()`` with the people it kept), the trial may take a copy of that
same frame and run the shadow model on it. Both models' people are then
judged by identical rules: the detector's own shape/size gates and per-box
threshold (``PersonDetector.postprocess``, ``person_threshold``) and the
camera's AI_IGNORE regions. For each pair only aggregate counters are kept:

* people found by each model, matched (IoU >= ``MATCH_IOU``), found by only
  one of them, by box height in the analysed frame (``SIZE_BUCKETS``) and by
  the camera's native resolution class (``resolution_class``);
* keypoints: mean confidence and joints >= 0.5 per person, and the share of
  people passing the keypoint gate (>= 5 joints >= 0.5), for all people and
  for matched people only (the live model's before and after the refiner);
* a static-box indicator per model: detections sitting on a box that has not
  moved (+-2 % of the frame) for more than ``SHADOW_STATIC_MINUTES`` (a
  fridge or a poster taken for a person);
* device milliseconds per inference for both models, and the lighting state
  (``low_light``) of the camera at the time.

Scheduling. The shadow model never delays a live inference by design and
never takes live capacity by budget:

* ``offer()`` is non-blocking and admits a frame only when the previous
  shadow run has been paid for: after a run of ``t`` ms the next may start
  ``t x (1 / SHADOW_POSE_SHARE - 1)`` ms later, so the shadow model keeps the
  device busy at most ``SHADOW_POSE_SHARE`` of the time. One frame waits at
  most; a frame offered while one waits is dropped (counted).
* On an accelerator the worker takes the device only through
  ``DeviceGate.try_low()``: never while a live run holds it or queues for it.
  A live run that arrives during a shadow run waits for that one run; those
  waits are counted and timed (``main_delayed_by_shadow``), not assumed away.
* Its time is not added to the live model's device-busy total, so the live
  scheduler's rates and model re-fits are unchanged by the trial.

Review samples. When the models disagree, at most one side-by-side image per
camera every ``SHADOW_SAMPLE_EVERY_SEC`` is written to
``STORAGE_DIR/shadow_trial`` with the camera's privacy masks burned in,
capped at ``SHADOW_SAMPLES_MAX`` files and ``SHADOW_SAMPLES_MAX_MB``; the
directory is declared to ``evidence_storage`` (kind ``shadow_trial``), so it
also counts against the device's evidence limit. Everything (samples and
``stats.json``) is deleted ``SHADOW_TRIAL_RETAIN_DAYS`` after the trial last
recorded a pair. Nothing goes to the NAS.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

MATCH_IOU = 0.5
KP_VISIBLE = 0.5
KP_GATE_MIN = 5
SIZE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 40.0, "<40"), (40.0, 80.0, "40-80"), (80.0, 160.0, "80-160"), (160.0, float("inf"), ">160"),
)
STATIC_TOLERANCE = 0.02         # of frame width / height
STATIC_FORGET_SEC = 300.0       # a candidate box unseen this long is dropped
STATIC_MAX_BOXES = 64           # candidates per (camera, model)
SAVE_EVERY_SEC = 60.0
HOUSEKEEP_EVERY_SEC = 3600.0
DEVICE_WAIT_SEC = 1.0           # how long a job may wait for an idle device
LOAD_WAIT_SEC = 900.0           # how long the loader waits for the live model
SAMPLE_WIDTH = 640              # each half of a review image
DIR_NAME = "shadow_trial"
STATS_NAME = "stats.json"
_SAMPLE_RE = re.compile(r"^[0-9T]+_[A-Za-z0-9_-]+\.jpg$")


# ------------------------------------------------------------------ helpers


def size_bucket(height_px: float) -> str:
    for lo, hi, name in SIZE_BUCKETS:
        if lo <= height_px < hi:
            return name
    return SIZE_BUCKETS[-1][2]


def resolution_class(native: Optional[tuple[int, int]]) -> str:
    """The camera's native stream class by its line count (NVR naming)."""
    if not native:
        return "unknown"
    h = int(native[1])
    if h <= 288:
        return "CIF (<=288 lines)"
    if h <= 576:
        return "D1 (<=576 lines)"
    if h <= 720:
        return "720p"
    if h <= 1080:
        return "1080p"
    return "above 1080p"


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def match_boxes(a: np.ndarray, b: np.ndarray, thr: float = MATCH_IOU) -> list[tuple[int, int]]:
    """Greedy one-to-one matching, highest IoU first, pairs with IoU >= ``thr``."""
    m = iou_matrix(a, b)
    pairs: list[tuple[int, int]] = []
    if m.size == 0:
        return pairs
    used_a, used_b = set(), set()
    for flat in np.argsort(-m, axis=None):
        i, j = divmod(int(flat), m.shape[1])
        if m[i, j] < thr:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j))
    return pairs


def keypoint_quality(kpts: Optional[np.ndarray]) -> Optional[tuple[float, int]]:
    """(mean joint confidence, joints >= 0.5) of one person, or None without keypoints."""
    if kpts is None or kpts.ndim != 2 or kpts.shape[1] < 3:
        return None
    conf = np.nan_to_num(kpts[:, 2].astype(np.float32), nan=0.0)
    return float(conf.mean()), int((conf >= KP_VISIBLE).sum())


def parse_window(spec: str) -> Optional[tuple[int, int]]:
    """"HH:MM-HH:MM" -> (start, end) minutes of the day; None when empty or invalid."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not m:
        return None
    a, b, c, d = (int(v) for v in m.groups())
    if a > 23 or c > 23 or b > 59 or d > 59:
        return None
    return a * 60 + b, c * 60 + d


def in_window(window: Optional[tuple[int, int]], when: datetime) -> bool:
    if window is None:
        return True
    start, end = window
    minute = when.hour * 60 + when.minute
    if start == end:
        return True
    return start <= minute < end if start < end else (minute >= start or minute < end)


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------ tallies


def _kp_block() -> dict:
    return {"persons": 0, "with_keypoints": 0, "conf_sum": 0.0, "visible_sum": 0, "gate_pass": 0}


def _kp_add(block: dict, kpts: Optional[np.ndarray]) -> None:
    block["persons"] += 1
    q = keypoint_quality(kpts)
    if q is None:
        return
    block["with_keypoints"] += 1
    block["conf_sum"] += q[0]
    block["visible_sum"] += q[1]
    block["gate_pass"] += int(q[1] >= KP_GATE_MIN)


def _kp_view(block: dict) -> dict:
    n = block["with_keypoints"]
    return {
        "persons": block["persons"],
        "mean_keypoint_confidence": round(block["conf_sum"] / n, 3) if n else None,
        "mean_joints_at_least_0_5": round(block["visible_sum"] / n, 2) if n else None,
        "keypoint_gate_pass_rate": round(block["gate_pass"] / n, 3) if n else None,
    }


def new_tally() -> dict:
    buckets = {name: 0 for _, _, name in SIZE_BUCKETS}
    return {
        "pairs": 0, "pairs_with_people": 0, "pairs_disagreeing": 0,
        "persons_main": 0, "persons_shadow": 0, "matched": 0, "only_main": 0, "only_shadow": 0,
        "persons_main_by_size": dict(buckets), "persons_shadow_by_size": dict(buckets),
        "matched_by_size": dict(buckets),
        "only_main_by_size": dict(buckets), "only_shadow_by_size": dict(buckets),
        "kp_main": _kp_block(), "kp_main_delivered": _kp_block(), "kp_shadow": _kp_block(),
        "kp_matched_main": _kp_block(), "kp_matched_shadow": _kp_block(),
        "static_main": 0, "static_shadow": 0,
        "main_ms_sum": 0.0, "main_ms_n": 0, "shadow_ms_sum": 0.0, "shadow_ms_n": 0, "shadow_ms_max": 0.0,
        "refiner_frames": 0,
    }


def tally_view(t: dict) -> dict:
    """Counters -> the reported figures (rates from counts; None where undefined)."""
    pm, ps, mt = t["persons_main"], t["persons_shadow"], t["matched"]
    return {
        "pairs": t["pairs"],
        "pairs_with_people": t["pairs_with_people"],
        "pairs_disagreeing": t["pairs_disagreeing"],
        "persons_main": pm,
        "persons_shadow": ps,
        "matched": mt,
        "only_main": t["only_main"],
        "only_shadow": t["only_shadow"],
        # Share of each model's people the other one also found.
        "main_found_by_shadow": round(mt / pm, 3) if pm else None,
        "shadow_found_by_main": round(mt / ps, 3) if ps else None,
        "by_box_height_px": {
            name: {"main": t["persons_main_by_size"][name], "shadow": t["persons_shadow_by_size"][name],
                   "matched": t["matched_by_size"][name], "only_main": t["only_main_by_size"][name],
                   "only_shadow": t["only_shadow_by_size"][name]}
            for _, _, name in SIZE_BUCKETS
        },
        "keypoints": {
            "main": _kp_view(t["kp_main"]),
            "main_as_delivered": _kp_view(t["kp_main_delivered"]),
            "shadow": _kp_view(t["kp_shadow"]),
            "matched_main": _kp_view(t["kp_matched_main"]),
            "matched_shadow": _kp_view(t["kp_matched_shadow"]),
        },
        "static_box_detections": {"main": t["static_main"], "shadow": t["static_shadow"]},
        "device_ms": {
            "main_mean": round(t["main_ms_sum"] / t["main_ms_n"], 2) if t["main_ms_n"] else None,
            "shadow_mean": round(t["shadow_ms_sum"] / t["shadow_ms_n"], 2) if t["shadow_ms_n"] else None,
            "shadow_max": round(t["shadow_ms_max"], 2) if t["shadow_ms_n"] else None,
        },
        "refiner_on_share": round(t["refiner_frames"] / t["pairs"], 3) if t["pairs"] else None,
    }


class StaticBoxes:
    """Boxes that have not moved for long, per (camera, model).

    ``observe()`` returns how many of the given boxes sit on a box seen within
    ``STATIC_TOLERANCE`` of the frame for at least ``minutes``.
    """

    def __init__(self) -> None:
        self._boxes: dict[tuple[str, str], list[list[float]]] = {}

    def observe(self, key: tuple[str, str], boxes: np.ndarray, w: int, h: int, now: float, minutes: float) -> int:
        cands = self._boxes.setdefault(key, [])
        cands[:] = [c for c in cands if now - c[5] <= STATIC_FORGET_SEC]
        tol = np.array([w, h, w, h], np.float32) * STATIC_TOLERANCE
        static = 0
        for b in boxes:
            hit = None
            for c in cands:
                if np.all(np.abs(np.asarray(c[:4], np.float32) - b) <= tol):
                    hit = c
                    break
            if hit is None:
                cands.append([float(b[0]), float(b[1]), float(b[2]), float(b[3]), now, now])
                continue
            hit[5] = now
            if now - hit[4] >= minutes * 60.0:
                static += 1
        if len(cands) > STATIC_MAX_BOXES:
            cands.sort(key=lambda c: c[5])
            del cands[: len(cands) - STATIC_MAX_BOXES]
        return static


# ------------------------------------------------------------------ the trial


class ShadowTrial:
    """Loads the shadow model, pairs it with the live model and keeps the tallies."""

    def __init__(self, detector=None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, start_threads: bool = True):
        self._detector = detector
        self._clock = clock
        self._wall = wall
        self._threads = start_threads
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.state = "off"
        self.reason = "SHADOW_POSE_MODEL is empty"
        self.model_path: Optional[Path] = None
        self.model_sha256: Optional[str] = None
        self.weights: Optional[str] = None
        self.provider: Optional[str] = None
        self.session = None
        self.spec = None
        self._job: Optional[dict] = None
        self._next_allowed = 0.0
        self._busy: deque = deque()               # (end, ms) of shadow runs, for the measured share
        self._counters = {"offered": 0, "admitted": 0, "dropped_budget": 0, "dropped_queue": 0,
                          "dropped_device_busy": 0, "failed": 0}
        self._totals = new_tally()
        self._by: dict[str, dict[str, dict]] = {"camera": {}, "lighting": {}, "resolution": {}, "main_model": {}}
        self._static = StaticBoxes()
        self._samples: list[dict] = []
        self._last_sample: dict[str, float] = {}
        self.started_at: Optional[float] = None
        self.last_pair_at: Optional[float] = None
        self._dirty = False
        self._last_save = 0.0
        self._last_housekeep = 0.0
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------ config

    @property
    def detector(self):
        if self._detector is None:
            from app.services.inference_backend import person_detector

            self._detector = person_detector
        return self._detector

    @staticmethod
    def directory() -> Path:
        return Path(settings.STORAGE_DIR) / DIR_NAME

    @staticmethod
    def cameras() -> set[str]:
        return {c.strip() for c in (settings.SHADOW_POSE_CAMERAS or "").split(",") if c.strip()}

    @staticmethod
    def share() -> float:
        return min(max(float(settings.SHADOW_POSE_SHARE), 0.0), 0.9)

    def _window_open(self) -> bool:
        return in_window(parse_window(settings.SHADOW_TRIAL_WINDOW), datetime.fromtimestamp(self._wall()))

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Called once from the application lifespan (after the live model loaded)."""
        self.housekeep(force=True)
        name = (settings.SHADOW_POSE_MODEL or "").strip()
        if not name:
            self.state, self.reason = "off", "SHADOW_POSE_MODEL is empty"
            return
        path = Path(name)
        self.model_path = path if path.is_absolute() else Path(settings.MODELS_DIR) / path
        if self.share() <= 0:
            self.state, self.reason = "off", "SHADOW_POSE_SHARE is 0"
            return
        self.state, self.reason = "loading", "waiting for the live model"
        if self._threads:
            threading.Thread(target=self._load_and_run, name="shadow-trial", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self.save()

    def _load_and_run(self) -> None:
        try:
            if self._load():
                self._run_worker()
        except Exception as e:  # noqa: BLE001 - the trial must never disturb the service
            self.state, self.reason = "failed", f"{type(e).__name__}: {e}"
            logger.error(f"Shadow trial stopped: {self.reason}")

    def _load(self) -> bool:
        det = self.detector
        path = self.model_path
        assert path is not None
        if not path.is_file():
            self.state, self.reason = "failed", f"model file not found: {path}"
            logger.error(f"Shadow trial: {self.reason}")
            return False
        deadline = self._clock() + LOAD_WAIT_SEC
        # The live model first: its provider is the one to compare on, and a
        # background GPU compile must finish before another model loads.
        while not self._stop.is_set():
            compiling = (getattr(det, "gpu_compile", {}) or {}).get("state") == "compiling"
            if det.initialised and det.available and not compiling:
                break
            if self._clock() > deadline:
                self.state, self.reason = "failed", "the live pose model did not become available"
                return False
            self._stop.wait(2.0)
        if self._stop.is_set():
            return False
        import onnxruntime as ort

        from app.services import amd_migraphx as amd
        from app.services.inference_backend import build_model_spec

        ep = det.execution_provider
        if det._is_plugin(ep) and not amd.is_warm(amd.cache_dir(), path):
            # A cold MIGraphX compile holds the GIL for minutes: do it in a
            # child process (scripts/prewarm_inference.py --only-model).
            self.state, self.reason = "compiling", f"compiling {path.name} for the GPU in a child process"
            ok, why = self._compile_in_child(path)
            if not ok:
                self.state, self.reason = "failed", f"GPU compile failed: {why}"
                return False
        self.state, self.reason = "loading", f"loading {path.name} on {det.provider}"
        sess, why = det._create_session(ort, path, ep, role="shadow")
        if sess is None:
            self.state, self.reason = "failed", f"could not load on {det.provider}: {why}"
            return False
        meta = sess.get_modelmeta().custom_metadata_map or {}
        spec = build_model_spec(path, meta, sess.get_inputs(), sess.get_outputs())
        if spec.task != "pose" or spec.kpt_shape is None:
            self.state, self.reason = "failed", f"{path.name} is not a pose model ({spec.task})"
            return False
        # Warm-up takes the device only when the live model does not want it.
        x = np.zeros((1, 3, spec.input_size[1], spec.input_size[0]), spec.input_dtype)
        for _ in range(max(1, int(settings.INFERENCE_WARMUP_RUNS)) + 1):
            if not self._with_device(lambda: sess.run(None, {spec.input_name: x}), wait=30.0):
                self.state, self.reason = "failed", "the device never became free for the warm-up"
                return False
        det._mark_compiled(path, ep=ep)
        self.session, self.spec, self.provider = sess, spec, det.provider
        self.weights = meta.get("weights") or None
        self.model_sha256 = _sha256(path)
        self._restore()
        if self.started_at is None:
            self.started_at = self._wall()
        self.state, self.reason = "running", f"{path.name} on {det.provider}"
        logger.info(f"Shadow trial running: {path.name} (layout {spec.layout}) on {det.provider}, "
                    f"share {self.share():.0%}; live analytics are not affected")
        return True

    def _compile_in_child(self, path: Path) -> tuple[bool, Optional[str]]:
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[2] / "scripts" / "prewarm_inference.py"
        env = dict(os.environ, MIGRAPHX_CACHE_DIR=str(settings.MIGRAPHX_CACHE_DIR))
        try:
            res = subprocess.run([sys.executable, str(script), "--only-model", str(path)], env=env,
                                 capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{type(e).__name__}: {e}"
        if res.returncode == 0:
            return True, None
        tail = " | ".join((res.stdout or res.stderr or "").strip().splitlines()[-3:])
        return False, f"exit {res.returncode}: {tail[-400:]}"

    # ------------------------------------------------------------ device

    def _with_device(self, fn, wait: Optional[float] = None):
        """Run ``fn`` holding the device at low priority; None if it never came free."""
        wait = DEVICE_WAIT_SEC if wait is None else wait
        det = self.detector
        if not det._is_accelerator(det.provider):
            return (fn(),)
        gate = det.device_gate
        deadline = time.monotonic() + wait          # real time: this waits for real threads
        while not gate.try_low():
            if time.monotonic() > deadline or self._stop.is_set():
                return None
            time.sleep(0.002)
        try:
            return (fn(),)
        finally:
            gate.release_low()

    # ------------------------------------------------------------ offering

    def active(self) -> bool:
        return self.state == "running"

    def offer(self, camera_id: str, frame: np.ndarray, main_persons: list, ignore: Optional[list] = None,
              max_frame_fraction: Optional[float] = None) -> bool:
        """The live model finished this frame: take a copy for the shadow model if due.

        Never blocks and never raises. ``main_persons``: the live model's people
        on this frame after the per-box threshold and AI_IGNORE regions.
        """
        if self.state != "running" or frame is None:
            return False
        try:
            cams = self.cameras()
            if cams and camera_id not in cams:
                return False
            if not self._window_open():
                return False
            now = self._clock()
            info = self.detector.last_detect_info() or {}
            with self._cv:
                self._counters["offered"] += 1
                if self._job is not None:
                    self._counters["dropped_queue"] += 1
                    return False
                if now < self._next_allowed:
                    self._counters["dropped_budget"] += 1
                    return False
                self._counters["admitted"] += 1
                # Shallow copies: later stages may reassign a detection's fields.
                self._job = {"camera": camera_id, "frame": frame.copy(),
                             "main": [copy.copy(d) for d in main_persons],
                             "ignore": ignore, "max_frac": max_frame_fraction, "info": dict(info),
                             "wall": self._wall()}
                self._cv.notify()
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"shadow offer failed: {e}")
            return False

    # ------------------------------------------------------------ worker

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                if self._job is None:
                    self._cv.wait(timeout=5.0)
                job, self._job = self._job, None
            if job is not None:
                self.run_job(job)
            self._periodic()

    def run_job(self, job: dict) -> float:
        """Process one admitted frame and schedule the next admission. Returns device ms."""
        ms = 0.0
        try:
            ms = self.process(job)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._counters["failed"] += 1
            logger.warning(f"Shadow trial pair failed: {type(e).__name__}: {e}")
        with self._cv:
            s = self.share()
            if ms < 0:
                # The device never came free: back off instead of copying frames for nothing.
                delay = 0.5
            else:
                # Pay for the run: the device is busy with the shadow model at
                # most ``share`` of the wall time.
                delay = ms * (1.0 / s - 1.0) / 1000.0 if s > 0 else 3600.0
            self._next_allowed = self._clock() + delay
        return ms

    def _periodic(self) -> None:
        now = self._wall()
        if self._dirty and now - self._last_save >= SAVE_EVERY_SEC:
            self.save()
        if now - self._last_housekeep >= HOUSEKEEP_EVERY_SEC:
            self.housekeep()

    def infer(self, frame: np.ndarray, enhance=None):
        """Run the shadow model on one frame at low priority: (raw, scale, px, py, ms) or None."""
        from app.services.inference_backend import letterbox

        spec, sess = self.spec, self.session
        blob, scale, px, py = letterbox(frame, spec.input_size, spec.input_dtype, enhance=enhance,
                                        rgb=spec.input_rgb, scale_to=spec.input_scale)
        timing: dict = {}

        def run():
            t = time.perf_counter()
            outs = sess.run(None, {spec.input_name: blob})
            timing["ms"] = (time.perf_counter() - t) * 1000.0
            return outs

        got = self._with_device(run)
        if got is None:
            return None
        outs = got[0]
        raw = outs if spec.layout == "rtmo" else outs[0]
        return raw, scale, px, py, timing["ms"]

    def process(self, job: dict) -> float:
        """Run the shadow model on a job's frame and add the pair. Returns device ms."""
        from app.services.inference_backend import person_threshold
        from app.services.privacy_mask import outside_ignore_regions

        det = self.detector
        frame, cam = job["frame"], job["camera"]
        res = self.infer(frame, (job.get("info") or {}).get("enhance"))
        if res is None:
            with self._lock:
                self._counters["dropped_device_busy"] += 1
            return -1.0
        raw, scale, px, py, ms = res
        dets, _ = det.postprocess(raw, self.spec, frame, scale, px, py, float(settings.TRACK_LOW_CONF_THRESHOLD),
                                  float(settings.PERSON_NMS_IOU), False, job.get("max_frac"), cam)
        shadow = [d for d in dets if d.confidence >= person_threshold(d)]
        if job.get("ignore"):
            shadow = outside_ignore_regions(shadow, job["ignore"])
        self.add_pair(cam, frame, job["main"], shadow, ms, job.get("info") or {}, job.get("wall"))
        with self._lock:
            end = self._clock()
            self._busy.append((end, ms))
            while self._busy and end - self._busy[0][0] > 60.0:
                self._busy.popleft()
        return ms

    # ------------------------------------------------------------ tallying

    def add_pair(self, cam: str, frame: np.ndarray, main: list, shadow: list, shadow_ms: float,
                 info: dict, wall: Optional[float] = None) -> dict:
        """Compare one frame's people from both models and add it to the tallies."""
        from app.services import low_light
        from app.services.frame_geometry import frame_sizes

        h, w = frame.shape[:2]
        mb = np.array([d.bbox for d in main], np.float32).reshape(-1, 4)
        sb = np.array([d.bbox for d in shadow], np.float32).reshape(-1, 4)
        pairs = match_boxes(mb, sb)
        mi = {i for i, _ in pairs}
        si = {j for _, j in pairs}
        only_main = [i for i in range(len(main)) if i not in mi]
        only_shadow = [j for j in range(len(shadow)) if j not in si]
        now = self._clock()
        minutes = float(settings.SHADOW_STATIC_MINUTES)
        with self._lock:
            static_main = self._static.observe((cam, "main"), mb, w, h, now, minutes)
            static_shadow = self._static.observe((cam, "shadow"), sb, w, h, now, minutes)
        light = (low_light.lighting_monitor.camera_status(cam) or {}).get("lighting") or "unknown"
        native = frame_sizes.native(cam)
        if native is None:
            scale = frame_sizes.downscale(cam)
            native = (int(round(w / scale)), int(round(h / scale)))
        res_class = resolution_class(native)
        main_model = info.get("model") or "unknown"

        def add(t: dict) -> None:
            t["pairs"] += 1
            t["pairs_with_people"] += int(bool(main or shadow))
            t["pairs_disagreeing"] += int(bool(only_main or only_shadow))
            t["persons_main"] += len(main)
            t["persons_shadow"] += len(shadow)
            t["matched"] += len(pairs)
            t["only_main"] += len(only_main)
            t["only_shadow"] += len(only_shadow)
            for d in main:
                t["persons_main_by_size"][size_bucket(d.y2 - d.y1)] += 1
                _kp_add(t["kp_main"], d.pose_keypoints if d.pose_keypoints is not None else d.keypoints)
                _kp_add(t["kp_main_delivered"], d.keypoints)
            for d in shadow:
                t["persons_shadow_by_size"][size_bucket(d.y2 - d.y1)] += 1
                _kp_add(t["kp_shadow"], d.keypoints)
            for i, j in pairs:
                t["matched_by_size"][size_bucket(main[i].y2 - main[i].y1)] += 1
                m = main[i]
                _kp_add(t["kp_matched_main"], m.pose_keypoints if m.pose_keypoints is not None else m.keypoints)
                _kp_add(t["kp_matched_shadow"], shadow[j].keypoints)
            for i in only_main:
                t["only_main_by_size"][size_bucket(main[i].y2 - main[i].y1)] += 1
            for j in only_shadow:
                t["only_shadow_by_size"][size_bucket(shadow[j].y2 - shadow[j].y1)] += 1
            t["static_main"] += static_main
            t["static_shadow"] += static_shadow
            if info.get("infer_ms") is not None:
                t["main_ms_sum"] += float(info["infer_ms"])
                t["main_ms_n"] += 1
            t["shadow_ms_sum"] += float(shadow_ms)
            t["shadow_ms_n"] += 1
            t["shadow_ms_max"] = max(t["shadow_ms_max"], float(shadow_ms))
            t["refiner_frames"] += int(float(info.get("refine_ms") or 0.0) > 0.0)

        with self._lock:
            add(self._totals)
            for dim, key in (("camera", cam), ("lighting", light), ("resolution", res_class),
                             ("main_model", main_model)):
                add(self._by[dim].setdefault(key, new_tally()))
            self.last_pair_at = wall if wall is not None else self._wall()
            self._dirty = True
        if only_main or only_shadow:
            self._maybe_sample(cam, frame, main, shadow, pairs, light, main_model)
        return {"matched": len(pairs), "only_main": len(only_main), "only_shadow": len(only_shadow),
                "static_main": static_main, "static_shadow": static_shadow, "lighting": light,
                "resolution": res_class}

    # ------------------------------------------------------------ samples

    def _maybe_sample(self, cam: str, frame: np.ndarray, main: list, shadow: list,
                      pairs: list[tuple[int, int]], light: str, main_model: str) -> Optional[Path]:
        now = self._wall()
        limit = max(0, int(settings.SHADOW_SAMPLES_MAX))
        with self._lock:
            if limit == 0 or now - self._last_sample.get(cam, 0.0) < float(settings.SHADOW_SAMPLE_EVERY_SEC):
                return None
            self._last_sample[cam] = now
        try:
            path = self._write_sample(cam, frame, main, shadow, pairs, light, main_model, now)
        except Exception as e:  # noqa: BLE001 - a review image is optional
            logger.warning(f"Shadow trial sample not written: {type(e).__name__}: {e}")
            return None
        if path is None:
            return None
        entry = {"name": path.name, "camera": cam, "at": now, "lighting": light, "main_model": main_model,
                 "only_main": len(main) - len(pairs), "only_shadow": len(shadow) - len(pairs),
                 "bytes": path.stat().st_size}
        with self._lock:
            self._samples.append(entry)
            doomed = self._samples[:-limit] if len(self._samples) > limit else []
            self._samples = self._samples[-limit:]
            self._dirty = True
        for old in doomed:
            self._unlink_sample(old["name"])
        try:
            from app.services.evidence_storage import evidence_storage

            evidence_storage.note_write(path)
            evidence_storage.enforce_kind_cap(DIR_NAME)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"evidence limit not notified: {e}")
        self._forget_missing_samples()
        return path

    def _write_sample(self, cam: str, frame: np.ndarray, main: list, shadow: list,
                      pairs: list[tuple[int, int]], light: str, main_model: str, now: float) -> Optional[Path]:
        import cv2

        from app.services.inference_backend import SKELETON_EDGES
        from app.services.privacy_mask import apply_privacy_masks

        d = self._ensure_dir()
        if d is None:
            return None
        base = apply_privacy_masks(frame, cam)
        matched_main = {i for i, _ in pairs}
        matched_shadow = {j for _, j in pairs}

        def draw(people: list, matched: set, label: str) -> np.ndarray:
            img = base.copy()
            h, w = img.shape[:2]
            thick = max(1, int(round(w / 640)))
            for n, p in enumerate(people):
                colour = (80, 200, 80) if n in matched else (0, 140, 255)
                cv2.rectangle(img, (int(p.x1), int(p.y1)), (int(p.x2), int(p.y2)), colour, thick + 1)
                k = p.pose_keypoints if getattr(p, "pose_keypoints", None) is not None else p.keypoints
                if k is not None:
                    for a, b in SKELETON_EDGES:
                        if k[a, 2] >= KP_VISIBLE and k[b, 2] >= KP_VISIBLE:
                            cv2.line(img, (int(k[a, 0]), int(k[a, 1])), (int(k[b, 0]), int(k[b, 1])),
                                     (255, 220, 0), thick)
            scale = SAMPLE_WIDTH / float(w)
            img = cv2.resize(img, (SAMPLE_WIDTH, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
            cv2.rectangle(img, (0, 0), (SAMPLE_WIDTH, 22), (0, 0, 0), -1)
            cv2.putText(img, label, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            return img

        left = draw(main, matched_main, f"live: {main_model}  ({len(main)} people)")
        right = draw(shadow, matched_shadow,
                     f"shadow: {self.model_path.name if self.model_path else '?'}  ({len(shadow)} people)")
        sheet = np.concatenate([left, right], axis=1)
        footer = np.zeros((20, sheet.shape[1], 3), np.uint8)
        cv2.putText(footer, f"{cam}  {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}  "
                            f"lighting {light}  green = found by both, orange = only this model",
                    (6, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
        sheet = np.concatenate([sheet, footer], axis=0)
        safe_cam = re.sub(r"[^A-Za-z0-9_-]", "_", cam)[:48] or "camera"
        path = d / f"{datetime.fromtimestamp(now).strftime('%Y%m%dT%H%M%S')}_{safe_cam}.jpg"
        ok, enc = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return None
        tmp = path.with_name(path.stem + ".temp.jpg")
        tmp.write_bytes(enc.tobytes())
        os.replace(tmp, path)
        return path

    def _ensure_dir(self) -> Optional[Path]:
        d = self.directory()
        root = Path(os.path.realpath(settings.STORAGE_DIR))
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Shadow trial directory unavailable: {e}")
            return None
        real = Path(os.path.realpath(d))
        if d.is_symlink() or root not in real.parents:
            logger.error(f"Shadow trial directory {d} resolves outside STORAGE_DIR; nothing written")
            return None
        return d

    def _unlink_sample(self, name: str) -> None:
        if not _SAMPLE_RE.match(name):
            return
        p = self.directory() / name
        try:
            if stat.S_ISREG(os.lstat(p).st_mode):
                os.unlink(p)
        except OSError:
            pass

    def _forget_missing_samples(self) -> None:
        """Samples the evidence limit deleted are dropped from the list too."""
        d = self.directory()
        with self._lock:
            kept = [s for s in self._samples if (d / s["name"]).is_file()]
            if len(kept) != len(self._samples):
                self._samples = kept
                self._dirty = True

    def sample_path(self, name: str) -> Optional[Path]:
        """The file of a listed sample (by name only; nothing outside the directory)."""
        if not _SAMPLE_RE.match(name or ""):
            return None
        with self._lock:
            listed = any(s["name"] == name for s in self._samples)
        p = self.directory() / name
        if not listed or p.is_symlink() or not p.is_file():
            return None
        return p

    # ------------------------------------------------------------ persistence

    def _trial_id(self) -> Optional[str]:
        if self.model_path is None:
            return None
        return f"{self.model_path.name}:{(self.model_sha256 or '')[:16]}"

    def save(self) -> None:
        with self._lock:
            if self._trial_id() is None or not self._dirty:
                return
            data = {"schema": 1, "trial_id": self._trial_id(), "model": self.model_path.name,
                    "model_sha256": self.model_sha256, "weights": self.weights,
                    "started_at": self.started_at, "last_pair_at": self.last_pair_at,
                    "totals": self._totals, "by": self._by, "samples": self._samples}
            self._dirty = False
            self._last_save = self._wall()
        d = self._ensure_dir()
        if d is None:
            return
        tmp = d / (STATS_NAME + ".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, d / STATS_NAME)
        except OSError as e:
            logger.warning(f"Shadow trial stats not saved: {e}")

    def _restore(self) -> None:
        """Resume the tallies of the same trial (same model file) after a restart."""
        p = self.directory() / STATS_NAME
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return
        if data.get("trial_id") != self._trial_id():
            logger.info("Shadow trial: previous stats are for another model; starting afresh")
            self._reset_dir(keep_dir=True)
            return
        with self._lock:
            base = new_tally()
            self._totals = {**base, **(data.get("totals") or {})}
            self._by = {dim: {k: {**base, **v} for k, v in (data.get("by", {}).get(dim) or {}).items()}
                        for dim in self._by}
            self._samples = list(data.get("samples") or [])
            self.started_at = data.get("started_at")
            self.last_pair_at = data.get("last_pair_at")
        self._forget_missing_samples()

    def _reset_dir(self, keep_dir: bool = False) -> None:
        d = self.directory()
        root = Path(os.path.realpath(settings.STORAGE_DIR))
        if not d.exists():
            return
        real = Path(os.path.realpath(d))
        if d.is_symlink() or root not in real.parents:
            logger.error(f"Shadow trial directory {d} resolves outside STORAGE_DIR; not deleted")
            return
        if keep_dir:
            for entry in d.iterdir():
                if entry.is_file() and not entry.is_symlink():
                    try:
                        entry.unlink()
                    except OSError:
                        pass
        else:
            shutil.rmtree(d, ignore_errors=True)
        with self._lock:
            self._samples = []

    def housekeep(self, force: bool = False) -> bool:
        """Delete the trial's files SHADOW_TRIAL_RETAIN_DAYS after its last pair. True if deleted."""
        now = self._wall()
        self._last_housekeep = now
        d = self.directory()
        if not d.exists():
            return False
        days = float(settings.SHADOW_TRIAL_RETAIN_DAYS)
        last = self.last_pair_at
        if last is None:
            try:
                data = json.loads((d / STATS_NAME).read_text())
                last = data.get("last_pair_at") or data.get("started_at")
            except (OSError, ValueError):
                last = None
        if last is None:
            try:
                last = max((e.stat().st_mtime for e in d.iterdir()), default=d.stat().st_mtime)
            except OSError:
                return False
        if days <= 0 or now - float(last) < days * 86400.0:
            return False
        logger.info(f"Shadow trial files deleted: last data {(now - float(last)) / 86400.0:.1f} days ago "
                    f"(SHADOW_TRIAL_RETAIN_DAYS={days:g})")
        self._reset_dir()
        with self._lock:
            self._totals = new_tally()
            self._by = {dim: {} for dim in self._by}
            self.started_at = self.last_pair_at = None
            self._dirty = False
        return True

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        det = self.detector
        with self._lock:
            counters = dict(self._counters)
            totals = tally_view(self._totals) if self._totals["pairs"] else None
            by = {dim: {k: tally_view(v) for k, v in sorted(vals.items())} for dim, vals in self._by.items()}
            samples = [dict(s, url=f"/api/v1/system/shadow-trial/samples/{s['name']}")
                       for s in reversed(self._samples)]
            busy = sum(ms for _, ms in self._busy)
            span = (self._busy[-1][0] - self._busy[0][0]) if len(self._busy) > 1 else 0.0
        window = (settings.SHADOW_TRIAL_WINDOW or "").strip()
        gate = getattr(det, "device_gate", None)
        return {
            "state": self.state,
            "reason": self.reason,
            "shadow_only": True,
            "model": self.model_path.name if self.model_path else None,
            "model_sha256": self.model_sha256,
            "weights": self.weights,
            "provider": self.provider,
            "live_model": getattr(det, "model_path", None).name if getattr(det, "model_path", None) else None,
            "share": self.share(),
            "cameras": sorted(self.cameras()) or "all",
            "window": window or None,
            "window_valid": (parse_window(window) is not None) if window else None,
            "in_window": self._window_open(),
            "started_at": self.started_at,
            "last_pair_at": self.last_pair_at,
            "retain_days": float(settings.SHADOW_TRIAL_RETAIN_DAYS),
            "scheduling": {
                **counters,
                # Device time the shadow model used over its last minute of runs.
                "measured_share": round(busy / (span * 1000.0), 3) if span >= 5.0 else None,
                "main_delayed_by_shadow": gate.low_priority_delays() if gate is not None else None,
            },
            "definitions": {
                "match": f"same person when box IoU >= {MATCH_IOU}",
                "people": "boxes kept by the live rules: shape/size gates, per-box threshold "
                          "(PERSON_CONF_THRESHOLD, darker boxes PERSON_CONF_THRESHOLD_DARK), AI_IGNORE regions",
                "box_height": "pixels in the analysed frame",
                "keypoint_gate": f">= {KP_GATE_MIN} joints with confidence >= {KP_VISIBLE}",
                "main_keypoints": "the live pose model's own keypoints; 'main_as_delivered' is after the "
                                  "keypoint refiner where it ran",
                "static_box": f"a detection on a box that stayed within {STATIC_TOLERANCE:.0%} of the frame for "
                              f"more than {float(settings.SHADOW_STATIC_MINUTES):g} min",
            },
            "totals": totals,
            "by_camera": by["camera"],
            "by_lighting": by["lighting"],
            "by_resolution": by["resolution"],
            "by_live_model": by["main_model"],
            "samples": samples,
            "samples_limit": {"files": int(settings.SHADOW_SAMPLES_MAX), "mb": float(settings.SHADOW_SAMPLES_MAX_MB),
                              "every_sec_per_camera": float(settings.SHADOW_SAMPLE_EVERY_SEC)},
        }


shadow_trial = ShadowTrial()
