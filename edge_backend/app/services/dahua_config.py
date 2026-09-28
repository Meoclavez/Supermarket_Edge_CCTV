"""Read and change a Dahua recorder's per-channel encoder settings over HTTP.

Used by the "Upgrade CIF sub-streams to D1" action (services/recorder_substreams.py).
Only the sub-stream (``ExtraFormat[0]``, RTSP ``subtype=1``) is ever written.

Transport: Dahua's CGI API over plain HTTP with Digest authentication. The
client keeps one persistent connection and reuses the nonce (``nc`` counting
up) for every later request. Unlike Dahua's RTSP server, the recorder's HTTP
server closes the connection after its ``401`` challenge (seen on the
Pearcedale NVR), so the challenge is kept and answered on a new connection,
as a browser does. When a recorder closes after an answered request instead,
the next connection asks for a fresh challenge first, so a nonce that is bound
to its connection is never replayed. An
authenticated request that is refused with ``401`` (not a ``stale`` nonce)
raises :class:`DahuaAuthError` at once, without a retry: Dahua locks the
account after about five failed logins.

Endpoints used (firmware varies; each is optional except ``Encode``):

* ``configManager.cgi?action=getConfig&name=Encode``: every channel's encoder
  settings, ``table.Encode[<channel-1>].ExtraFormat[0].Video.*``.
* ``encode.cgi?action=getConfigCaps&channel=<N>`` (1-based): the channel's
  capabilities, ``caps.ExtraFormat[0].Video.ResolutionTypes=D1,CIF,QCIF``.
  Older/other firmware: ``configManager.cgi?action=getConfig&name=EncodeCaps``
  (``table.EncodeCaps[<idx>].ExtraFormat[0].Video.ResolutionTypes``).
  ``ResolutionTypes`` may be a comma list or indexed (``ResolutionTypes[0]=D1``)
  and hold names (``D1``, ``CIF``) or sizes (``704x576``).
* ``configManager.cgi?action=getConfig&name=VideoStandard``: ``PAL``/``NTSC``,
  which decides whether D1 is 704x576 or 704x480.
* ``configManager.cgi?action=setConfig&Encode[<idx>].ExtraFormat[0].Video.<key>=<v>``;
  the recorder answers ``OK`` when it accepted the change.

No credentials are logged or returned.
"""

from __future__ import annotations

import base64
import http.client
import logging
import re
from typing import Optional

from app.services.rtsp_probe import _pick_challenge, digest_authorization

logger = logging.getLogger(__name__)

USER_AGENT = "EdgeCCTV-Config"
MAX_BODY_BYTES = 4 * 1024 * 1024

# Named Dahua resolutions: (PAL size, NTSC size).
RESOLUTION_NAMES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "D1": ((704, 576), (704, 480)),
    "HD1": ((352, 576), (352, 480)),
    "BCIF": ((704, 288), (704, 240)),
    "2CIF": ((704, 288), (704, 240)),
    "CIF": ((352, 288), (352, 240)),
    "QCIF": ((176, 144), (176, 120)),
    "960H": ((960, 576), (960, 480)),
    "VGA": ((640, 480), (640, 480)),
    "QVGA": ((320, 240), (320, 240)),
    "SVGA": ((800, 600), (800, 600)),
    "XVGA": ((1024, 768), (1024, 768)),
    "XGA": ((1024, 768), (1024, 768)),
    "WXGA": ((1280, 800), (1280, 800)),
    "720P": ((1280, 720), (1280, 720)),
    "HD": ((1280, 720), (1280, 720)),
    "1_3M": ((1280, 960), (1280, 960)),
    "1080P": ((1920, 1080), (1920, 1080)),
    "FHD": ((1920, 1080), (1920, 1080)),
}

D1_PAL = (704, 576)
D1_NTSC = (704, 480)
# Sub-stream bit rate below which a CIF-era setting is raised for D1 (kbps),
# and what it is raised to.
CIF_LEVEL_BITRATE = 512
D1_BITRATE_H264 = 1024
D1_BITRATE_H265 = 768

_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x*X×]\s*(\d{2,5})\s*$")


class DahuaAuthError(RuntimeError):
    """The recorder rejected the username/password. Do not retry."""


