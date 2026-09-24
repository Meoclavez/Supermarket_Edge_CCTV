"""A camera that accepts the TCP connection but never answers must not hold a worker.

Previously ``CameraWorker._open`` passed no open/read timeout and only the
``stimeout`` FFmpeg option, which current FFmpeg ignores, so one such camera
blocked for ~30 s on every reconnect attempt. The timed opens run in a fresh
subprocess: OpenCV serialises FFmpeg opens behind a process-wide lock, so
workers left running by other tests in this process would skew the timing.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services import live_analytics_engine as lae

BACKEND = Path(__file__).resolve().parent.parent

PROBE = r"""
import json, sys, time
from app.config import settings
settings.CAMERA_OPEN_TIMEOUT_SEC = 2.0
settings.CAMERA_READ_TIMEOUT_SEC = 2.0
from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine
url = sys.argv[1]
w = CameraWorker(CameraRuntime(camera_id="cam_timeout", name="t", source=url), LiveAnalyticsEngine())
t0 = time.monotonic()
try:
    cap = w._open(url)
    opened = cap.isOpened()
    cap.release()
    err = None
except Exception as e:
    opened, err = False, str(e)
print(json.dumps({"elapsed": time.monotonic() - t0, "opened": opened, "error": err}))
"""


@pytest.fixture
def silent_server():
    """Accepts connections and never sends a byte (a hung camera / NVR)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held, stop = [], threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                continue

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    for c in held:
        c.close()
    srv.close()


def _probe(url: str) -> dict:
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, "-c", PROBE, url], cwd=BACKEND, capture_output=True, text=True, timeout=60)
    line = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert line, out.stderr[-2000:]
    res = json.loads(line[-1])
    res["wall"] = time.monotonic() - t0
    return res


def test_ffmpeg_options_use_a_timeout_the_linked_ffmpeg_honours():
    opts = lae.ffmpeg_capture_options()
    assert opts.startswith("rtsp_transport;tcp|")
    assert "rw_timeout;" in opts and ("|timeout;" in opts or "|stimeout;" in opts)


@pytest.mark.parametrize("scheme,path", [("rtsp", "/cam/realmonitor?channel=1&subtype=1"), ("http", "/video.mjpg")])
def test_unresponsive_stream_open_returns_within_the_timeout(silent_server, scheme, path):
    res = _probe(f"{scheme}://127.0.0.1:{silent_server}{path}")
    assert res["opened"] is False
    assert res["elapsed"] < 6.0, f"open blocked for {res['elapsed']:.1f}s (timeout 2 s)"


def test_unresolvable_host_fails_fast_without_entering_ffmpeg():
    res = _probe("rtsp://camera-that-does-not-exist.invalid:554/stream")
    assert res["opened"] is False and "resolve" in (res["error"] or "")
    assert res["elapsed"] < 6.0
