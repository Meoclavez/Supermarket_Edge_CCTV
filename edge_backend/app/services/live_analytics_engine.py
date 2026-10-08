"""The live pipeline: frames in, persisted retail facts out.

This is the piece the system never had. Every subsystem around it was real --
the RTSP ingest worker, the DVR segmenter, the funnel maths, the decision
engine -- but nothing connected a camera to any of them, so every number on the
dashboard came from a literal in a route handler.

One worker thread per enabled camera does the following loop:

    capture frame -> (when inference_scheduler admits it: the camera's fair
      share of the accelerator, at most every Nth frame) pose-estimate
      people (box + 17 keypoints) -> ByteTrack association -> project foot point to floor
      metres -> resolve which zone that is -> emit zone entry/exit facts
      -> hand confirmed, moving person tracks (with skeletons) to
      pose_analytics -> mark zone visits that saw a shelf interaction
      -> tripwire crossings / restricted areas (tripwire_engine) -> persist

The most recent frame from each camera is also retained in memory, which is
what ``/snapshot`` and ``/stream`` now serve. Those endpoints previously drew a
synthetic bouncing box with OpenCV and labelled it live AI inference.

If a camera cannot be opened, its worker reports the camera OFFLINE and
produces nothing. No frames means no detections means no analytics -- the
emptiness propagates honestly all the way to the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from app.config import settings
from app.services import capture_backends
from app.services.camera_drivers import alternate_stream_url, redact_url
# Defined with the capture backends (which raise them too); imported here by name.
from app.services.capture_backends import CameraAuthError, CameraUnreachableError, Frame
from app.services.frame_geometry import frame_sizes
from app.services.inference_backend import (
    SKELETON_EDGES,
    keypoints_to_list,
    person_detector,
    person_threshold,
)
from app.services.inference_scheduler import inference_scheduler
from app.services.motion_gate import MotionGate
from app.services import motion_gate as motion_gate_mod
from app.services.shadow_trial import shadow_trial
from app.services import night_watch as nw
from app.services.privacy_mask import (
    apply_privacy_masks,
    camera_masks,
    deferred_privacy_masks,
    ignore_polygons,
    outside_ignore_regions,
)
from app.services.store_layout_service import point_in_polygon
from app.services.timeutil import utc_from_ts  # stored times are naive UTC
from app.services.tracking_service import ASSIGNMENT_METHOD, CentroidTracker, Track, floor_projector

logger = logging.getLogger(__name__)


# ------------------------------------------------------ pose analytics hook
# pose_analytics (theft / shelf interaction / heatmap) is optional: if the
# module is missing or raises, the pipeline keeps counting people and logs
# the problem once instead of once per frame.

_pose_state = {"module": None, "status": "not_loaded", "error": None}
_pose_state_lock = threading.Lock()
_logged_once: set[str] = set()


def _log_once(key: str, message: str) -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    logger.warning(message)


def _get_pose_analytics():
    if _pose_state["status"] == "not_loaded":
        with _pose_state_lock:
            if _pose_state["status"] == "not_loaded":
                try:
                    from app.services.pose_analytics import pose_analytics

                    _pose_state.update(module=pose_analytics, status="ok", error=None)
                except Exception as e:
                    _pose_state.update(module=None, status="unavailable", error=str(e))
                    _log_once(
                        "pose_import",
                        f"pose_analytics unavailable ({e}); people are still tracked and "
                        "counted, but shelf interactions and theft cues are not analysed.",
                    )
    return _pose_state["module"]


ANALYSIS_FLAGS = ("people_counting", "shelf_interaction", "theft_detection")


def camera_flag(camera_id: str, flag: str) -> bool:
    """Per-camera feature toggle. A broken flag store keeps the old behaviour (on)."""
    try:
        from app.services.feature_manager import feature_manager

        return bool(feature_manager.is_enabled(camera_id, flag))
    except Exception as e:
        _log_once(f"flag:{flag}", f"feature flag {flag} unreadable ({e}); treating it as enabled")
        return True


def camera_setting(camera_id: str, name: str):
    """Per-camera detector setting (feature_manager); None = the global default."""
    try:
        from app.services.feature_manager import feature_manager

        return feature_manager.get_setting(camera_id, name)
    except Exception as e:
        _log_once(f"setting:{name}", f"camera setting {name} unreadable ({e}); using the global default")
        return None


def static_figure_config(camera_id: str) -> tuple[bool, Optional[float]]:
    """(filter enabled, seconds before static or None = STATIC_FIGURE_SECONDS) for a camera.

    Read from the per-camera features (static_figure_filter /
    static_figure_seconds). A store that does not know these keys yet, or
    cannot be read, keeps the filter on with the server default.
    """
    from app.services.feature_manager import feature_manager

    try:
        enabled = bool(feature_manager.is_enabled(camera_id, "static_figure_filter"))
    except KeyError:
        enabled = True
    except Exception as e:  # noqa: BLE001
        _log_once("static_figure_filter", f"static-figure setting unreadable ({e}); filter stays on")
        enabled = True
    try:
        seconds = feature_manager.get_setting(camera_id, "static_figure_seconds")
    except Exception:  # noqa: BLE001 - unknown key or unreadable store: server default
        seconds = None
    try:
        seconds = None if seconds is None else float(seconds)
    except (TypeError, ValueError):
        seconds = None
    return enabled, seconds


def static_memory_path(camera_id: str):
    """Where a camera's remembered static figures are saved (boxes normalised 0..1)."""
    import re
    from pathlib import Path

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(camera_id))[:120] or "camera"
    return Path(settings.STORAGE_DIR) / "static_memory" / f"{safe}.json"


def _append_path_point(t, fx: float, fy: float, w: int, h: int, floor, now: float) -> None:
    """Sample the track's path for the recorded heatmaps (services/heatmap_history.py).

    At most one point per TRAJECTORY_SAMPLE_SEC and TRAJECTORY_MAX_POINTS per
    track: the normalised image foot point always (uncalibrated cameras still
    get image-space heatmaps), floor metres only when calibrated.
    """
    pts = t.path_points
    if len(pts) >= settings.TRAJECTORY_MAX_POINTS or w <= 0 or h <= 0:
        return
    if pts and now - float(pts[-1]["t"]) < settings.TRAJECTORY_SAMPLE_SEC:
        return
    p = {"u": round(min(max(fx / w, 0.0), 1.0), 4), "v": round(min(max(fy / h, 0.0), 1.0), 4),
         "t": round(now, 2)}
    if floor is not None:
        p["x"], p["y"] = round(floor[0], 2), round(floor[1], 2)
    pts.append(p)


def _reset_pose_camera(camera_id: str) -> None:
    pa = _get_pose_analytics()
    if pa is None:
        return
    try:
        pa.reset_camera(camera_id)
    except Exception as e:
        _log_once(f"pose_reset:{type(e).__name__}", f"pose_analytics.reset_camera failed: {e}")


@dataclass
class ZoneVisitFact:
    """A dwell, emitted twice: once when it becomes real, once when it ends.

    A visit is opened as soon as the person has stayed past the dwell floor,
    not when they leave. Waiting for the exit would make anyone currently
    standing in an aisle invisible to the dashboard, so "shoppers in store
    right now" could never be answered from the database.
    """

    visit_id: str
    phase: str                       # "open" | "interact" | "close"
    zone_id: str
    track_id: str
    camera_id: str
    entered_at: datetime
    exited_at: Optional[datetime] = None
    dwell_seconds: float = 0.0
    interacted: bool = False


