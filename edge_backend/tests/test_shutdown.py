"""Graceful shutdown must finish in seconds, even with live MJPEG streams open.

Uvicorn runs the lifespan shutdown only after every open connection has
finished; an endless ``/stream`` response used to keep it waiting until the
process was SIGKILLed (skipping the final flush of visits and tracks).
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

EDGE = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_sigterm_with_open_streams_exits_within_seconds(tmp_path):
    port = _free_port()
    key = secrets.token_hex(16)
    empty_models = tmp_path / "models"
    empty_models.mkdir()
    env = dict(
        os.environ,
        STORAGE_DIR=str(tmp_path),
        DATABASE_PATH=str(tmp_path / "db.sqlite"),
        SQLITE_DB_PATH=str(tmp_path / "db.sqlite"),
        MODELS_DIR=str(empty_models),       # no model: fast start, detector honestly unavailable
        OBJECT_MODEL_PATH="",
        INTERNAL_SERVICE_KEY=key,
        PYTHONPATH=str(EDGE),
    )
    log = open(tmp_path / "server.log", "w")
    # No --timeout-graceful-shutdown: the application itself must end the streams.
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(tmp_path), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    streams = []
    try:
        deadline = time.time() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if proc.poll() is not None or time.time() > deadline:
                    pytest.fail("server did not start:\n" + (tmp_path / "server.log").read_text()[-3000:])
                time.sleep(0.2)
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(f"GET /stream?fps=5 HTTP/1.1\r\nHost: t\r\nX-Edge-API-Key: {key}\r\n\r\n".encode())
            assert s.recv(4096).startswith(b"HTTP/1.1 200"), "stream did not start"
            streams.append(s)
        time.sleep(0.5)

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("server still running 8 s after SIGTERM:\n"
                        + (tmp_path / "server.log").read_text()[-2000:])
        elapsed = time.monotonic() - started
        text = (tmp_path / "server.log").read_text()
        assert "Application shutdown complete" in text, text[-2000:]
        assert elapsed < 5.0, f"shutdown took {elapsed:.1f}s"
    finally:
        for s in streams:
            s.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()


def test_stop_all_joins_workers_and_bounds_the_wait():
    from app.services.live_analytics_engine import LiveAnalyticsEngine

    class _Worker(threading.Thread):
        def __init__(self, name, ignore_stop=False):
            super().__init__(daemon=True, name=name)
            self._ev = threading.Event()
            self.ignore_stop = ignore_stop

        def stop(self):
            self._ev.set()

        def run(self):
            if self.ignore_stop:          # e.g. blocked in an RTSP read
                time.sleep(3)
                return
            self._ev.wait()
            time.sleep(0.05)              # closes its tracks on the way out

    eng = LiveAnalyticsEngine()
    good, stuck = _Worker("cam-good"), _Worker("cam-stuck", ignore_stop=True)
    for w in (good, stuck):
        w.start()
        eng.workers[w.name] = w
    started = time.monotonic()
    left = eng.stop_all(timeout=0.5)
    elapsed = time.monotonic() - started
    assert not good.is_alive()
    assert left == ["cam-stuck"]
    assert elapsed < 1.5
    assert eng.workers == {}
