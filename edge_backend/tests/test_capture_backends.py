"""GPU video decoding for camera capture (services/capture_backends.py).

Nothing here needs a GPU: the backend chain is exercised with a stubbed test
decode, and FfmpegHwCapture runs against tests/fixtures/fake_ffmpeg.py (and,
where a system ffmpeg exists, a real software decode of the bundled clip).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.services import capture_backends as cb
from app.services import live_analytics_engine as lae
from app.services.preflight import check_env

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_ffmpeg.py"
URL = "rtsp://admin:pa'ss@127.0.0.1:554/cam/realmonitor?channel=1&subtype=1"


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def _open_fake(monkeypatch, mode="frames", url=URL, **env):
    monkeypatch.setenv("FAKE_FFMPEG_MODE", mode)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return cb.FfmpegHwCapture(url, cb.VAAPI, "/dev/dri/renderD128", "gpu_rgb", max_fps=10, max_width=0,
                              open_timeout=3, read_timeout=1.5, ffmpeg="ffmpeg")


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Run the stand-in script wherever capture_backends would run ffmpeg."""
    real = cb.ffmpeg_argv

    def argv(ffmpeg, *a, **k):
        return [sys.executable, str(FAKE)] + real("ffmpeg", *a, **k)[1:]

    monkeypatch.setattr(cb, "ffmpeg_argv", argv)
    yield


@pytest.fixture(autouse=True)
def _no_leftover_ffmpeg():
    yield
    assert cb.live_ffmpeg_pids() == []


# ------------------------------------------------------------ command line

def test_url_and_login_travel_over_a_pipe_not_the_command_line(fake_ffmpeg, monkeypatch, tmp_path):
    rec = tmp_path / "rec.txt"
    cap = _open_fake(monkeypatch, FAKE_FFMPEG_RECORD=rec)
    try:
        assert cap.isOpened()
        with open(f"/proc/{cap.pid}/cmdline", "rb") as f:
            cmdline = f.read().decode()
    finally:
        cap.release()
    listing, argv = rec.read_text().split("\n--argv--\n")
    assert "pa'ss" not in cmdline and "rtsp://" not in cmdline and "rtsp://" not in argv
    assert "file 'rtsp://admin:pa'\\''ss@127.0.0.1:554/cam/realmonitor?channel=1&subtype=1'" in listing
    assert "option rtsp_transport tcp" in listing
    assert f"option timeout {int(1.5 * 1_000_000)}" in listing


def test_filter_chain_thins_and_converts_on_the_gpu_before_download():
    vf = cb.filter_chain(cb.VAAPI, "gpu_rgb", 10, 1280)
    assert vf.index("select=") < vf.index("scale_vaapi=") < vf.index("hwdownload")
    assert "w=min(iw\\,1280):h=-2:format=bgr0" in vf and vf.endswith("format=bgr24")
    assert "scale_cuda=format=nv12,hwdownload" in cb.filter_chain(cb.CUDA, "cpu_rgb", 0, 0)
    assert "select" not in cb.filter_chain(cb.CUDA, "cpu_rgb", 0, 0)       # 0 = every frame
    argv = cb.ffmpeg_argv("ffmpeg", cb.VAAPI, "/dev/dri/renderD129", "gpu_rgb", 3, 10, 0)
    assert argv[argv.index("-hwaccel_device") + 1] == "/dev/dri/renderD129"
    assert argv[argv.index("-i") + 1] == "pipe:3" and argv[argv.index("-fps_mode") + 1] == "passthrough"
    assert argv[-3:] == ["-f", "rawvideo", "pipe:1"] and "-an" in argv


def test_concat_list_rejects_line_breaks_and_keeps_local_paths_absolute():
    with pytest.raises(ValueError):
        cb._concat_list("rtsp://h/x\nfile '/etc/passwd'", None)
    assert b"file 'file:/tmp/a.mp4'" in cb._concat_list("/tmp/a.mp4", None)


# ----------------------------------------------------------------- stderr

