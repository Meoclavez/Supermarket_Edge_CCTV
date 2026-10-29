"""Stuck cameras restart themselves (owner decision 2026-10-29, automation A9).

pipeline_supervisor already restarts a camera whose worker thread died. A
worker that is alive but stuck (status STALLED: ONLINE with no new picture
for live_analytics_engine.stall_seconds()) is restarted too, once it has
been stuck for twice that long, at most ``MAX_PER_HOUR`` times an hour per
camera and never twice within ``MIN_GAP_S``: a camera that is really broken
is not restarted in a loop (its status keeps saying why).

Every restart is counted per store-local day for the dashboard ("restarted 2
times today"). Memory only: the count starts again after a box restart.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

MAX_PER_HOUR = 6
MIN_GAP_S = 300.0
STUCK_FACTOR = 2.0


class CameraRestarts:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._log: Dict[str, Deque[tuple[float, str]]] = {}

    def record(self, camera_id: str, why: str, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            q = self._log.setdefault(camera_id, deque(maxlen=200))
            q.append((now, why))

    def may_restart(self, camera_id: str, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            q = self._log.get(camera_id) or ()
            recent = [t for t, _ in q if now - t < 3600]
        if len(recent) >= MAX_PER_HOUR:
            return False
        return not recent or now - max(recent) >= MIN_GAP_S

    def stuck_for_restart(self, rt, now: Optional[float] = None) -> bool:
        """The camera's worker is alive but has sent no picture for long enough to restart it."""
        from app.services.live_analytics_engine import stall_seconds

        now = self._clock() if now is None else now
        if getattr(rt, "status", None) != "ONLINE" or not getattr(rt, "last_frame_at", None):
            return False
        return now - float(rt.last_frame_at) > STUCK_FACTOR * stall_seconds()

    def today(self, now: Optional[float] = None) -> Dict[str, dict]:
        """camera_id -> {"today": n, "last_at": epoch, "last_why": str} for restarts since store midnight."""
        from app.services.timeutil import local_now

        now = self._clock() if now is None else now
        ln = local_now()
        midnight = ln.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out: Dict[str, dict] = {}
        with self._lock:
            for cam, q in self._log.items():
                today = [(t, w) for t, w in q if t >= midnight]
                if today:
                    out[cam] = {"today": len(today), "last_at": today[-1][0], "last_why": today[-1][1]}
        return out

    def forget(self, camera_id: str) -> None:
        with self._lock:
            self._log.pop(camera_id, None)


camera_restarts = CameraRestarts()
