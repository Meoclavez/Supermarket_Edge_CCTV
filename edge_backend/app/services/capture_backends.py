"""Camera capture on the GPU's video decoder, with software decoding as the fallback.

The live workers used to decode every RTSP stream with ``cv2.VideoCapture``
on the CPU: ~3 ms of CPU per 720p frame, every frame, ~12 % of a core per
camera (390 % for 33 cameras on the store box). The pip OpenCV wheel has no
VA-API/NVDEC support, so this module runs one system ``ffmpeg`` per camera
instead, and does all the heavy work before a frame reaches this process:

    RTSP (TCP) -> GPU decode -> drop frames above DECODE_MAX_FPS -> GPU colour
    conversion (and optional downscale) -> download -> bgr24 on a pipe

Measured on an RX 9060 XT (VA-API, Mesa 26): frames must be thinned on the
GPU, before ``hwdownload``. The GPU's own YUV->RGB conversion (``scale_vaapi
format=bgr0``) came out washed out / off-matrix there (PSNR 21-23 dB against
software decoding), so the probe rejects it and the frame is converted on the
CPU after download, via yuv420p, which hits swscale's fast unscaled path:
~3.9 % of a core per 720p camera at 10 fps, against ~12 % for today's
software decoding at 25 fps.

Which backend is used is probed at runtime, never assumed from this box:
NVIDIA NVDEC (``-hwaccel cuda``) -> VA-API on each render node (AMD/Intel) ->
software (the OpenCV path in live_analytics_engine). The probe decodes a tiny
bundled H.264 clip through the exact filter chain a camera would use and
compares the frames with a software decode of the same clip, so a decoder
that "opens" but returns wrong colours is rejected. Each camera can still
fall back to software on its own when GPU decoding fails for its stream.

The camera URL (with its login) is never put on ffmpeg's command line, where
any local user could read it from ``/proc/<pid>/cmdline``: ffmpeg reads it
from an inherited pipe as a one-entry concat list (``-f concat -i pipe:N``).
"""

from __future__ import annotations

import atexit
import glob
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings
from app.services.camera_drivers import redact_url

logger = logging.getLogger(__name__)

SOFTWARE = "software"
VAAPI = "vaapi"
CUDA = "cuda"
BACKENDS = (CUDA, VAAPI, SOFTWARE)
BACKEND_LABELS = {CUDA: "GPU (NVDEC)", VAAPI: "GPU (VA-API)", SOFTWARE: "software (CPU)"}

# Why an ffmpeg capture did not deliver a first frame (FfmpegHwCapture.failure).
AUTH = "auth"                  # 401/403: the camera rejected the login
NOT_FOUND = "not_found"        # 404: wrong stream path
UNREACHABLE = "unreachable"    # refused / no route / DNS: nothing answered
TIMEOUT = "timeout"            # nothing usable before the open deadline
DECODE = "decode"              # the stream opened but the GPU decode/filter chain failed
NO_FRAMES = "no_frames"        # the stream opened but no frame arrived in time
EXITED = "exited"              # ffmpeg ended for another reason
STALLED = "stalled"            # read(): no new frame within CAMERA_READ_TIMEOUT_SEC


class CameraAuthError(RuntimeError):
    """The camera answered and rejected (or demanded) a login."""


class CameraUnreachableError(RuntimeError):
    """Nothing usable answered at the camera's address."""


