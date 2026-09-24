"""Manually added camera sources: validation, credentials and connection tests.

A camera added by URL (rather than adopted from a scan) can be one of five
source types:

* ``rtsp``  -- ``rtsp://`` / ``rtsps://`` stream (IP camera, NVR channel)
* ``http``  -- HTTP(S) MJPEG stream
* ``onvif`` -- an ONVIF device address; the RTSP URI is resolved from the
  device's Media service (``GetProfiles`` + ``GetStreamUri``)
* ``usb``   -- a local capture device, by index or ``/dev/...`` path
* ``file``  -- a local video file, for testing an installation without a camera

Credentials are never stored inside ``cameras.rtsp_url``. The URL is stored
without a userinfo part and the username/password go to the per-machine
encrypted named-secret store (``secret_store.set_named_secret``, Fernet keyed
from ``NVR_CREDENTIAL_KEY``), one secret per camera. They are injected only
when a stream is opened (:func:`stream_source_for`). API responses show the
URL through ``redact_url``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.services.camera_drivers import build_stream_urls, redact_url

logger = logging.getLogger(__name__)

SOURCE_TYPES = ("rtsp", "http", "onvif", "usb", "file")
MAX_URL_LEN = 480          # cameras.rtsp_url is VARCHAR(512)
DEFAULT_TIMEOUT_S = 8.0
MAX_TIMEOUT_S = 20.0
PREVIEW_MAX_WIDTH = 480

_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")


class SourceError(ValueError):
    """The operator's input cannot describe a usable camera source."""


@dataclass
class CameraSource:
    source_type: str
    url: str                          # credential-free; what is stored in rtsp_url
    username: Optional[str] = None
    password: Optional[str] = None
    onvif_host: Optional[str] = None  # onvif only
    onvif_port: int = 80

    @property
    def has_credentials(self) -> bool:
        return bool(self.username or self.password)


# --------------------------------------------------------------------- parsing

def infer_source_type(url: str) -> str:
    u = (url or "").strip().lower()
    if u.startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if u.startswith(("http://", "https://")):
        return "http"
    if u.isdigit() or u.startswith("/dev/"):
        return "usb"
    return "file"


def _check_text(value: Optional[str], label: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    if len(value) > limit:
        raise SourceError(f"{label} is too long (max {limit} characters).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SourceError(f"{label} contains control characters.")
    return value


def _split_network_url(raw: str, schemes: tuple[str, ...], label: str):
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise SourceError(f"{label} URL is malformed: {exc}") from None
    if parts.scheme.lower() not in schemes:
        raise SourceError(f"{label} URL must start with {' or '.join(s + '://' for s in schemes)}.")
    host = parts.hostname or ""
    if not host or not _HOST_RE.match(host if ":" not in host else f"[{host}]"):
        raise SourceError(f"{label} URL has no valid host.")
    if port is not None and not (1 <= port <= 65535):
        raise SourceError(f"{label} URL port must be between 1 and 65535.")
    user = unquote(parts.username) if parts.username else None
    pw = unquote(parts.password) if parts.password else None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    clean = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))
    return clean, user, pw