@pytest.mark.parametrize("lines,opened,want", [
    (["[rtsp @ 0x1] method DESCRIBE failed: 401 (Unauthorized)"], False, cb.AUTH),
    (["Error opening input: Server returned 403 Forbidden (access denied)"], False, cb.AUTH),
    (["[in#0 @ 0x1] Error opening input: Connection refused"], False, cb.UNREACHABLE),
    (["Error opening input: No route to host"], False, cb.UNREACHABLE),
    (["Error opening input: Server returned 404 Not Found"], False, cb.NOT_FOUND),
    (["Error opening input: Connection timed out"], False, cb.TIMEOUT),
    (["Input #0, concat", "Device creation failed: -5.", "No device available for decoder"], True, cb.DECODE),
    (["Input #0, concat", "Impossible to convert between the formats supported by the filter"], True, cb.DECODE),
    (["Unrecognized option 'fps_mode'.", "Error splitting the argument list: Option not found"], False, cb.DECODE),
    (["Input #0, concat"], True, cb.NO_FRAMES),
    ([], False, cb.EXITED),
])
def test_ffmpeg_errors_are_classified(lines, opened, want):
    assert cb.classify_stderr(lines, opened) == want


def test_error_summary_drops_prefixes_and_keeps_cause_and_outcome():
    s = cb.summarise_errors(["Input #0, concat, from 'pipe:3':",
                             "[rtsp @ 0x55d0] method DESCRIBE failed: 401 (Unauthorized)",
                             "[in#0 @ 0x55d1] Impossible to open 'rtsp://***@10.0.0.2/x'",
                             "[in#0 @ 0x55d1] Error opening input: Server returned 401 Unauthorized",
                             "Error opening input file pipe:3."])
    assert s == "method DESCRIBE failed: 401 (Unauthorized); Error opening input: Server returned 401 Unauthorized"


def test_login_rejection_and_refusal_are_reported_without_the_password(fake_ffmpeg, monkeypatch):
    cap = _open_fake(monkeypatch, "auth")
    assert not cap.isOpened() and cap.failure == cb.AUTH
    assert "pa'ss" not in cap.error_summary and "401" in cap.error_summary
    assert not _alive(cap.pid)
    cap = _open_fake(monkeypatch, "refused")
    assert cap.failure == cb.UNREACHABLE and "pa'ss" not in cap.error_summary


# ------------------------------------------------------------------ reader

def test_frames_are_contiguous_bgr_of_the_announced_size(fake_ffmpeg, monkeypatch):
    import cv2

    cap = _open_fake(monkeypatch)
    try:
        ok, frame = cap.read()
        assert ok and frame.shape == (48, 64, 3) and frame.dtype == np.uint8
        assert frame.flags["C_CONTIGUOUS"] and frame.flags["OWNDATA"]
        assert cap.get(cv2.CAP_PROP_FRAME_WIDTH) == 64 and cap.get(cv2.CAP_PROP_FRAME_HEIGHT) == 48
        assert cap.source_codec == "h264" and cap.source_size == (1280, 720) and cap.source_fps == 25.0
        assert cap.output_fps == 10.0 and cap.get(cv2.CAP_PROP_FPS) == 10.0
        ok2, frame2 = cap.read()
        assert ok2 and frame2 is not frame                 # a new array per frame: callers may keep it
    finally:
        cap.release()


def test_only_the_newest_frame_is_kept_for_a_slow_consumer(fake_ffmpeg, monkeypatch):
    cap = _open_fake(monkeypatch, "burst", FAKE_FFMPEG_BURST=20)
    try:
        deadline = time.monotonic() + 3
        while cap._seq < 20 and time.monotonic() < deadline:
            time.sleep(0.02)
        ok, frame = cap.read()
        assert ok and int(frame[0, 0, 0]) == 19 and cap.frames_dropped == 19
        t0 = time.monotonic()
        ok, frame = cap.read()                               # nothing new: bounded wait, then a failure
        assert not ok and frame is None and cap.failure == cb.STALLED
        assert 1.2 < time.monotonic() - t0 < 3.0
    finally:
        cap.release()


