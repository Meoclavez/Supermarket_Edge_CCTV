"""go2rtc child process: the live video gateway for direct peer-to-peer WebRTC.

go2rtc (AlexxIT/go2rtc, MIT, pinned in scripts/bootstrap.py) pulls a camera's
RTSP stream and sends it to a browser over WebRTC. The media path is direct
between the browser and this device (host/srflx/prflx candidates, STUN only);
nothing passes through the online-access tunnel or any relay
(docs/REMOTE_VIDEO_CONTRACT.md).

Lifecycle
    Not started at boot. :meth:`Go2RtcManager.ensure_running` starts it on the
    first live-video session (remote viewer, or a LAN browser that chose direct
    WebRTC). It then stays up: without streams it pulls nothing and sends
    nothing. A crash is restarted with backoff by the supervisor thread; the
    app's shutdown stops it (whole process group, so a transcoding ffmpeg goes
    too). A go2rtc left over from a killed app (pid file) is stopped first.

Config (``STORAGE_DIR/go2rtc/go2rtc.yaml``, 0600, rewritten on every start)
    * contains no streams and no camera credentials. Streams are added per
      session with ``PATCH /api/streams`` (in memory only: go2rtc's ``PUT``
      would write the source, credentials included, into the config file) and
      removed with ``DELETE``;
    * the API listens on ``127.0.0.1:<GO2RTC_API_PORT>`` only, requires HTTP
      Basic auth even from loopback (``local_auth``; the password is random per
      start and reaches go2rtc through its environment, never the file or the
      command line), and exposes only ``/api``, ``/api/streams`` and
      ``/api/webrtc`` (``allow_paths``: no web UI, no ``/api/config``,
      ``/api/exit``, websocket, HLS, MJPEG or MP4 endpoints);
    * only the modules needed are loaded: api, http, rtsp, webrtc, exec, ffmpeg.
      RTMP, SRTP, HLS, HomeKit, ONVIF server and the rest are not. go2rtc's RTSP
      server stays on (127.0.0.1 only) because the ffmpeg transcode source
      publishes its output back to go2rtc over it;
    * WebRTC ICE, by mode (Settings -> Online access):
        - ``auto``: ``listen: ""``. No fixed port: every connection gets its own
          ephemeral UDP sockets, and pion gathers a srflx candidate per STUN
          server on them. go2rtc is a full (not lite) ICE agent, so it sends its
          own connectivity checks to the viewer's candidates: that opens the
          store router's mapping and the box firewall's conntrack entry (hole
          punching), and needs no port forward on an endpoint-independent NAT.
        - ``fixed_port``: ``listen: ":<port>"`` (UDP+TCP) and ``candidates:
          ["stun:<port>"]``: go2rtc advertises <public IP from STUN>:<port>, for
          a store router that forwards that port (and a firewall that allows it).
      ``ice_servers`` lists only the configured ``stun:`` URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("edge.go2rtc")

REPO_DIR = Path(__file__).resolve().parents[3]
API_USER = "edge"
API_PASSWORD_ENV = "EDGE_GO2RTC_API_PASSWORD"
RTSP_PORT_OFFSET = 16570  # 1984 -> 18554 (loopback only)
MODULES = ["api", "http", "rtsp", "webrtc", "exec", "ffmpeg"]
ALLOW_PATHS = ["/api", "/api/streams", "/api/webrtc"]
# Transcode engine names go2rtc understands (#hardware=...). "auto" lets go2rtc
# probe once: NVENC (cuda), then VA-API (AMD/Intel, which also drives Quick
# Sync on Linux), then libx264; on ARM v4l2m2m, then rkmpp.
ENCODER_ENGINES = {"auto": "", "vaapi": "vaapi", "qsv": "vaapi", "cuda": "cuda", "nvenc": "cuda",
                   "v4l2m2m": "v4l2m2m", "rkmpp": "rkmpp", "software": "software", "libx264": "software",
                   "cpu": "software"}
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "HOME", "XDG_CACHE_HOME", "LIBVA_DRIVER_NAME",
                   "LIBVA_DRIVERS_PATH", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES",
                   "NVIDIA_DRIVER_CAPABILITIES", "CUDA_CACHE_PATH")
_LINE_RE = re.compile(r"^(?:\S+\s+)?(TRC|DBG|INF|WRN|ERR|FTL|PNC)\s+(.*)$")
_LEVELS = {"TRC": logging.DEBUG, "DBG": logging.DEBUG, "INF": logging.INFO, "WRN": logging.WARNING,
           "ERR": logging.ERROR, "FTL": logging.ERROR, "PNC": logging.ERROR}


class Go2RtcUnavailable(RuntimeError):
    """go2rtc cannot serve video right now; the message is shown to the operator."""


class Go2RtcError(RuntimeError):
    """A go2rtc API call failed (``status`` is its HTTP status, 0 = unreachable)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def redact(text: str) -> str:
    try:
        from app.services.log_redaction import redact as _redact

        text = _redact(text)
    except Exception:
        pass
    # Any userinfo left in a URL (user-only, or a scheme the shared redactor skips).
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s\"']+@", r"\1***@", text)


