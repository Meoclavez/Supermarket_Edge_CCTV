"""Memory-optimized, thread-safe ring-buffer for short alert clips.

Stores JPEG-compressed frames in memory (reducing RAM usage by 98%) and exports
optimized MP4 clips with +faststart flags. There is no continuous recording:
a clip is only written for an alert or an operator request, and the saved
stills/clips are aged out by services/evidence_storage.py (oldest first).
"""

import os
import cv2
import time
import shutil
import asyncio
import logging
import threading
import subprocess
from collections import deque
from typing import Dict, Optional, List, Tuple
from pathlib import Path
import numpy as np

from app.config import settings
from app.services.evidence_storage import note_evidence_written
from app.services.resilience import ServiceHealthTracker

logger = logging.getLogger("ClipRecorder")


class StreamRingBuffer:
    """Thread-safe, memory-optimized circular buffer storing JPEG-compressed frames."""

    def __init__(self, camera_id: str, max_seconds: int = 5, fps: int = 15):
        self.camera_id = camera_id
        self.max_seconds = max_seconds
        self.fps = fps
        self.max_frames = max_seconds * fps
        self.buffer = deque(maxlen=self.max_frames)
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_frame_time: float = 0.0
        self._lock = threading.Lock()

    def push_frame(self, frame: np.ndarray):
        """Compress BGR frame to JPEG and store with timestamp under mutex lock.

        Frames wider than CLIP_MAX_WIDTH are scaled down first: the encode is
        on the camera thread and its cost grows with the pixels (audit opt. 6).
        The latest full-size frame is kept for snapshots.
        """
        small = frame
        limit = int(getattr(settings, "CLIP_MAX_WIDTH", 0) or 0)
        if limit > 0 and frame.ndim >= 2 and frame.shape[1] > limit:
            h = max(2, int(round(frame.shape[0] * limit / frame.shape[1])) // 2 * 2)
            small = cv2.resize(frame, (limit // 2 * 2, h), interpolation=cv2.INTER_AREA)
        ret, encoded_jpeg = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ret:
            return

        ts = time.time()
        with self._lock:
            self.buffer.append((ts, encoded_jpeg))
            self.latest_frame = frame
            self.latest_frame_time = ts

    def get_pre_event_frames(self, max_age: Optional[float] = None) -> List[np.ndarray]:
        """Safely decompress and retrieve buffered pre-event frames.

        Only frames from the last ``max_age`` seconds (default: the ring's
        length plus one): a ring that was fed on demand earlier must not put
        old footage in front of a new clip.
        """
        limit = time.time() - (self.max_seconds + 1.0 if max_age is None else float(max_age))
        with self._lock:
            raw_items = [item for item in self.buffer if item[0] >= limit]

        frames = []
        for _, enc in raw_items:
            img = cv2.imdecode(enc, cv2.IMREAD_COLOR)
            if img is not None:
                frames.append(img)
        return frames

    def get_pre_event_jpegs(self, max_age: Optional[float] = None) -> List[Tuple[float, bytes]]:
        """Like get_pre_event_frames, as (timestamp, JPEG bytes): nothing decoded.

        Clips are muxed straight from these, so a clip holds compressed frames
        only (a 30 s post-roll of decoded 6 MP frames was ~8 GB; audit L7/M6).
        """
        limit = time.time() - (self.max_seconds + 1.0 if max_age is None else float(max_age))
        with self._lock:
            return [(ts, bytes(enc)) for ts, enc in self.buffer if ts >= limit]

    def jpegs_since(self, ts: float) -> List[Tuple[float, bytes]]:
        """Frames pushed after ``ts`` (each frame once: no repeats in a clip)."""
        with self._lock:
            return [(t, bytes(enc)) for t, enc in self.buffer if t > ts]

    def get_latest_frame_copy(self) -> Optional[np.ndarray]:
        with self._lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None


class ClipRecorderService:
    """Per-camera pre-event rings, fed by the camera workers while ``wants()``.

    Keeping a ring means JPEG-encoding ~5 frames a second per camera, so it
    is kept only where a clip can be needed (CLIP_PRE_EVENT_BUFFER): always
    ("on"), never ("off"), or ("auto") for a camera whose armed night watch
    saves clips (NIGHT_WATCH_CLIP) or whose theft detection saves an
    evidence clip with each incident (per-camera ``theft_clip`` feature).
    A clip being recorded asks for frames for its post-roll (``request``) in
    every mode, so an on-demand clip still gets real footage from the moment
    it was asked for. The ring is memory only: nothing reaches the disk
    unless an incident or an operator asks for a clip.
    """

    # How long a "does this camera need a ring" answer is reused (seconds).
    WANT_CACHE_SEC = 2.0

    def __init__(self):
        self.buffers: Dict[str, StreamRingBuffer] = {}
        self._service_lock = threading.Lock()
        self._demand: Dict[str, float] = {}             # camera -> monotonic deadline
        self._auto_cache: Dict[str, Tuple[float, bool]] = {}

    @staticmethod
    def mode() -> str:
        m = str(getattr(settings, "CLIP_PRE_EVENT_BUFFER", "auto") or "auto").strip().lower()
        if m in ("on", "always", "1", "true", "yes"):
            return "on"
        if m in ("off", "never", "0", "false", "no"):
            return "off"
        return "auto"

    def request(self, camera_id: str, seconds: float) -> None:
        """Feed this camera's ring for the next ``seconds`` (a clip being recorded)."""
        until = time.monotonic() + max(0.0, float(seconds))
        with self._service_lock:
            self._demand[camera_id] = max(self._demand.get(camera_id, 0.0), until)

    def keeps_ring(self, camera_id: str) -> bool:
        """Whether this camera's ring is fed continuously (a pre-event part exists)."""
        mode = self.mode()
        if mode != "auto":
            return mode == "on"
        now = time.monotonic()
        cached = self._auto_cache.get(camera_id)
        if cached is not None and now - cached[0] < self.WANT_CACHE_SEC:
            return cached[1]
        want = self.theft_clip_enabled(camera_id)
        if not want:
            # A camera that sees an armed restricted area keeps the pre-event
            # ring, so its evidence clip starts before the alert.
            try:
                from app.services.restricted_area_engine import restricted_area_engine

                want = restricted_area_engine.wants_clip_ring(camera_id)
            except Exception:  # noqa: BLE001
                want = False
        if not want and settings.NIGHT_WATCH_CLIP:
            try:
                from app.services.night_watch import night_watch

                want = night_watch.is_armed(camera_id)
            except Exception:  # noqa: BLE001 - unknown: keep the ring rather than lose a clip
                want = True
        self._auto_cache[camera_id] = (now, want)
        return want

    @staticmethod
    def theft_clip_enabled(camera_id: str) -> bool:
        """The camera saves a clip with each theft incident (theft_clip and theft_detection on)."""
        try:
            from app.services.feature_manager import feature_manager

            return (feature_manager.is_enabled(camera_id, "theft_detection")
                    and feature_manager.is_enabled(camera_id, "theft_clip"))
        except Exception:  # noqa: BLE001 - unreadable settings: no clip (the default is off)
            return False

    def invalidate(self, camera_id: Optional[str] = None) -> None:
        """Forget cached "keep a ring" answers (after a camera's features changed)."""
        if camera_id is None:
            self._auto_cache.clear()
        else:
            self._auto_cache.pop(camera_id, None)

    def wants(self, camera_id: str) -> bool:
        """Should the camera worker push frames into this camera's ring now?"""
        if self._demand and self._demand.get(camera_id, 0.0) > time.monotonic():
            return True
        return self.keeps_ring(camera_id)

    def get_or_create_buffer(self, camera_id: str, fps: int = 15) -> StreamRingBuffer:
        with self._service_lock:
            if camera_id not in self.buffers:
                self.buffers[camera_id] = StreamRingBuffer(
                    camera_id=camera_id,
                    max_seconds=settings.PRE_EVENT_BUFFER_SECONDS,
                    fps=fps
                )
            return self.buffers[camera_id]

    def save_snapshot(self, camera_id: str, event_id: str) -> Optional[str]:
        """Save high-resolution snapshot for rich push notification.

        From the clip ring when it is being fed, else (no ring kept for this
        camera, CLIP_PRE_EVENT_BUFFER) the live pipeline's latest frame, which
        carries the privacy masks like the ring's frames do.
        """
        buf = self.buffers.get(camera_id)
        frame = None
        if buf is not None and time.time() - buf.latest_frame_time <= 5.0:
            frame = buf.get_latest_frame_copy()
        if frame is None:
            try:
                from app.services.live_analytics_engine import live_engine

                frame = live_engine.get_frame(camera_id)
            except Exception:  # noqa: BLE001 - no pipeline: no snapshot
                frame = None
        if frame is None:
            return None

        filename = f"{event_id}.jpg"
        filepath = settings.SNAPSHOTS_DIR / filename
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not cv2.imwrite(str(filepath), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
            # Nothing was saved: no link to a still that does not exist.
            return None
        note_evidence_written(filepath)

        # A same-origin path without a token, not EDGE_BASE_URL: right on every
        # address the dashboard or phone uses; a scoped token is added when the
        # link is served (services/evidence_urls.py).
        return f"/api/v1/events/snapshots/{filename}"

    async def record_event_clip(
        self,
        event_id: str,
        camera_id: str,
        post_roll_seconds: int = 10,
        fps: int = 15
    ) -> str:
        """Pre-event ring plus post-roll as an MP4; returns its link, or "" when no clip was written.

        The waiting and muxing run on a worker thread (they used to decode the
        ring and copy a frame every 1/15 s on the event loop), each new ring
        frame is used once, and the clip plays at the rate the frames were
        captured, not 3x fast (audit L7).
        """
        try:
            total, used, free = shutil.disk_usage(str(settings.CLIPS_DIR))
        except OSError:
            total, used = 1, 0
        if total and (used / total) > 0.90:
            logger.critical(f"Disk usage > 90% ({(used/total)*100:.1f}%), skipping clip recording for {event_id}")
            ServiceHealthTracker.report_status("clip_recorder", "degraded", "Disk usage critical")
            return ""

        # The worker feeds the ring while the post-roll is recorded, whatever
        # CLIP_PRE_EVENT_BUFFER says (pre-event frames exist only where it is kept).
        self.request(camera_id, post_roll_seconds + 2.0)
        buf = self.get_or_create_buffer(camera_id, fps=fps)
        output_path = settings.CLIPS_DIR / f"{event_id}.mp4"
        ok = await asyncio.to_thread(self._record_blocking, buf, output_path, time.time(),
                                     None, float(post_roll_seconds))
        if not ok:
            logger.info(f"No clip for {event_id}: camera {camera_id} delivered no frames")
            return ""
        ServiceHealthTracker.report_status("clip_recorder", "healthy", "Clip recorded successfully")
        # No token in the stored link (services/evidence_urls.py).
        return f"/api/v1/events/clips/{output_path.name}"

    def _record_blocking(self, buf: "StreamRingBuffer", path: Path, event_ts: float,
                         pre_seconds: Optional[float], post_seconds: float) -> bool:
        """Collect the ring's frames around ``event_ts`` and mux them to ``path`` (blocking)."""
        now = time.time()
        max_age = None if pre_seconds is None else (now - event_ts) + float(pre_seconds)
        items = buf.get_pre_event_jpegs(max_age=max_age)
        end = event_ts + max(0.0, float(post_seconds))
        last_ts = items[-1][0] if items else now - 1.0
        tick = 1.0 / max(1, int(buf.fps))
        while time.time() < end:
            time.sleep(min(tick, max(0.0, end - time.time())) or 0.01)
            new = buf.jpegs_since(last_ts)
            if new:
                items.extend(new)
                last_ts = new[-1][0]
        if not items:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._mux_jpegs_to_mp4(items, path)
        except Exception as e:  # noqa: BLE001 - an encoder failure loses the clip, not the alert
            logger.error(f"Clip {path.name} not written: {e}")
            return False
        if not path.is_file():
            return False
        note_evidence_written(path)
        logger.info(f"Clip recorded: {path.name} ({len(items)} frames, {path.stat().st_size} bytes)")
        return True

    @staticmethod
    def capture_fps(items: List[Tuple[float, bytes]], default: float = 5.0) -> float:
        """Frames per second the ring was really fed at (so clips play in real time)."""
        if len(items) < 2:
            return float(default)
        span = items[-1][0] - items[0][0]
        if span <= 0:
            return float(default)
        return float(min(30.0, max(1.0, (len(items) - 1) / span)))

    def _mux_jpegs_to_mp4(self, items: List[Tuple[float, bytes]], output_path: Path,
                          fps: Optional[float] = None) -> None:
        """H.264 MP4 (+faststart) from JPEG bytes piped to ffmpeg; no decoded frame list in memory."""
        if not items:
            return
        rate = fps or self.capture_fps(items)
        tmp = output_path.with_suffix(f".{os.getpid()}.part.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", f"{rate:.3f}",
               "-c:v", "mjpeg", "-i", "-",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except OSError as e:  # no ffmpeg: decode one frame at a time into OpenCV's writer
            logger.warning(f"ffmpeg not available ({e}); writing {output_path.name} with OpenCV")
            self._write_opencv(items, output_path, rate)
            return
        try:
            for _, data in items:
                proc.stdin.write(data)
            proc.stdin.close()
            _, err = proc.communicate(timeout=120.0)
        except Exception:
            proc.kill()
            proc.wait()
            tmp.unlink(missing_ok=True)
            raise
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            logger.error(f"ffmpeg could not encode {output_path.name}: {(err or b'').decode(errors='replace')[:300]}")
            self._write_opencv(items, output_path, rate)
            return
        os.replace(tmp, output_path)
        logger.info(f"Encoding successful for {output_path.name} | {len(items)} frames at {rate:.1f} fps | "
                    f"encoding_latency_ms={int((time.time() - started) * 1000)}")

    @staticmethod
    def _write_opencv(items: List[Tuple[float, bytes]], output_path: Path, rate: float) -> None:
        writer = None
        size = None
        try:
            for _, data in items:
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                if writer is None:
                    size = (img.shape[1], img.shape[0])
                    writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), rate, size)
                if (img.shape[1], img.shape[0]) != size:
                    img = cv2.resize(img, size)
                writer.write(img)
        finally:
            if writer is not None:
                writer.release()

    def record_incident_clip(
        self,
        incident_id: str,
        camera_id: str,
        out_dir: Path,
        event_ts: Optional[float] = None,
        pre_seconds: Optional[float] = None,
        post_seconds: Optional[float] = None,
    ) -> Optional[Path]:
        """Theft evidence clip: the ring's pre-event part plus the post-roll, as ``<id>.mp4``.

        Blocking (it waits for the post-roll): run it on a worker thread.
        ``event_ts`` is when the behaviour was seen (epoch seconds); the clip
        covers about ``pre_seconds`` before it (as much as the ring holds) to
        ``post_seconds`` after it. Frames come from the same masked ring as
        every other clip and are muxed by ``_mux_frames_to_mp4``. Returns the
        file, or None when no real frame was available (no clip is invented).
        The file lands in the theft evidence directory, so the evidence
        storage limit counts it and ages it out with the incident's image.
        """
        pre = float(settings.PRE_EVENT_BUFFER_SECONDS if pre_seconds is None else pre_seconds)
        post = float(settings.POST_EVENT_RECORD_SECONDS if post_seconds is None else post_seconds)
        now = time.time()
        event_ts = now if event_ts is None else min(float(event_ts), now)
        end = event_ts + max(0.0, post)
        # Keep the worker feeding the ring for the post-roll whatever the mode.
        self.request(camera_id, max(0.0, end - now) + 2.0)
        buf = self.buffers.get(camera_id)
        if buf is None:
            # No ring yet (e.g. theft_clip was switched on a moment ago): wait
            # briefly for the worker's first push, then record post-roll only.
            deadline = time.monotonic() + 2.0
            while buf is None and time.monotonic() < deadline:
                time.sleep(0.1)
                buf = self.buffers.get(camera_id)
            if buf is None:
                logger.info(f"No clip for incident {incident_id}: camera {camera_id} delivered no frames")
                return None
        path = Path(out_dir) / f"{incident_id}.mp4"
        if not self._record_blocking(buf, path, event_ts, pre, post):
            logger.info(f"No clip for incident {incident_id}: no frames from {camera_id} around the event")
            return None
        return path

    def _mux_frames_to_mp4(self, frames: List[np.ndarray], output_path: Path, fps: int):
        """Write frames to H.264 MP4 with faststart flags for instant streaming."""
        if not frames:
            logger.warning(f"No frames to write for {output_path}")
            return

        height, width, _ = frames[0].shape
        temp_raw_path = output_path.with_suffix(f".{os.getpid()}.temp.mp4")

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(temp_raw_path), fourcc, fps, (width, height))
        for frame in frames:
            out.write(frame)
        out.release()

        start_time = time.time()
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(temp_raw_path),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60.0)
            encoding_latency_ms = int((time.time() - start_time) * 1000)
            logger.info(f"Encoding successful for {output_path.name} | encoding_latency_ms={encoding_latency_ms}")
        except Exception as e:
            logger.error(f"FFmpeg remuxing fallback to raw video: {e}")
            if temp_raw_path.exists():
                temp_raw_path.rename(output_path)
        finally:
            if temp_raw_path.exists():
                try:
                    temp_raw_path.unlink()
                except OSError:
                    pass

clip_recorder_service = ClipRecorderService()