def test_release_from_another_thread_ends_ffmpeg_and_a_blocked_read(fake_ffmpeg, monkeypatch):
    import threading

    cap = _open_fake(monkeypatch, "burst", FAKE_FFMPEG_BURST=1)
    assert cap.read()[0]
    threading.Timer(0.2, cap.release).start()
    t0 = time.monotonic()
    assert cap.read() == (False, None)
    # Terminate then kill after 0.5 s; the bound only has to tell "ends" from
    # "blocked forever", with headroom for a loaded test machine.
    assert time.monotonic() - t0 < 3.0
    assert not _alive(cap.pid) and cap.pid not in cb.live_ffmpeg_pids()


def test_a_stream_that_opens_but_sends_nothing_times_out(fake_ffmpeg, monkeypatch):
    t0 = time.monotonic()
    cap = _open_fake(monkeypatch, "stall")
    assert cap.failure == cb.NO_FRAMES and not cap.isOpened()
    assert time.monotonic() - t0 < 3 + 1.5 + 2.5 and not _alive(cap.pid)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no system ffmpeg")
def test_real_ffmpeg_software_decode_through_the_reader(monkeypatch):
    """The real ffmpeg, its real stream dump lines and the bundled clip (software decode, no GPU)."""
    real = cb.ffmpeg_argv

    def software(ffmpeg, backend, device, chain, list_fd, max_fps, max_width):
        argv = real(ffmpeg, backend, device, chain, list_fd, max_fps, max_width)
        hw = argv.index("-hwaccel")
        del argv[hw:hw + 6]
        argv.insert(argv.index("-f"), "-re")                    # a camera's pace, not a file's
        argv[argv.index("-vf") + 1] = cb._max_fps_select(max_fps) + ",format=bgr24"
        return argv

    monkeypatch.setattr(cb, "ffmpeg_argv", software)
    cap = cb.FfmpegHwCapture(str(cb._SAMPLES / "probe_h264.mp4"), cb.VAAPI, None, "gpu_rgb", max_fps=10,
                             max_width=0, open_timeout=5, read_timeout=2)
    try:
        assert cap.isOpened(), cap.error_summary
        frames = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
    finally:
        cap.release()
    ref, err = cb._decode_sample(shutil.which("ffmpeg"), cb._SAMPLES / "probe_h264.mp4", cb.SOFTWARE,
                                 None, None, 10, 0)
    assert err == "" and len(frames) == ref.shape[0] == 10          # 1 s of 25 fps, at most 10 fps
    assert frames[0].shape == (240, 320, 3) and cap.source_codec == "h264"
    assert cb.psnr(np.stack(frames), ref) > 40


# ------------------------------------------------------------------- probe

def _probe_with(monkeypatch, requested, results, nvidia=True, nodes=("/dev/dri/renderD128",), ffmpeg="/x/ffmpeg"):
    """Run the probe with a stubbed test decode: results[(backend, device, chain)] -> 'ok'|'bad'|'fail'."""
    ref = np.full((10, 24, 32, 3), 100, np.uint8)
    calls = []

    def decode(ff, sample, backend, device, chain, max_fps, max_width):
        calls.append((backend, device, chain))
        if backend == cb.SOFTWARE:
            return ref, ""
        r = results.get((backend, device, chain), "fail")
        if r == "ok":
            return ref.copy(), ""
        if r == "bad":
            return ref[..., ::-1] * 0 + 10, ""      # a decoder that returns the wrong picture
        return None, "Device creation failed"

    monkeypatch.setattr(settings, "DECODE_BACKEND", requested)
    monkeypatch.setattr(settings, "DECODE_DEVICE", "")
    monkeypatch.setattr(cb, "_decode_sample", decode)
    monkeypatch.setattr(cb, "_nvidia_present", lambda: nvidia)
    monkeypatch.setattr(cb, "ffmpeg_binary", lambda: ffmpeg)
    monkeypatch.setattr(cb.glob, "glob", lambda pattern: list(nodes))
    return cb._run_probe(), calls