# Filter chains per backend, most efficient first; the probe keeps the first
# one whose frames match software decoding. "gpu_rgb" converts to BGR on the
# GPU (scale_cuda cannot convert YUV to RGB at all in FFmpeg 9; scale_vaapi's
# result is off on Mesa radeonsi, see above). "cpu_rgb" downloads NV12 and
# converts NV12 -> yuv420p -> bgr24 in ffmpeg: both steps are swscale fast
# paths, measured 40 % cheaper than NV12 -> bgr24 directly (0.75 vs 1.27 s CPU
# for 300 frames of 720p).
_CHAINS = {
    VAAPI: (("gpu_rgb", "scale_vaapi={opts}format=bgr0,hwdownload,format=bgr0,format=bgr24"),
            ("cpu_rgb", "scale_vaapi={opts}format=nv12,hwdownload,format=nv12,format=yuv420p,format=bgr24")),
    CUDA: (("gpu_rgb", "scale_cuda={opts}format=bgr0,hwdownload,format=bgr0,format=bgr24"),
           ("cpu_rgb", "scale_cuda={opts}format=nv12,hwdownload,format=nv12,format=yuv420p,format=bgr24")),
}
# glibc gives each of ffmpeg's ~30 threads its own malloc arena; two arenas
# halved an ffmpeg's private memory (76 -> 38 MB for 720p) at no CPU cost.
_FFMPEG_ENV = {"MALLOC_ARENA_MAX": "2"}
# Protocols the concat list may open: the list itself (pipe/file) and RTSP.
_PROTOCOLS = "file,pipe,crypto,data,rtsp,rtsps,rtp,srtp,udp,tcp,tls"
_SAMPLES = Path(__file__).resolve().parent / "decode_samples"
# The probe rejects a backend whose frames differ this much from software
# decoding: catches swapped channels and garbage, tolerates colour-matrix
# rounding between decoders.
PROBE_MIN_PSNR_DB = 25.0
STDERR_LINES = 40


def hw_eligible(source: str) -> bool:
    """Only RTSP cameras are decoded by ffmpeg; HTTP/MJPEG and /dev/video stay on OpenCV."""
    return (source or "").lower().startswith(("rtsp://", "rtsps://"))


def ffmpeg_binary() -> Optional[str]:
    return shutil.which("ffmpeg")