@dataclass
class CameraRuntime:
    """Live state for one camera. Read by the API, written by the worker."""

    camera_id: str
    name: str
    source: str
    enabled: bool = True
    status: str = "STARTING"        # STARTING | ONLINE | OFFLINE | AUTH_FAILED | DISABLED
    last_error: Optional[str] = None
    # Wall-clock time of the next automatic connection attempt while the
    # camera is not delivering video (None while ONLINE / not yet failed).
    next_retry_at: Optional[float] = None
    frames_read: int = 0
    detections_last: int = 0
    # People-confidence detections dropped by AI_IGNORE areas on the last
    # analysed frame, and static figures this camera's tracker remembers.
    ignored_detections_last: int = 0
    static_memory_count: int = 0
    live_track_count: int = 0
    fps: float = 0.0
    last_frame_at: float = 0.0
    connected_at: Optional[float] = None
    # Pixel size of the frames being read (native, or DECODE_MAX_WIDTH-capped
    # on the GPU path); None until the first frame.
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    # What the current connection really decodes with (capture_backends:
    # "cuda" / "vaapi" / "software"), None while not streaming, and why when
    # it is software.
    decoder: Optional[str] = None
    decoder_note: Optional[str] = None
    # Per-camera feature toggles as last read by the worker.
    analysis_flags: dict = field(default_factory=dict)
    # The newest delivered frame (capture_backends.Frame: NV12 from the GPU,
    # converted to BGR only when something reads it).
    _frame: Optional[Frame] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Live tracks as plain dicts, rewritten by the worker after every analysis
    # pass and read by the API / stream overlay. Never mutated in place.
    _tracks: list[dict] = field(default_factory=list, repr=False)
    _tracks_at: float = 0.0
    # Live overlay (/api/v1/live/tracks): analysed-frame counter, wall time and
    # pixel size of the latest analysed frame, monotonic times of recent
    # analysed frames (measured analysis rate) and per-track centre velocity
    # state {track_id: (t, cx, cy, vx, vy)} in pixels per second.
    analysis_seq: int = 0
    analysed_at: Optional[float] = None
    analysed_size: Optional[tuple] = None
    _analysed_mono: deque = field(default_factory=lambda: deque(maxlen=64), repr=False)
    _velocity: dict = field(default_factory=dict, repr=False)

    def put_frame(self, frame) -> None:
        """Keep ``frame`` (a Frame, or a BGR array) as the newest one."""
        frame = Frame.of(frame)
        with self._lock:
            self._frame = frame
            self.frame_width, self.frame_height = frame.width, frame.height

    def get_frame(self, masked: bool = True) -> Optional[np.ndarray]:
        """A BGR copy of the latest frame, with privacy masks burned in.

        Every caller that shows or saves a frame gets the masked copy. Only
        analysis code asks for ``masked=False`` (see ``privacy_mask``).
        """
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        # Converted from NV12 here if the worker never needed this frame
        # itself; either way an array the caller owns.
        frame = frame.bgr_copy()
        if not masked:
            return frame
        return apply_privacy_masks(frame, self.camera_id, inplace=True)

    def get_raw_frame(self) -> Optional[np.ndarray]:
        """Unmasked frame, for inference only. Never serve or store this."""
        return self.get_frame(masked=False)

    def set_tracks(self, tracks: list[dict], now: Optional[float] = None,
                   frame_size: Optional[tuple] = None) -> None:
        """Publish the worker's track snapshot (replaced whole, never mutated).

        ``frame_size`` (width, height) marks the snapshot of an analysed frame:
        it advances ``analysis_seq``, stamps ``analysed_at``, feeds the measured
        analysis rate and gives each entry ``vel_px`` (see ``_with_velocity``).
        Clearing calls (camera offline, analysis off) pass no frame size.
        """
        tracks = list(tracks)
        stamp = now or time.time()
        if frame_size is not None:
            tracks = self._with_velocity(tracks, stamp)
        with self._lock:
            self._tracks = tracks
            self._tracks_at = stamp
            if frame_size is not None:
                self.analysis_seq += 1
                self.analysed_at = stamp
                self.analysed_size = (int(frame_size[0]), int(frame_size[1]))
                self._analysed_mono.append(time.monotonic())

    # EMA weight of the newest centre displacement, and the longest gap
    # between two matches still turned into a velocity (longer: unknown).
    VELOCITY_ALPHA = 0.5
    VELOCITY_MAX_GAP_SEC = 2.0

    def _with_velocity(self, tracks: list[dict], now: float) -> list[dict]:
        """Entries copied with ``vel_px`` = [vx, vy] box-centre pixels/second, or None.

        Measured from consecutive *matched* boxes (time of each match), EMA
        smoothed; a coasting entry carries its track's last velocity. Runs on
        the worker thread only (the only writer of ``_velocity``).
        """
        out: list[dict] = []
        seen = set()
        a = self.VELOCITY_ALPHA
        for e in tracks:
            tid = e.get("track_id") if isinstance(e, dict) else None
            if tid is None or not all(k in e for k in ("x1", "y1", "x2", "y2")):
                out.append(e)
                continue
            seen.add(tid)
            prev = self._velocity.get(tid)
            vel = prev[3] if prev is not None else None
            if e.get("fresh", True):
                t = float(e.get("last_seen") or now)
                cx, cy = (e["x1"] + e["x2"]) / 2.0, (e["y1"] + e["y2"]) / 2.0
                if prev is None:
                    self._velocity[tid] = (t, cx, cy, None)
                elif t > prev[0]:
                    dt = t - prev[0]
                    if dt <= self.VELOCITY_MAX_GAP_SEC:
                        inst = ((cx - prev[1]) / dt, (cy - prev[2]) / dt)
                        vel = inst if vel is None else (a * inst[0] + (1 - a) * vel[0],
                                                         a * inst[1] + (1 - a) * vel[1])
                    else:
                        vel = None
                    self._velocity[tid] = (t, cx, cy, vel)
            out.append({**e, "vel_px": None if vel is None else [round(vel[0], 2), round(vel[1], 2)]})
        for tid in [k for k in self._velocity if k not in seen]:
            del self._velocity[tid]
        return out

    def live_meta(self) -> dict:
        """Analysed-frame counter, time, pixel size and measured analysis rate (copied under the lock)."""
        with self._lock:
            times = list(self._analysed_mono)
            seq, at, size = self.analysis_seq, self.analysed_at, self.analysed_size
        fps = None
        mono = time.monotonic()
        recent = [t for t in times if mono - t <= 15.0]
        if len(recent) >= 2 and recent[-1] > recent[0]:
            fps = round((len(recent) - 1) / (recent[-1] - recent[0]), 2)
        return {"seq": seq, "analysed_at": at, "size": size, "analysis_fps": fps}

    def get_tracks(self) -> list[dict]:
        with self._lock:
            return list(self._tracks)

    @property
    def calibrated(self) -> bool:
        return floor_projector.has_homography(self.camera_id)

    def motion_summary(self) -> dict:
        """Static-figure telemetry: counts by motion state and normalised boxes.

        ``static_tracks`` / ``pending_tracks`` count confirmed tracks only
        (as ``live_tracks`` does); ``boxes`` lists every live track, each
        ``{track_id, box: [x1, y1, x2, y2] normalised 0..1 to the analysed
        frame, motion_state: "pending" | "moving" | "static", confidence,
        confirmed, pending_static}`` (``pending_static``: on a remembered
        static figure or mostly inside an ignore area, not counted yet).
        """
        tracks = self.get_tracks()
        w, h = self.frame_width, self.frame_height
        boxes = []
        if w and h:
            for t in tracks:
                boxes.append({
                    "track_id": t["track_id"],
                    "box": [round(min(max(t["x1"] / w, 0.0), 1.0), 4), round(min(max(t["y1"] / h, 0.0), 1.0), 4),
                            round(min(max(t["x2"] / w, 0.0), 1.0), 4), round(min(max(t["y2"] / h, 0.0), 1.0), 4)],
                    "motion_state": t.get("motion_state") or "pending",
                    "confidence": t["confidence"],
                    "confirmed": bool(t.get("confirmed")),
                    "pending_static": bool(t.get("pending_static")),
                })
        conf = [t for t in tracks if t.get("confirmed")]
        return {
            "static_tracks": sum(1 for t in conf if t.get("motion_state") == "static"),
            "pending_tracks": sum(1 for t in conf if t.get("motion_state", "pending") == "pending"),
            "ignored_detections_last": int(self.ignored_detections_last),
            "static_memory_count": int(self.static_memory_count),
            "boxes": boxes,
        }

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "name": self.name,
            "status": self.status,
            "enabled": self.enabled,
            "frames_read": self.frames_read,
            "detections_last_frame": self.detections_last,
            "live_tracks": self.live_track_count,
            # static_tracks, pending_tracks, ignored_detections_last,
            # static_memory_count, boxes (motion_summary).
            **self.motion_summary(),
            "fps": round(self.fps, 1),
            "has_frame": self._frame is not None,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "decoder": self.decoder,
            "decoder_note": self.decoder_note,
            "calibrated": self.calibrated,
            "analysis_flags": dict(self.analysis_flags),
            "seconds_since_frame": (
                round(time.time() - self.last_frame_at, 1) if self.last_frame_at else None
            ),
            "last_error": self.last_error,
            "next_retry_in_s": (
                max(0, round(self.next_retry_at - time.time())) if self.next_retry_at else None
            ),
            # How often this camera is really analysed (inference_scheduler).
            "analysis_rate": inference_scheduler.camera_status(self.camera_id),
            # Measured lighting (day | mixed | low_light | ir, with hysteresis);
            # None until the camera's first analysed frame.
            "lighting": _lighting_status(self.camera_id),
            # disarmed | armed_idle | motion | person_confirmed | cooldown | unavailable,
            # with the next arm / disarm time in store time.
            "night_watch": _night_watch_status(self.camera_id, self.status == "ONLINE"),
            # Which recorder stream this camera is analysed from, its size and
            # why (Stream quality: auto | sub | main; services/stream_selection.py).
            "stream_selection": _stream_selection_status(self.camera_id),
            # Duplicate camera guard: set when this camera is the same physical
            # camera as another one and is excluded from store totals.
            **_duplicate_status(self.camera_id),
        }


def _duplicate_status(camera_id: str) -> dict:
    """duplicate_group / duplicate_of (services/duplicate_cameras.py); empty values if unavailable."""
    try:
        from app.services.duplicate_cameras import duplicate_guard

        f = duplicate_guard.camera_fields(camera_id)
        return {"duplicate_group": f["duplicate_group"], "duplicate_of": f["duplicate_of"]}
    except Exception:  # noqa: BLE001 - status must never fail over this
        return {"duplicate_group": None, "duplicate_of": None}


def _stream_selection_status(camera_id: str) -> Optional[dict]:
    try:
        from app.services.stream_selection import stream_selection

        return stream_selection.status(camera_id)
    except Exception:  # noqa: BLE001 - status must answer regardless
        return None


def _lighting_status(camera_id: str) -> Optional[dict]:
    try:
        from app.services.low_light import lighting_monitor

        return lighting_monitor.camera_status(camera_id)
    except Exception:  # noqa: BLE001 - status must answer regardless
        return None


def _night_watch_status(camera_id: str, streaming: bool) -> Optional[dict]:
    try:
        return nw.night_watch.camera_status(camera_id, streaming=streaming)
    except Exception as e:  # noqa: BLE001 - status must answer regardless
        return {"state": "unknown", "error": f"{type(e).__name__}: {e}"}


def _drop_clip_buffer(camera_id: str) -> None:
    """Free a camera's pre-event frame buffer (it holds seconds of video in RAM)."""
    try:
        from app.services.clip_recorder import clip_recorder_service

        with clip_recorder_service._service_lock:
            clip_recorder_service.buffers.pop(camera_id, None)
    except Exception:  # noqa: BLE001 - nothing to free
        pass


# Reconnect policy. A camera that is unreachable is retried with exponential
# backoff up to CAMERA_RETRY_MAX_SEC. A camera that REJECTS the login is not:
# Dahua and most NVR/IP camera firmware lock the account after ~5 failed
# logins, so retrying every 30 s kept the account locked forever (for the
# operator too). After an auth failure the worker retries once after
# AUTH_QUICK_RETRY_SEC, then only every AUTH_RETRY_SEC, and immediately when the
# operator changes the credentials/URL (new worker) or presses Reconnect.
CAMERA_RETRY_MAX_SEC = 30.0
AUTH_QUICK_RETRIES = 1
AUTH_QUICK_RETRY_SEC = 60.0
AUTH_RETRY_SEC = 1800.0
AUTH_FAILED_HINT = ("Check the username and password in this camera's settings. "
                    "Automatic retries are paused so the camera does not lock the account.")


_FFMPEG_OPTIONS: Optional[str] = None


def ffmpeg_capture_options() -> str:
    """OPENCV_FFMPEG_CAPTURE_OPTIONS with a socket timeout the linked FFmpeg understands.

    FFmpeg >= 5 (libavformat 59) takes ``timeout`` (microseconds) for RTSP/TCP
    I/O and dropped ``stimeout``; before 5, ``timeout`` on RTSP meant "listen
    for an incoming connection", so older builds keep ``stimeout``.
    ``rw_timeout`` bounds every other protocol read (HTTP/MJPEG). The same
    string is used for every open, so concurrent workers never disagree.

    The decoder thread count is deliberately not in here: OpenCV passes this
    string to the demuxer only, and a ``threads;N`` entry was measured to have
    no effect (OpenCV 5.0: still ~31 threads per stream on 16 CPUs). It is set
    per capture with ``CAP_PROP_N_THREADS`` instead (``decode_threads_for``).
    """
    global _FFMPEG_OPTIONS
    if _FFMPEG_OPTIONS is None:
        import re

        import cv2

        us = int(max(0.5, settings.CAMERA_READ_TIMEOUT_SEC) * 1_000_000)
        m = re.search(r"avformat:\s+YES \((\d+)\.", cv2.getBuildInformation())
        modern = m is None or int(m.group(1)) >= 59
        sock = f"timeout;{us}" if modern else f"stimeout;{us}"
        _FFMPEG_OPTIONS = f"rtsp_transport;tcp|{sock}|rw_timeout;{us}"
    return _FFMPEG_OPTIONS


_opencv_threads: Optional[int] = None


def configure_opencv_threads() -> Optional[int]:
    """Size OpenCV's own thread pool (``OPENCV_THREADS``), once per process.

    OpenCV parallelises resize / colour conversion / blur over a pool of one
    thread per CPU by default, and its workers spin while waiting for work.
    With one camera worker thread per camera the calls already run in
    parallel, so the pool only added wake-ups and spinning: measured on the
    store box, 8 test cameras cost 188 % of a core with the default pool and
    97 % with 1 thread (same frames, same analysed rate). The pool threads
    also inherit the name of the camera thread that first used OpenCV, which
    is why the live service showed "cam-cam_b4fbe4d" at 35 %: 16 pool threads,
    not that camera. 0 = OpenCV's default.
    """
    global _opencv_threads
    if _opencv_threads is None:
        n = max(0, int(settings.OPENCV_THREADS))
        try:
            import cv2

            if n > 0:
                cv2.setNumThreads(n)
            _opencv_threads = int(cv2.getNumThreads())
        except Exception as e:  # noqa: BLE001 - OpenCV missing: nothing to size
            logger.debug(f"OpenCV thread pool not configured: {e}")
            _opencv_threads = 0
    return _opencv_threads


# Decode threads by stream size (pixels), smallest first; above the last: 4.
_DECODE_THREAD_STEPS = ((1280 * 720, 1), (1920 * 1080, 2))
# Used for a first open, before the stream's size is known.
DECODE_THREADS_UNKNOWN = 2


def decode_threads_for(width: Optional[int], height: Optional[int]) -> int:
    """FFmpeg (CPU) decode threads for one stream.

    ``CAMERA_DECODE_THREADS`` > 0 is taken as is. Otherwise by resolution: 1 up
    to 720p (a 352x288 or 720p H.264 stream decodes at hundreds of fps on one
    thread), 2 up to 1080p, 4 above (4K: 72 fps on 1 thread, 133 fps on 4 on a
    Ryzen 7 7435HS). OpenCV's own default is one per CPU, ~31 threads per
    stream on a 16-CPU box, which is how 33 cameras became 1143 threads.
    """
    fixed = int(settings.CAMERA_DECODE_THREADS)
    if fixed > 0:
        return fixed
    if not width or not height:
        return DECODE_THREADS_UNKNOWN
    pixels = int(width) * int(height)
    for limit, threads in _DECODE_THREAD_STEPS:
        if pixels <= limit:
            return threads
    return 4


# Last known frame size per source URL, so a reconnect opens with the right
# thread count straight away.
_source_sizes: dict[str, tuple[int, int]] = {}