def test_probe_prefers_nvdec_then_vaapi_then_software(monkeypatch):
    p, calls = _probe_with(monkeypatch, "auto", {(cb.CUDA, "0", "cpu_rgb"): "ok",
                                                  (cb.VAAPI, "/dev/dri/renderD128", "gpu_rgb"): "ok"})
    assert (p.backend, p.device, p.chain) == (cb.CUDA, "0", "cpu_rgb")
    assert [c for c in calls if c[0] != cb.SOFTWARE][:2] == [(cb.CUDA, "0", "gpu_rgb"), (cb.CUDA, "0", "cpu_rgb")]
    assert "conversion runs on the CPU after download (on the GPU: Device creation failed)" in p.reason

    p, _ = _probe_with(monkeypatch, "auto", {(cb.VAAPI, "/dev/dri/renderD129", "gpu_rgb"): "ok"},
                       nvidia=False, nodes=("/dev/dri/renderD128", "/dev/dri/renderD129"))
    assert (p.backend, p.device, p.chain) == (cb.VAAPI, "/dev/dri/renderD129", "gpu_rgb")
    assert [a["device"] for a in p.attempts] == ["/dev/dri/renderD128"] * 2 + ["/dev/dri/renderD129"]

    p, _ = _probe_with(monkeypatch, "auto", {}, nvidia=False)
    assert p.backend == cb.SOFTWARE and "no GPU decoder passed" in p.reason and len(p.attempts) == 2


def test_probe_rejects_a_decoder_that_returns_the_wrong_picture(monkeypatch):
    p, _ = _probe_with(monkeypatch, "auto", {(cb.VAAPI, "/dev/dri/renderD128", "gpu_rgb"): "bad",
                                              (cb.VAAPI, "/dev/dri/renderD128", "cpu_rgb"): "ok"}, nvidia=False)
    assert p.chain == "cpu_rgb" and "differ from software" in p.attempts[0]["error"]


def test_probe_honours_explicit_and_software_settings(monkeypatch):
    p, calls = _probe_with(monkeypatch, "software", {})
    assert p.backend == cb.SOFTWARE and calls == [] and p.reason == "DECODE_BACKEND=software"
    p, _ = _probe_with(monkeypatch, "vaapi", {(cb.CUDA, "0", "gpu_rgb"): "ok"})
    assert p.backend == cb.SOFTWARE and all(a["backend"] == cb.VAAPI for a in p.attempts)
    p, calls = _probe_with(monkeypatch, "auto", {}, ffmpeg=None)
    assert p.backend == cb.SOFTWARE and "no ffmpeg binary" in p.reason and calls == []


# ----------------------------------------------------------- live worker

class _SoftCap:
    def __init__(self):
        self.released = False

    def isOpened(self):
        return True

    def release(self):
        self.released = True


class _FailedHw:
    def __init__(self, failure, summary="boom"):
        self.failure, self.error_summary = failure, summary

    def isOpened(self):
        return False

    def release(self):
        pass


@pytest.fixture
def vaapi_worker(monkeypatch):
    probe = cb.DecodeProbe(backend=cb.VAAPI, device="/dev/dri/renderD128", chain="gpu_rgb", reason="test")
    monkeypatch.setattr(cb, "decode_probe", lambda force=False: probe)
    monkeypatch.setattr(lae, "_resolve_host_bounded", lambda *a: None)
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", 0)     # every camera on the GPU
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="cam_hw", name="c", source=URL), lae.LiveAnalyticsEngine())
    soft = []
    monkeypatch.setattr(w, "_open_software", lambda src: soft.append(src) or _SoftCap())
    return w, soft