def find_go2rtc() -> Optional[str]:
    """``GO2RTC_PATH``, then PATH, then ``<repo>/bin/go2rtc`` (bootstrap.py)."""
    explicit = os.environ.get("GO2RTC_PATH")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    found = shutil.which("go2rtc")
    if found:
        return found
    local = REPO_DIR / "bin" / "go2rtc"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def go2rtc_version(binary: str, timeout: float = 5.0) -> Optional[str]:
    try:
        res = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"go2rtc version (\S+)", res.stdout or "")
    return m.group(1) if res.returncode == 0 and m else None


def api_port() -> int:
    return int(settings.GO2RTC_API_PORT)


def rtsp_port() -> int:
    port = int(os.environ.get("GO2RTC_RTSP_PORT") or 0)
    return port or min(65535, api_port() + RTSP_PORT_OFFSET)


def build_config(*, mode: str, webrtc_port: int, stun_servers: list[str], ffmpeg_bin: Optional[str],
                 api_listen_port: Optional[int] = None, rtsp_listen_port: Optional[int] = None) -> dict[str, Any]:
    """go2rtc config (as a dict; written as JSON, which YAML reads)."""
    stuns = [u for u in stun_servers if u.lower().startswith("stun:")]
    webrtc: dict[str, Any] = {"ice_servers": [{"urls": stuns}] if stuns else []}
    if mode == "fixed_port":
        webrtc["listen"] = f":{int(webrtc_port)}"
        if stuns:
            # <public IP from STUN>:<port> as a host candidate, for the port forward.
            webrtc["candidates"] = [f"stun:{int(webrtc_port)}"]
    else:
        # No fixed port: per-connection ephemeral UDP sockets, srflx via STUN.
        webrtc["listen"] = ""
    cfg: dict[str, Any] = {
        "app": {"modules": list(MODULES)},
        "api": {
            "listen": f"127.0.0.1:{int(api_listen_port or api_port())}",
            "username": API_USER,
            "password": "${" + API_PASSWORD_ENV + "}",
            "local_auth": True,
            "allow_paths": list(ALLOW_PATHS),
            "origin": "",
        },
        "rtsp": {"listen": f"127.0.0.1:{int(rtsp_listen_port or rtsp_port())}"},
        "webrtc": webrtc,
        "log": {"format": "text", "level": "info", "output": "stdout", "time": ""},
    }
    # Named argument templates the stream sources refer to (no spaces allowed in
    # an API-added source); no credentials.
    cfg["ffmpeg"] = {VAAPI_DEVICE_TEMPLATE: VAAPI_DEVICE_ARGS}
    if ffmpeg_bin:
        cfg["ffmpeg"]["bin"] = ffmpeg_bin
    return cfg


def config_path() -> Path:
    return Path(settings.STORAGE_DIR) / "go2rtc" / "go2rtc.yaml"


def pid_path() -> Path:
    return Path(settings.STORAGE_DIR) / "go2rtc" / "go2rtc.pid"


def write_config(cfg: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Directory 0700, file 0600, written atomically."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    text = ("# Generated by Edge AI CCTV on every go2rtc start (services/go2rtc_manager.py).\n"
            "# No streams and no credentials: streams are added per viewing session over the API.\n"
            + json.dumps(cfg, indent=2) + "\n")
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