def normalize_source(
    source_type: Optional[str],
    url: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    check_local: bool = True,
) -> CameraSource:
    """Validate operator input and return a credential-free source.

    Credentials embedded in the URL (``rtsp://user:pw@host``) are lifted out;
    explicit ``username``/``password`` take precedence over embedded ones.
    Raises :class:`SourceError` with an operator-readable message.
    """
    raw = (url or "").strip()
    if not raw:
        raise SourceError("Enter the stream URL, device or file path.")
    if len(raw) > MAX_URL_LEN:
        raise SourceError(f"URL or path is too long (max {MAX_URL_LEN} characters).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw) or " " in raw and not raw.startswith("/"):
        raise SourceError("URL contains spaces or control characters.")

    st = (source_type or "").strip().lower() or infer_source_type(raw)
    if st == "mjpeg":
        st = "http"
    if st not in SOURCE_TYPES:
        raise SourceError(f"Unknown source type '{source_type}'. Use one of: {', '.join(SOURCE_TYPES)}.")

    username = _check_text(username.strip() if isinstance(username, str) else None, "Username", 128) or None
    password = _check_text(password if isinstance(password, str) else None, "Password", 256) or None

    if st == "rtsp":
        clean, u, p = _split_network_url(raw, ("rtsp", "rtsps"), "RTSP")
        return CameraSource("rtsp", clean, username or u, password or p)

    if st == "http":
        clean, u, p = _split_network_url(raw, ("http", "https"), "HTTP MJPEG")
        return CameraSource("http", clean, username or u, password or p)

    if st == "onvif":
        target = raw if "://" in raw else f"http://{raw}"
        clean, u, p = _split_network_url(target, ("http", "https"), "ONVIF")
        parts = urlsplit(clean)
        return CameraSource(
            "onvif", clean, username or u, password or p,
            onvif_host=parts.hostname, onvif_port=parts.port or (443 if parts.scheme == "https" else 80),
        )

    if st == "usb":
        if raw.isdigit():
            index = int(raw)
            if index > 63:
                raise SourceError("USB device index must be between 0 and 63.")
            if not sys.platform.startswith("linux"):
                raise SourceError("Add a USB camera by its device path; capture by index is only mapped on Linux (V4L2).")
            path = f"/dev/video{index}"
        elif raw.startswith("/dev/") and ".." not in raw:
            path = raw
        else:
            raise SourceError("USB camera must be a device index (0, 1, ...) or a /dev/... path.")
        if check_local and not os.path.exists(path):
            raise SourceError(f"Capture device {path} does not exist on this machine.")
        return CameraSource("usb", path)

    # file
    path_str = raw[len("file://"):] if raw.startswith("file://") else raw
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        raise SourceError("Video file path must be absolute (e.g. /home/me/test.mp4).")
    if check_local:
        if not path.is_file():
            raise SourceError(f"Video file {path} does not exist.")
        if not os.access(path, os.R_OK):
            raise SourceError(f"Video file {path} is not readable by the server.")
    return CameraSource("file", str(path))


def inject_credentials(url: str, username: Optional[str], password: Optional[str]) -> str:
    """Return ``url`` with a userinfo part. Only used when opening a stream."""
    if not (username or password) or not url.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return url
    parts = urlsplit(url)
    if parts.username or parts.password:
        return url
    userinfo = quote(username or "", safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def mask_url(url: Optional[str]) -> str:
    return redact_url(url or "")


def is_masked(url: Optional[str]) -> bool:
    return "://***@" in (url or "")


# ------------------------------------------------------------ credential store

def _secret_name(camera_id: str) -> str:
    return "camcred-" + hashlib.sha256(camera_id.encode("utf-8")).hexdigest()[:32]


def _store_args(storage_dir=None, machine_key=None):
    if storage_dir is not None and machine_key is not None:
        return storage_dir, machine_key
    from app.config import settings

    return (storage_dir if storage_dir is not None else settings.STORAGE_DIR,
            machine_key if machine_key is not None else settings.NVR_CREDENTIAL_KEY)


def store_credentials(camera_id: str, username: Optional[str], password: Optional[str], *,
                      storage_dir=None, machine_key=None) -> None:
    from app.services.secret_store import set_named_secret

    storage, key = _store_args(storage_dir, machine_key)
    if not key:
        raise RuntimeError("NVR_CREDENTIAL_KEY is not available; cannot store camera credentials")
    payload = json.dumps({"username": username or "", "password": password or ""})
    set_named_secret(storage, _secret_name(camera_id), payload, key)


def load_credentials(camera_id: str, *, storage_dir=None, machine_key=None) -> Optional[tuple[str, str]]:
    from app.services.secret_store import get_named_secret

    storage, key = _store_args(storage_dir, machine_key)
    if not key:
        return None
    try:
        raw = get_named_secret(storage, _secret_name(camera_id), key)
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
        return str(data.get("username") or ""), str(data.get("password") or "")
    except (ValueError, UnicodeDecodeError):
        return None


def has_credentials(camera_id: str, *, storage_dir=None) -> bool:
    from app.services.secret_store import has_named_secret

    storage, _ = _store_args(storage_dir, "")
    try:
        return has_named_secret(storage, _secret_name(camera_id))
    except (OSError, ValueError):
        return False


def delete_credentials(camera_id: str, *, storage_dir=None) -> bool:
    from app.services.secret_store import delete_named_secret

    storage, _ = _store_args(storage_dir, "")
    try:
        return delete_named_secret(storage, _secret_name(camera_id))
    except (OSError, ValueError):
        return False


def split_url_credentials(url: str) -> tuple[str, Optional[str], Optional[str]]:
    """``(url_without_userinfo, username, password)``.

    URLs without a userinfo part, non-network sources and URLs that cannot be
    parsed come back unchanged with ``(None, None)``.
    """
    if not url or "://" not in url or "@" not in url:
        return url, None, None
    try:
        parts = urlsplit(url)
        if not (parts.username or parts.password):
            return url, None, None
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return url, None, None
    if not host:
        return url, None, None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    clean = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    user = unquote(parts.username) if parts.username else None
    pw = unquote(parts.password) if parts.password else None
    return clean, user, pw


def detach_credentials(camera_id: str, url: str, username: Optional[str] = None,
                       password: Optional[str] = None) -> str:
    """Store a camera's credentials encrypted and return its credential-free URL.

    Credentials come from the explicit arguments or, failing that, from a
    ``user:pw@`` part of ``url``. Every flow that creates a camera row calls
    this before writing ``rtsp_url``. Raises RuntimeError if the store cannot
    be written (the caller must not fall back to writing the URL with them).
    """
    clean, u, p = split_url_credentials(url)
    username = username or u
    password = password or p
    if username or password:
        store_credentials(camera_id, username, password)
    return clean


def stream_source_for(camera_id: str, stored_url: str) -> str:
    """The URL a capture worker should open: stored URL + injected credentials."""
    if not stored_url or not stored_url.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return stored_url
    creds = load_credentials(camera_id)
    if not creds:
        return stored_url
    return inject_credentials(stored_url, creds[0], creds[1])


# ----------------------------------------------------------------------- ONVIF

_SOAP_ENV = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
)


def _wsse_header(username: Optional[str], password: Optional[str]) -> str:
    if not username:
        return ""
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + (password or "").encode("utf-8")).digest()
    ).decode()
    from xml.sax.saxutils import escape

    return (
        '<wsse:Security s:mustUnderstand="1" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
    )