def test_gpu_failure_on_one_stream_falls_back_to_software_for_that_camera(vaapi_worker, monkeypatch):
    w, soft = vaapi_worker
    hw = []
    monkeypatch.setattr(cb, "open_hw_capture", lambda src, probe, cancel=None: hw.append(src) or _FailedHw(cb.DECODE))
    assert isinstance(w._open(), _SoftCap) and soft == [URL] and len(hw) == 1
    assert "decoding failed for this stream (boom)" in w._hw_disabled
    assert "decoding failed" in w._software_reason(URL)
    w._open()
    assert len(hw) == 1 and len(soft) == 2                     # no second GPU attempt for this worker


def test_a_stream_with_no_frames_twice_falls_back(vaapi_worker, monkeypatch):
    w, soft = vaapi_worker
    monkeypatch.setattr(cb, "open_hw_capture", lambda *a, **k: _FailedHw(cb.NO_FRAMES))
    first = w._open()
    assert isinstance(first, _FailedHw) and soft == [] and w._hw_disabled is None
    assert isinstance(w._open(), _SoftCap) and w._hw_disabled


def test_auth_and_unreachable_raise_without_trying_software(vaapi_worker, monkeypatch):
    w, soft = vaapi_worker
    monkeypatch.setattr(cb, "open_hw_capture", lambda *a, **k: _FailedHw(cb.AUTH, "401 Unauthorized"))
    with pytest.raises(lae.CameraAuthError):
        w._open()
    monkeypatch.setattr(cb, "open_hw_capture", lambda *a, **k: _FailedHw(cb.UNREACHABLE, "Connection refused"))
    with pytest.raises(lae.CameraUnreachableError):
        w._open()
    assert soft == [] and w._hw_disabled is None


def test_non_rtsp_sources_and_software_probe_use_opencv(vaapi_worker, monkeypatch):
    w, soft = vaapi_worker
    monkeypatch.setattr(cb, "open_hw_capture", lambda *a, **k: pytest.fail("GPU capture for a non-RTSP source"))
    w._open("http://10.0.0.5/mjpeg")
    assert soft == ["http://10.0.0.5/mjpeg"]
    monkeypatch.setattr(cb, "decode_probe", lambda force=False: cb.DecodeProbe(backend=cb.SOFTWARE))
    w._open()
    assert soft[-1] == URL


def test_worker_streams_on_the_gpu_reports_it_and_leaves_no_ffmpeg_behind(fake_ffmpeg, monkeypatch, tmp_path):
    from app.services import hardware_detector as hd
    from app.services.inference_scheduler import inference_scheduler

    monkeypatch.setenv("FAKE_FFMPEG_MODE", "frames")
    probe = cb.DecodeProbe(backend=cb.VAAPI, device="/dev/dri/renderD128", chain="gpu_rgb", ffmpeg=sys.executable)
    monkeypatch.setattr(cb, "decode_probe", lambda force=False: probe)
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", 0)
    monkeypatch.setattr(cb, "stream_sizes", cb.StreamSizes(tmp_path / "sizes.json"))
    monkeypatch.setattr(lae.CameraWorker, "_preflight", lambda self, src: None)
    monkeypatch.setattr(inference_scheduler, "admit", lambda *a, **k: False)
    monkeypatch.setattr(settings, "CAMERA_OPEN_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(settings, "CAMERA_READ_TIMEOUT_SEC", 2.0)
    engine = lae.LiveAnalyticsEngine()
    engine.start_camera("cam_hw", "c", "rtsp://127.0.0.1:1/x")
    engine.start_camera("cam_off", "off", "rtsp://127.0.0.1:1/y", enabled=False)
    try:
        rt = engine.runtimes["cam_hw"]
        deadline = time.monotonic() + 8
        while rt.frames_read < 5 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rt.status == "ONLINE" and rt.decoder == cb.VAAPI and rt.frame_width == 64
        assert rt.to_dict()["decoder"] == "vaapi"
        pids = cb.live_ffmpeg_pids()
        assert len(pids) == 1                                    # the camera that is off spawned nothing
        in_use, note, counts = hd._decoder_in_use("vaapi_amd", runtimes=list(engine.runtimes.values()),
                                                   probe=probe)
        assert in_use == "vaapi" and counts == {"vaapi": 1} and "GPU (VA-API) for 1 camera" in note
    finally:
        stuck = engine.stop_all(timeout=3.0)
    assert stuck == [] and not _alive(pids[0]) and cb.live_ffmpeg_pids() == []


def test_clip_buffer_keeps_the_native_clip_rate_when_frames_are_thinned():
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="c", name="c", source=URL), lae.LiveAnalyticsEngine())
    n = settings.ANALYTICS_DETECT_EVERY_N_FRAMES
    assert w._clip_every(object()) == n                          # software: every Nth, as before
    w.rt.fps = 10.0
    assert w._clip_every(SimpleNamespace(source_fps=25.0, output_fps=10.0)) == max(1, round(n * 10 / 25))
    assert w._clip_every(SimpleNamespace(source_fps=8.0, output_fps=8.0)) == n