def _concat_list(url: str, timeout_us: Optional[int]) -> bytes:
    """A one-entry ffconcat list carrying the URL and its demuxer options."""
    if any(c in url for c in "\r\n\0"):
        raise ValueError("camera URL contains a line break")
    if url.startswith("/"):
        url = "file:" + url    # a bare path would resolve against "pipe:N"
    quoted = "'" + url.replace("'", "'\\''") + "'"
    lines = ["ffconcat version 1.0", f"file {quoted}"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        lines.append("option rtsp_transport tcp")
        if timeout_us:
            # FFmpeg >= 5: socket I/O timeout in microseconds (``stimeout`` is gone).
            lines.append(f"option timeout {int(timeout_us)}")
    return ("\n".join(lines) + "\n").encode()


def _max_fps_select(max_fps: float) -> Optional[str]:
    """Pass at most ``max_fps`` frames a second, never duplicating any.

    The ``fps`` filter would make a 6 fps camera 10 fps by repeating frames.
    This keeps the first frame of every 1/max_fps slot (25 fps -> exactly 10),
    and every frame of a slower camera. Frames without a timestamp, or after
    the camera's clock jumped back, are passed rather than stalling the feed.
    """
    if not max_fps or max_fps <= 0:
        return None
    r = f"{float(max_fps):g}"
    return ("select='isnan(prev_selected_t)+isnan(t)+lt(t\\,prev_selected_t)"
            f"+gt(floor(t*{r})\\,floor(prev_selected_t*{r}))'")


def filter_chain(backend: str, chain: str, max_fps: float, max_width: int) -> str:
    template = dict(_CHAINS[backend])[chain]
    opts = f"w=min(iw\\,{int(max_width)}):h=-2:" if max_width and max_width > 0 else ""
    parts = [p for p in (_max_fps_select(max_fps), template.format(opts=opts)) if p]
    return ",".join(parts)


def ffmpeg_argv(ffmpeg: str, backend: str, device: Optional[str], chain: str, list_fd: int,
                max_fps: float, max_width: int) -> list[str]:
    """The ffmpeg command for one camera. It holds no URL and no credentials."""
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-filter_threads", "1"]
    if backend == VAAPI:
        argv += ["-hwaccel", "vaapi", "-hwaccel_device", device or "/dev/dri/renderD128",
                 "-hwaccel_output_format", "vaapi"]
    elif backend == CUDA:
        argv += ["-hwaccel", "cuda", "-hwaccel_device", device or "0", "-hwaccel_output_format", "cuda"]
    else:
        raise ValueError(f"not a hardware decode backend: {backend}")
    argv += ["-threads", "1", "-protocol_whitelist", _PROTOCOLS,
             "-f", "concat", "-safe", "0", "-i", f"pipe:{list_fd}",
             "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", filter_chain(backend, chain, max_fps, max_width),
             # No CFR padding: frames leave exactly as the filter selected them.
             "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"]
    return argv


# ------------------------------------------------------------------ stderr

_AUTH_RE = re.compile(r"unauthori[sz]ed|authorization failed|forbidden|access denied", re.I)
_NOT_FOUND_RE = re.compile(r"\b404\b|no such file or directory", re.I)
_UNREACHABLE_RE = re.compile(
    r"connection refused|no route to host|network is unreachable|host is unreachable|"
    r"name or service not known|temporary failure in name resolution|failed to resolve|"
    r"connection reset by peer", re.I)
_TIMEOUT_RE = re.compile(r"timed out|timeout", re.I)
# The GPU half of the pipeline failing (device, decoder or hardware filters).
_DECODE_RE = re.compile(
    r"vaapi|cuda|nvdec|cuvid|hwaccel|hardware device|hwdownload|no device available|"
    r"device creation failed|impossible to convert between the formats|error reinitializing filters|"
    r"failed to configure|unsupported conversion|no decoder surfaces|failed setup for format|"
    r"not supported by the hardware|unrecognized option|option not found|error splitting the argument", re.I)
_OUTPUT_STREAM_RE = re.compile(r"Stream #0:0.*Video: rawvideo.*?,\s(\d{2,5})x(\d{2,5})(?=[\s,\[]|$)")
_INPUT_STREAM_RE = re.compile(r"Stream #0:\d+.*?: Video: (\w+).*?,\s(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r",\s([\d.]+) fps")


_LOG_PREFIX_RE = re.compile(r"^\[[^\]]*@ 0x[0-9a-f]+\]\s*")
_NOISE = ("Stream #", "Input #", "Output #", "Metadata", "Duration", "Stream mapping", "Press [q]",
          "frame=", "Conversion failed", "Error opening input file", "Error opening output file", "  ")
_ERRORISH_RE = re.compile(r"error|fail|unsupported|impossible|cannot|refused|unreachable|timed out|"
                          r"unauthori|forbidden|not found|invalid|no such", re.I)


def summarise_errors(lines: list[str]) -> str:
    """The first and last distinct error lines ffmpeg wrote (cause and outcome),
    without its [ctx @ 0x..] prefixes."""
    out: list[str] = []
    for ln in lines:
        if ln.startswith(_NOISE):
            continue
        ln = _LOG_PREFIX_RE.sub("", ln.strip())
        if ln and _ERRORISH_RE.search(ln) and ln not in out:
            out.append(ln)
    if len(out) > 2:
        out = [out[0], out[-1]]
    return "; ".join(out)[:300]


def classify_stderr(lines: list[str], input_opened: bool) -> str:
    """Why ffmpeg failed before the first frame, from its own error lines."""
    text = "\n".join(lines)
    if _AUTH_RE.search(text):
        return AUTH
    if _NOT_FOUND_RE.search(text) and not input_opened:
        return NOT_FOUND
    if _UNREACHABLE_RE.search(text) and not input_opened:
        return UNREACHABLE
    if _DECODE_RE.search(text):
        return DECODE
    if _TIMEOUT_RE.search(text) and not input_opened:
        return TIMEOUT
    return NO_FRAMES if input_opened else EXITED


# ----------------------------------------------------------------- capture

_live_captures: "weakref.WeakSet[FfmpegHwCapture]" = weakref.WeakSet()
_live_lock = threading.Lock()


def _kill_all_at_exit() -> None:
    """Interpreter exit with captures still open (a worker stuck past stop_all)."""
    with _live_lock:
        caps = list(_live_captures)
    for cap in caps:
        cap.release(wait=0.5)


atexit.register(_kill_all_at_exit)


def live_ffmpeg_pids() -> list[int]:
    with _live_lock:
        return [c.pid for c in _live_captures if c.pid is not None and not c.released]


class FfmpegHwCapture:
    """One camera decoded by an ffmpeg child on the GPU.

    A drop-in for how the live worker uses ``cv2.VideoCapture``: ``read()`` ->
    ``(ok, frame)`` with a contiguous BGR uint8 frame, ``isOpened()``,
    ``release()``, ``get()``/``set()``. The constructor blocks until the first
    frame arrives or the open fails (bounded by the camera timeouts); on
    failure ``isOpened()`` is False and ``failure`` / ``error_summary`` say why.

    A reader thread drains ffmpeg's pipe continuously into a fresh array per
    frame and keeps only the newest one, so a slow consumer never makes ffmpeg
    block or fall behind the live stream: older frames are dropped
    (``frames_dropped``). ``release()`` is safe from any thread and also
    interrupts a blocked ``read()``.
    """

    interruptible = True

    def __init__(self, url: str, backend: str, device: Optional[str], chain: str, *,
                 max_fps: float, max_width: int, open_timeout: float, read_timeout: float,
                 ffmpeg: Optional[str] = None, cancel: Optional[threading.Event] = None):
        self.decoder = backend
        self.device = device
        self.chain = chain
        self.read_timeout = max(0.5, float(read_timeout))
        self.failure: Optional[str] = None
        self.width = self.height = None
        self.max_fps = float(max_fps or 0)
        self.source_codec: Optional[str] = None
        self.source_size: Optional[tuple[int, int]] = None
        self.source_fps: Optional[float] = None
        self.frames_delivered = 0
        self.frames_dropped = 0
        self.pid: Optional[int] = None
        self.released = False
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._err_lock = threading.Lock()
        self._cancel = cancel
        self._input_opened = False
        self._output_seen = False
        self._cond = threading.Condition()
        self._latest: Optional[np.ndarray] = None
        self._seq = 0
        self._returned = 0
        self._eof = False
        self._release_lock = threading.Lock()
        self._release_done = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._threads: list[threading.Thread] = []
        self._redact = url

        ffmpeg = ffmpeg or ffmpeg_binary()
        if not ffmpeg:
            self.failure = EXITED
            self._stderr.append("ffmpeg binary not found")
            return
        timeout_us = int(max(0.5, float(read_timeout)) * 1_000_000)
        r, w = os.pipe()
        try:
            os.write(w, _concat_list(url, timeout_us))
            os.close(w)
            w = -1
            argv = ffmpeg_argv(ffmpeg, backend, device, chain, r, max_fps, max_width)
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, pass_fds=(r,), start_new_session=True,  # own process group: killpg
                env={**os.environ, **_FFMPEG_ENV},
            )
        except (OSError, ValueError) as e:
            self.failure = EXITED
            self._stderr.append(f"could not start ffmpeg: {e}")
            return
        finally:
            os.close(r)
            if w >= 0:
                os.close(w)
        self.pid = self._proc.pid
        with _live_lock:
            _live_captures.add(self)
        for target, name in ((self._read_stderr, "err"), (self._read_frames, "rd")):
            t = threading.Thread(target=target, daemon=True, name=f"ffmpeg-{name}-{self.pid}")
            t.start()
            self._threads.append(t)
        self._wait_first_frame(max(0.5, float(open_timeout)), self.read_timeout)

    # ------------------------------------------------------------- threads

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = redact_url(raw.decode("utf-8", "replace").rstrip())
                if not line:
                    continue
                with self._err_lock:
                    self._stderr.append(line)
                if line.startswith("Input #0"):
                    self._input_opened = True
                elif line.startswith("Output #0"):
                    self._output_seen = True
                elif "Stream #0:" in line and " Video: " in line:
                    self._parse_stream_line(line)
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._cond.notify_all()

    def _lines(self) -> list[str]:
        with self._err_lock:
            return list(self._stderr)

    @property
    def output_fps(self) -> Optional[float]:
        """Frames per second delivered: the camera's rate, capped at DECODE_MAX_FPS."""
        if self.source_fps and self.max_fps > 0:
            return min(self.source_fps, self.max_fps)
        return self.source_fps or (self.max_fps or None)

    def _parse_stream_line(self, line: str) -> None:
        fps = _FPS_RE.search(line)
        if self._output_seen:
            m = _OUTPUT_STREAM_RE.search(line)
            if m and self.width is None:
                with self._cond:
                    self.width, self.height = int(m.group(1)), int(m.group(2))
                    self._cond.notify_all()
            return
        m = _INPUT_STREAM_RE.search(line)
        if m and self.source_codec is None:
            self.source_codec = m.group(1)
            self.source_size = (int(m.group(2)), int(m.group(3)))
            self.source_fps = float(fps.group(1)) if fps else None

    def _read_frames(self) -> None:
        out = self._proc.stdout
        try:
            with self._cond:
                # The frame size comes from ffmpeg's output stream line.
                while self.width is None and self._proc.poll() is None and not self.released:
                    self._cond.wait(0.2)
                w, h = self.width, self.height
            if w is None:
                return
            size = w * h * 3
            while True:
                frame = np.empty((h, w, 3), np.uint8)
                view = memoryview(frame).cast("B")
                got = 0
                while got < size:
                    n = out.readinto(view[got:])
                    if not n:
                        return
                    got += n
                with self._cond:
                    if self._seq > self._returned:
                        self.frames_dropped += 1
                    self._latest = frame
                    self._seq += 1
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _wait_first_frame(self, open_timeout: float, read_timeout: float) -> None:
        """Up to ``open_timeout`` to open the stream, then ``read_timeout`` for a frame."""
        start = time.monotonic()
        with self._cond:
            while self._seq == 0 and not self._eof:
                if self._cancel is not None and self._cancel.is_set():
                    break
                limit = open_timeout + (read_timeout if self._input_opened else 0.0)
                remaining = start + limit - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.2))
            got = self._seq > 0
        if got:
            return
        # Let the stderr thread catch up with the lines ffmpeg wrote as it exited.
        if self._proc is not None:
            try:
                self._proc.wait(0.3)
            except subprocess.TimeoutExpired:
                pass
        if len(self._threads) > 0:
            self._threads[0].join(0.3)
        timed_out = self._proc is not None and self._proc.poll() is None
        failure = classify_stderr(self._lines(), self._input_opened)
        if timed_out and failure in (EXITED, NO_FRAMES):
            failure = NO_FRAMES if self._input_opened else TIMEOUT
        self.failure = failure
        self.release()

    # ------------------------------------------------------------- VideoCapture API

    def isOpened(self) -> bool:  # noqa: N802 - cv2.VideoCapture's name
        return self.failure is None and not self.released and self._seq > 0

    def read(self):
        deadline = time.monotonic() + self.read_timeout
        with self._cond:
            while self._seq == self._returned:
                if self._eof or self.released:
                    self.failure = self.failure or EXITED
                    return False, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failure = STALLED
                    return False, None
                self._cond.wait(min(remaining, 0.5))
            self._returned = self._seq
            frame, self._latest = self._latest, None
        self.frames_delivered += 1
        return True, frame

    def get(self, prop) -> float:
        import cv2

        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.width or 0)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.height or 0)
        if prop == cv2.CAP_PROP_FPS:
            return float(self.output_fps or self.source_fps or 0)
        return 0.0

    def set(self, prop, value) -> bool:
        return False  # nothing to tune: the reader already keeps only the newest frame

    @property
    def error_summary(self) -> str:
        """The last meaningful ffmpeg error lines (URLs redacted), for status and logs."""
        return summarise_errors(self._lines())

    def release(self, wait: float = 0.5) -> None:
        """Stop ffmpeg: SIGTERM to its process group, SIGKILL after ``wait`` s.

        A second caller (the worker's own cleanup after ``stop()``) waits
        until the first has finished, so once a worker has exited its ffmpeg
        is gone.
        """
        with self._release_lock:
            first = not self.released
            self.released = True
        if not first:
            self._release_done.wait(5.0)
            return
        try:
            self._terminate(wait)
        finally:
            self._release_done.set()

    def _terminate(self, wait: float) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            for sig, grace in ((signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if proc is not None:
            try:
                proc.wait(0.1)
            except subprocess.TimeoutExpired:
                pass
        with self._cond:
            self._cond.notify_all()
        current = threading.current_thread()
        for t in self._threads:
            if t is not current:
                t.join(1.0)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - already closed
                    pass
        with _live_lock:
            _live_captures.discard(self)

    def __del__(self):
        try:
            self.release(wait=0.2)
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass


# ------------------------------------------------------------------- probe

@dataclass
class DecodeProbe:
    """Which decoder camera capture uses, and why. Cached for the process."""

    backend: str                          # cuda | vaapi | software
    device: Optional[str] = None          # render node or CUDA device index
    chain: Optional[str] = None           # gpu_rgb | cpu_rgb
    requested: str = "auto"               # DECODE_BACKEND
    reason: str = ""
    codecs: dict = field(default_factory=dict)       # {"h264": True, "hevc": False}
    attempts: list = field(default_factory=list)     # what was tried, with the result
    ffmpeg: Optional[str] = None
    elapsed_s: float = 0.0

    @property
    def label(self) -> str:
        return BACKEND_LABELS.get(self.backend, self.backend)

    def to_dict(self) -> dict:
        return {"backend": self.backend, "label": self.label, "device": self.device, "chain": self.chain,
                "requested": self.requested, "reason": self.reason, "codecs": dict(self.codecs),
                "attempts": list(self.attempts), "ffmpeg": self.ffmpeg, "elapsed_s": self.elapsed_s,
                "max_fps": float(settings.DECODE_MAX_FPS), "max_width": int(settings.DECODE_MAX_WIDTH)}


_probe: Optional[DecodeProbe] = None
_probe_lock = threading.Lock()


def _parse_output_size(stderr: str) -> Optional[tuple[int, int]]:
    seen_output = False
    for line in stderr.splitlines():
        if line.startswith("Output #0"):
            seen_output = True
        elif seen_output:
            m = _OUTPUT_STREAM_RE.search(line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def _decode_sample(ffmpeg: str, sample: Path, backend: str, device: Optional[str], chain: Optional[str],
                   max_fps: float, max_width: int) -> tuple[Optional[np.ndarray], str]:
    """Decode the bundled sample as a camera would be; (frames[N,H,W,3] or None, error)."""
    r, w = os.pipe()
    try:
        os.write(w, _concat_list(str(sample), None))
        os.close(w)
        w = -1
        if backend == SOFTWARE:
            vf = ",".join(p for p in (_max_fps_select(max_fps), "format=bgr24") if p)
            if max_width and max_width > 0:
                vf = f"scale=w=min(iw\\,{int(max_width)}):h=-2," + vf
            argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-threads", "1",
                    "-protocol_whitelist", _PROTOCOLS, "-f", "concat", "-safe", "0", "-i", f"pipe:{r}",
                    "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"]
        else:
            argv = ffmpeg_argv(ffmpeg, backend, device, chain, r, max_fps, max_width)
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                             pass_fds=(r,), start_new_session=True)
    except subprocess.TimeoutExpired:
        return None, "timed out after 20 s"
    except OSError as e:
        return None, str(e)
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    err = res.stderr.decode("utf-8", "replace")
    size = _parse_output_size(err)
    if res.returncode != 0 or size is None:
        return None, summarise_errors(err.splitlines()) or f"ffmpeg exited with {res.returncode}"
    fw, fh = size
    n = len(res.stdout) // (fw * fh * 3)
    if n == 0:
        return None, "no frames decoded"
    return np.frombuffer(res.stdout[: n * fw * fh * 3], np.uint8).reshape(n, fh, fw, 3), ""


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2))
    return 99.0 if mse <= 1e-10 else 10.0 * float(np.log10(255.0 ** 2 / mse))