def is_file_source(src: Optional[str]) -> bool:
    """A local video file ("Local video file (testing)"), not a URL or a /dev/video device."""
    import os

    if not src or "://" in src or src.startswith("/dev/"):
        return False
    try:
        return os.path.isfile(src)
    except (OSError, ValueError):
        return False


def file_frame_interval(cap) -> float:
    """Seconds between frames of a video file at its own rate (0: rate unknown)."""
    import cv2

    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0
    return 1.0 / fps if 1.0 <= fps <= 120.0 else 0.0


def rewind_file(cap) -> bool:
    """Back to the first frame of a video file capture (it plays in a loop)."""
    import cv2

    try:
        return bool(cap.set(cv2.CAP_PROP_POS_FRAMES, 0))
    except Exception:  # noqa: BLE001
        return False


def _resolve_host_bounded(url: str, timeout_s: float) -> None:
    """Raise if the URL's host cannot be resolved within ``timeout_s`` (IP literals pass)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    result: dict = {}

    def lookup():
        try:
            socket.getaddrinfo(host, None)
            result["ok"] = True
        except OSError as e:
            result["error"] = e

    t = threading.Thread(target=lookup, daemon=True, name="dns-probe")
    t.start()
    t.join(max(0.5, timeout_s))
    if not result.get("ok"):
        raise RuntimeError(f"Cannot resolve camera host {host!r}"
                           + (f": {result['error']}" if "error" in result else " (DNS timed out)"))


class CameraWorker(threading.Thread):
    """Captures and analyses one camera."""

    def __init__(self, runtime: CameraRuntime, engine: "LiveAnalyticsEngine"):
        super().__init__(daemon=True, name=f"cam-{runtime.camera_id}")
        self.rt = runtime
        self.engine = engine
        self.tracker = CentroidTracker(runtime.camera_id)
        self._stop = threading.Event()
        # Set by wake() (operator pressed Reconnect) to cut a retry wait short.
        self._wake = threading.Event()
        self._auth_failures = 0
        self._frame_index = 0
        # FFmpeg decode threads the current capture was opened with.
        self._decode_threads: Optional[int] = None
        # The open capture, so stop() can end a GPU capture's ffmpeg at once.
        self._cap = None
        # Set when GPU decoding failed for this camera's stream: the worker
        # then decodes in software until it is restarted (new credentials or
        # URL, camera off/on, service restart).
        self._hw_disabled: Optional[str] = None
        self._hw_no_frames = 0
        # Where the current capture decodes and why (capture_backends.choose_decoder),
        # and whether this worker already moved its camera to the other decoder
        # after measuring the stream (at most once per worker).
        self._choice: Optional[capture_backends.DecoderChoice] = None
        self._decoder_switched = False
        # Every how many delivered frames one goes to the clip ring (_clip_every).
        self._clip_n = max(1, int(settings.ANALYTICS_DETECT_EVERY_N_FRAMES))
        # Every how many delivered frames one may be analysed (_clip_every).
        self._detect_n = self._clip_n
        # Night watch armed on this camera (the normal analysis is paused).
        self._night_armed = False
        # Cheap per-frame motion check that wakes a quiet camera
        # (services/motion_gate.py, ANALYTICS_MOTION_WAKE).
        self._motion_gate = MotionGate()
        self._motion_gate_on = False

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        # A GPU capture can be ended from here (its ffmpeg is killed and a
        # blocked read returns); an OpenCV capture is released by the worker.
        # On a helper thread, so stop_all() ends every camera's ffmpeg at once
        # instead of one after another.
        cap = self._cap
        if cap is not None and getattr(cap, "interruptible", False):
            threading.Thread(target=cap.release, daemon=True, name=f"stop-{self.rt.camera_id}").start()

    def wake(self) -> None:
        """Retry the connection now, even while paused after an auth failure."""
        self._auth_failures = 0
        self._wake.set()

    def _sleep(self, seconds: float) -> bool:
        """Wait up to ``seconds``; return True if the worker is stopping."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._wake.wait(min(remaining, 1.0)):
                self._wake.clear()
                return self._stop.is_set()
        return True

    def _preflight(self, src: str) -> None:
        """RTSP handshake before handing the URL to OpenCV.

        FFmpeg reports a rejected login only on stderr ("401 Unauthorized") and
        OpenCV then just fails to open, which looked identical to a network
        fault. This answers the question first, at the cost of at most one
        login attempt. Only certain answers change behaviour: an auth
        rejection raises CameraAuthError, a refused/unroutable address raises
        CameraUnreachableError; anything else falls through to OpenCV.
        """
        if not src.lower().startswith("rtsp://"):
            return
        from app.services import rtsp_probe

        # The probe's own DNS lookup would be unbounded; fail fast here instead.
        _resolve_host_bounded(src, settings.CAMERA_OPEN_TIMEOUT_SEC)
        res = rtsp_probe.probe_rtsp(src, timeout_s=min(5.0, max(1.0, settings.CAMERA_OPEN_TIMEOUT_SEC)))
        if res.outcome == rtsp_probe.AUTH_FAILED:
            raise CameraAuthError(f"camera rejected the username/password (RTSP {res.status_code})")
        if res.outcome == rtsp_probe.AUTH_REQUIRED:
            raise CameraAuthError("camera requires a username/password, but none is saved for it")
        if res.outcome == rtsp_probe.UNREACHABLE:
            raise CameraUnreachableError(res.detail)

    # ------------------------------------------------------------------ capture

    def _open(self, source_url: Optional[str] = None):
        """Open the camera on the GPU decoder where that is cheaper, else with OpenCV.

        The decoder is chosen per open (capture_backends.choose_decoder): an
        RTSP camera goes to the probed GPU backend when its last measured
        stream size is at least DECODE_GPU_MIN_PIXELS (every RTSP camera with
        DECODE_BACKEND=cuda/vaapi), unless GPU decoding already failed for
        this camera; everything else, and every fallback, is the OpenCV path.
        """
        src = source_url or self.rt.source
        self._choice = None
        probe = None
        if self._hw_disabled is None and capture_backends.hw_eligible(src):
            probe = capture_backends.decode_probe()
            if probe.backend == capture_backends.SOFTWARE:
                probe = None
        if probe is not None:
            self._choice = capture_backends.choose_decoder(probe, self._known_size(src))
            if self._choice.use_gpu:
                cap = self._open_hw(src, probe)
                if cap is not None:
                    return cap
                self._choice = None          # GPU failed for this stream: _hw_disabled says why
        return self._open_software(src)

    def _known_size(self, src: str) -> Optional[tuple[int, int]]:
        """The stream's last measured native size: this process, else the saved one."""
        size = _source_sizes.get(src)
        if size is None:
            size = capture_backends.stream_sizes.get(self.rt.camera_id, src)
            if size is not None:
                _source_sizes[src] = size
        return size

    def _open_hw(self, src: str, probe):
        """A GPU capture; None means "decode this camera in software instead".

        A rejected login or an unreachable address raises (the software path
        would only fail the same way, costing the camera another login). A
        failure of the GPU half of the chain disables GPU decoding for this
        camera at once; a stream that opens but yields no frame does so after
        two attempts in a row. Anything else returns the closed capture, so
        the caller tries the alternate stream and reports the reason.
        """
        # FFmpeg's DNS lookup ignores its timeouts; resolve first, bounded.
        _resolve_host_bounded(src, settings.CAMERA_OPEN_TIMEOUT_SEC)
        cap = capture_backends.open_hw_capture(
            src, probe, cancel=self._stop, max_width=self._max_width(),
            normalise=capture_backends.stream_sizes.needs_normalise(self.rt.camera_id, src))
        if cap.isOpened():
            self._hw_no_frames = 0
            return cap
        detail = cap.error_summary or cap.failure
        if cap.failure == capture_backends.AUTH:
            raise CameraAuthError(f"camera rejected the username/password ({detail})")
        if cap.failure == capture_backends.UNREACHABLE:
            raise CameraUnreachableError(detail)
        if self._stop.is_set():
            return cap
        if cap.failure == capture_backends.NO_FRAMES:
            self._hw_no_frames += 1
        if cap.failure == capture_backends.DECODE or self._hw_no_frames >= 2:
            self._hw_disabled = f"{probe.label} decoding failed for this stream ({detail})"
            logger.warning(f"Camera {self.rt.camera_id}: {self._hw_disabled}; decoding it in software "
                           "until the camera is restarted")
            return None
        return cap

    def _max_width(self) -> int:
        """Width limit for this camera's GPU-decoded frames (0 = native size).

        The camera's ``decode_max_width``, else DECODE_MAX_WIDTH; "auto"
        follows the pose model: 1920 for a wide-input (544x960) model, native
        for a 640x640 one, which finds fewer far people on scaled frames.
        """
        value = camera_setting(self.rt.camera_id, "decode_max_width")
        if value is not None:
            return max(0, int(value))
        fixed = capture_backends.max_width_setting()
        if fixed is not None:
            return fixed
        spec = getattr(person_detector, "spec", None)
        width = spec.input_size[0] if spec is not None and getattr(spec, "input_size", None) else None
        return capture_backends.auto_max_width(width)

    def _width_changed(self, cap) -> bool:
        """A GPU capture whose delivered width no longer matches ``_max_width()``
        (the pose model changed class, or the setting was edited)."""
        if not isinstance(cap, capture_backends.FfmpegHwCapture) or not cap.source_size:
            return False
        native = int(cap.source_size[0])

        def width(limit: int) -> int:
            return min(native, limit) if limit and limit > 0 else native

        return width(self._max_width()) != width(getattr(cap, "max_width", 0))

    def _fps_changed(self, cap, source: str) -> bool:
        """A capture opened with another DECODE_MAX_FPS than the current one (site setting changed)."""
        want = max(0.0, float(settings.DECODE_MAX_FPS or 0))
        if isinstance(cap, (capture_backends.ThinnedCapture, capture_backends.FfmpegHwCapture)):
            return abs(float(cap.max_fps or 0) - want) > 1e-6
        try:
            import cv2

            # An RTSP capture opened unthinned (DECODE_MAX_FPS was 0).
            return want > 0 and isinstance(cap, cv2.VideoCapture) and capture_backends.hw_eligible(source)
        except Exception:  # noqa: BLE001
            return False

    def _rate_settings(self) -> None:
        """Push this camera's analysis_priority / max_analysis_fps and AI_IGNORE areas to
        the scheduler and the motion gate (on connect and every 2 s)."""
        cam = self.rt.camera_id
        try:
            inference_scheduler.configure(cam, camera_setting(cam, "analysis_priority"),
                                          camera_setting(cam, "max_analysis_fps"))
        except Exception as e:  # noqa: BLE001 - the defaults apply
            _log_once(f"rate_settings:{type(e).__name__}", f"analysis rate settings unreadable ({e})")
        if settings.ANALYTICS_MOTION_WAKE:
            try:
                self._motion_gate.set_ignore(
                    ignore_polygons(cam, motion_gate_mod.WIDTH, motion_gate_mod.HEIGHT))
            except Exception as e:  # noqa: BLE001 - motion anywhere counts
                _log_once(f"motion_ignore:{type(e).__name__}", f"motion gate ignore areas unreadable ({e})")

    def _people_in_view(self) -> int:
        """Tracks that may be people (tentative included, static figures not)."""
        return sum(1 for t in list(self.tracker.tracks.values())
                   if not t.is_static and not t.is_pending_static)

    def _motion_step(self, frame, now: float) -> None:
        """Motion gate on this decoded frame; motion keeps / makes the camera active."""
        on = bool(settings.ANALYTICS_MOTION_WAKE) and (not self.rt.analysis_flags
                                                        or any(self.rt.analysis_flags.values()))
        if on != self._motion_gate_on:
            self._motion_gate_on = on
            self._motion_gate.reset()
        if not on:
            return
        try:
            if self._motion_gate.update(frame, now):
                inference_scheduler.motion(self.rt.camera_id)
        except Exception as e:  # noqa: BLE001 - never take the capture loop down
            _log_once(f"motion_gate:{type(e).__name__}",
                      f"motion gate failed on {self.rt.camera_id} ({type(e).__name__}: {e}); "
                      "quiet cameras wake only on their idle-rate analysis")

    def _open_software(self, src: str):
        import cv2

        # A bare /dev/videoN path and a network URL both go through
        # VideoCapture. Network streams get a latency-bounded transport and
        # real open/read timeouts: without them one unreachable camera held
        # FFmpeg for ~30 s per attempt (current FFmpeg ignores the old
        # ``stimeout`` option), stalling that worker's reconnects.
        if src.startswith(("rtsp://", "rtsps://", "http://", "https://")):
            import os

            # OpenCV serialises FFmpeg opens behind one process-wide lock and
            # FFmpeg's DNS lookup ignores its timeouts, so a camera whose
            # hostname does not resolve would hold every other worker's open.
            # Resolve first, bounded, outside that lock.
            _resolve_host_bounded(src, settings.CAMERA_OPEN_TIMEOUT_SEC)
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = ffmpeg_capture_options()
            open_ms = int(max(0.5, settings.CAMERA_OPEN_TIMEOUT_SEC) * 1000)
            read_ms = int(max(0.5, settings.CAMERA_READ_TIMEOUT_SEC) * 1000)
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, read_ms]
            # Software decoding: only its thread count is bounded (a
            # per-capture open parameter, so concurrent opens cannot race).
            n_threads = getattr(cv2, "CAP_PROP_N_THREADS", None)
            if n_threads is not None:
                self._decode_threads = decode_threads_for(*_source_sizes.get(src, (None, None)))
                params += [n_threads, self._decode_threads]
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, params)
        else:
            cap = cv2.VideoCapture(src)

        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # An RTSP camera delivers at most DECODE_MAX_FPS frames, like the GPU
        # path: the others are decoded but never converted to BGR.
        if capture_backends.hw_eligible(src) and settings.DECODE_MAX_FPS > 0:
            cap = capture_backends.ThinnedCapture(cap, settings.DECODE_MAX_FPS)
        return cap

    def _software_reason(self, source: str) -> str:
        if self._hw_disabled:
            return self._hw_disabled
        if not capture_backends.hw_eligible(source):
            return "only RTSP cameras are decoded on the GPU"
        if self._choice is not None and not self._choice.use_gpu:
            return self._choice.reason
        probe = capture_backends.cached_probe()
        return probe.reason if probe is not None else "GPU decoding not probed"

    def _report_decoder(self, cap, source: str) -> None:
        """What this connection really decodes with, and why, for status and health."""
        if isinstance(cap, capture_backends.FfmpegHwCapture):
            self.rt.decoder = cap.decoder
            self.rt.decoder_note = self._choice.reason if self._choice is not None else None
        else:
            self.rt.decoder = capture_backends.SOFTWARE
            self.rt.decoder_note = self._software_reason(source)

    def _attach_static_store(self) -> None:
        """Load this camera's remembered static figures (STORAGE_DIR/static_memory) and save there."""
        if not settings.STATIC_MEMORY_PERSIST:
            return
        try:
            n = self.tracker.attach_static_store(static_memory_path(self.rt.camera_id))
            self.rt.static_memory_count = n
        except Exception as e:  # noqa: BLE001 - the camera runs without it
            _log_once(f"static_store:{self.rt.camera_id}",
                      f"static-figure memory for {self.rt.camera_id} unavailable ({type(e).__name__}: {e})")

    def run(self) -> None:
        import cv2

        self._attach_static_store()
        backoff = 1.0
        while not self._stop.is_set():
            cap = None
            auth_paused = False
            reopen = False
            wait = backoff
            try:
                try:
                    self._preflight(self.rt.source)
                except CameraAuthError:
                    # The handshake is our own RTSP client; FFmpeg is what
                    # actually streams. Before pausing a camera for a bad
                    # password, let FFmpeg try once, so a quirk in the probe can
                    # never block a camera that works (at most one more failed
                    # login per attempt, still well under a lockout).
                    try:
                        cap = self._open(self.rt.source)
                    except CameraAuthError:
                        cap = None   # ffmpeg was refused too: report the probe's clearer reason
                    if not cap or not cap.isOpened():
                        if cap is not None:
                            try:
                                cap.release()
                            except Exception:
                                pass
                        cap = None
                        raise
                    logger.warning(f"Camera {self.rt.camera_id}: the RTSP login check reported a rejected "
                                   f"login, but the stream opened with the same credentials; streaming")
                self._auth_failures = 0
                if cap is None:
                    cap = self._open(self.rt.source)
                active_source = self.rt.source

                # Automatic fallback: if primary stream fails, try alternate quality (e.g. subtype=1 <-> subtype=0)
                if not cap or not cap.isOpened():
                    alt = alternate_stream_url(self.rt.source)
                    if alt:
                        logger.info(
                            f"Camera {self.rt.camera_id} primary stream failed. "
                            f"Attempting fallback to alternate stream: {redact_url(alt)}"
                        )
                        if cap is not None:
                            try:
                                cap.release()
                            except Exception:
                                pass
                        cap = self._open(alt)
                        if cap and cap.isOpened():
                            active_source = alt

                if not cap or not cap.isOpened():
                    why = getattr(cap, "error_summary", "") if cap is not None else ""
                    raise RuntimeError(f"Cannot open RTSP source {redact_url(self.rt.source)}"
                                       + (f" ({why})" if why else ""))

                self._cap = cap
                self._report_decoder(cap, active_source)
                self.rt.status = "ONLINE"
                self.rt.last_error = None
                self.rt.next_retry_at = None
                self.rt.connected_at = time.time()
                backoff = 1.0
                logger.info(f"Camera {self.rt.camera_id} connected (source: {redact_url(active_source)}, "
                            f"decoding: {capture_backends.BACKEND_LABELS[self.rt.decoder]})")

                last_tick = time.time()
                frames_in_window = 0
                sized = False
                self._clip_n = self._clip_every(cap)
                self._rate_settings()
                self._motion_gate.reset()
                # A test video file plays in a loop at its own frame rate, like
                # a camera: its end is not the camera going offline, and
                # unpaced it was read at decode speed and ended in seconds.
                file_src = is_file_source(active_source)
                file_step = file_frame_interval(cap) if file_src else 0.0
                file_due = time.monotonic()
                file_frames = 0

                while not self._stop.is_set():
                    # A Frame: a GPU capture's stays NV12 until something
                    # needs BGR (analysis, the clip ring, a snapshot).
                    ok, frame = capture_backends.read_frame(cap)
                    if (not ok or frame is None) and file_src and file_frames and not self._stop.is_set():
                        # End of the file: start it again from the first frame.
                        file_frames = 0
                        if rewind_file(cap):
                            ok, frame = capture_backends.read_frame(cap)
                    if not ok or frame is None:
                        if self._stop.is_set():
                            break   # stop() ended the capture
                        why = getattr(cap, "error_summary", "")
                        raise RuntimeError("Stream ended or frame read failed" + (f" ({why})" if why else ""))
                    if file_src:
                        file_frames += 1
                        if file_step:
                            file_due += file_step
                            ahead = file_due - time.monotonic()
                            if ahead > 0:
                                if self._stop.wait(ahead):
                                    break
                            elif ahead < -1.0:
                                file_due = time.monotonic()   # fell behind (slow analysis): no burst
                    if not sized:
                        sized = True
                        reopened = self._after_first_frame(cap, active_source, frame)
                        if reopened is not cap:
                            # Moved to the other decoder, or reopened with
                            # colour normalisation: this frame is the old one's.
                            cap = self._cap = reopened
                            self._clip_n = self._clip_every(cap)
                            continue
                        self._clip_n = self._clip_every(cap)

                    self.rt.frames_read += 1
                    self.rt.last_frame_at = time.time()
                    self.rt.put_frame(frame)
                    frames_in_window += 1
                    self._frame_index += 1
                    # Feed the pre-event ring buffer, when a clip can need it
                    # (clip_recorder.wants), so clips are cut from real footage.
                    if self._frame_index % self._clip_n == 0 and self._clip_wanted():
                        self._push_clip_buffer(frame.bgr())

                    now = time.time()
                    if now - last_tick >= 2.0:
                        # Delivered frames: on the GPU path at most DECODE_MAX_FPS.
                        self.rt.fps = frames_in_window / (now - last_tick)
                        frames_in_window = 0
                        last_tick = now
                        self._clip_n = self._clip_every(cap)
                        self._rate_settings()
                        if self._width_changed(cap):
                            logger.info(f"Camera {self.rt.camera_id}: frame width limit is now "
                                        f"{self._max_width() or 'native'} (pose model or setting changed); "
                                        "reopening the stream")
                            reopen = "width"
                            break
                        if self._fps_changed(cap, active_source):
                            # Site setting DECODE_MAX_FPS changed: the decoder's
                            # frame-rate cap is fixed at open, so reopen (same
                            # frame size: tracks are kept).
                            logger.info(f"Camera {self.rt.camera_id}: decode frame-rate cap is now "
                                        f"{float(settings.DECODE_MAX_FPS):g} fps (setting changed); "
                                        "reopening the stream")
                            reopen = "fps"
                            break

                    # Night watch: while armed, a cheap motion check replaces
                    # the pose model (the scheduler holds this camera); motion
                    # asks for a short person-detection burst to confirm.
                    mode = self._night_watch_step(frame, now)
                    if mode == nw.HOLD:
                        continue
                    if mode == nw.NORMAL:
                        # Motion on a quiet camera (no people lately) wakes it
                        # to its full share on this very frame.
                        self._motion_step(frame, now)
                    # The scheduler decides whether this frame gets inferred:
                    # the camera's share of the accelerator (more where people
                    # are, the idle rate where nobody is), never above its
                    # ceiling. Frames it skips are still shown.
                    if inference_scheduler.admit(self.rt.camera_id, fps=self.rt.fps,
                                                 frame_index=self._frame_index, every_n=self._detect_n):
                        try:
                            if mode == nw.CONFIRM:
                                self._night_confirm(frame.bgr(), now)
                            else:
                                self._analyse(frame.bgr(), now)
                                inference_scheduler.set_activity(self.rt.camera_id, self._people_in_view())
                        finally:
                            inference_scheduler.done(self.rt.camera_id)

            except Exception as e:
                clean_err = redact_url(str(e))
                if isinstance(e, CameraAuthError):
                    auth_paused = True
                    self._auth_failures += 1
                    wait = AUTH_QUICK_RETRY_SEC if self._auth_failures <= AUTH_QUICK_RETRIES else AUTH_RETRY_SEC
                    self.rt.status = "AUTH_FAILED"
                    self.rt.last_error = f"{clean_err}. {AUTH_FAILED_HINT}"
                else:
                    wait = backoff
                    self.rt.status = "OFFLINE"
                    self.rt.last_error = clean_err
                self.rt.next_retry_at = time.time() + wait
                self.rt.decoder = None
                # A camera that drops out contributes nothing until it returns,
                # and its share of the accelerator goes to the others.
                inference_scheduler.forget(self.rt.camera_id)
                nw.night_watch.forget(self.rt.camera_id)
                self._night_armed = False
                for t in self.tracker.flush_all():
                    self.engine.close_track(t, reason="camera_offline")
                self.rt.set_tracks([])
                self.rt.live_track_count = 0
                self.rt.detections_last = 0
                self.rt.ignored_detections_last = 0
                _reset_pose_camera(self.rt.camera_id)
                if isinstance(e, CameraAuthError):
                    logger.warning(
                        f"Camera {self.rt.camera_id} login failed: {clean_err}; not retrying for "
                        f"{wait / 60:.0f} min to avoid locking the camera account "
                        "(edit the credentials or press Reconnect to retry now)"
                    )
                else:
                    logger.warning(f"Camera {self.rt.camera_id} error: {clean_err}; retrying in {wait:.0f}s")
            finally:
                self._cap = None
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass

            if reopen and not self._stop.is_set():
                # A planned reopen: no retry wait. At a new frame width, track
                # boxes and pose state are in the old frame's pixels; at a new
                # frame rate they stay valid (the tracker works in seconds).
                if reopen == "width":
                    for t in self.tracker.flush_all():
                        self.engine.close_track(t, reason="frame_size_changed")
                    self.rt.set_tracks([])
                    self.rt.live_track_count = 0
                    _reset_pose_camera(self.rt.camera_id)
                continue
            if self._sleep(wait):
                break
            if not auth_paused:
                backoff = min(backoff * 2, CAMERA_RETRY_MAX_SEC)

        self.rt.status = "DISABLED" if not self.rt.enabled else "OFFLINE"
        self.rt.decoder = None
        inference_scheduler.forget(self.rt.camera_id)
        nw.night_watch.forget(self.rt.camera_id)
        for t in self.tracker.flush_all():
            self.engine.close_track(t, reason="worker_stopped")
        _reset_pose_camera(self.rt.camera_id)
        # Turned off (or removed) while a last frame was being buffered.
        current = self.engine.runtimes.get(self.rt.camera_id)
        if current is None or not current.enabled:
            _drop_clip_buffer(self.rt.camera_id)

    def _after_first_frame(self, cap, source: str, frame):
        """Remember the stream's size; move the camera to the cheaper decoder once.

        A camera whose size was not known (first start, new stream) opened on
        the decoder chosen for an unknown size. If its measured size says the
        other decoder is cheaper, it is reopened there, once per worker life
        and logged (the login already succeeded, so this costs no failed
        attempt). A GPU capture whose stream turned out to need colour
        normalisation (full range / non-BT.601, ``capture_backends``) is
        reopened with it, once; the need is remembered with the stream size,
        so later opens have it from the start. Otherwise a software capture
        gets its decode threads fitted.
        """
        hw = isinstance(cap, capture_backends.FfmpegHwCapture)
        h, w = frame.shape[:2]
        if hw and cap.source_size:
            w, h = cap.source_size            # native, before any DECODE_MAX_WIDTH scaling
        _source_sizes[source] = (int(w), int(h))
        normalise = cap.colour_nonstandard if hw and cap.nv12 else None
        capture_backends.stream_sizes.put(self.rt.camera_id, source, int(w), int(h), normalise=normalise)
        # Pixel geometry authored at another size is scaled to this one.
        frame_sizes.set(self.rt.camera_id, (frame.shape[1], frame.shape[0]), (int(w), int(h)))
        if hw and cap.needs_colour_reopen and not self._stop.is_set():
            logger.info(f"Camera {self.rt.camera_id}: stream is not BT.601 limited range; reopening with "
                        "colour normalisation")
            try:
                cap.release()
            except Exception:
                pass
            cap = self._open(source)
            if not cap or not cap.isOpened():
                raise RuntimeError(f"Cannot reopen {redact_url(source)} with colour normalisation")
            self._report_decoder(cap, source)
            return cap

        choice = self._choice
        want_gpu = capture_backends.size_prefers_gpu(w, h)
        if choice is not None and choice.by_size and want_gpu == hw:
            # Opened on the right decoder: state the measured reason.
            self._choice = capture_backends.choose_decoder(capture_backends.decode_probe(), (int(w), int(h)))
            self._report_decoder(cap, source)
        if (choice is not None and choice.by_size and want_gpu is not None and want_gpu != hw
                and not self._decoder_switched and not self._stop.is_set()):
            self._decoder_switched = True
            before = capture_backends.BACKEND_LABELS[cap.decoder if hw else capture_backends.SOFTWARE]
            try:
                cap.release()
            except Exception:
                pass
            cap = self._open(source)
            if not cap or not cap.isOpened():
                raise RuntimeError(f"Cannot reopen {redact_url(source)} on the cheaper decoder")
            self._report_decoder(cap, source)
            logger.info(f"Camera {self.rt.camera_id}: {w}x{h} stream moved from {before} to "
                        f"{capture_backends.BACKEND_LABELS[self.rt.decoder]} decoding ({self.rt.decoder_note})")
            return cap
        if hw:
            return cap   # a GPU capture has no decode threads to fit
        return self._fit_decode_threads(cap, source, frame)

    def _fit_decode_threads(self, cap, source: str, frame):
        """Remember the stream's size; reopen once if it needs more decode threads.

        The first open of a source cannot know its resolution, so it uses
        ``DECODE_THREADS_UNKNOWN``. A stream larger than that allows (above
        1080p) is reopened once with its own count; reconnects then open with
        it directly. A stream that needs fewer keeps the capture it has.
        """
        h, w = frame.shape[:2]
        _source_sizes[source] = (int(w), int(h))
        wanted = decode_threads_for(w, h)
        if self._decode_threads is None or wanted <= self._decode_threads:
            return cap
        logger.info(f"Camera {self.rt.camera_id}: {w}x{h} stream needs {wanted} decode threads "
                    f"(opened with {self._decode_threads}); reopening")
        try:
            cap.release()
        except Exception:
            pass
        cap = self._open(source)
        if not cap or not cap.isOpened():
            raise RuntimeError(f"Cannot reopen {redact_url(source)} with {wanted} decode threads")
        return cap

    def _clip_every(self, cap) -> int:
        """Buffer every Nth delivered frame for evidence clips.

        N keeps the clip rate at the camera's native fps /
        ANALYTICS_DETECT_EVERY_N_FRAMES (25 / 5 = 5 fps) when the GPU path
        delivers fewer frames (DECODE_MAX_FPS): every 2nd of 10 fps, not every
        5th. Software capture delivers the native rate: every Nth, as before.

        Also sets ``_detect_n``, the stride the scheduler may analyse at: the
        same ratio rounded down, so thinning never lowers a camera's analysis
        ceiling below native / N (a 15 fps camera at 5 delivered fps: every
        frame, 5 fps, rather than every 2nd, 2.5 fps).
        """
        n = max(1, int(settings.ANALYTICS_DETECT_EVERY_N_FRAMES))
        native = getattr(cap, "source_fps", None)
        delivered = self.rt.fps or getattr(cap, "output_fps", None)
        if not native or not delivered:
            self._detect_n = n
            return n
        ratio = n * float(delivered) / float(native)
        self._detect_n = max(1, min(n, int(ratio + 1e-6)))
        return max(1, min(n, int(round(ratio))))

    def _clip_wanted(self) -> bool:
        """Whether the pre-event clip ring is fed for this camera (clip_recorder.wants)."""
        try:
            from app.services.clip_recorder import clip_recorder_service

            return clip_recorder_service.wants(self.rt.camera_id)
        except Exception:  # noqa: BLE001 - a broken recorder must not stop the capture
            return False

    def _push_clip_buffer(self, frame: np.ndarray) -> None:
        try:
            from app.services.clip_recorder import clip_recorder_service

            # Clips are saved footage: privacy masks apply.
            frame = apply_privacy_masks(frame, self.rt.camera_id)

            # The ring is sized on its first push, usually before the fps is
            # measured: estimate it from the capture (was: 1 fps, a 1 s ring).
            rate = self.rt.fps or getattr(self._cap, "output_fps", None) or float(settings.RECORDING_FPS)
            fps = max(1, int(round(float(rate) / self._clip_n)))
            clip_recorder_service.get_or_create_buffer(self.rt.camera_id, fps=fps).push_frame(frame)
        except Exception as e:  # never let the recorder take the capture loop down
            logger.debug(f"clip buffer push failed for {self.rt.camera_id}: {e}")

    # ----------------------------------------------------------------- analysis

    def _night_watch_step(self, frame, now: float) -> str:
        """NORMAL, HOLD or CONFIRM for this frame (services/night_watch.py).

        On arming, the camera's tracks are finished as when every analysis
        feature is off: nobody is counted while the pose model is paused.
        A night-watch failure falls back to the normal pipeline.
        """
        cam = self.rt.camera_id
        try:
            mode = nw.night_watch.step(cam, self.rt.name, frame, now)
        except Exception as e:  # noqa: BLE001 - never take the capture loop down
            _log_once(f"night_watch:{type(e).__name__}",
                      f"night watch failed on {cam} ({type(e).__name__}: {e}); running the normal analysis")
            inference_scheduler.hold(cam, False)
            mode = nw.NORMAL
        armed = mode != nw.NORMAL
        if armed and not self._night_armed:
            for t in self.tracker.flush_all():
                self.engine.close_track(t, reason="night_watch")
            self.rt.set_tracks([], now)
            self.rt.live_track_count = 0
            self.rt.detections_last = 0
            _reset_pose_camera(cam)
        self._night_armed = armed
        return mode

    def _night_confirm(self, frame: np.ndarray, now: float) -> None:
        """Person detection on a frame of a night-watch confirmation burst.

        Per-box thresholds (a dark box needs less confidence) and AI_IGNORE
        regions apply as in the live pipeline; no tracking, counting or pose
        analytics run.
        """
        cam = self.rt.camera_id
        h, w = frame.shape[:2]
        kw = {}
        max_frac = camera_setting(cam, "person_max_frame_fraction")
        if max_frac is not None:
            kw["max_frame_fraction"] = max_frac
        dets = person_detector.detect(frame, camera_id=cam, **kw)
        kept = outside_ignore_regions(dets, ignore_polygons(cam, w, h))
        self.rt.ignored_detections_last = len(dets) - len(kept)
        dets = kept
        # A remembered static figure (poster, mannequin: same box and pose as
        # one the tracker flagged) is not a person at night either.
        self.tracker.set_frame_size(w, h)
        enabled, seconds = static_figure_config(cam)
        self.tracker.configure_static(enabled, seconds)
        if enabled:
            dets = [d for d in dets
                    if not self.tracker.matches_static_memory(d.bbox, d.keypoints, now)]
        self.rt.detections_last = len(dets)
        nw.night_watch.confirm(cam, frame, dets, now)

    def _analyse(self, frame: np.ndarray, now: float) -> None:
        cam = self.rt.camera_id
        flags = {f: camera_flag(cam, f) for f in ANALYSIS_FLAGS}
        self.rt.analysis_flags = flags
        inference_scheduler.set_idle(cam, not any(flags.values()))
        if not any(flags.values()):
            # Every analysis feature is off for this camera: spend no GPU time,
            # show no boxes, and finish any tracks (closing open visits).
            for t in self.tracker.flush_all():
                self.engine.close_track(t, reason="analysis_disabled", persist=False)
            self.rt.set_tracks([], now)
            self.rt.live_track_count = 0
            self.rt.detections_last = 0
            self.rt.ignored_detections_last = 0
            return
        counting = flags["people_counting"]

        # The detector sees the unmasked frame. AI_IGNORE regions then drop
        # anything whose foot point falls inside them, or enough of whose box
        # does (ignore_box_fraction), before tracking.
        masks = camera_masks(cam)
        h, w = frame.shape[:2]
        ignore = ignore_polygons(cam, w, h, masks)
        self.tracker.set_frame_size(w, h)
        self.tracker.set_ignore_regions(ignore)

        # Ask for boxes down to the tracker's low threshold: the tracker uses
        # the low band only to keep existing identities through occlusion.
        # A camera-specific size gate (close-mounted camera) is passed only
        # when the operator set one; otherwise the detector's global default.
        detect_kw = {"conf_threshold": settings.TRACK_LOW_CONF_THRESHOLD}
        max_frac = camera_setting(cam, "person_max_frame_fraction")
        if max_frac is not None:
            detect_kw["max_frame_fraction"] = max_frac
        # ``camera_id`` gives the lighting measurement per-camera hysteresis
        # (low_light), reported in this camera's status as ``lighting``.
        detections = person_detector.detect(frame, camera_id=cam, **detect_kw)
        kept = outside_ignore_regions(detections, ignore)
        if len(kept) != len(detections):
            kept_ids = {id(d) for d in kept}
            self.rt.ignored_detections_last = sum(
                1 for d in detections if id(d) not in kept_ids and d.confidence >= person_threshold(d))
        else:
            self.rt.ignored_detections_last = 0
        detections = kept
        # People by the per-box rule (a dark box needs less confidence), as
        # the detector itself keeps them when no explicit threshold is given.
        persons = [d for d in detections if d.confidence >= person_threshold(d)]
        self.rt.detections_last = len(persons)
        if shadow_trial.active():
            # Developer pose-model trial: a copy of this frame may be compared
            # with a second model later; nothing it finds comes back here.
            shadow_trial.offer(cam, frame, persons, ignore, detect_kw.get("max_frame_fraction"))

        # Static-figure filter (tracking_service): posters and mannequins the
        # pose model keeps recognising become "static" tracks, drawn grey on
        # the overlay but kept out of counts, paths, zones, rules and theft.
        self.tracker.configure_static(*static_figure_config(cam))
        live = self.tracker.update(detections, now=now)
        self.rt.live_track_count = sum(1 for t in live if t.is_human)
        self.rt.static_memory_count = len(self.tracker.static_memory)

        snapshot: list[dict] = []
        for t in live:
            x1, y1, x2, y2 = t.bbox
            entry = {
                "track_id": t.track_id,
                "x1": round(float(x1), 1),
                "y1": round(float(y1), 1),
                "x2": round(float(x2), 1),
                "y2": round(float(y2), 1),
                "confidence": round(float(t.confidence), 3),
                "hits": t.hits,
                "confirmed": t.confirmed,
                # "pending" | "moving" | "static" (a poster or mannequin).
                "motion_state": t.motion_state,
                # Pending on a remembered static figure / mostly in an ignore
                # area: not counted until it moves (tracking_service).
                "pending_static": t.is_pending_static,
                "age_seconds": round(t.age_seconds, 1),
                # Only keypoints measured on this detection frame are shown;
                # a coasting track keeps its box but not a stale skeleton.
                "keypoints": keypoints_to_list(t.keypoints) if t.keypoints_fresh else None,
                # Live overlay (live_tracks / render_overlay): matched on this
                # frame, time of the last match, the tracker's predicted box
                # for a coasting track and its last matched skeleton.
                "fresh": t.misses == 0,
                "last_seen": round(float(t.last_seen), 3),
                "pred_box": None if t.misses == 0 else [
                    round(float(v), 1) for v in (getattr(t, "predicted_bbox", None) or t.bbox)],
                "kp_last": keypoints_to_list(t.keypoints) if t.misses != 0 else None,
                # Filled only when a homography exists; otherwise the track
                # has no place on the blueprint and these stay None.
                "x_m": None,
                "y_m": None,
                "zone_id": None,
            }
            t.floor_xy = None
            if t.is_static and t.current_zone_id is not None:
                # Turned out to be a static figure: finish its visit honestly.
                self.engine.leave_zone(t, now)
            if t.is_human:
                fx, fy = t.foot_point
                floor = floor_projector.to_floor(cam, fx, fy, frame_size=(w, h))
                if floor is not None:
                    t.floor_xy = (floor[0], floor[1])
                    entry["x_m"] = round(floor[0], 2)
                    entry["y_m"] = round(floor[1], 2)
                    if counting:
                        t.floor_points.append(
                            {"x": round(floor[0], 2), "y": round(floor[1], 2), "t": round(now, 2)}
                        )
                        self.engine.attribute_zone(t, floor[0], floor[1], now)
                        entry["zone_id"] = t.current_zone_id
                elif counting:
                    # No calibration: a camera linked to a zone counts its
                    # whole view into that zone (layout PUT /cameras/{id}/zone).
                    linked = self.engine.linked_zone(cam)
                    if linked is not None or t.current_zone_id is not None:
                        self.engine.attribute_zone(t, None, None, now, zone_id=linked)
                    entry["zone_id"] = t.current_zone_id
                if counting:
                    _append_path_point(t, fx, fy, w, h, floor, now)
                if not counting and t.current_zone_id is not None:
                    # Counting was switched off mid-visit: finish it honestly.
                    self.engine.leave_zone(t, now)
            snapshot.append(entry)
        self.rt.set_tracks(snapshot, now, frame_size=(w, h))

        # Evidence frames are saved by pose_analytics / tripwire_engine, so
        # they carry the privacy masks; the analysis itself already ran on the
        # raw frame. The masks are burned in only if evidence is actually
        # taken (a copy of this frame), not on every analysed frame.
        evidence = deferred_privacy_masks(frame, cam, masks)
        humans = [t for t in live if t.is_human]
        self._observe_pose(evidence, now, humans)
        self._evaluate_zone_rules(evidence, now, humans, counting)

        for finished in self.tracker.drain_finished():
            self.engine.close_track(finished, reason="track_lost", persist=counting)

    def _evaluate_zone_rules(self, frame: np.ndarray, now: float, tracks: list[Track],
                             counting: bool) -> None:
        """Tripwire crossings and restricted areas (services/tripwire_engine.py).

        Image-space rules on the confirmed tracks' foot points; runs after
        inference returned (no inference lock held). ``frame`` is the
        privacy-masked copy and is only used for alert snapshots. Crossings are
        recorded only while ``people_counting`` is on.
        """
        try:
            from app.services.tripwire_engine import tripwire_engine

            h, w = frame.shape[:2]
            tripwire_engine.evaluate(self.rt.camera_id, tracks, w, h, now, frame=frame,
                                     counting=counting, camera_name=self.rt.name)
        except Exception as e:
            _log_once(
                f"zone_rules:{type(e).__name__}",
                f"tripwire/restricted-area evaluation failed ({type(e).__name__}: {e}); continuing without it",
            )

    def _observe_pose(self, frame: np.ndarray, now: float, tracks: list[Track]) -> None:
        """Hand confirmed tracks to pose_analytics; apply the interactions it reports.

        Runs after inference has returned, so the shared inference lock is
        never held here.
        """
        pa = _get_pose_analytics()
        if pa is None:
            return
        try:
            result = pa.observe(self.rt.camera_id, now, frame, tracks)
        except Exception as e:
            _log_once(
                f"pose_observe:{type(e).__name__}",
                f"pose_analytics.observe failed ({type(e).__name__}: {e}); continuing without it",
            )
            return
        interactions = getattr(result, "interactions", None) or []
        if not interactions:
            return
        by_id = {t.track_id: t for t in tracks}
        for item in interactions:
            try:
                track_id, product_zone_id = item[0], item[1]
            except Exception:
                continue
            track = by_id.get(track_id) or by_id.get(str(track_id))
            if track is not None:
                self.engine.mark_interaction(track, str(product_zone_id), now)