def test_decode_settings_are_known_to_the_env_check(tmp_path, monkeypatch):
    from app.config import Settings

    env = tmp_path / ".env"
    env.write_text("DECODE_BACKEND=vaapi\nDECODE_DEVICE=/dev/dri/renderD128\nDECODE_MAX_FPS=10\nDECODE_MAX_WIDTH=0\n")
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    info, _, _ = check_env()
    assert info["unused_keys"] == []


def test_bundled_probe_clips_exist():
    for name in ("probe_h264.mp4", "probe_hevc.mp4"):
        assert (cb._SAMPLES / name).stat().st_size < 64 * 1024
    assert os.access(FAKE, os.X_OK)


# ------------------------------------------------- per-camera decoder choice

MIN_PX = 1280 * 720
AUTO = cb.DecodeProbe(backend=cb.VAAPI, device="/dev/dri/renderD128", chain="cpu_rgb", requested="auto")


def test_decoder_is_chosen_by_stream_size_in_auto_mode(monkeypatch):
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", MIN_PX)
    small = cb.choose_decoder(AUTO, (352, 288))
    assert not small.use_gpu and small.by_size
    assert small.reason == "software: 352x288 is cheaper on the CPU than GPU decoding"
    big = cb.choose_decoder(AUTO, (2560, 1440))
    assert big.use_gpu and big.by_size and big.reason == "GPU (VA-API): 2560x1440 is cheaper to decode on the GPU"
    assert cb.choose_decoder(AUTO, (1280, 720)).use_gpu                  # the threshold itself is GPU
    unknown = cb.choose_decoder(AUTO, None)                               # opened like a CIF sub-stream
    assert not unknown.use_gpu and unknown.by_size and "not known yet" in unknown.reason
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", 0)
    assert cb.choose_decoder(AUTO, (352, 288)).use_gpu and cb.choose_decoder(AUTO, None).use_gpu


def test_forced_backends_ignore_the_size(monkeypatch):
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", MIN_PX)
    forced = cb.DecodeProbe(backend=cb.VAAPI, device="/dev/dri/renderD128", chain="cpu_rgb", requested="vaapi")
    c = cb.choose_decoder(forced, (352, 288))
    assert c.use_gpu and not c.by_size and c.reason == "GPU (VA-API): DECODE_BACKEND=vaapi"
    soft = cb.DecodeProbe(backend=cb.SOFTWARE, requested="software", reason="DECODE_BACKEND=software")
    c = cb.choose_decoder(soft, (3072, 2048))
    assert not c.use_gpu and not c.by_size and c.reason == "DECODE_BACKEND=software"


