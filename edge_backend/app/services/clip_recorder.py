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
from app.services.auth_service import auth_service
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
        """Compress BGR frame to JPEG and store with timestamp under mutex lock."""
        ret, encoded_jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
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

    def get_latest_frame_copy(self) -> Optional[np.ndarray]:
        with self._lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None


class ClipRecorderService:
    """Per-camera pre-event rings, fed by the camera workers while ``wants()``.

    Keeping a ring means JPEG-encoding ~5 frames a second per camera, so it
    is kept only where a clip can be needed (CLIP_PRE_EVENT_BUFFER): always
    ("on"), never ("off"), or ("auto") for a camera whose armed night watch
    saves clips (NIGHT_WATCH_CLIP). A clip being recorded asks for frames for
    its post-roll (``request``) in every mode, so an on-demand clip still
    gets real footage from the moment it was asked for.
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
        want = False
        if settings.NIGHT_WATCH_CLIP:
            try:
                from app.services.night_watch import night_watch

                want = night_watch.is_armed(camera_id)
            except Exception:  # noqa: BLE001 - unknown: keep the ring rather than lose a clip
                want = True
        self._auto_cache[camera_id] = (now, want)
        return want

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
        if cv2.imwrite(str(filepath), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
            note_evidence_written(filepath)

        token = auth_service.generate_clip_token(event_id)
        return f"{settings.EDGE_BASE_URL}/api/v1/events/snapshots/{filename}?token={token}"

    async def record_event_clip(
        self,
        event_id: str,
        camera_id: str,
        post_roll_seconds: int = 10,
        fps: int = 15
    ) -> str:
        """Capture pre-event buffer and record post-roll frames, muxing into an optimized MP4."""
        total, used, free = shutil.disk_usage(str(settings.CLIPS_DIR))
        if (used / total) > 0.90:
            logger.critical(f"Disk usage > 90% ({(used/total)*100:.1f}%), skipping clip recording for {event_id}")
            ServiceHealthTracker.report_status("clip_recorder", "degraded", "Disk usage critical")
            return ""

        # The worker feeds the ring while the post-roll is recorded, whatever
        # CLIP_PRE_EVENT_BUFFER says (pre-event frames exist only where it is kept).
        self.request(camera_id, post_roll_seconds + 2.0)
        buf = self.get_or_create_buffer(camera_id, fps=fps)
        pre_frames = buf.get_pre_event_frames()
        post_frames = []

        logger.info(f"Starting clip capture for {event_id} ({camera_id}): {len(pre_frames)} pre-frames")

        start_time = time.time()
        while time.time() - start_time < post_roll_seconds:
            # Not a frame left over from an earlier, on-demand feed.
            latest = buf.get_latest_frame_copy() if buf.latest_frame_time >= start_time - 1.0 else None
            if latest is not None:
                post_frames.append(latest)
            await asyncio.sleep(1.0 / fps)

        all_frames = pre_frames + post_frames

        output_filename = f"{event_id}.mp4"
        output_path = settings.CLIPS_DIR / output_filename

        await asyncio.to_thread(self._mux_frames_to_mp4, all_frames, output_path, fps)

        if output_path.exists():
            file_size_bytes = output_path.stat().st_size
            clip_duration_ms = int((len(all_frames) / fps) * 1000)
            logger.info(f"Clip recorded: {output_filename} | clip_duration_ms={clip_duration_ms} | file_size_bytes={file_size_bytes}")
            ServiceHealthTracker.report_status("clip_recorder", "healthy", "Clip recorded successfully")
            note_evidence_written(output_path)

        token = auth_service.generate_clip_token(event_id)
        clip_url = f"{settings.EDGE_BASE_URL}/api/v1/events/clips/{output_filename}?token={token}"
        return clip_url

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
