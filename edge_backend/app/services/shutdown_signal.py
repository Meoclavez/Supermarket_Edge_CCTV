"""Process-wide "the server is shutting down" flag.

Uvicorn runs the lifespan shutdown only *after* every open HTTP connection has
finished. A live MJPEG ``/stream`` never finishes on its own, so with a
dashboard open a SIGTERM used to print "Waiting for connections to close" and
hang until the service manager SIGKILLed the process -- which also skipped the
final flush of zone visits and tracks.

The fix is to learn about the shutdown at signal time: ``install()`` (called
from the lifespan startup, i.e. after uvicorn has installed its own handlers)
chains a handler in front of uvicorn's that sets :data:`shutdown_requested`.
Long-lived generators poll the event and end their response, so uvicorn's
connection drain completes immediately and the normal lifespan shutdown runs.

``--timeout-graceful-shutdown`` on the uvicorn command line stays as the
backstop for anything that does not poll this flag.
"""

from __future__ import annotations

import logging
import signal
import threading

logger = logging.getLogger(__name__)

shutdown_requested = threading.Event()


def install() -> bool:
    """Chain SIGINT/SIGTERM handlers that set :data:`shutdown_requested`.

    Returns False (and does nothing) off the main thread, e.g. under a test
    client, where Python does not allow installing signal handlers.
    """
    shutdown_requested.clear()
    if threading.current_thread() is not threading.main_thread():
        return False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)
        except (ValueError, OSError):  # pragma: no cover - platform specific
            continue
        if getattr(previous, "_edge_shutdown_chain", False):
            continue  # already chained in front of the current handler

        def _handler(signum, frame, _previous=previous):
            shutdown_requested.set()
            if callable(_previous):
                _previous(signum, frame)
            elif _previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        _handler._edge_shutdown_chain = True  # type: ignore[attr-defined]
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover
            continue
    return True


def request_shutdown() -> None:
    """Mark the process as shutting down (also used by the lifespan exit)."""
    shutdown_requested.set()