def _nvidia_present() -> bool:
    return os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/nvidia0") or bool(shutil.which("nvidia-smi"))


def _candidates(requested: str) -> list[tuple[str, Optional[str]]]:
    """(backend, device) pairs to try, in the owner's preference order."""
    device = (settings.DECODE_DEVICE or "").strip() or None
    out: list[tuple[str, Optional[str]]] = []
    if requested in ("auto", CUDA) and (requested == CUDA or _nvidia_present()):
        if device is None or not device.startswith("/dev/"):
            out.append((CUDA, device or "0"))
    if requested in ("auto", VAAPI):
        if device and device.startswith("/dev/"):
            out.append((VAAPI, device))
        elif device is None:
            out.extend((VAAPI, node) for node in sorted(glob.glob("/dev/dri/renderD*")))
    return out


def _run_probe() -> DecodeProbe:
    requested = (settings.DECODE_BACKEND or "auto").strip().lower()
    if requested not in ("auto", CUDA, VAAPI, SOFTWARE):
        logger.warning(f"DECODE_BACKEND={settings.DECODE_BACKEND!r} is not auto|cuda|vaapi|software; using auto")
        requested = "auto"
    probe = DecodeProbe(backend=SOFTWARE, requested=requested)
    if requested == SOFTWARE:
        probe.reason = "DECODE_BACKEND=software"
        return probe
    ffmpeg = ffmpeg_binary()
    probe.ffmpeg = ffmpeg
    if not ffmpeg:
        probe.reason = "no ffmpeg binary on PATH, so GPU decoding cannot be used"
        return probe
    max_fps, max_width = float(settings.DECODE_MAX_FPS), int(settings.DECODE_MAX_WIDTH)
    h264, hevc = _SAMPLES / "probe_h264.mp4", _SAMPLES / "probe_hevc.mp4"
    ref, err = _decode_sample(ffmpeg, h264, SOFTWARE, None, None, max_fps, max_width)
    if ref is None:
        probe.reason = f"ffmpeg could not decode the test clip in software ({err}); GPU decoding not attempted"
        return probe
    candidates = _candidates(requested)
    if not candidates:
        probe.reason = (f"DECODE_BACKEND={requested}: no matching GPU device found" if requested != "auto"
                        else "no GPU video decoder device found (no NVIDIA device, no /dev/dri render node)")
    for backend, device in candidates:
        for chain, _ in _CHAINS[backend]:
            frames, err = _decode_sample(ffmpeg, h264, backend, device, chain, max_fps, max_width)
            attempt = {"backend": backend, "device": device, "chain": chain, "ok": False}
            if frames is not None and frames.shape == ref.shape:
                attempt["psnr_db"] = round(psnr(frames, ref), 1)
                attempt["ok"] = attempt["psnr_db"] >= PROBE_MIN_PSNR_DB
                if not attempt["ok"]:
                    attempt["error"] = f"frames differ from software decoding (PSNR {attempt['psnr_db']} dB)"
            else:
                attempt["error"] = err or f"frame shape {None if frames is None else frames.shape} != {ref.shape}"
            probe.attempts.append(attempt)
            if attempt["ok"]:
                probe.backend, probe.device, probe.chain = backend, device, chain
                probe.codecs["h264"] = True
                hevc_frames, _ = _decode_sample(ffmpeg, hevc, backend, device, chain, max_fps, max_width)
                probe.codecs["hevc"] = hevc_frames is not None and hevc_frames.shape[0] == ref.shape[0]
                probe.reason = (f"{BACKEND_LABELS[backend]} on {device} decoded the H.264 test clip "
                                f"correctly (PSNR {attempt['psnr_db']} dB vs software)")
                if chain == "cpu_rgb":
                    gpu = next((a for a in probe.attempts if a["device"] == device and a["chain"] == "gpu_rgb"), {})
                    probe.reason += ("; YUV->BGR conversion runs on the CPU after download (on the GPU: "
                                     f"{gpu.get('error', 'not usable')})")
                return probe
    if candidates:
        tried = "; ".join(f"{a['backend']} {a['device']} {a['chain']}: {a.get('error', '')}" for a in probe.attempts)
        probe.reason = f"no GPU decoder passed the test decode ({tried})"
    return probe