def test_stream_sizes_persist_per_camera_and_stream_without_credentials(tmp_path):
    path = tmp_path / "sizes.json"
    sizes = cb.StreamSizes(path)
    main = "rtsp://admin:secret@10.0.0.9:554/cam/realmonitor?channel=1&subtype=0"
    sub = main.replace("subtype=0", "subtype=1")
    sizes.put("cam1", main, 3072, 2048)
    sizes.put("cam1", sub, 704, 576)
    assert "secret" not in path.read_text() and "10.0.0.9" not in path.read_text()
    again = cb.StreamSizes(path)                                          # a restarted service
    assert again.get("cam1", main) == (3072, 2048) and again.get("cam1", sub) == (704, 576)
    assert again.get("cam1", main.replace("secret", "changed")) == (3072, 2048)   # new password, same stream
    assert again.get("cam2", main) is None
    mtime = path.stat().st_mtime_ns
    again.put("cam1", main, 3072, 2048)                                   # unchanged: not rewritten
    assert path.stat().st_mtime_ns == mtime
    path.write_text("{not json")
    assert cb.StreamSizes(path).get("cam1", main) is None                 # unreadable file: start empty


class _FakeHw(cb.FfmpegHwCapture):
    """An FfmpegHwCapture that opened (no ffmpeg is started)."""

    def __init__(self, source_size=(2560, 1440)):  # noqa: D107 - skip the real open
        self.decoder, self.source_size, self.source_fps, self.max_fps = cb.VAAPI, source_size, 25.0, 10.0
        self.released, self.failure, self.pid = False, None, None

    def isOpened(self):
        return not self.released

    def release(self, wait=0.5):
        self.released = True


@pytest.fixture
def auto_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "decode_probe", lambda force=False: AUTO)
    monkeypatch.setattr(settings, "DECODE_GPU_MIN_PIXELS", MIN_PX)
    monkeypatch.setattr(cb, "stream_sizes", cb.StreamSizes(tmp_path / "sizes.json"))
    monkeypatch.setattr(lae, "_resolve_host_bounded", lambda *a: None)
    monkeypatch.setattr(lae, "_source_sizes", {})
    opened = []
    monkeypatch.setattr(cb, "open_hw_capture", lambda src, probe, cancel=None: opened.append("gpu") or _FakeHw())

    def make(cam="cam_auto"):
        w = lae.CameraWorker(lae.CameraRuntime(camera_id=cam, name="c", source=URL), lae.LiveAnalyticsEngine())
        monkeypatch.setattr(w, "_open_software", lambda src: opened.append("software") or _SoftCap())
        return w

    return make, opened


def test_a_large_stream_opened_in_software_moves_to_the_gpu_once(auto_worker):
    make, opened = auto_worker
    w = make()
    first = w._open()
    assert opened == ["software"] and not w._choice.use_gpu              # size unknown: CIF guess
    cap = w._after_first_frame(first, URL, np.zeros((1440, 2560, 3), np.uint8))
    assert isinstance(cap, _FakeHw) and first.released and opened == ["software", "gpu"]
    assert w.rt.decoder == cb.VAAPI and w.rt.decoder_note == "GPU (VA-API): 2560x1440 is cheaper to decode on the GPU"
    assert cb.stream_sizes.get("cam_auto", URL) == (2560, 1440)
    # Bounded: whatever the next measurement says, this worker does not switch again.
    w._choice = cb.choose_decoder(AUTO, None)
    again = w._after_first_frame(cap, URL, np.zeros((288, 352, 3), np.uint8))
    assert again is cap and opened == ["software", "gpu"]


def test_a_small_stream_stays_in_software_and_says_why(auto_worker):
    make, opened = auto_worker
    w = make()
    cap = w._open()
    assert w._after_first_frame(cap, URL, np.zeros((288, 352, 3), np.uint8)) is cap
    assert opened == ["software"]
    w._report_decoder(cap, URL)
    assert w.rt.decoder == cb.SOFTWARE and w.rt.decoder_note == "software: 352x288 is cheaper on the CPU than GPU decoding"


def test_a_restart_opens_on_the_remembered_decoder_without_switching(auto_worker):
    make, opened = auto_worker
    cb.stream_sizes.put("cam_big", URL, 3072, 2048)
    cb.stream_sizes.reset()                                               # as after a restart: read the file
    w = make("cam_big")
    cap = w._open()
    assert opened == ["gpu"] and isinstance(cap, _FakeHw)
    assert w._after_first_frame(cap, URL, np.zeros((2048, 3072, 3), np.uint8)) is cap
    assert opened == ["gpu"] and not w._decoder_switched