# go2rtc 1.9.14 builds VA-API transcodes as "-hwaccel vaapi ... -vf format=vaapi|nv12,hwupload"
# and relies on the decoder to create the VA-API device. FFmpeg 7.1+ checks the
# filter graph when it opens the output, before any decoder exists, so hwupload
# fails with "A hardware device reference is required" (live box, FFmpeg 8.0.1,
# AMD GPU, 2026-09-29; the same with CUDA reproduced on FFmpeg 9). Creating the
# device up front fixes it: the decoder reuses it and hwupload finds it. Global
# option, so its place after -i (where #raw puts it) is fine. go2rtc refuses a
# source with spaces added through its API ("source with spaces may be
# insecure", live box 2026-09-29), so the arguments are a named template in the
# config's ffmpeg section and the source names it: #raw=<template>.
VAAPI_DEVICE_TEMPLATE = "edge_vaapi_device"
VAAPI_DEVICE_ARGS = "-init_hw_device vaapi"
VAAPI_DEVICE_RAW = f"#raw={VAAPI_DEVICE_TEMPLATE}"
# go2rtc's own probes (internal/ffmpeg/hardware/hardware_unix.go), same order on x86.
_ENGINE_PROBES = (
    ("cuda", ["-init_hw_device", "cuda", "-f", "lavfi", "-i", "testsrc2", "-t", "1", "-c", "h264_nvenc",
              "-f", "null", "-"]),
    ("vaapi", ["-init_hw_device", "vaapi", "-f", "lavfi", "-i", "testsrc2", "-t", "1", "-vf",
               "format=nv12,hwupload", "-c", "h264_vaapi", "-f", "null", "-"]),
)
_engine_cache: dict[str, str] = {}
_engine_lock = threading.Lock()