def _soap(url: str, body: str, username, password, timeout: float) -> ET.Element:
    data = _SOAP_ENV.format(header=_wsse_header(username, password), body=body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-supplied LAN device)
            payload = resp.read(1 << 20)
    except urllib.error.HTTPError as exc:
        text = exc.read(1 << 16).decode("utf-8", "replace") if exc.fp else ""
        if exc.code in (400, 401, 403, 500) and ("NotAuthorized" in text or exc.code in (401, 403)):
            raise SourceError("ONVIF device rejected the username or password.") from None
        raise SourceError(f"ONVIF device returned HTTP {exc.code}.") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SourceError(f"ONVIF device is not reachable ({reason}).") from None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise SourceError("ONVIF device returned a response that is not valid XML.") from None
    fault = _first(root, "Fault")
    if fault is not None:
        text = " ".join(t.strip() for t in fault.itertext() if t.strip())
        if "NotAuthorized" in text or "Sender" in text and "auth" in text.lower():
            raise SourceError("ONVIF device rejected the username or password.")
        raise SourceError(f"ONVIF request failed: {text[:160]}")
    return root


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(root: ET.Element, name: str) -> Optional[ET.Element]:
    for node in root.iter():
        if _local(node.tag) == name:
            return node
    return None


def _swap_host(xaddr: str, host: str, port: int) -> str:
    parts = urlsplit(xaddr)
    netloc = f"[{host}]" if ":" in host else host
    netloc += f":{port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, ""))


def resolve_onvif_stream(
    host: str, port: int, username: Optional[str], password: Optional[str], timeout: float = 4.0,
    scheme: str = "http",
) -> dict[str, Any]:
    """Ask an ONVIF device for its RTSP stream URI.

    Returns ``{"url": <credential-free rtsp url>, "profile": token, "width", "height", "profiles": n}``.
    The profile chosen is the smallest one at least 640 px wide (the sub
    stream, which is what analytics wants), else the largest available.
    """
    netloc = f"[{host}]" if ":" in host else host
    device_url = f"{scheme}://{netloc}:{port}/onvif/device_service"
    media_url = None
    try:
        caps = _soap(
            device_url,
            '<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
            "<tds:Category>Media</tds:Category></tds:GetCapabilities>",
            username, password, timeout,
        )
        media = _first(caps, "Media")
        xaddr = _first(media, "XAddr") if media is not None else None
        if xaddr is not None and (xaddr.text or "").strip():
            media_url = xaddr.text.strip()
    except SourceError as exc:
        if "rejected" in str(exc) or "not reachable" in str(exc):
            raise
    candidates = [u for u in (media_url, media_url and _swap_host(media_url, host, port),
                              f"{scheme}://{netloc}:{port}/onvif/Media",
                              f"{scheme}://{netloc}:{port}/onvif/media_service") if u]
    seen: set[str] = set()
    profiles_root = None
    last_err: Optional[SourceError] = None
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            profiles_root = _soap(
                url, '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>',
                username, password, timeout,
            )
            media_url = url
            break
        except SourceError as exc:
            last_err = exc
            if "rejected" in str(exc):
                raise
    if profiles_root is None:
        raise last_err or SourceError("ONVIF device has no Media service.")

    profiles = []
    for node in profiles_root.iter():
        if _local(node.tag) != "Profiles" or not node.get("token"):
            continue
        w = h = None
        res = _first(node, "Resolution")
        if res is not None:
            try:
                w = int((_first(res, "Width").text or "0"))
                h = int((_first(res, "Height").text or "0"))
            except (AttributeError, ValueError):
                w = h = None
        profiles.append({"token": node.get("token"), "width": w, "height": h})
    if not profiles:
        raise SourceError("ONVIF device reported no media profiles.")

    sized = [p for p in profiles if p["width"]]
    choice = profiles[0]
    if sized:
        wide = sorted((p for p in sized if p["width"] >= 640), key=lambda p: p["width"] * (p["height"] or 0))
        choice = wide[0] if wide else max(sized, key=lambda p: p["width"] * (p["height"] or 0))

    from xml.sax.saxutils import escape

    uri_root = _soap(
        media_url,
        '<trt:GetStreamUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><trt:StreamSetup>'
        "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        f"</trt:StreamSetup><trt:ProfileToken>{escape(choice['token'])}</trt:ProfileToken></trt:GetStreamUri>",
        username, password, timeout,
    )
    uri = _first(uri_root, "Uri")
    if uri is None or not (uri.text or "").strip():
        raise SourceError("ONVIF device did not return a stream URI.")
    clean, _, _ = _split_network_url(uri.text.strip(), ("rtsp", "rtsps"), "ONVIF stream")
    return {
        "url": clean, "profile": choice["token"], "width": choice["width"],
        "height": choice["height"], "profiles": len(profiles),
    }