def test_forced_vaapi_never_switches_a_small_stream_to_software(auto_worker, monkeypatch):
    make, opened = auto_worker
    forced = cb.DecodeProbe(backend=cb.VAAPI, device="/dev/dri/renderD128", chain="cpu_rgb", requested="vaapi")
    monkeypatch.setattr(cb, "decode_probe", lambda force=False: forced)
    w = make()
    cap = w._open()
    assert w._after_first_frame(cap, URL, np.zeros((288, 352, 3), np.uint8)) is cap
    assert opened == ["gpu"]


def test_decoder_usage_counts_cameras_per_decoder():
    rts = [SimpleNamespace(status="ONLINE", decoder=cb.VAAPI)] * 8 + [SimpleNamespace(status="ONLINE", decoder=cb.SOFTWARE)] * 25
    rts += [SimpleNamespace(status="OFFLINE", decoder=None)]
    u = cb.decoder_usage(rts)
    assert u["in_use"] == "mixed" and u["cameras"] == {"vaapi": 8, "software": 25}
    assert u["summary"] == "GPU (VA-API) for 8 cameras, software (CPU) for 25 cameras"


# ------------------------------------------------------- thinned software capture

class _StreamCap:
    """cv2.VideoCapture stand-in: ``grab`` advances a stream clock at ``fps``."""

    def __init__(self, fps=25.0, frames=50, stuck_clock=False):
        import cv2

        self._cv2, self.fps, self.frames, self.stuck = cv2, fps, frames, stuck_clock
        self.i, self.retrieved = -1, 0

    def grab(self):
        self.i += 1
        return self.i < self.frames

    def retrieve(self):
        self.retrieved += 1
        return True, np.full((4, 4, 3), self.i % 256, np.uint8)

    def get(self, prop):
        if prop == self._cv2.CAP_PROP_FPS:
            return self.fps
        if prop == self._cv2.CAP_PROP_POS_MSEC:
            return 0.0 if self.stuck else self.i * 1000.0 / self.fps
        return 0.0

    def isOpened(self):
        return True

    def release(self):
        pass


def _drain(cap):
    got = []
    while True:
        ok, f = cap.read()
        if not ok:
            return got
        got.append(int(f[0, 0, 0]))


def test_thinned_capture_converts_ten_of_twenty_five_frames_a_second():
    src = _StreamCap(fps=25.0, frames=50)                                 # 2 s of video
    cap = cb.ThinnedCapture(src, 10)
    got = _drain(cap)
    assert len(got) == 20 and src.retrieved == 20 and cap.frames_skipped == 30
    assert got[:5] == [0, 3, 5, 8, 10]                                    # first frame of every 0.1 s slot
    assert cap.source_fps == 25.0 and cap.output_fps == 10.0


def test_thinned_capture_keeps_every_frame_of_a_slow_camera_and_survives_a_stuck_clock(monkeypatch):
    slow = cb.ThinnedCapture(_StreamCap(fps=6.0, frames=12), 10)
    assert len(_drain(slow)) == 12 and slow.output_fps == 6.0
    # No usable timestamps: wall-clock slots instead, never a stalled feed.
    clock = iter(i * 0.04 for i in range(1000))
    monkeypatch.setattr(cb.time, "monotonic", lambda: next(clock))
    stuck = cb.ThinnedCapture(_StreamCap(fps=25.0, frames=50, stuck_clock=True), 10)
    assert 18 <= len(_drain(stuck)) <= 21


def test_clip_stride_of_a_thinned_software_camera_keeps_the_native_clip_rate():
    w = lae.CameraWorker(lae.CameraRuntime(camera_id="c", name="c", source=URL), lae.LiveAnalyticsEngine())
    n = settings.ANALYTICS_DETECT_EVERY_N_FRAMES
    w.rt.fps = 10.0
    assert w._clip_every(cb.ThinnedCapture(_StreamCap(fps=25.0), 10)) == max(1, round(n * 10 / 25))
