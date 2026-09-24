"""The dashboard's CPU reading is a real measurement from any request thread.

psutil.cpu_percent(interval=None) keeps its baseline per calling thread, and
FastAPI runs sync routes on a thread pool, so a thread's first call returned
0.0 and System health showed "0.0 % busy" on every fresh thread.
"""

from __future__ import annotations

import threading
from collections import namedtuple

import pytest

from app.routes import system

T = namedtuple("T", "user nice system idle iowait irq softirq steal guest guest_nice")


class FakeClock:
    """cpu_times() for a machine that is 40 % busy: +0.4 s busy per +1.0 s total per call."""

    def __init__(self):
        self.n = 0

    def cpu_times(self):
        self.n += 1
        busy, idle = 0.4 * self.n, 0.6 * self.n
        return T(busy, 0.0, 0.0, idle, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


@pytest.fixture
def fake_psutil(monkeypatch):
    import psutil

    clock = FakeClock()
    monkeypatch.setattr(psutil, "cpu_times", clock.cpu_times)
    monkeypatch.setattr(system, "_cpu_prev", None)
    monkeypatch.setattr(system, "_cpu_value", None)
    monkeypatch.setattr(system, "CPU_MIN_WINDOW_S", 0.0)
    monkeypatch.setattr(system.time, "sleep", lambda s: None)
    return clock


def test_first_reading_is_measured_not_zero(fake_psutil):
    assert system._cpu_percent() == 40.0


def test_readings_from_new_threads_are_real(fake_psutil):
    system._cpu_percent()
    out = []
    threads = [threading.Thread(target=lambda: out.append(system._cpu_percent())) for _ in range(4)]
    for t in threads:
        t.start()
        t.join()
    assert out == [40.0] * 4


def test_close_readings_reuse_the_last_value(fake_psutil, monkeypatch):
    monkeypatch.setattr(system, "CPU_MIN_WINDOW_S", 60.0)
    first = system._cpu_percent()
    calls = fake_psutil.n
    assert system._cpu_percent() == first
    assert fake_psutil.n == calls           # no new sample inside the window


def test_real_machine_reading_in_range():
    system._cpu_prev = system._cpu_value = None
    value = system._cpu_percent()
    assert value is not None and 0.0 <= value <= 100.0


def test_video_decoding_reports_what_capture_uses():
    """A VA-API render node is a capability; capture still decodes on the CPU."""
    from app.services import hardware_detector as hd

    in_use, note = hd._decoder_in_use("vaapi_amd")
    assert in_use == "cpu"
    assert "software" in note and "vaapi_amd decode hardware is present but not used" in note
    assert hd._decoder_in_use("cpu") == ("cpu", "camera streams are decoded in software (FFmpeg via OpenCV) on the CPU")
    profile = hd.HardwareDetector.detect_hardware()
    assert profile.decoder_in_use == "cpu" and profile.decoder_note