# ----------------------------------------------------------------------- probe

def _valid_fps(value: float) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if 0.5 <= v <= 240 else None


def probe_stream(open_target: str, source_type: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Open ``open_target``, grab one frame, and describe it. Blocking.

    ``fps`` is the rate measured over a few consecutive frames of a live
    source; ``fps_reported`` is what the container/stream header claims
    (FFmpeg's MJPEG demuxer, for one, reports 25 when it does not know).
    Either is ``None`` when unknown. Never raises.
    """
    import cv2

    started = time.monotonic()
    cap = None
    try:
        ms = int(max(1.0, timeout_s) * 1000)
        if source_type == "usb":
            api = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
            cap = cv2.VideoCapture(open_target, api)
        else:
            if source_type == "rtsp":
                os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
            cap = cv2.VideoCapture(
                open_target, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms],
            )
        if cap is None or not cap.isOpened():
            return {"success": False, "error_code": "CONNECT_FAILED",
                    "error": (f"Could not open {redact_url(open_target)}. Check the address, port, "
                              "credentials and that the stream is running.")}
        ok, frame = cap.read()
        if not ok or frame is None or getattr(frame, "size", 0) == 0:
            return {"success": False, "error_code": "NO_SIGNAL",
                    "error": "Connected, but no video frame arrived before the timeout."}
        h, w = frame.shape[:2]
        reported = _valid_fps(cap.get(cv2.CAP_PROP_FPS))

        measured = None
        if source_type != "file":
            stamps = []
            deadline = time.monotonic() + min(1.5, max(0.3, timeout_s - (time.monotonic() - started)))
            while len(stamps) < 8 and time.monotonic() < deadline:
                if not cap.grab():
                    break
                stamps.append(time.monotonic())
            # Skip the first grab: it often drains a buffered frame instantly.
            if len(stamps) >= 4 and stamps[-1] > stamps[1]:
                measured = _valid_fps((len(stamps) - 2) / (stamps[-1] - stamps[1]))

        scale = min(1.0, PREVIEW_MAX_WIDTH / float(w))
        preview = frame if scale >= 1.0 else cv2.resize(frame, (int(w * scale), int(round(h * scale))),
                                                         interpolation=cv2.INTER_AREA)
        ok_jpg, jpg = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
        return {
            "success": True,
            "width": int(w),
            "height": int(h),
            "resolution": f"{w}x{h}",
            "fps": measured if measured is not None else (reported if source_type == "file" else None),
            "fps_reported": reported,
            "fps_source": "measured" if measured is not None else ("container" if source_type == "file" and reported else None),
            "preview_jpeg": ("data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode("ascii"))
            if ok_jpg else None,
            "preview_width": int(preview.shape[1]),
            "preview_height": int(preview.shape[0]),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - a probe must report, not raise
        return {"success": False, "error_code": "PROBE_ERROR",
                "error": f"Connection test failed: {redact_url(str(exc))[:200]}"}
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass


_EDGE_BACKEND_DIR = Path(__file__).resolve().parents[2]
_probes_running = 0
MAX_CONCURRENT_PROBES = 3


async def probe_stream_isolated(open_target: str, source_type: str, timeout_s: float) -> dict[str, Any]:
    """Run :func:`probe_stream` in a child process.

    OpenCV's FFmpeg backend serialises ``VideoCapture`` opens behind one
    process-wide mutex, so an in-process probe queues behind any camera
    worker that is stuck opening an unreachable stream (measured: a local
    file open waited 5.5 s behind a hanging RTSP open). A child process has
    its own mutex, and a probe that hangs past the deadline is killed instead
    of leaking a thread. The target (which may carry credentials) is sent on
    stdin, never argv.
    """
    global _probes_running
    if _probes_running >= MAX_CONCURRENT_PROBES:
        return {"success": False, "error_code": "BUSY",
                "error": "Too many connection tests are running. Try again in a few seconds."}
    _probes_running += 1
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.camera_source", "--probe",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=str(_EDGE_BACKEND_DIR),
            )
        except (OSError, NotImplementedError) as exc:
            # No subprocess support here: fall back to a worker thread.
            logger.debug("isolated probe unavailable (%s); probing in-process", type(exc).__name__)
            return await asyncio.to_thread(probe_stream, open_target, source_type, timeout_s)
        request = json.dumps({"target": open_target, "type": source_type, "timeout": timeout_s}).encode()
        try:
            out, _ = await asyncio.wait_for(proc.communicate(request), timeout=timeout_s + 6)
        except asyncio.TimeoutError:
            return {"success": False, "error_code": "TIMEOUT",
                    "error": f"No frame within {timeout_s:.0f} s. Check the address and that the stream is running."}
        try:
            return json.loads(out.decode("utf-8").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"success": False, "error_code": "PROBE_ERROR",
                    "error": "The connection test process failed without a result."}
    finally:
        _probes_running -= 1
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


async def resolve_source_url(src: CameraSource, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """For ONVIF, resolve the RTSP URI; other types resolve to themselves."""
    if src.source_type != "onvif":
        return {"url": src.url, "resolved_via": None}
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(
                resolve_onvif_stream, src.onvif_host, src.onvif_port, src.username, src.password,
                min(4.0, timeout_s), urlsplit(src.url).scheme or "http",
            ),
            timeout=timeout_s + 2,
        )
        return {**info, "resolved_via": "onvif"}
    except asyncio.TimeoutError:
        raise SourceError("ONVIF device did not answer in time.") from None


async def test_connection(src: CameraSource, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Open the source for real and return one frame's facts plus a preview."""
    timeout_s = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    started = time.monotonic()
    targets: list[tuple[str, Optional[str]]] = []
    onvif_error = None
    if src.source_type == "onvif":
        try:
            info = await resolve_source_url(src, timeout_s)
            targets.append((info["url"], "onvif"))
        except SourceError as exc:
            onvif_error = str(exc)
            if "rejected" in onvif_error:
                return {"success": False, "error_code": "AUTH_FAILED", "error": onvif_error,
                        "source_type": src.source_type}
            # No usable Media service: try the common RTSP paths on port 554.
            targets += [(p.url, "candidate") for p in build_stream_urls("onvif", src.onvif_host or "")]
    else:
        targets.append((src.url, None))

    result: dict[str, Any] = {"success": False, "error_code": "CONNECT_FAILED",
                              "error": onvif_error or "Could not open the stream."}
    for url, via in targets:
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0.5:
            break
        per_try = remaining if len(targets) == 1 else min(4.0, remaining)
        open_target = inject_credentials(url, src.username, src.password) \
            if src.source_type in ("rtsp", "http", "onvif") else url
        try:
            result = await probe_stream_isolated(
                open_target, "rtsp" if src.source_type == "onvif" else src.source_type, per_try,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error_code": "PROBE_ERROR",
                      "error": f"Connection test failed: {type(exc).__name__}"}
        if result.get("success"):
            result["resolved_url"] = redact_url(url)
            result["resolved_via"] = via
            break
        if via == "candidate" and onvif_error:
            result["error"] = f"{onvif_error} Common RTSP paths did not answer either."
    result["source_type"] = src.source_type
    result["url"] = redact_url(src.url)
    return result


def _probe_main() -> int:
    """Child-process entry for :func:`probe_stream_isolated` (request on stdin)."""
    req = json.loads(sys.stdin.read() or "{}")
    result = probe_stream(str(req.get("target") or ""), str(req.get("type") or "file"),
                          float(req.get("timeout") or DEFAULT_TIMEOUT_S))
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__" and "--probe" in sys.argv:
    raise SystemExit(_probe_main())