class DahuaHttpError(RuntimeError):
    """The recorder could not be reached or answered something unusable."""


# --------------------------------------------------------------------- client

class DahuaHttpClient:
    """One persistent HTTP connection to a Dahua recorder, Digest-authenticated.

    Blocking; call it from a worker thread. Not thread safe (one run at a time).
    """

    def __init__(self, host: str, username: str, password: str, *, port: int = 80,
                 timeout: float = 8.0):
        self.host = host
        self.port = int(port or 80)
        self.username = username or ""
        self.password = password or ""
        self.timeout = float(timeout)
        self._conn: Optional[http.client.HTTPConnection] = None
        self._challenge: Optional[tuple[str, dict]] = None   # bound to self._conn
        self._nc = 0
        self.requests_sent = 0

    # -- connection ----------------------------------------------------------
    def _connect(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        return self._conn

    def _drop(self) -> None:
        """Close the socket but keep the challenge: the next request answers it."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None
        self._challenge = None
        self._nc = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one round trip --------------------------------------------------------
    def _auth_header(self, path: str) -> Optional[str]:
        if self._challenge is None:
            return None
        scheme, params = self._challenge
        if scheme == "basic":
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            return f"Basic {token}"
        self._nc += 1
        return digest_authorization("GET", path, self.username, self.password, params, nc=f"{self._nc:08x}")

    def _round_trip(self, path: str, auth: Optional[str]) -> tuple[int, dict, str]:
        conn = self._connect()
        headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive", "Accept": "*/*"}
        if auth:
            headers["Authorization"] = auth
        self.requests_sent += 1
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read(MAX_BODY_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise DahuaHttpError(f"recorder {self.host}:{self.port} did not answer ({type(exc).__name__})") from None
        hdrs: dict[str, list[str]] = {}
        for k, v in resp.getheaders():
            hdrs.setdefault(k.lower(), []).append(v)
        if resp.will_close:
            if resp.status == 401:
                # Closed right after its challenge (the Pearcedale NVR does):
                # the only way to answer it is on the next connection.
                self._drop()
            else:
                # A nonce may be tied to this connection: fetch a fresh one on
                # the next, so a replay never counts as a failed login.
                self.close()
        return resp.status, hdrs, body[:MAX_BODY_BYTES].decode("utf-8", "replace")

    def get(self, path: str) -> tuple[int, str]:
        """GET ``path`` (``/cgi-bin/...``). Returns (status, body). Raises on auth failure.

        At most one unauthenticated request (to receive the challenge) and
        one authenticated answer per new connection; a refused answer raises.
        """
        stale_retry_used = False
        reconnect_used = False
        while True:
            had_challenge = self._challenge is not None
            try:
                status, hdrs, body = self._round_trip(path, self._auth_header(path))
            except DahuaHttpError:
                # A kept-alive connection the recorder closed meanwhile: one
                # fresh connection (new challenge), then give up.
                if had_challenge and not reconnect_used:
                    reconnect_used = True
                    continue
                raise
            if status != 401:
                return status, body
            scheme, params = _pick_challenge(hdrs.get("www-authenticate", []))
            if scheme not in ("digest", "basic"):
                raise DahuaAuthError(f"recorder {self.host} refused the request (HTTP 401, no usable challenge)")
            if not had_challenge:
                if not self.username:
                    raise DahuaAuthError(f"recorder {self.host} needs a username and password")
                self._challenge = (scheme, params)
                self._nc = 0
                continue
            if scheme == "digest" and str(params.get("stale", "")).lower() == "true" and not stale_retry_used:
                # Our nonce expired; the password was not judged wrong.
                stale_retry_used = True
                self._challenge = (scheme, params)
                self._nc = 0
                continue
            self.close()
            raise DahuaAuthError(f"recorder {self.host} rejected the username/password (HTTP 401)")

    # -- API helpers -----------------------------------------------------------
    def get_config(self, name: str) -> dict[str, str]:
        status, body = self.get(f"/cgi-bin/configManager.cgi?action=getConfig&name={name}")
        if status != 200:
            raise DahuaHttpError(f"getConfig {name}: HTTP {status} {first_line(body)}")
        data = parse_kv(body)
        if not data:
            raise DahuaHttpError(f"getConfig {name}: {first_line(body) or 'empty answer'}")
        return data

    def get_encode_caps(self, channel: int) -> dict[str, str]:
        """Caps of one channel (1-based) via encode.cgi; {} when unsupported."""
        status, body = self.get(f"/cgi-bin/encode.cgi?action=getConfigCaps&channel={int(channel)}")
        if status != 200:
            return {}
        return parse_kv(body)

    def set_config(self, assignments: dict[str, str]) -> tuple[bool, str]:
        """``setConfig`` with ``key=value`` pairs. (accepted, reason)."""
        query = "&".join(f"{k}={v}" for k, v in assignments.items())
        status, body = self.get(f"/cgi-bin/configManager.cgi?action=setConfig&{query}")
        text = first_line(body)
        if status == 200 and text.upper().startswith("OK"):
            return True, "OK"
        return False, (f"HTTP {status}" + (f": {text}" if text else ""))


# -------------------------------------------------------------------- parsing

def first_line(body: str) -> str:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def parse_kv(body: str) -> dict[str, str]:
    """``a.b[0].c=value`` lines -> dict (without the ``table.``/``caps.`` prefix kept)."""
    out: dict[str, str] = {}
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if not k or " " in k:
            continue
        out[k] = v.strip()
    return out


def _strip_prefix(key: str) -> str:
    for p in ("table.", "caps."):
        if key.startswith(p):
            return key[len(p):]
    return key


def encode_prefix(channel: int) -> str:
    """``Encode[<channel-1>].ExtraFormat[0].`` (the sub-stream of a 1-based channel)."""
    return f"Encode[{int(channel) - 1}].ExtraFormat[0]."


def channel_substream(config: dict[str, str], channel: int) -> dict[str, str]:
    """This channel's sub-stream keys, relative (``Video.Width`` -> value)."""
    prefix = encode_prefix(channel)
    out = {}
    for k, v in config.items():
        bare = _strip_prefix(k)
        if bare.startswith(prefix):
            out[bare[len(prefix):]] = v
    return out


def channels_in_config(config: dict[str, str]) -> list[int]:
    found = set()
    for k in config:
        m = re.match(r"^Encode\[(\d+)\]\.ExtraFormat\[0\]\.", _strip_prefix(k))
        if m:
            found.add(int(m.group(1)) + 1)
    return sorted(found)


def parse_size(value: Optional[str], standard: str = "PAL") -> Optional[tuple[int, int]]:
    if not value:
        return None
    m = _SIZE_RE.match(str(value))
    if m:
        return int(m.group(1)), int(m.group(2))
    named = RESOLUTION_NAMES.get(str(value).strip().upper())
    if named:
        return named[1] if standard == "NTSC" else named[0]
    return None


def substream_size(sub: dict[str, str], standard: str = "PAL") -> Optional[tuple[int, int]]:
    """Current sub-stream size from ``Video.Width``/``Height`` or ``Video.resolution``."""
    try:
        w, h = int(sub.get("Video.Width", "")), int(sub.get("Video.Height", ""))
        if w > 0 and h > 0:
            return w, h
    except ValueError:
        pass
    for key in ("Video.resolution", "Video.Resolution"):
        size = parse_size(sub.get(key), standard)
        if size:
            return size
    return None


def caps_resolutions(caps: dict[str, str], channel: Optional[int] = None,
                     standard: str = "PAL") -> Optional[list[tuple[int, int]]]:
    """Sizes the sub-stream supports, from any caps layout; None if not found.

    Accepts ``[caps.|table.][EncodeCaps[idx].|caps[idx].]ExtraFormat[0].Video.ResolutionTypes``
    as a comma list or indexed entries. With an index in the key, only
    ``channel-1`` counts.
    """
    values: list[str] = []
    for k, v in caps.items():
        bare = _strip_prefix(k)
        m = re.match(r"^(?:(?:EncodeCaps|caps|Encode)\[(\d+)\]\.)?ExtraFormat\[0\]\.Video\.ResolutionTypes(?:\[\d+\])?$",
                     bare)
        if not m:
            continue
        if m.group(1) is not None and channel is not None and int(m.group(1)) != int(channel) - 1:
            continue
        values.extend(x for x in re.split(r"[,;|\s]+", v) if x)
    if not values:
        return None
    sizes: list[tuple[int, int]] = []
    for item in values:
        size = parse_size(item, standard)
        if size and size not in sizes:
            sizes.append(size)
    return sizes


def caps_max_bitrate(caps: dict[str, str], channel: Optional[int] = None) -> Optional[int]:
    for k, v in caps.items():
        bare = _strip_prefix(k)
        m = re.match(r"^(?:(?:EncodeCaps|caps|Encode)\[(\d+)\]\.)?ExtraFormat\[0\]\.Video\.BitRateOptions$", bare)
        if not m:
            continue
        if m.group(1) is not None and channel is not None and int(m.group(1)) != int(channel) - 1:
            continue
        nums = [int(x) for x in re.findall(r"\d+", v)]
        if nums:
            return max(nums)
    return None


def video_standard(config: dict[str, str]) -> Optional[str]:
    for k, v in config.items():
        if _strip_prefix(k) == "VideoStandard":
            val = v.strip().upper()
            if val in ("PAL", "NTSC"):
                return val
    return None


def d1_size(standard: str) -> tuple[int, int]:
    return D1_NTSC if standard == "NTSC" else D1_PAL


def is_d1_or_higher(size: Optional[tuple[int, int]]) -> bool:
    return bool(size) and size[0] >= 704 and size[0] * size[1] >= 704 * 480


def size_label(size: Optional[tuple[int, int]]) -> str:
    if not size:
        return "unknown size"
    w, h = size
    name = {
        (352, 288): "CIF", (352, 240): "CIF", (704, 576): "D1", (704, 480): "D1",
        (176, 144): "QCIF", (1280, 720): "720p", (1920, 1080): "1080p", (2560, 1440): "1440p",
        (640, 480): "VGA", (960, 576): "960H", (704, 288): "2CIF", (352, 576): "HD1",
    }.get((w, h))
    return f"{w}×{h}" + (f" ({name})" if name else "")


def d1_assignments(channel: int, sub: dict[str, str], target: tuple[int, int],
                   max_bitrate: Optional[int] = None) -> dict[str, str]:
    """The ``setConfig`` pairs that move this sub-stream to ``target`` (D1).

    Writes whichever size keys the channel already has (``Width``/``Height``
    and/or ``resolution``, in the same notation). Raises the bit rate to a D1
    value only when the current one is CIF-level; codec, FPS and GOP are not
    touched.
    """
    prefix = encode_prefix(channel)
    w, h = target
    out: dict[str, str] = {}
    if "Video.Width" in sub or "Video.Height" in sub:
        out[prefix + "Video.Width"] = str(w)
        out[prefix + "Video.Height"] = str(h)
    for key in ("Video.resolution", "Video.Resolution"):
        if key in sub:
            current = sub[key]
            out[prefix + key] = f"{w}x{h}" if _SIZE_RE.match(current or "") else "D1"
    if not out:
        out[prefix + "Video.Width"] = str(w)
        out[prefix + "Video.Height"] = str(h)
    try:
        rate = int(sub.get("Video.BitRate", ""))
    except ValueError:
        rate = None
    if rate is not None and rate < CIF_LEVEL_BITRATE:
        codec = (sub.get("Video.Compression") or "").upper()
        new_rate = D1_BITRATE_H265 if "265" in codec or "HEVC" in codec else D1_BITRATE_H264
        if max_bitrate:
            new_rate = min(new_rate, max_bitrate)
        if new_rate > rate:
            out[prefix + "Video.BitRate"] = str(new_rate)
    return out


# Keys a restore writes back (only those present in the saved copy).
RESTORE_KEYS = ("Video.Width", "Video.Height", "Video.resolution", "Video.Resolution", "Video.BitRate")


def restore_assignments(channel: int, saved: dict[str, str]) -> dict[str, str]:
    prefix = encode_prefix(channel)
    return {prefix + k: saved[k] for k in RESTORE_KEYS if k in saved and saved[k] != ""}