class LiveAnalyticsEngine:
    """Owns the worker pool and buffers the facts they produce."""

    def __init__(self):
        self.runtimes: dict[str, CameraRuntime] = {}
        self.workers: dict[str, CameraWorker] = {}
        self._zones: list[dict] = []          # [{id, category, polygon}]
        self._zones_lock = threading.Lock()
        # Uncalibrated camera -> the zone its whole view belongs to (m0017).
        self._camera_zones: dict[str, str] = {}
        self._pending_visits: list[ZoneVisitFact] = []
        self._pending_tracks: list[Track] = []
        self._buffer_lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------ configuration

    def set_zones(self, zones: list[dict]) -> None:
        """Publish the current blueprint to the workers.

        Called at startup and whenever the operator edits the floor plan, so
        zone changes take effect without restarting the pipeline.
        """
        with self._zones_lock:
            self._zones = [
                {"id": z["id"], "category": z.get("category", "AISLE"), "polygon": z.get("polygon") or []}
                for z in zones
                if z.get("category") != "EXCLUDED"
            ]
        logger.info(f"Live pipeline now tracking {len(self._zones)} zone(s)")

    def set_camera_zone(self, camera_id: str, zone_id: Optional[str]) -> None:
        """Link (or with None unlink) an uncalibrated camera to one zone."""
        with self._zones_lock:
            if zone_id:
                self._camera_zones[camera_id] = zone_id
            else:
                self._camera_zones.pop(camera_id, None)

    def linked_zone(self, camera_id: str) -> Optional[str]:
        """The zone this camera counts into, if it is linked to a live zone.

        A link to a zone that was deleted or excluded counts nowhere.
        """
        with self._zones_lock:
            zone_id = self._camera_zones.get(camera_id)
            if zone_id and any(z["id"] == zone_id for z in self._zones):
                return zone_id
        return None

    def _zone_at(self, x_m: float, y_m: float) -> Optional[str]:
        with self._zones_lock:
            for z in self._zones:
                if point_in_polygon(x_m, y_m, z["polygon"]):
                    return z["id"]
        return None

    # ------------------------------------------------------------------- facts

    def attribute_zone(self, track: Track, x_m: Optional[float], y_m: Optional[float], now: float,
                       *, zone_id: Optional[str] = None) -> None:
        """Place a track in a zone, opening and closing visits as it moves.

        Calibrated cameras pass the floor point; an uncalibrated camera linked
        to a zone passes that ``zone_id`` (and no point) instead.
        """
        if x_m is not None and y_m is not None:
            zone_id = self._zone_at(x_m, y_m)

        if zone_id == track.current_zone_id:
            # Still in the same zone. Once the dwell floor is cleared the visit
            # becomes real and is published immediately so live occupancy is
            # answerable while the person is still standing there.
            # A track that has never moved (``established``) is not published
            # yet: a poster must not open visits. Its entry time is kept, so a
            # shopper who stood still first gets the whole dwell once they move.
            if (
                zone_id
                and track.open_visit_id is None
                and track.zone_entered_at is not None
                and getattr(track, "established", True)
                and (now - track.zone_entered_at) >= settings.ZONE_DWELL_MIN_SECONDS
            ):
                track.open_visit_id = f"zv_{uuid.uuid4().hex[:14]}"
                self._buffer_visit(
                    ZoneVisitFact(
                        visit_id=track.open_visit_id,
                        phase="open",
                        zone_id=zone_id,
                        track_id=track.track_id,
                        camera_id=track.camera_id,
                        entered_at=utc_from_ts(track.zone_entered_at),
                        interacted=track.interacted,
                    )
                )
            return

        self._close_open_visit(track, now)

        track.current_zone_id = zone_id
        track.zone_entered_at = now if zone_id else None
        track.interacted = False

    def mark_interaction(self, track: Track, product_zone_id: str, now: float) -> None:
        """Record that this track touched a shelf during its current zone visit.

        Sets ``interacted`` so the visit closes with it, and, if the visit is
        already open in the database, publishes the flag straight away so the
        funnel reflects it while the shopper is still there.
        """
        if track.current_zone_id is None:
            return
        already = track.interacted
        track.interacted = True
        if already or not track.open_visit_id or track.zone_entered_at is None:
            return
        self._buffer_visit(
            ZoneVisitFact(
                visit_id=track.open_visit_id,
                phase="interact",
                zone_id=track.current_zone_id,
                track_id=track.track_id,
                camera_id=track.camera_id,
                entered_at=utc_from_ts(track.zone_entered_at),
                interacted=True,
            )
        )

    def _close_open_visit(self, track: Track, now: float) -> None:
        """Finish the visit a track currently holds, if it was ever opened.

        A track that leaves before clearing the dwell floor never opened a
        visit, so nothing is written -- that is a pass-by, not a dwell.
        """
        if track.open_visit_id and track.zone_entered_at and track.current_zone_id:
            self._buffer_visit(
                ZoneVisitFact(
                    visit_id=track.open_visit_id,
                    phase="close",
                    zone_id=track.current_zone_id,
                    track_id=track.track_id,
                    camera_id=track.camera_id,
                    entered_at=utc_from_ts(track.zone_entered_at),
                    exited_at=utc_from_ts(now),
                    dwell_seconds=round(now - track.zone_entered_at, 2),
                    interacted=track.interacted,
                )
            )
        track.open_visit_id = None

    def leave_zone(self, track: Track, now: float) -> None:
        """Close the track's open visit and forget its zone (no new one opens)."""
        self._close_open_visit(track, now)
        track.current_zone_id = None
        track.zone_entered_at = None
        track.interacted = False

    def close_track(self, track: Track, reason: str = "", persist: Optional[bool] = None) -> None:
        """Finalise a track: close its open visit and queue it for storage.

        An open visit is always closed so no row is left "in the zone"
        forever. The track itself is stored only while people counting is
        enabled for its camera (``persist`` overrides the lookup).
        """
        if not track.confirmed:
            return
        self._close_open_visit(track, track.last_seen)
        track.current_zone_id = None
        track.zone_entered_at = None

        if getattr(track, "is_static", False) or not getattr(track, "established", True):
            # A poster or mannequin is never a customer: no customer_tracks
            # row, so no footfall, traffic or heatmap from it. Neither is a
            # track that never moved at all (still "pending" at its end).
            return
        if persist is None:
            persist = camera_flag(track.camera_id, "people_counting")
        if not persist:
            return
        with self._buffer_lock:
            self._pending_tracks.append(track)

    def _buffer_visit(self, visit: ZoneVisitFact) -> None:
        with self._buffer_lock:
            self._pending_visits.append(visit)

    def drain(self) -> tuple[list[ZoneVisitFact], list[Track]]:
        with self._buffer_lock:
            visits, self._pending_visits = self._pending_visits, []
            tracks, self._pending_tracks = self._pending_tracks, []
        return visits, tracks

    # ----------------------------------------------------------------- workers

    def start_camera(self, camera_id: str, name: str, source: str, enabled: bool = True) -> None:
        """Run a worker for the camera, or, with ``enabled=False``, record it as off.

        A camera the operator turned off keeps a DISABLED runtime and nothing
        else: no worker thread, no connection, no decode, no inference and
        no clip buffer. Its source is not kept either, so no login sits in
        memory for a camera nobody is watching.
        """
        self.stop_camera(camera_id)
        configure_opencv_threads()
        rt = CameraRuntime(camera_id=camera_id, name=name, source=source if enabled else "", enabled=enabled)
        self.runtimes[camera_id] = rt
        if not enabled:
            rt.status = "DISABLED"
            _drop_clip_buffer(camera_id)
            return
        worker = CameraWorker(rt, self)
        self.workers[camera_id] = worker
        worker.start()

    def reconnect_camera(self, camera_id: str) -> bool:
        """Cut the camera's retry wait short (also after an auth failure)."""
        w = self.workers.get(camera_id)
        if w is None:
            return False
        w.wake()
        return True

    def stop_camera(self, camera_id: str) -> None:
        if (w := self.workers.pop(camera_id, None)) is not None:
            w.stop()
        self.runtimes.pop(camera_id, None)

    def stop_all(self, timeout: float = 3.0) -> list[str]:
        """Stop every worker and wait (bounded) for them to finish.

        Joining matters at shutdown: each worker closes its tracks (and so
        its open zone visits) on the way out, and the supervisor's final
        flush must see them. It also keeps a worker from being inside an ONNX
        Runtime call while the interpreter tears the session down. A worker
        blocked in a network read past ``timeout`` is left behind (they are
        daemon threads) and named in the returned list.
        """
        workers = list(self.workers.values())
        for cid in list(self.workers.keys()):
            self.stop_camera(cid)
        deadline = time.monotonic() + max(0.0, timeout)
        stuck = []
        for w in workers:
            w.join(max(0.0, deadline - time.monotonic()))
            if w.is_alive():
                stuck.append(w.name)
        if stuck:
            logger.warning(f"Camera worker(s) still running after {timeout:.1f}s: {', '.join(stuck)}")
        return stuck

    def get_frame(self, camera_id: str) -> Optional[np.ndarray]:
        """Latest frame with privacy masks applied: the only one to show or save."""
        rt = self.runtimes.get(camera_id)
        return rt.get_frame() if rt else None

    def get_raw_frame(self, camera_id: str) -> Optional[np.ndarray]:
        """Unmasked latest frame, for running inference only."""
        rt = self.runtimes.get(camera_id)
        return rt.get_raw_frame() if rt else None

    def status(self) -> dict:
        runtimes = [rt.to_dict() for rt in self.runtimes.values()]
        online = sum(1 for r in runtimes if r["status"] == "ONLINE")
        off = sum(1 for r in runtimes if not r["enabled"])
        return {
            "running": self._started,
            # Every configured camera, including those turned off; "working"
            # is out of cameras_enabled, so a switched-off camera never counts
            # as a failure.
            "cameras_total": len(runtimes),
            "cameras_enabled": len(runtimes) - off,
            "cameras_off": off,
            "cameras_online": online,
            # Store-wide: a duplicate camera's people are already counted by
            # its group's primary camera (services/duplicate_cameras.py).
            "live_tracks": sum(r["live_tracks"] for r in runtimes if not r.get("duplicate_of")),
            "duplicate_groups": sum(1 for r in runtimes if r.get("duplicate_group") and not r.get("duplicate_of")),
            "cameras_excluded_from_totals": [r["camera_id"] for r in runtimes if r.get("duplicate_of")],
            "detector": person_detector.status(),
            "tracker": {"method": "bytetrack", "assignment": ASSIGNMENT_METHOD},
            "pose_analytics": {"status": _pose_state["status"], "error": _pose_state["error"]},
            "zones_loaded": len(self._zones),
            "cameras": runtimes,
        }

    def live_snapshot(self) -> dict:
        """The 2D person map: where every confirmed, floor-projected track is now.

        ``persons`` holds only tracks that are confirmed AND come from a
        calibrated camera, because those are the only ones with a real floor
        position. Every camera still appears under ``detections`` with its
        image-space boxes, so an uncalibrated feed can be overlaid without
        anyone being invented on the plan.
        """
        persons: list[dict] = []
        detections: list[dict] = []
        uncalibrated_tracks = 0

        for rt in list(self.runtimes.values()):
            tracks = rt.get_tracks()
            calibrated = rt.calibrated
            dup = _duplicate_status(rt.camera_id)
            detections.append(
                {
                    "camera_id": rt.camera_id,
                    "camera_name": rt.name,
                    "status": rt.status,
                    "calibrated": calibrated,
                    "live_tracks": rt.live_track_count,
                    **dup,
                    "frame_width": rt.frame_width,
                    "frame_height": rt.frame_height,
                    "boxes": [
                        {
                            "track_id": t["track_id"],
                            "x1": t["x1"],
                            "y1": t["y1"],
                            "x2": t["x2"],
                            "y2": t["y2"],
                            "confidence": t["confidence"],
                            "confirmed": t["confirmed"],
                            "motion_state": t.get("motion_state"),
                            "pending_static": bool(t.get("pending_static")),
                            "keypoints": t.get("keypoints"),
                        }
                        for t in tracks
                    ],
                }
            )
            if dup.get("duplicate_of"):
                # Same physical camera as its group's primary: its people are
                # already on the map from there (still in ``detections``).
                continue
            for t in tracks:
                if not t["confirmed"] or t.get("motion_state") == "static" or t.get("pending_static"):
                    continue
                if t["x_m"] is None or t["y_m"] is None:
                    uncalibrated_tracks += 1
                    continue
                persons.append(
                    {
                        "track_id": t["track_id"],
                        "camera_id": rt.camera_id,
                        "camera_name": rt.name,
                        "x_m": t["x_m"],
                        "y_m": t["y_m"],
                        "zone_id": t["zone_id"],
                        "age_seconds": t["age_seconds"],
                        "confidence": t["confidence"],
                        "hits": t["hits"],
                    }
                )

        return {
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "running": self._started,
            "persons": persons,
            "detections": detections,
            "persons_total": len(persons),
            "uncalibrated_track_count": uncalibrated_tracks,
        }

    def live_tracks(self, camera_ids) -> dict:
        """Per-camera live tracks for the dashboard's client-side overlay (/api/v1/live/tracks).

        Built from the snapshot each worker already published plus the pose
        analytics' behaviour (``behaviour_state``, cached briefly); no
        inference runs and no worker lock is held for longer than a copy.
        Unknown camera ids are omitted.
        """
        cameras: dict = {}
        for cid in camera_ids:
            rt = self.runtimes.get(cid)
            if rt is not None:
                cameras[cid] = _live_camera_tracks(rt)
        return {"server_time": round(time.time(), 3), "cameras": cameras}

    def live_behaviour(self) -> dict:
        """Live tracks at "watch" or "alert" on every camera, newest first (/api/v1/live/behaviour)."""
        rows: list[dict] = []
        for rt in list(self.runtimes.values()):
            state = behaviour_state(rt.camera_id, dict(rt.analysis_flags))
            if not state:
                continue
            by_id = state.get("tracks") or {}
            for t in rt.get_tracks():
                b = by_id.get(str(t.get("track_id")))
                if not b or b.get("level") not in ("watch", "alert"):
                    continue
                rows.append({
                    "camera_id": rt.camera_id,
                    "camera_name": rt.name,
                    "track_id": t.get("track_id"),
                    "level": b["level"],
                    "labels": list(b.get("labels") or []),
                    "pattern_score": b.get("pattern_score"),
                    "incident_id": b.get("incident_id"),
                    "since": None if b.get("since") is None else round(float(b["since"]), 3),
                })
        rows.sort(key=lambda r: r["since"] if r["since"] is not None else float("-inf"), reverse=True)
        return {"tracks": rows}

    def mark_started(self) -> None:
        self._started = True


