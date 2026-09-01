"""Video ingestion service with threaded QuickSync VA-API frame grabbing and auto-reconnect."""

import time
import logging
import threading
from typing import Dict, Optional, Tuple
import cv2
import numpy as np

from app.services.clip_recorder import clip_recorder_service
from app.services.ai_zone_service import ai_zone_service

logger = logging.getLogger("VideoIngestService")


class ThreadedVideoIngestWorker:
    """Dedicated background OS thread for continuous RTSP decoding via OpenCV."""

    def __init__(self, camera_id: str, rtsp_url: str):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.is_running = False
        self.latest_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.fps = 0.0
        self.last_frame_time = 0.0

    def start(self):
        self.is_running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()
        logger.info(f"Spawned threaded video ingest worker for {self.camera_id}")

    def stop(self):
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info(f"Stopped video ingest worker for {self.camera_id}")

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

    def _worker_loop(self):
        backoff = 1.0
        ring_buffer = clip_recorder_service.get_or_create_buffer(self.camera_id)

        # Assuming CircuitBreaker from resilience.py
        # If camera fails 5 times, stop hammering for 60s
        from app.services.resilience import CircuitBreaker, ServiceHealthTracker
        circuit_breaker = CircuitBreaker(f"rtsp_{self.camera_id}", failure_threshold=5, recovery_timeout=60.0)
        
        reconnect_attempts = 0
        frame_errors = 0
        window_start = time.time()
        window_frames = 0
        
        was_open = False

        while self.is_running:
            if not circuit_breaker.can_execute():
                if not was_open:
                    logger.warning(f"Circuit OPEN for {self.camera_id}. Emitting CAMERA_OFFLINE & triggering auto-recovery search.")
                    ServiceHealthTracker.report_status("video_ingest", "degraded", f"Camera {self.camera_id} offline")
                    was_open = True
                    # Trigger background dynamic IP auto-recovery scan
                    try:
                        import asyncio
                        from app.services.camera_network_manager import camera_network_manager
                        loop = None
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            pass
                        if loop and loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                camera_network_manager.attempt_auto_recover(self.camera_id, self.rtsp_url),
                                loop
                            )
                    except Exception as e:
                        logger.debug(f"Auto-recovery dispatch note: {e}")
                time.sleep(1.0)
                continue
            
            if was_open and circuit_breaker.state == "CLOSED":
                logger.info(f"Circuit CLOSED for {self.camera_id}. Emitting CAMERA_RECOVERED.")
                ServiceHealthTracker.report_status("video_ingest", "healthy", f"Camera {self.camera_id} recovered")
                was_open = False

            # Set RTSP over TCP and socket timeout (5 seconds in microseconds)
            gst_pipeline = (
                f"rtspsrc location={self.rtsp_url} protocols=tcp timeout=5000000 ! "
                "rtph264depay ! vaapih264dec ! videoconvert ! appsink"
            )

            reconnect_attempts += 1
            # Try VA-API accelerated GStreamer pipeline first, fallback to standard RTSP
            cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                logger.debug(f"GStreamer VA-API not available for {self.camera_id}. Using OpenCV FFMPEG.")
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                logger.warning(f"Failed to open RTSP stream {self.camera_id}. Reconnects: {reconnect_attempts}")
                circuit_breaker.record_failure()
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
                continue

            circuit_breaker.record_success()
            logger.info(f"Successfully connected to RTSP stream for {self.camera_id}")
            backoff = 1.0
            reconnect_attempts = 0
            frame_count = 0
            start_time = time.time()

            while self.is_running:
                decode_start = time.time()
                try:
                    ret, frame = cap.read()
                except Exception as e:
                    logger.error(f"OpenCV read error for {self.camera_id}: {e}")
                    ret, frame = False, None
                decode_latency_ms = int((time.time() - decode_start) * 1000)
                
                now = time.time()
                if now - window_start >= 60.0:
                    if window_frames > 0 and (frame_errors / window_frames) > 0.5:
                        logger.warning(f"High frame error rate for {self.camera_id} (>50% in 60s). Consider lowering resolution.")
                        ServiceHealthTracker.report_status("video_ingest", "degraded", f"High frame error rate for {self.camera_id}")
                    frame_errors = 0
                    window_frames = 0
                    window_start = now

                window_frames += 1
                
                if not ret or frame is None:
                    frame_errors += 1
                    logger.warning(f"RTSP stream dropped for {self.camera_id}. Reconnecting...")
                    break

                # Apply Privacy Masking in-place on raw frame before buffer & snapshots
                masked_frame = ai_zone_service.mask_frame(self.camera_id, frame)

                frame_count += 1
                if now - start_time >= 1.0:
                    self.fps = frame_count / (now - start_time)
                    if frame_count % 30 == 0:
                        logger.debug(f"[{self.camera_id}] fps={self.fps:.1f} | frames={frame_count} | decode_latency_ms={decode_latency_ms} | reconnects={reconnect_attempts}")
                    frame_count = 0
                    start_time = now

                with self._lock:
                    self.latest_frame = masked_frame
                    self.last_frame_time = now

                # Push masked frame into thread-safe JPEG compressed ring buffer
                ring_buffer.push_frame(masked_frame)

            cap.release()
            if self.is_running:
                time.sleep(1.0)


class VideoIngestService:
    def __init__(self):
        self.workers: Dict[str, ThreadedVideoIngestWorker] = {}
        self._lock = threading.Lock()

    async def register_and_start_camera(self, camera_id: str, rtsp_url: str):
        with self._lock:
            if camera_id in self.workers:
                self.workers[camera_id].stop()
            worker = ThreadedVideoIngestWorker(camera_id, rtsp_url)
            self.workers[camera_id] = worker
            worker.start()

    def get_latest_frame(self, camera_id: str) -> Optional[np.ndarray]:
        worker = self.workers.get(camera_id)
        return worker.get_latest_frame() if worker else None

    async def stop_all(self):
        with self._lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()


video_ingest_service = VideoIngestService()