def detect_transcode_engine(ffmpeg_bin: Optional[str] = None) -> str:
    """The H.264 encoder engine go2rtc's ``#hardware`` would pick here: cuda, vaapi or software.

    Probed once per ffmpeg binary (about a second), like go2rtc. On ARM the
    choice is left to go2rtc (v4l2m2m / rkmpp need no extra device): "".
    """
    import platform

    if platform.machine().lower() not in ("x86_64", "amd64", "i686", "x86"):
        return ""
    if ffmpeg_bin is None:
        from app.services.capture_backends import ffmpeg_binary

        ffmpeg_bin = ffmpeg_binary()
    if not ffmpeg_bin:
        return "software"
    with _engine_lock:
        if ffmpeg_bin in _engine_cache:
            return _engine_cache[ffmpeg_bin]
        engine = "software"
        for name, args in _ENGINE_PROBES:
            try:
                ok = subprocess.run([ffmpeg_bin, "-hide_banner", "-loglevel", "error", *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=15).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok:
                engine = name
                break
        _engine_cache[ffmpeg_bin] = engine
        logger.info(f"Live video transcoding engine: {engine} (probed {ffmpeg_bin})")
        return engine


def transcode_source(url: str, engine: Optional[str] = None) -> str:
    """go2rtc ffmpeg source that re-encodes the video to H.264 (audio dropped).

    "auto" (``WEBRTC_TRANSCODE_ENCODER``, default) probes this machine once the
    way go2rtc does (NVENC, then VA-API, else libx264 on x86; on ARM go2rtc's
    own ``#hardware`` probe: v4l2m2m, rkmpp). A configured engine is used as is.
    VA-API gets its device created up front (see VAAPI_DEVICE_RAW).
    """
    choice = (engine if engine is not None else settings.WEBRTC_TRANSCODE_ENCODER or "auto").strip().lower()
    hw = ENCODER_ENGINES.get(choice, "")
    if choice in ("auto", "") or choice not in ENCODER_ENGINES:
        hw = detect_transcode_engine()
    suffix = "#video=h264"
    if hw == "software":
        pass
    elif hw == "vaapi":
        suffix += "#hardware=vaapi" + VAAPI_DEVICE_RAW
    elif hw:
        suffix += f"#hardware={hw}"
    else:
        suffix += "#hardware"
    return f"ffmpeg:{url}{suffix}"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_is_go2rtc(pid: int) -> bool:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return os.path.basename(exe).startswith("go2rtc")


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def reap_group(pgid: int, grace: float = 2.0) -> bool:
    """End what is left of go2rtc's process group once go2rtc itself is gone.

    go2rtc runs in its own session (``start_new_session``), so the group is
    go2rtc plus the ffmpeg transcoders it spawned. An ffmpeg that is decoding
    on the GPU can ignore SIGTERM indefinitely (seen with ``-hwaccel cuda``:
    it outlived its go2rtc, kept pulling the camera and held the GPU context,
    and made ``systemctl stop`` wait for its 90 s timeout). SIGTERM, a short
    grace, then SIGKILL. Returns True if something had to be ended.
    """
    if os.name != "posix" or not pgid or not _group_alive(pgid):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
        logger.warning("go2rtc's ffmpeg child ignored SIGTERM after go2rtc ended; killed")
    except (ProcessLookupError, PermissionError):
        pass
    return True


class Go2RtcManager:
    """Supervises one go2rtc child and talks to its API."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._password = ""
        self._start_lock: Optional[tuple[object, asyncio.Lock]] = None
        self.state = "stopped"          # stopped | starting | running | error
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.restarts = 0
        self.config_in_use: Optional[dict[str, Any]] = None
        self._version_cache: Optional[tuple[str, Optional[str]]] = None
        self.backoff_base = 1.0
        self.backoff_max = 30.0
        # Injectable for tests.
        self.binary_finder = find_go2rtc
        self.on_exit = None  # callable(reason) when the process ends unexpectedly

    # -- settings ---------------------------------------------------------------

    @staticmethod
    def _video_settings() -> dict[str, Any]:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.webrtc_settings()

    def desired_config(self) -> dict[str, Any]:
        s = self._video_settings()
        from app.services.capture_backends import ffmpeg_binary

        return build_config(mode=s["webrtc_mode"], webrtc_port=s["webrtc_port"],
                            stun_servers=s["stun_servers"], ffmpeg_bin=ffmpeg_binary())

    # -- status -------------------------------------------------------------------

    def binary_info(self) -> tuple[Optional[str], Optional[str]]:
        binary = self.binary_finder()
        if not binary:
            return None, None
        with self._lock:
            cached = self._version_cache
        if cached and cached[0] == binary:
            return binary, cached[1]
        version = go2rtc_version(binary)
        with self._lock:
            self._version_cache = (binary, version)
        return binary, version

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None and self.state == "running"

    def status(self) -> dict[str, Any]:
        binary, version = self.binary_info()
        with self._lock:
            proc = self._proc
            cfg = self.config_in_use or {}
            out = {
                "installed": bool(binary),
                "binary": binary,
                "version": version,
                "state": self.state,
                "running": proc is not None and proc.poll() is None and self.state == "running",
                "pid": proc.pid if proc is not None and proc.poll() is None else None,
                "last_error": self.last_error,
                "restarts": self.restarts,
                "api": f"127.0.0.1:{api_port()}",
                "listen": (cfg.get("webrtc") or {}).get("listen"),
            }
        return out

    # -- process ------------------------------------------------------------------

    def _async_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._start_lock is None or self._start_lock[0] is not loop:
            self._start_lock = (loop, asyncio.Lock())
        return self._start_lock[1]

    async def ensure_running(self) -> None:
        """Start go2rtc if needed and wait until its API answers. Raises Go2RtcUnavailable."""
        if self.running():
            return
        async with self._async_lock():
            if self.running():
                return
            await asyncio.to_thread(self._start)
            deadline = time.monotonic() + float(settings.GO2RTC_START_TIMEOUT_S)
            last = None
            while time.monotonic() < deadline:
                with self._lock:
                    proc = self._proc
                if proc is None or proc.poll() is not None:
                    break
                try:
                    await self.api_info()
                    with self._lock:
                        self.state = "running"
                        self.last_error = None
                    self._report_health()
                    return
                except Go2RtcError as exc:
                    last = str(exc)
                await asyncio.sleep(0.15)
            with self._lock:
                reason = self.last_error or last or "go2rtc did not answer on its API in time"
                self.state = "error"
                self.last_error = reason
            await asyncio.to_thread(self._stop_supervisor)
            with self._lock:
                self.state, self.last_error = "error", reason
            self._report_health()
            raise Go2RtcUnavailable(reason)

    def _fail(self, reason: str) -> None:
        with self._lock:
            self.state = "error"
            self.last_error = reason
        logger.error(f"go2rtc: {reason}")

    def stop_leftover(self) -> None:
        """Stop a go2rtc from an earlier run of this app (pid file), never anything else."""
        path = pid_path()
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return
        if pid > 1 and _pid_is_go2rtc(pid):
            logger.warning(f"Stopping go2rtc left over from an earlier run (pid {pid})")
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            for _ in range(30):
                if not _pid_is_go2rtc(pid):
                    break
                time.sleep(0.1)
        try:
            path.unlink()
        except OSError:
            pass

    def _prepare(self) -> Optional[tuple[list[str], dict[str, str]]]:
        binary = self.binary_finder()
        if not binary:
            self._fail("go2rtc is not installed (re-run the installer; it fetches the pinned go2rtc into "
                       "/opt/edge-cctv/bin, or set GO2RTC_PATH)")
            return None
        self.stop_leftover()
        if _port_in_use(api_port()):
            self._fail(f"port 127.0.0.1:{api_port()} (GO2RTC_API_PORT) is already used by another program")
            return None
        cfg = self.desired_config()
        try:
            path = write_config(cfg)
        except OSError as exc:
            self._fail(f"its configuration could not be written: {exc.strerror or exc}")
            return None
        with self._lock:
            self._password = secrets.token_urlsafe(24)
            self.config_in_use = cfg
        env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH}
        env[API_PASSWORD_ENV] = self._password
        return [binary, "-config", str(path)], env

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self.state = "starting"
            self.last_error = None
            prepared = self._prepare()
            if prepared is None:
                return
            self._launch(*prepared)
            self._thread = threading.Thread(target=self._supervise, args=(self._stop,),
                                            name="go2rtc-supervisor", daemon=True)
            self._thread.start()

    def _launch(self, cmd: list[str], env: dict[str, str]) -> None:
        mode = (self.config_in_use or {}).get("webrtc", {}).get("listen")
        logger.info(f"Starting go2rtc ({cmd[0]}), API 127.0.0.1:{api_port()}, WebRTC listen={mode!r}")
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                start_new_session=(os.name == "posix"))
        with self._lock:
            self._proc = proc
            self.started_at = time.time()
        try:
            pid_path().write_text(str(proc.pid))
        except OSError:
            pass

    def _supervise(self, stop: threading.Event) -> None:
        backoff = self.backoff_base
        while not stop.is_set():
            with self._lock:
                proc = self._proc
            if proc is None:
                break
            assert proc.stdout is not None
            for raw in proc.stdout:
                self._handle_line(raw.rstrip())
            code = proc.wait()
            reap_group(proc.pid)   # its ffmpeg children must not outlive it
            if stop.is_set():
                break
            reason = f"go2rtc exited unexpectedly (code {code})"
            with self._lock:
                self.state = "error"
                self.last_error = reason
                self.restarts += 1
            logger.error(reason + f"; restarting in {backoff:.0f}s")
            self._report_health()
            if self.on_exit is not None:
                try:
                    self.on_exit(reason)
                except Exception:
                    logger.exception("go2rtc exit handler failed")
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.backoff_max)
            prepared = self._prepare()
            if prepared is None:
                continue
            try:
                self._launch(*prepared)
                with self._lock:
                    self.state = "running"
            except OSError as exc:
                self._fail(f"go2rtc could not be started: {exc.strerror or exc}")
                with self._lock:
                    self._proc = None
                break

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        line = redact(line)
        m = _LINE_RE.match(line)
        level, msg = (m.group(1), m.group(2)) if m else ("INF", line)
        logger.log(_LEVELS.get(level, logging.INFO), f"go2rtc: {msg[:500]}")
        if level in ("FTL", "PNC") or (level == "ERR" and "listen" in msg):
            with self._lock:
                self.last_error = msg[:300]

    def _terminate(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM) if os.name == "posix" else proc.terminate()
                proc.wait(timeout=5)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL) if os.name == "posix" else proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        # Also when go2rtc had already exited (e.g. systemd's SIGTERM reached
        # the whole unit): a transcoding ffmpeg may still be running.
        reap_group(proc.pid)

    def _stop_supervisor(self) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
        stop.set()
        self._terminate()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._lock:
            was = self._proc is not None
            self._proc = None
            self._thread = None
            if self.state != "error":
                self.state = "stopped"
        try:
            pid_path().unlink()
        except OSError:
            pass
        if was:
            logger.info("go2rtc stopped")

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_supervisor)
        with self._lock:
            failed = self.state == "error"
            self.state = "stopped"
        if not failed:
            # Health must not keep saying HEALTHY for a process that is gone.
            try:
                from app.services.resilience import ServiceHealthTracker as T

                T.report_status("go2rtc", T.NOT_CHECKED, "stopped: it starts again on the next live-video session")
            except Exception:
                pass

    async def restart(self) -> None:
        await self.stop()
        await self.ensure_running()

    def _report_health(self) -> None:
        try:
            from app.services.resilience import ServiceHealthTracker as T

            with self._lock:
                state, err = self.state, self.last_error
            if state == "running":
                T.report_status("go2rtc", T.HEALTHY, None)
            elif not self.binary_finder():
                T.report_status("go2rtc", T.NOT_PRESENT, err or "go2rtc is not installed")
            else:
                T.report_status("go2rtc", T.FAILED, err)
        except Exception:
            pass

    # -- API ----------------------------------------------------------------------

    def _client(self, timeout: float = 10.0):
        import httpx

        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port()}", auth=(API_USER, self._password),
                                 timeout=timeout)

    async def _request(self, method: str, path: str, *, timeout: float = 10.0, **kw):
        import httpx

        try:
            async with self._client(timeout) as client:
                return await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise Go2RtcError(f"go2rtc API not reachable ({exc.__class__.__name__})") from None

    async def api_info(self) -> dict[str, Any]:
        res = await self._request("GET", "/api", timeout=2.0)
        if res.status_code != 200:
            raise Go2RtcError(f"go2rtc API answered HTTP {res.status_code}", res.status_code)
        return res.json()

    async def add_stream(self, name: str, source: str) -> None:
        """Create (or re-point) a stream in memory only (PATCH never writes the config file)."""
        res = await self._request("PATCH", "/api/streams", params={"name": name, "src": source})
        if res.status_code != 200:
            raise Go2RtcError(redact(f"adding the stream failed: HTTP {res.status_code} {res.text.strip()[:200]}"),
                              res.status_code)

    async def delete_stream(self, name: str) -> None:
        res = await self._request("DELETE", "/api/streams", params={"src": name}, timeout=5.0)
        # 400 = go2rtc could not update a config file; the stream is removed from memory first.
        if res.status_code not in (200, 400, 404):
            raise Go2RtcError(f"deleting the stream failed: HTTP {res.status_code}", res.status_code)

    async def streams(self) -> dict[str, Any]:
        res = await self._request("GET", "/api/streams", timeout=5.0)
        if res.status_code != 200:
            raise Go2RtcError(f"listing streams failed: HTTP {res.status_code}", res.status_code)
        data = res.json()
        return data if isinstance(data, dict) else {}

    async def stream(self, name: str) -> Optional[dict[str, Any]]:
        res = await self._request("GET", "/api/streams", params={"src": name}, timeout=5.0)
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise Go2RtcError(f"reading the stream failed: HTTP {res.status_code}", res.status_code)
        try:
            data = res.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def webrtc_answer(self, name: str, offer_sdp: str, timeout: float = 25.0) -> str:
        """Complete (non-trickle) SDP answer for the browser's offer. go2rtc opens the camera now."""
        res = await self._request("POST", "/api/webrtc", params={"src": name}, timeout=timeout,
                                  json={"type": "offer", "sdp": offer_sdp})
        if res.status_code != 200:
            raise Go2RtcError(redact(res.text.strip()[:400] or f"HTTP {res.status_code}"), res.status_code)
        try:
            body = res.json()
        except ValueError:
            raise Go2RtcError("go2rtc returned an answer that is not JSON", res.status_code) from None
        sdp = body.get("sdp") if isinstance(body, dict) else None
        if not sdp:
            raise Go2RtcError("go2rtc returned an empty answer", res.status_code)
        return sdp


go2rtc_manager = Go2RtcManager()