# Smallest width a preview may be scaled to (snapshot / stream ``max_width``).
PREVIEW_MIN_WIDTH = 64


def fit_width(frame: np.ndarray, max_width: Optional[int]) -> tuple[np.ndarray, float]:
    """``frame`` scaled down to at most ``max_width`` pixels wide, and the scale used.

    For previews (dashboard tiles, the enlarged live view): a 3072x2048 frame
    is ~850 KB as a JPEG, 9-18 s over a slow remote link, while the tile shows
    it ~500 px wide. INTER_AREA keeps the downscaled picture sharp without
    aliasing; the aspect ratio is kept, and a frame is never enlarged.
    ``max_width`` of 0 / None means native size (evidence and downloads).
    """
    import cv2

    if not max_width or max_width <= 0:
        return frame, 1.0
    h, w = frame.shape[:2]
    target = max(PREVIEW_MIN_WIDTH, int(max_width))
    if w <= target:
        return frame, 1.0
    scale = target / float(w)
    size = (target, max(1, int(round(h * scale))))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA), scale


IGNORE_AREA_COLOUR = (200, 90, 200)     # BGR violet: an AI_IGNORE area, drawn faintly
STATIC_COLOUR = (150, 150, 150)          # muted grey: a figure that never moves
PENDING_STATIC_COLOUR = (175, 160, 140)  # grey-blue: maybe a static figure, not counted yet
PENDING_COLOUR = (60, 200, 255)          # amber: not yet seen moving