def decode_probe(force: bool = False) -> DecodeProbe:
    """Probe once per process (about a second); every later call is the cached result."""
    global _probe
    with _probe_lock:
        if _probe is None or force:
            started = time.monotonic()
            try:
                p = _run_probe()
            except Exception as e:  # noqa: BLE001 - a broken probe must leave software decoding working
                p = DecodeProbe(backend=SOFTWARE, reason=f"decoder probe failed: {e}")
            p.elapsed_s = round(time.monotonic() - started, 2)
            _probe = p
            log = logger.error if p.requested in (CUDA, VAAPI) and p.backend == SOFTWARE else logger.info
            log(f"Camera video decoding: {p.label} ({p.reason})")
        return _probe


def cached_probe() -> Optional[DecodeProbe]:
    """The probe result if it has run, without running it."""
    return _probe


def reset_probe() -> None:
    global _probe
    with _probe_lock:
        _probe = None


def open_hw_capture(url: str, probe: DecodeProbe, cancel: Optional[threading.Event] = None) -> FfmpegHwCapture:
    return FfmpegHwCapture(
        url, probe.backend, probe.device, probe.chain or "gpu_rgb",
        max_fps=float(settings.DECODE_MAX_FPS), max_width=int(settings.DECODE_MAX_WIDTH),
        open_timeout=float(settings.CAMERA_OPEN_TIMEOUT_SEC), read_timeout=float(settings.CAMERA_READ_TIMEOUT_SEC),
        ffmpeg=probe.ffmpeg, cancel=cancel,
    )


def decoder_usage(runtimes) -> dict:
    """What the cameras that are streaming right now actually decode with.

    ``in_use`` is ``cuda`` / ``vaapi`` / ``cpu`` when every streaming camera
    uses the same, ``mixed`` otherwise, ``none`` when nothing is streaming.
    """
    counts: dict[str, int] = {}
    for rt in runtimes:
        if rt.status == "ONLINE" and rt.decoder:
            counts[rt.decoder] = counts.get(rt.decoder, 0) + 1
    if not counts:
        in_use = "none"
    elif len(counts) > 1:
        in_use = "mixed"
    else:
        only = next(iter(counts))
        in_use = "cpu" if only == SOFTWARE else only
    parts = [f"{BACKEND_LABELS.get(b, b)} for {n} camera{'s' if n != 1 else ''}"
             for b, n in sorted(counts.items(), key=lambda kv: (kv[0] == SOFTWARE, -kv[1]))]
    summary = ", ".join(parts) if parts else "no camera is streaming"
    return {"in_use": in_use, "cameras": counts, "summary": summary}
