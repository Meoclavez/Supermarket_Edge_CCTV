"""The live pipeline: frames in, persisted retail facts out.

This is the piece the system never had. Every subsystem around it was real --
the RTSP ingest worker, the DVR segmenter, the funnel maths, the decision
engine -- but nothing connected a camera to any of them, so every number on the
dashboard came from a literal in a route handler.

One worker thread per enabled camera does the following loop:

    capture frame -> (every Nth frame) detect people -> associate into tracks
      -> project foot point to floor metres -> resolve which zone that is
      -> emit zone entry/exit facts -> persist

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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

from app.config import settings
from app.services.inference_backend import person_detector
from app.services.store_layout_service import point_in_polygon
from app.services.tracking_service import CentroidTracker, Track, floor_projector

logger = logging.getLogger(__name__)


@dataclass
class ZoneVisitFact:
    """A dwell, emitted twice: once when it becomes real, once when it ends.

    A visit is opened as soon as the person has stayed past the dwell floor,
    not when they leave. Waiting for the exit would make anyone currently
    standing in an aisle invisible to the dashboard, so "shoppers in store
    right now" could never be answered from the database.
    """

    visit_id: str
    phase: str                       # "open" | "close"
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
    status: str = "STARTING"        # STARTING | ONLINE | OFFLINE | DISABLED
    last_error: Optional[str] = None
    frames_read: int = 0
    detections_last: int = 0
    live_track_count: int = 0
    fps: float = 0.0
    last_frame_at: float = 0.0
    connected_at: Optional[float] = None
    # Native pixel size of the frames being read; None until the first frame.
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    _frame: Optional[np.ndarray] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Live tracks as plain dicts, rewritten by the worker after every analysis
    # pass and read by the API / stream overlay. Never mutated in place.
    _tracks: list[dict] = field(default_factory=list, repr=False)
    _tracks_at: float = 0.0

    def put_frame(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        with self._lock:
            self._frame = frame
            self.frame_width, self.frame_height = int(w), int(h)

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def set_tracks(self, tracks: list[dict], now: Optional[float] = None) -> None:
        with self._lock:
            self._tracks = list(tracks)
            self._tracks_at = now or time.time()

    def get_tracks(self) -> list[dict]:
        with self._lock:
            return list(self._tracks)

    @property
    def calibrated(self) -> bool:
        return floor_projector.has_homography(self.camera_id)

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "name": self.name,
            "status": self.status,
            "enabled": self.enabled,
            "frames_read": self.frames_read,
            "detections_last_frame": self.detections_last,
            "live_tracks": self.live_track_count,
            "fps": round(self.fps, 1),
            "has_frame": self._frame is not None,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "calibrated": self.calibrated,
            "seconds_since_frame": (
                round(time.time() - self.last_frame_at, 1) if self.last_frame_at else None
            ),
            "last_error": self.last_error,
        }


class CameraWorker(threading.Thread):
    """Captures and analyses one camera."""

    def __init__(self, runtime: CameraRuntime, engine: "LiveAnalyticsEngine"):
        super().__init__(daemon=True, name=f"cam-{runtime.camera_id}")
        self.rt = runtime
        self.engine = engine
        self.tracker = CentroidTracker(runtime.camera_id)
        self._stop = threading.Event()
        self._frame_index = 0

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ capture

    def _open(self):
        import cv2

        src = self.rt.source
        # A bare /dev/videoN path and an RTSP URL both go through VideoCapture,
        # but network streams need a latency-bounded transport or frames pile
        # up in the decoder until the feed is minutes behind real time.
        if src.startswith("rtsp://"):
            import os

            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000"
            )
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        elif src.startswith("http://") or src.startswith("https://"):
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(src)

        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def run(self) -> None:
        import cv2

        backoff = 1.0
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._open()
                if not cap or not cap.isOpened():
                    raise RuntimeError(f"cannot open source {self.rt.source}")

                self.rt.status = "ONLINE"
                self.rt.last_error = None
                self.rt.connected_at = time.time()
                backoff = 1.0
                logger.info(f"Camera {self.rt.camera_id} connected")

                last_tick = time.time()
                frames_in_window = 0

                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        raise RuntimeError("stream ended or read failed")

                    self.rt.frames_read += 1
                    self.rt.last_frame_at = time.time()
                    self.rt.put_frame(frame)
                    frames_in_window += 1
                    self._frame_index += 1
                    # Feed the pre-event ring buffer so incident clips and
                    # the clip export action are cut from real footage. It
                    # JPEG-encodes on push, so it is fed at the analysis
                    # cadence rather than every decoded frame.
                    if self._frame_index % settings.ANALYTICS_DETECT_EVERY_N_FRAMES == 0:
                        self._push_clip_buffer(frame)

                    now = time.time()
                    if now - last_tick >= 2.0:
                        self.rt.fps = frames_in_window / (now - last_tick)
                        frames_in_window = 0
                        last_tick = now

                    if self._frame_index % settings.ANALYTICS_DETECT_EVERY_N_FRAMES == 0:
                        self._analyse(frame, now)

            except Exception as e:
                self.rt.status = "OFFLINE"
                self.rt.last_error = str(e)
                # A camera that drops out contributes nothing until it returns.
                # Its in-flight tracks are closed so their dwell is not left
                # accumulating against a feed that is no longer arriving.
                for t in self.tracker.flush_all():
                    self.engine.close_track(t, reason="camera_offline")
                self.rt.set_tracks([])
                self.rt.live_track_count = 0
                self.rt.detections_last = 0
                logger.warning(f"Camera {self.rt.camera_id} error: {e}; retrying in {backoff:.0f}s")
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass

            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

        self.rt.status = "DISABLED" if not self.rt.enabled else "OFFLINE"
        for t in self.tracker.flush_all():
            self.engine.close_track(t, reason="worker_stopped")

    def _push_clip_buffer(self, frame: np.ndarray) -> None:
        try:
            from app.services.clip_recorder import clip_recorder_service

            fps = max(1, int(round((self.rt.fps or 5.0) / settings.ANALYTICS_DETECT_EVERY_N_FRAMES)) or 1)
            clip_recorder_service.get_or_create_buffer(self.rt.camera_id, fps=fps).push_frame(frame)
        except Exception as e:  # never let the recorder take the capture loop down
            logger.debug(f"clip buffer push failed for {self.rt.camera_id}: {e}")

    # ----------------------------------------------------------------- analysis

    def _analyse(self, frame: np.ndarray, now: float) -> None:
        detections = person_detector.detect(frame)
        self.rt.detections_last = len(detections)

        live = self.tracker.update(detections, now=now)
        self.rt.live_track_count = sum(1 for t in live if t.confirmed)

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
                "age_seconds": round(t.age_seconds, 1),
                # Filled only when a homography exists; otherwise the track
                # has no place on the blueprint and these stay None.
                "x_m": None,
                "y_m": None,
                "zone_id": None,
            }
            if t.confirmed:
                fx, fy = t.foot_point
                floor = floor_projector.to_floor(self.rt.camera_id, fx, fy)
                if floor is not None:
                    t.floor_points.append(
                        {"x": round(floor[0], 2), "y": round(floor[1], 2), "t": round(now, 2)}
                    )
                    self.engine.attribute_zone(t, floor[0], floor[1], now)
                    entry["x_m"] = round(floor[0], 2)
                    entry["y_m"] = round(floor[1], 2)
                    entry["zone_id"] = t.current_zone_id
            snapshot.append(entry)
        self.rt.set_tracks(snapshot, now)

        for finished in self.tracker.drain_finished():
            self.engine.close_track(finished, reason="track_lost")


class LiveAnalyticsEngine:
    """Owns the worker pool and buffers the facts they produce."""

    def __init__(self):
        self.runtimes: dict[str, CameraRuntime] = {}
        self.workers: dict[str, CameraWorker] = {}
        self._zones: list[dict] = []          # [{id, category, polygon}]
        self._zones_lock = threading.Lock()
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

    def _zone_at(self, x_m: float, y_m: float) -> Optional[str]:
        with self._zones_lock:
            for z in self._zones:
                if point_in_polygon(x_m, y_m, z["polygon"]):
                    return z["id"]
        return None

    # ------------------------------------------------------------------- facts

    def attribute_zone(self, track: Track, x_m: float, y_m: float, now: float) -> None:
        """Place a track in a zone, opening and closing visits as it moves."""
        zone_id = self._zone_at(x_m, y_m)

        if zone_id == track.current_zone_id:
            # Still in the same zone. Once the dwell floor is cleared the visit
            # becomes real and is published immediately so live occupancy is
            # answerable while the person is still standing there.
            if (
                zone_id
                and track.open_visit_id is None
                and track.zone_entered_at is not None
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
                        entered_at=datetime.fromtimestamp(track.zone_entered_at),
                    )
                )
            return

        self._close_open_visit(track, now)

        track.current_zone_id = zone_id
        track.zone_entered_at = now if zone_id else None
        track.interacted = False

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
                    entered_at=datetime.fromtimestamp(track.zone_entered_at),
                    exited_at=datetime.fromtimestamp(now),
                    dwell_seconds=round(now - track.zone_entered_at, 2),
                    interacted=track.interacted,
                )
            )
        track.open_visit_id = None

    def close_track(self, track: Track, reason: str = "") -> None:
        """Finalise a track: close its open visit and queue it for storage."""
        if not track.confirmed:
            return
        self._close_open_visit(track, track.last_seen)
        track.current_zone_id = None
        track.zone_entered_at = None

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
        self.stop_camera(camera_id)
        rt = CameraRuntime(camera_id=camera_id, name=name, source=source, enabled=enabled)
        self.runtimes[camera_id] = rt
        if not enabled:
            rt.status = "DISABLED"
            return
        worker = CameraWorker(rt, self)
        self.workers[camera_id] = worker
        worker.start()

    def stop_camera(self, camera_id: str) -> None:
        if (w := self.workers.pop(camera_id, None)) is not None:
            w.stop()
        self.runtimes.pop(camera_id, None)

    def stop_all(self) -> None:
        for cid in list(self.workers.keys()):
            self.stop_camera(cid)

    def get_frame(self, camera_id: str) -> Optional[np.ndarray]:
        rt = self.runtimes.get(camera_id)
        return rt.get_frame() if rt else None

    def status(self) -> dict:
        runtimes = [rt.to_dict() for rt in self.runtimes.values()]
        online = sum(1 for r in runtimes if r["status"] == "ONLINE")
        return {
            "running": self._started,
            "cameras_total": len(runtimes),
            "cameras_online": online,
            "live_tracks": sum(r["live_tracks"] for r in runtimes),
            "detector": person_detector.status(),
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
            detections.append(
                {
                    "camera_id": rt.camera_id,
                    "camera_name": rt.name,
                    "status": rt.status,
                    "calibrated": calibrated,
                    "live_tracks": rt.live_track_count,
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
                        }
                        for t in tracks
                    ],
                }
            )
            for t in tracks:
                if not t["confirmed"]:
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
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "running": self._started,
            "persons": persons,
            "detections": detections,
            "persons_total": len(persons),
            "uncalibrated_track_count": uncalibrated_tracks,
        }

    def mark_started(self) -> None:
        self._started = True


def render_overlay(frame: np.ndarray, rt: CameraRuntime) -> np.ndarray:
    """Draw the worker's current track boxes onto a copy of ``frame``.

    Uses only the snapshot the worker already produced -- no inference runs
    here -- so the overlay costs a few rectangles per frame and always shows
    exactly what the pipeline is counting. Confirmed tracks are drawn solid,
    tentative ones (below the hit floor) thin, and a corner tag states
    whether the camera is calibrated so nobody mistakes boxes for positions.
    """
    import cv2

    out = frame if frame.flags.writeable else frame.copy()
    tracks = rt.get_tracks()
    confirmed_colour = (80, 220, 90)     # BGR green
    tentative_colour = (60, 200, 255)    # BGR amber

    for t in tracks:
        x1, y1, x2, y2 = int(t["x1"]), int(t["y1"]), int(t["x2"]), int(t["y2"])
        colour = confirmed_colour if t["confirmed"] else tentative_colour
        thickness = 2 if t["confirmed"] else 1
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, thickness)
        label = f"{t['track_id'][-4:]} {t['confidence']:.2f}"
        if t["x_m"] is not None:
            label += f" ({t['x_m']:.1f},{t['y_m']:.1f})m"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = max(th + 4, y1 - 4)
        cv2.rectangle(out, (x1, ty - th - 4), (x1 + tw + 4, ty + 2), colour, -1)
        cv2.putText(out, label, (x1 + 2, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10, 10, 10), 1)

    tag = "calibrated" if rt.calibrated else "uncalibrated"
    n_conf = sum(1 for t in tracks if t["confirmed"])
    header = f"{rt.name} | {tag} | {n_conf} tracked"
    cv2.rectangle(out, (0, 0), (10 + 8 * len(header), 22), (18, 20, 26), -1)
    cv2.putText(out, header, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 224, 232), 1)
    return out


live_engine = LiveAnalyticsEngine()