def draw_ignore_areas(out: np.ndarray, polys, alpha: float = 0.16) -> None:
    """AI_IGNORE areas (pixel polygons of ``out``): a faint fill, a thin outline and "ignored"."""
    import cv2

    h, w = out.shape[:2]
    for p in polys:
        pts = np.round(np.asarray(p, dtype=np.float64).reshape(-1, 2)).astype(np.int32)
        if len(pts) < 3:
            continue
        x, y, bw, bh = cv2.boundingRect(pts)
        x, y = max(0, x), max(0, y)
        bw, bh = min(bw, w - x), min(bh, h - y)
        if bw <= 0 or bh <= 0:
            continue
        roi = out[y: y + bh, x: x + bw]
        tint = roi.copy()
        cv2.fillPoly(tint, [pts - np.array([x, y], dtype=np.int32)], IGNORE_AREA_COLOUR)
        cv2.addWeighted(tint, alpha, roi, 1.0 - alpha, 0, dst=roi)
        cv2.polylines(out, [pts], True, IGNORE_AREA_COLOUR, 1)
        cv2.putText(out, "ignored", (x + 4, min(h - 4, y + 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    IGNORE_AREA_COLOUR, 1, cv2.LINE_AA)


def _overlay_ignore_areas(out: np.ndarray, rt: CameraRuntime, scale: float) -> None:
    try:
        fw = rt.frame_width or int(round(out.shape[1] / max(scale, 1e-6)))
        fh = rt.frame_height or int(round(out.shape[0] / max(scale, 1e-6)))
        polys = ignore_polygons(rt.camera_id, fw, fh)
        if polys:
            draw_ignore_areas(out, [np.asarray(p, dtype=np.float64) * scale for p in polys])
    except Exception as e:  # noqa: BLE001 - an overlay detail must never break the stream
        _log_once(f"overlay_ignore:{type(e).__name__}", f"ignore areas not drawn on the overlay: {e}")


# ----------------------------------------------------------------- live view

WATCH_COLOUR = (0, 165, 255)     # BGR amber/orange: an active behaviour cue
ALERT_COLOUR = (40, 40, 235)     # BGR red: a rule fired on this track (cooldown)
# A coasting track's last skeleton is drawn (moved with the predicted box)
# for this long after its last match; after that only the dashed box.
COASTING_SKELETON_MAX_SEC = 1.0
# pose_analytics.live_state is read at most this often per camera (shared
# by the stream overlay and every dashboard poll).
BEHAVIOUR_CACHE_SEC = 0.2
_behaviour_cache: dict = {}
_behaviour_cache_lock = threading.Lock()


def behaviour_state(camera_id: str, flags: Optional[dict] = None) -> Optional[dict]:
    """The camera's per-track behaviour (``pose_analytics.live_state``), or None.

    None ("not available") when shelf interaction and theft detection are
    both off for the camera (``flags`` = the worker's last-read analysis
    flags; empty = not read yet), when pose analytics is unavailable, or when
    it has nothing for this camera. Cached ``BEHAVIOUR_CACHE_SEC`` per camera;
    ``live_state`` itself waits at most a few ms for the camera's lock, so a
    reader never stalls the worker thread.
    """
    if flags and not (flags.get("theft_detection") or flags.get("shelf_interaction")):
        return None
    pa = _get_pose_analytics()
    fn = getattr(pa, "live_state", None) if pa is not None else None
    if fn is None:
        return None
    mono = time.monotonic()
    with _behaviour_cache_lock:
        hit = _behaviour_cache.get(camera_id)
        if hit is not None and hit[2] is pa and mono - hit[0] < BEHAVIOUR_CACHE_SEC:
            return hit[1]
    try:
        state = fn(camera_id)
    except Exception as e:  # noqa: BLE001 - the live view must never break on it
        _log_once(f"live_state:{type(e).__name__}", f"pose_analytics.live_state failed: {e}")
        state = None
    with _behaviour_cache_lock:
        _behaviour_cache[camera_id] = (mono, state, pa)
    return state


def _norm_box(box, w: int, h: int) -> list:
    return [round(min(max(float(box[0]) / w, 0.0), 1.0), 4), round(min(max(float(box[1]) / h, 0.0), 1.0), 4),
            round(min(max(float(box[2]) / w, 0.0), 1.0), 4), round(min(max(float(box[3]) / h, 0.0), 1.0), 4)]


def _live_camera_tracks(rt: CameraRuntime) -> dict:
    """One camera's entry of /api/v1/live/tracks: everything normalised to the analysed frame."""
    meta = rt.live_meta()
    tracks = rt.get_tracks()
    flags = dict(rt.analysis_flags)
    size = meta["size"]
    if not size and rt.frame_width and rt.frame_height:
        size = (rt.frame_width, rt.frame_height)
    analysing = (not flags) or any(flags.values())
    state = behaviour_state(rt.camera_id, flags)
    by_id = (state or {}).get("tracks") if state else None
    out: list[dict] = []
    if size and size[0] and size[1]:
        w, h = int(size[0]), int(size[1])
        ref = meta["analysed_at"]
        for t in tracks:
            if not all(k in t for k in ("track_id", "x1", "y1", "x2", "y2")):
                continue
            fresh = bool(t.get("fresh", True))
            pred = t.get("pred_box")
            box = pred if (not fresh and pred) else (t["x1"], t["y1"], t["x2"], t["y2"])
            last_seen = t.get("last_seen")
            if fresh:
                age = 0.0
            elif last_seen is not None and ref is not None:
                age = round(max(0.0, float(ref) - float(last_seen)), 2)
            else:
                age = None
            kps = t.get("keypoints") if fresh else t.get("kp_last")
            vel = t.get("vel_px")
            state_name = t.get("motion_state") or "pending"
            if t.get("pending_static") and state_name != "static":
                state_name = "pending_static"
            b = by_id.get(str(t["track_id"])) if by_id is not None else None
            out.append({
                "track_id": t["track_id"],
                "confirmed": bool(t.get("confirmed")),
                "motion_state": state_name,
                "box": _norm_box(box, w, h),
                "velocity": None if not vel else [round(float(vel[0]) / w, 4), round(float(vel[1]) / h, 4)],
                "fresh": fresh,
                "age_sec": age,
                "keypoints": None if not kps else [
                    [round(float(p[0]) / w, 4), round(float(p[1]) / h, 4), round(float(p[2]), 3)] for p in kps],
                "behaviour": None if not b else {k: v for k, v in b.items() if k != "since"},
            })
    return {
        "seq": int(meta["seq"]),
        "analysed_at": None if meta["analysed_at"] is None else round(float(meta["analysed_at"]), 3),
        "frame_width": int(size[0]) if size else None,
        "frame_height": int(size[1]) if size else None,
        "analysis_fps": meta["analysis_fps"] if analysing else None,
        "tracks": out,
    }


def _dashed_rect(out: np.ndarray, p1: tuple, p2: tuple, colour, thickness: int = 1, dash: int = 6) -> None:
    """A dashed rectangle (a coasting track: position predicted, not measured)."""
    import cv2

    x1, y1 = p1
    x2, y2 = p2
    step = max(2, dash) * 2
    for x in range(min(x1, x2), max(x1, x2), step):
        xe = min(x + dash, max(x1, x2))
        cv2.line(out, (x, y1), (xe, y1), colour, thickness)
        cv2.line(out, (x, y2), (xe, y2), colour, thickness)
    for y in range(min(y1, y2), max(y1, y2), step):
        ye = min(y + dash, max(y1, y2))
        cv2.line(out, (x1, y), (x1, ye), colour, thickness)
        cv2.line(out, (x2, y), (x2, ye), colour, thickness)


def render_overlay(frame: np.ndarray, rt: CameraRuntime, scale: float = 1.0) -> np.ndarray:
    """Draw the worker's current track boxes onto a copy of ``frame``.

    Uses only the snapshot the worker already produced -- no inference runs
    here -- so the overlay costs a few rectangles per frame and always shows
    exactly what the pipeline is counting. Confirmed moving tracks are drawn
    solid green, confirmed tracks not yet seen moving amber and labelled
    "pending", tentative ones (below the hit floor) thin amber, static
    figures (posters, mannequins: not counted) thin grey and labelled
    "static", pending tracks on a remembered static figure or mostly inside
    an ignore area thin grey-blue "pending static" (not counted), and AI_IGNORE
    areas faintly violet "ignored". A corner tag states whether the camera is
    calibrated so nobody mistakes boxes for positions. When the pose model
    supplied keypoints on the latest detection frame, the visible limbs are
    drawn too (wrists marked larger). A coasting track (not matched on the
    latest analysed frame) is a thin dashed box at the tracker's predicted
    position, with its last skeleton for up to ``COASTING_SKELETON_MAX_SEC``
    and none after. A track with an active behaviour cue is amber, one a
    rule fired on (within the incident cooldown) red, with the behaviour
    labels under the box (``behaviour_state``).

    ``scale`` is the size of ``frame`` relative to the camera's frames (a
    preview from ``fit_width``): boxes are drawn scaled onto the small frame,
    so lines and labels stay legible instead of being shrunk with it.
    """
    import cv2

    out = frame if frame.flags.writeable else frame.copy()
    _overlay_ignore_areas(out, rt, scale)
    tracks = rt.get_tracks()
    confirmed_colour = (80, 220, 90)     # BGR green
    try:
        beh_state = behaviour_state(rt.camera_id, dict(rt.analysis_flags))
    except Exception:  # noqa: BLE001 - an overlay detail must never break the stream
        beh_state = None
    behaviours = (beh_state or {}).get("tracks") or {}
    analysed_at = rt.analysed_at

    for t in tracks:
        fresh = bool(t.get("fresh", True))
        pred = t.get("pred_box")
        box = pred if (not fresh and pred) else (t["x1"], t["y1"], t["x2"], t["y2"])
        x1, y1, x2, y2 = (int(float(v) * scale) for v in box)
        stale = 0.0
        if not fresh and t.get("last_seen") is not None and analysed_at is not None:
            stale = max(0.0, float(analysed_at) - float(t["last_seen"]))
        state = t.get("motion_state")
        tid = t["track_id"][-4:]
        label = f"{tid} {t['confidence']:.2f}"
        if state == "static":
            colour, thickness, label = STATIC_COLOUR, 1, f"{tid} static"
        elif t.get("pending_static"):
            colour, thickness, label = PENDING_STATIC_COLOUR, 1, f"{tid} pending static"
        elif not t["confirmed"]:
            colour, thickness = PENDING_COLOUR, 1
        elif state == "pending":
            colour, thickness, label = PENDING_COLOUR, 2, f"{tid} pending {t['confidence']:.2f}"
        else:
            colour, thickness = confirmed_colour, 2
        if state != "static" and not t.get("pending_static") and t.get("x_m") is not None:
            label += f" ({t['x_m']:.1f},{t['y_m']:.1f})m"
        # Behaviour (pose_analytics): amber while a cue is active, red after
        # a rule fired on this track; its labels are drawn under the box.
        beh = behaviours.get(str(t["track_id"]))
        level = beh.get("level") if beh else None
        if level == "alert":
            colour, thickness = ALERT_COLOUR, max(thickness, 2)
        elif level == "watch":
            colour, thickness = WATCH_COLOUR, max(thickness, 2)
        if fresh:
            cv2.rectangle(out, (x1, y1), (x2, y2), colour, thickness)
        else:
            # Coasting: where the tracker predicts the person is, not a measurement.
            _dashed_rect(out, (x1, y1), (x2, y2), colour, 1)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = max(th + 4, y1 - 4)
        cv2.rectangle(out, (x1, ty - th - 4), (x1 + tw + 4, ty + 2), colour, -1)
        cv2.putText(out, label, (x1 + 2, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10, 10, 10), 1)
        if level in ("watch", "alert") and beh.get("labels"):
            text = " | ".join(beh["labels"][:3])
            (bw, bh), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            by = min(out.shape[0] - 3, y2 + bh + 6)
            cv2.rectangle(out, (x1, by - bh - 4), (x1 + bw + 4, by + 3), colour, -1)
            cv2.putText(out, text, (x1 + 2, by - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (255, 255, 255) if level == "alert" else (10, 10, 10), 1)

        kpts = t.get("keypoints")
        if not fresh:
            # The last matched skeleton, moved with the predicted box, for a
            # short coast only: after that a stale pose would be misleading.
            kpts = None
            last = t.get("kp_last")
            if last and stale <= COASTING_SKELETON_MAX_SEC:
                dx = (box[0] + box[2] - t["x1"] - t["x2"]) / 2.0
                dy = (box[1] + box[3] - t["y1"] - t["y2"]) / 2.0
                kpts = [(p[0] + dx, p[1] + dy, p[2]) for p in last]
        if kpts:
            if scale != 1.0:
                kpts = [(p[0] * scale, p[1] * scale, p[2]) for p in kpts]
            _draw_skeleton(out, kpts, colour)

    tag = "calibrated" if rt.calibrated else "uncalibrated"
    n_static = sum(1 for t in tracks if t.get("motion_state") == "static")
    n_pstatic = sum(1 for t in tracks if t["confirmed"] and t.get("pending_static")
                    and t.get("motion_state") != "static")
    n_conf = sum(1 for t in tracks if t["confirmed"]) - n_static - n_pstatic
    header = f"{rt.name} | {tag} | {n_conf} tracked"
    if n_static:
        header += f" | {n_static} static"
    if n_pstatic:
        header += f" | {n_pstatic} pending static"
    cv2.rectangle(out, (0, 0), (10 + 8 * len(header), 22), (18, 20, 26), -1)
    cv2.putText(out, header, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 224, 232), 1)
    return out


def _draw_skeleton(out: np.ndarray, kpts, colour) -> None:
    """Draw visible COCO limbs and joints. Invisible points are never drawn."""
    import cv2

    thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
    pts = [(int(p[0]), int(p[1]), float(p[2])) for p in kpts]
    joint_colour = (255, 200, 60)   # BGR light blue
    for a, b in SKELETON_EDGES:
        if a < len(pts) and b < len(pts) and pts[a][2] >= thr and pts[b][2] >= thr:
            cv2.line(out, pts[a][:2], pts[b][:2], colour, 2, cv2.LINE_AA)
    for i, (x, y, v) in enumerate(pts):
        if v >= thr:
            # Wrists are what shelf-interaction analytics watch; mark them larger.
            cv2.circle(out, (x, y), 5 if i in (9, 10) else 3, joint_colour, -1, cv2.LINE_AA)


live_engine = LiveAnalyticsEngine()
