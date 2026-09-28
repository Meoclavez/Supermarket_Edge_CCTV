"""Duplicate camera guard: one physical camera configured more than once.

A camera reachable two ways gets added twice easily: the same recorder channel
typed as a URL on its main stream and adopted again from the recorder's channel
list on its sub-stream, or a camera added directly by its own IP address and
again as the recorder channel it is connected to. Both copies are analysed, so
every person is counted twice and the GPU does the work twice.

**Identity.** :func:`identity_for` derives a physical-camera identity from a
stored (credential-free) URL; credentials are ignored whatever the URL holds.

* Recorder / camera channel grammars -- Dahua ``/cam/realmonitor?channel=N``,
  Hikvision ``/Streaming/Channels/NNN`` (and ``/h264/chN/...``): the key is
  ``host:port`` + channel N; ``subtype`` / the stream digits are ignored.
* Generic RTSP (ONVIF-resolved URIs included): ``host:port`` + path + query,
  with well-known stream selectors normalised (``stream1/2``, ``onvif1/2``,
  ``profileN``, ``/main`` ``/sub``) and ``channel``/``ch`` kept.
* HTTP MJPEG: ``host:port`` + path + query. USB: the device path. Video
  files have no identity (test sources are never flagged).
* Every RTSP camera also carries a *device alias* ``dev:<host>[:port]/<input>``
  (port only when not 554). For a Dahua recorder whose connected-device list
  could be read (``RemoteDevice``, :func:`parse_remote_devices`), a channel's
  alias is the connected camera's address instead, so the recorder channel
  and a direct URL to that camera share a key. When the list cannot be read
  the channel keeps its host+channel identity and the report says so.

Two or more cameras sharing any key form a duplicate group, unless the
operator marked the pair "different cameras" (stored). In each group exactly
one camera is the *primary* and feeds store-wide totals; the others are
"duplicate, excluded from store totals" (:meth:`DuplicateGuard.excluded_ids`)
and are still viewable. Primary: the operator's choice, else an enabled
camera over a switched-off one, then a recorder channel over a direct address,
then a sub-stream over the main stream, then the oldest.

State: ``STORAGE_DIR/duplicate_cameras.json`` (dismissed pairs, chosen
primaries) and ``STORAGE_DIR/recorder_devices.json`` (per recorder: when its
device list was read, the channel -> camera address map, or why not). No
password is read into, logged or stored by this module; ``RemoteDevice``
answers contain the connected cameras' passwords and only address, port and
input fields are kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

from app.config import settings

logger = logging.getLogger(__name__)

STATE_FILE = "duplicate_cameras.json"
RECORDERS_FILE = "recorder_devices.json"
CACHE_TTL_S = 10.0
RECORDER_REFRESH_S = 24 * 3600.0

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}
STREAM_TEXT = {"main": "main stream", "sub": "sub-stream", "sub2": "sub-stream 2"}
_STREAM_RANK = {"sub": 0, "sub2": 1, None: 2, "main": 3}

_DAHUA_RE = re.compile(r"/cam/realmonitor$", re.IGNORECASE)
_HIK_RE = re.compile(r"/(?:isapi/)?streaming/channels/(\d+)/?$", re.IGNORECASE)
_HIK_OLD_RE = re.compile(r"/h26[45]/ch(\d+)/(main|sub)/av_stream$", re.IGNORECASE)
# Query keys that select a stream, a transport or carry a login: not identity.
_DROP_QUERY = {"subtype", "stream", "streamtype", "profile", "transportmode", "unicast", "rtsp_transport",
               "tcp", "proto", "user", "username", "password", "pwd", "passwd", "auth"}
_CHANNEL_QUERY = ("channel", "ch", "chn", "cam")
# Paths that address a camera's only video source (a stream selector at most).
_SINGLE_SOURCE_PATHS = {"", "/", "/live", "/live.sdp", "/stream", "/video", "/h264", "/h265", "/media.amp"}


# ------------------------------------------------------------------- identity

@dataclass(frozen=True)
class Identity:
    key: str                       # host:port + channel / path: never matches another physical camera
    keys: frozenset                # key plus device aliases
    scheme: str
    host: Optional[str] = None
    port: Optional[int] = None
    channel: Optional[int] = None  # recorder channel / video input (1-based), when the URL says
    stream: Optional[str] = None   # main | sub | sub2 | None
    grammar: str = "generic"       # dahua | hikvision | generic | http | usb
    mapped_to: Optional[str] = None  # the connected camera's address, from the recorder's device list


def _stream_from_digit(n: int) -> Optional[str]:
    return {0: "main", 1: "sub", 2: "sub2"}.get(n)


def parse_source(url: Optional[str]) -> Optional[dict]:
    """Host, port, channel, stream and a normalised path key of a stored URL; None if not a camera address."""
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.isdigit() or raw.startswith("/dev/"):
        path = f"/dev/video{int(raw)}" if raw.isdigit() else raw
        return {"scheme": "usb", "host": None, "port": None, "channel": None, "stream": None,
                "grammar": "usb", "path_key": path, "single": True}
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not host:
        return None
    port = port or DEFAULT_PORTS[scheme]
    path = parts.path or ""
    query = parse_qsl(parts.query, keep_blank_values=True)
    qmap = {k.lower(): v for k, v in query}
    out = {"scheme": scheme, "host": host, "port": port, "channel": None, "stream": None,
           "grammar": "http" if scheme in ("http", "https") else "generic", "path_key": "", "single": False}

    if scheme in ("rtsp", "rtsps"):
        if _DAHUA_RE.search(path) and "channel" in qmap:
            try:
                out.update(grammar="dahua", channel=int(qmap["channel"]),
                           stream=_stream_from_digit(int(qmap.get("subtype") or 0)))
                return out
            except ValueError:
                pass
        m = _HIK_RE.search(path)
        if m:
            n = int(m.group(1))
            ch, s = (n // 100, n % 100) if n >= 100 else (n, 1)
            out.update(grammar="hikvision", channel=ch, stream=_stream_from_digit(s - 1) if s else None)
            return out
        m = _HIK_OLD_RE.search(path)
        if m:
            out.update(grammar="hikvision", channel=int(m.group(1)), stream=m.group(2).lower())
            return out

    # Generic: normalise well-known stream selectors, keep everything else.
    p = path.lower().rstrip("/") if path not in ("", "/") else ""
    stream = None
    single = p in _SINGLE_SOURCE_PATHS
    for rx, repl in ((r"/(stream|onvif)([12])$", r"/\1*"), (r"profile_?(\d+)", "profile*"),
                     (r"/(main|sub)(?:stream)?$", "/*")):
        mm = re.search(rx, p)
        if mm:
            token = mm.group(mm.lastindex)
            stream = {"1": "main", "2": "sub", "main": "main", "sub": "sub"}.get(token.lower(), stream)
            p = re.sub(rx, repl, p)
            single = True
    for key in _CHANNEL_QUERY:
        if key in qmap:
            try:
                out["channel"] = int(qmap[key])
                break
            except ValueError:
                pass
    if "subtype" in qmap and stream is None:
        try:
            stream = _stream_from_digit(int(qmap["subtype"]))
        except ValueError:
            pass
    rest = sorted((k.lower(), v) for k, v in query if k.lower() not in _DROP_QUERY)
    out["path_key"] = p + ("?" + "&".join(f"{k}={v}" for k, v in rest) if rest else "")
    out["stream"] = stream
    out["single"] = single and (not rest or out["channel"] is not None)
    return out


def _host_port(host: str, port: Optional[int], scheme: str = "rtsp") -> str:
    h = f"[{host}]" if ":" in host else host
    return h if port in (None, DEFAULT_PORTS.get(scheme)) else f"{h}:{port}"


def identity_for(url: Optional[str], recorders: Optional[dict] = None) -> Optional[Identity]:
    """The physical-camera identity of a stored URL (see module docstring).

    ``recorders``: ``{host: entry}`` from :meth:`DuplicateGuard.recorder_maps`;
    an entry with ``channels`` maps a recorder channel to the connected
    camera's address.
    """
    src = parse_source(url)
    if src is None:
        return None
    scheme, host, port = src["scheme"], src["host"], src["port"]
    if scheme == "usb":
        key = f"usb:{src['path_key']}"
        return Identity(key=key, keys=frozenset({key}), scheme="usb", grammar="usb")
    hp = f"{host}:{port}"
    if src["grammar"] in ("dahua", "hikvision"):
        key = f"net:{hp}/ch{src['channel']}"
    else:
        key = f"{'http' if scheme in ('http', 'https') else 'net'}:{hp}{src['path_key']}"
        if src["channel"] is not None and "channel" not in src["path_key"]:
            key += f"#ch{src['channel']}"
    keys = {key}
    mapped_to = None
    if scheme in ("rtsp", "rtsps"):
        source = src["channel"]
        if source is None and src["single"]:
            source = 1
        entry = (recorders or {}).get(host) if host else None
        mapped = None
        if entry and entry.get("ok") and source is not None:
            mapped = (entry.get("channels") or {}).get(str(source))
        if mapped and mapped.get("address"):
            mapped_to = str(mapped["address"]).lower()
            keys.add(f"dev:{_host_port(mapped_to, mapped.get('rtsp_port'))}/{int(mapped.get('input') or 1)}")
        elif source is not None:
            keys.add(f"dev:{_host_port(host, port, scheme)}/{source}")
    return Identity(key=key, keys=frozenset(keys), scheme=scheme, host=host, port=port,
                    channel=src["channel"], stream=src["stream"], grammar=src["grammar"], mapped_to=mapped_to)


# ------------------------------------------------------ recorder device list

def _strip_table(key: str) -> str:
    return key[len("table."):] if key.startswith("table.") else key


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() not in ("false", "0", "no", "off")


def _port(v: Any) -> Optional[int]:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 65535 else None


def parse_remote_devices(kv: dict) -> dict[str, dict]:
    """``getConfig&name=RemoteDevice`` -> ``{device_id: {address, port, rtsp_port, enable, inputs}}``.

    Accepts ``RemoteDevice.<id>.<field>`` and ``RemoteDevice[<i>].<field>``.
    Only address, ports, enable flags and the number of video inputs are kept;
    passwords and user names in the answer are never copied. A device with no
    usable address (empty, ``0.0.0.0``) or ``Enable=false`` is left out. When
    ``Address`` is missing, the host of a ``VideoInputs[k].MainStreamUrl``
    (RTSP-protocol devices) is used.
    """
    devices: dict[str, dict] = {}
    rx = re.compile(r"^RemoteDevice(?:\[(\d+)\]|\.([^.\[]+))\.(.+)$")
    for k, v in (kv or {}).items():
        m = rx.match(_strip_table(k))
        if not m:
            continue
        dev_id = f"[{m.group(1)}]" if m.group(1) is not None else m.group(2)
        fld = m.group(3)
        d = devices.setdefault(dev_id, {"address": None, "port": None, "rtsp_port": None,
                                        "enable": True, "inputs": 0, "url_host": None})
        low = fld.lower()
        if low == "address":
            d["address"] = (v or "").strip().lower() or None
        elif low == "port":
            d["port"] = _port(v)
        elif low == "rtspport":
            d["rtsp_port"] = _port(v)
        elif low == "enable":
            d["enable"] = _truthy(v)
        elif low == "videoinputchannels":
            d["inputs"] = _port(v) or d["inputs"]
        else:
            mi = re.match(r"^VideoInputs\[(\d+)\]\.(\w+)$", fld)
            if mi:
                d["inputs"] = max(d["inputs"], int(mi.group(1)) + 1)
                if mi.group(2).lower() == "mainstreamurl" and v and not d["url_host"]:
                    try:
                        d["url_host"] = (urlsplit(v.strip()).hostname or "").lower() or None
                    except ValueError:
                        pass
    out = {}
    for dev_id, d in devices.items():
        addr = d["address"] or d["url_host"]
        if not d["enable"] or not addr or addr in ("0.0.0.0", "::"):
            continue
        out[dev_id] = {"address": addr, "port": d["port"], "rtsp_port": d["rtsp_port"],
                       "inputs": max(1, d["inputs"] or 1)}
    return out


def map_channels(devices: dict[str, dict], remote_channel: Optional[dict] = None) -> tuple[dict, str]:
    """Recorder channel (1-based, as str) -> ``{address, rtsp_port, input}``, and how it was mapped.

    1. ``RemoteChannel[i].Device`` / ``.Channel`` (0-based input) when the
       recorder answers ``getConfig&name=RemoteChannel``;
    2. else device ids ending ``INFO_<n>`` (``uuid:System_CONFIG_NETCAMERA_INFO_<n>``)
       or the array form ``RemoteDevice[<n>]``: local channel n+1, input 1.
    Devices neither method places are left unmapped.
    """
    channels: dict[str, dict] = {}
    rc: dict[int, dict] = {}
    for k, v in (remote_channel or {}).items():
        m = re.match(r"^RemoteChannel\[(\d+)\]\.(\w+)$", _strip_table(k))
        if m:
            rc.setdefault(int(m.group(1)), {})[m.group(2).lower()] = v
    if rc:
        for idx, e in sorted(rc.items()):
            if not _truthy(e.get("enable", "true")):
                continue
            dev = devices.get(str(e.get("device") or "").strip())
            if dev is None:
                continue
            try:
                inp = int(e.get("channel") or 0) + 1
            except ValueError:
                inp = 1
            channels[str(idx + 1)] = {"address": dev["address"], "rtsp_port": dev.get("rtsp_port"), "input": inp}
        if channels:
            return channels, "RemoteChannel"
    for dev_id, dev in devices.items():
        m = re.search(r"INFO_(\d+)$", dev_id) or re.match(r"^\[(\d+)\]$", dev_id)
        if m:
            channels[str(int(m.group(1)) + 1)] = {"address": dev["address"], "rtsp_port": dev.get("rtsp_port"),
                                                  "input": 1}
    return channels, "device ids"


def read_recorder_devices(client) -> dict:
    """Blocking: read the recorder's connected cameras. Raises DahuaAuthError / DahuaHttpError.

    One ``RemoteDevice`` read, then an optional ``RemoteChannel`` read (a
    recorder that does not know it answers an error, which is not fatal). A
    rejected login raises at once: the client never retries it.
    """
    from app.services import dahua_config as dc

    devices = parse_remote_devices(client.get_config("RemoteDevice"))
    try:
        rc = client.get_config("RemoteChannel")
    except dc.DahuaAuthError:
        raise
    except dc.DahuaHttpError:
        rc = {}
    channels, source = map_channels(devices, rc)
    return {"devices": len(devices), "channels": channels, "source": source,
            "unmapped": max(0, len(devices) - len({(c["address"]) for c in channels.values()}))}


# ---------------------------------------------------------------------- guard

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_s(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _pair(a: str, b: str) -> str:
    return "|".join(sorted((str(a), str(b))))


@dataclass
class _Snapshot:
    report: dict
    excluded: frozenset
    by_camera: dict = field(default_factory=dict)
    at: float = 0.0


class DuplicateGuard:
    def __init__(self, storage_dir: Optional[Path] = None):
        self._storage_dir = storage_dir
        self._lock = threading.RLock()
        self._snap: Optional[_Snapshot] = None
        self._override: Optional[list[dict]] = None
        self._refreshing: set[str] = set()
        self.client_factory = None     # tests: DahuaHttpClient stand-in
        self.http_port = 80
        self.auto_refresh = True

    # -- storage -----------------------------------------------------------------
    @property
    def storage_dir(self) -> Path:
        return Path(self._storage_dir or settings.STORAGE_DIR)

    def _read(self, name: str) -> dict:
        try:
            data = json.loads((self.storage_dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Unreadable {name}: {e}")
            return {}

    def state(self) -> dict:
        s = self._read(STATE_FILE)
        s.setdefault("dismissed", {})
        s.setdefault("primary", {})
        s.setdefault("retired", {})
        return s

    def _save_state(self, s: dict) -> None:
        _write_private_json(self.storage_dir / STATE_FILE, s)
        self.invalidate()

    def recorder_maps(self) -> dict:
        return self._read(RECORDERS_FILE)

    def _save_recorder(self, host: str, entry: dict) -> None:
        with self._lock:
            data = self.recorder_maps()
            data[host] = entry
            _write_private_json(self.storage_dir / RECORDERS_FILE, data)
        self.invalidate()

    # -- cameras -------------------------------------------------------------------
    def set_cameras_override(self, cams: Optional[list[dict]]) -> None:
        """Tests: fix the camera list (None returns to reading the database)."""
        with self._lock:
            self._override = [dict(c) for c in cams] if cams is not None else None
            self._snap = None

    def invalidate(self) -> None:
        with self._lock:
            self._snap = None

    def _load_cameras(self) -> list[dict]:
        if self._override is not None:
            return [dict(c) for c in self._override]
        try:
            with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=2.0)) as conn:
                rows = conn.execute(
                    "SELECT id, name, location, rtsp_url, is_ai_enabled, created_at FROM cameras").fetchall()
        except sqlite3.Error:
            return []
        return [{"id": str(r[0]), "name": r[1] or str(r[0]), "location": r[2] or "", "rtsp_url": r[3] or "",
                 "enabled": bool(r[4]) if r[4] is not None else True, "created_at": str(r[5] or "")}
                for r in rows]

    # -- analysis ------------------------------------------------------------------
    @staticmethod
    def _recorder_hosts(cams: list[dict], idents: dict, maps: dict) -> set:
        """Hosts that are recorders: device list read, several channels in use, a channel above 1,
        or a camera adopted from the Dahua recorder panel."""
        out = {h for h, e in maps.items() if e.get("ok") and e.get("devices")}
        chans: dict[str, set] = {}
        for c in cams:
            ident = idents.get(c["id"])
            if ident is None or not ident.host:
                continue
            if ident.grammar in ("dahua", "hikvision") and ident.channel is not None:
                chans.setdefault(ident.host, set()).add(ident.channel)
                if ident.channel > 1:
                    out.add(ident.host)
            if str(c.get("location") or "").startswith("Dahua NVR ") or str(c.get("name") or "").startswith("Dahua NVR Ch"):
                out.add(ident.host)
        out |= {h for h, s in chans.items() if len(s) > 1}
        return out

    @staticmethod
    def describe(ident: Optional[Identity], recorder_hosts: set, *, long: bool = False) -> str:
        if ident is None:
            return "no network address"
        stream = STREAM_TEXT.get(ident.stream or "")
        tail = f", {stream}" if stream else ""
        if ident.grammar == "usb":
            return "USB capture device"
        where = _host_port(ident.host or "", ident.port, ident.scheme)
        if ident.host in recorder_hosts and ident.channel is not None:
            text = f"NVR {where} channel {ident.channel}" if long else f"NVR channel {ident.channel}"
            if ident.mapped_to:
                text += f" (camera {ident.mapped_to})"
            return text + tail
        if ident.channel is not None and ident.channel > 1:
            return f"camera {where} input {ident.channel}{tail}"
        return f"camera {where}{tail}"

    def analyse(self, cams: list[dict], maps: Optional[dict] = None, state: Optional[dict] = None) -> _Snapshot:
        maps = self.recorder_maps() if maps is None else maps
        state = self.state() if state is None else state
        dismissed = state.get("dismissed") or {}
        chosen = state.get("primary") or {}
        idents = {c["id"]: identity_for(c.get("rtsp_url"), maps) for c in cams}
        rec_hosts = self._recorder_hosts(cams, idents, maps)

        # Graph: an edge where two cameras share a key and the pair was not dismissed.
        by_key: dict[str, list[str]] = {}
        for cid, ident in idents.items():
            if ident is None:
                continue
            for k in ident.keys:
                by_key.setdefault(k, []).append(cid)
        adj: dict[str, set] = {}
        for members in by_key.values():
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    if a != b and _pair(a, b) not in dismissed:
                        adj.setdefault(a, set()).add(b)
                        adj.setdefault(b, set()).add(a)
        seen: set = set()
        comps: list[list[str]] = []
        for start in sorted(adj):
            if start in seen:
                continue
            stack, comp = [start], []
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                comp.append(n)
                stack.extend(adj.get(n, ()))
            if len(comp) > 1:
                comps.append(comp)

        cam_by_id = {c["id"]: c for c in cams}
        groups, excluded, by_camera = [], set(), {}
        for comp in comps:
            def rank(cid: str):
                c, ident = cam_by_id[cid], idents[cid]
                return (0 if cid in chosen else 1,
                        0 if c.get("enabled", True) else 1,
                        0 if ident and ident.host in rec_hosts else 1,
                        _STREAM_RANK.get(ident.stream if ident else None, 2),
                        c.get("created_at") or "", cid)
            # Several operator choices in one group: the most recent wins.
            picked = [cid for cid in comp if cid in chosen]
            if len(picked) > 1:
                primary = max(picked, key=lambda cid: chosen.get(cid) or "")
            else:
                primary = min(comp, key=rank)
            ordered = [primary] + sorted((c for c in comp if c != primary), key=rank)
            gid = "dup_" + "_".join(sorted(comp))[:120]
            members = []
            for cid in ordered:
                c, ident = cam_by_id[cid], idents[cid]
                is_primary = cid == primary
                members.append({
                    "camera_id": cid, "name": c.get("name") or cid,
                    "description": self.describe(ident, rec_hosts),
                    "source": self.describe(ident, rec_hosts, long=True),
                    "stream": ident.stream if ident else None,
                    "via_recorder": bool(ident and ident.host in rec_hosts),
                    "enabled": bool(c.get("enabled", True)),
                    "created_at": c.get("created_at") or None,
                    "primary": is_primary, "primary_chosen": cid in chosen and is_primary,
                    "excluded": not is_primary,
                })
                if not is_primary:
                    excluded.add(cid)
                by_camera[cid] = {"duplicate_group": gid, "duplicate_of": None if is_primary else primary,
                                  "duplicate_primary": is_primary}
            mapped = [m for m in members if idents[m["camera_id"]] and idents[m["camera_id"]].mapped_to]
            direct = [m for m in members if not m["via_recorder"]]
            reason = "nvr_and_direct" if mapped and direct else "same_source"
            names = [f"{m['name']} ({m['description']})" for m in members]
            joined = names[0] + " and " + names[1] if len(names) == 2 else ", ".join(names[:-1]) + " and " + names[-1]
            head = "Same camera added twice" if len(names) == 2 else f"Same camera added {len(names)} times"
            message = f"{head}: {joined}."
            if reason == "nvr_and_direct":
                m0 = mapped[0]
                message += (f" The recorder reports that {m0['description'].split(' (')[0]} is the camera at "
                            f"{idents[m0['camera_id']].mapped_to}.")
            groups.append({
                "id": gid, "reason": reason, "primary_id": primary, "message": message,
                "identity": self.describe(idents[primary], rec_hosts, long=True),
                "cameras": members,
                "excluded_camera_ids": [m["camera_id"] for m in members if m["excluded"]],
            })
        groups.sort(key=lambda g: (g["cameras"][0]["name"] or "", g["id"]))

        hosts_in_use = {i.host for i in idents.values() if i and i.grammar == "dahua" and i.host in rec_hosts}
        recorders = []
        for host in sorted(hosts_in_use | set(maps)):
            e = maps.get(host) or {}
            if host not in hosts_in_use:
                continue
            recorders.append({
                "host": host, "read_at": e.get("read_at"), "ok": bool(e.get("ok")),
                "error": e.get("error"), "auth_failed": bool(e.get("auth_failed")),
                "devices": e.get("devices"), "channels_mapped": len(e.get("channels") or {}),
                "mapped_by": e.get("source"), "refreshing": host in self._refreshing,
                "note": (None if e.get("ok") else
                         "Connected-camera list not read yet: duplicates are matched by recorder address and "
                         "channel only; a camera added by its own IP is not matched to its recorder channel."
                         if not e else
                         f"Connected-camera list unreadable ({e.get('error') or 'unknown'}): duplicates are "
                         "matched by recorder address and channel only."),
            })
        ids = set(cam_by_id)
        dismissed_now = [{"camera_ids": k.split("|"), **(v if isinstance(v, dict) else {})}
                         for k, v in dismissed.items() if set(k.split("|")) <= ids]
        retired = {k: v for k, v in (state.get("retired") or {}).items() if k not in cam_by_id}
        excluded |= set(retired)
        report = {"groups": groups, "count": len(groups), "excluded_camera_ids": sorted(excluded),
                  "removed_duplicates": [{"camera_id": k, **(v if isinstance(v, dict) else {})}
                                         for k, v in sorted(retired.items())],
                  "recorders": recorders, "dismissed": dismissed_now,
                  "note": ("Setup warning, not a failure: each group's primary camera feeds store-wide "
                           "totals; the others are still analysed and viewable but excluded from store "
                           "totals until resolved.") if groups else None}
        return _Snapshot(report=report, excluded=frozenset(excluded), by_camera=by_camera, at=time.monotonic())

    def snapshot(self) -> _Snapshot:
        with self._lock:
            snap = self._snap
            if snap is not None and time.monotonic() - snap.at < CACHE_TTL_S:
                return snap
        try:
            snap = self.analyse(self._load_cameras())
        except Exception as e:  # noqa: BLE001 - never break a caller over this
            logger.warning(f"duplicate camera analysis failed: {e}")
            snap = _Snapshot(report={"groups": [], "count": 0, "excluded_camera_ids": [], "recorders": [],
                                     "dismissed": [], "error": str(e)}, excluded=frozenset(), at=time.monotonic())
        with self._lock:
            self._snap = snap
        return snap

    def report(self) -> dict:
        return json.loads(json.dumps(self.snapshot().report))

    def excluded_ids(self) -> frozenset:
        """Cameras excluded from store-wide totals (non-primary members of duplicate groups)."""
        return self.snapshot().excluded

    def camera_fields(self, camera_id: str) -> dict:
        return dict(self.snapshot().by_camera.get(camera_id)
                    or {"duplicate_group": None, "duplicate_of": None, "duplicate_primary": False})

    # -- prevention ------------------------------------------------------------------
    def conflicts(self, url: str, *, exclude_id: Optional[str] = None,
                  extra: Iterable[dict] = ()) -> list[dict]:
        """Configured cameras (and ``extra`` pending ones) that are the same camera as ``url``."""
        maps = self.recorder_maps()
        cand = identity_for(url, maps)
        if cand is None:
            return []
        cams = [c for c in self._load_cameras() if c["id"] != exclude_id] + [dict(c) for c in extra]
        idents = {c["id"]: identity_for(c.get("rtsp_url"), maps) for c in cams}
        rec_hosts = self._recorder_hosts(cams + [{"id": "__new__", "rtsp_url": url}],
                                         {**idents, "__new__": cand}, maps)
        new_desc = self.describe(cand, rec_hosts, long=True)
        out = []
        for c in cams:
            ident = idents.get(c["id"])
            if ident is not None and ident.keys & cand.keys:
                out.append({"camera_id": c["id"], "name": c.get("name") or c["id"],
                            "description": self.describe(ident, rec_hosts),
                            "source": self.describe(ident, rec_hosts, long=True),
                            "via_recorder": ident.host in rec_hosts, "new_description": new_desc})
        return out

    def conflict_detail(self, url: str, found: list[dict]) -> dict:
        """409 body: names the existing camera and suggests the NVR-channel form."""
        first = found[0]
        new_desc = first.get("new_description")
        msg = (f"This is the same camera as \"{first['name']}\" ({first['source']}), which is already added"
               + (f" (and {len(found) - 1} more)" if len(found) > 1 else "") + ". ")
        if any(c.get("via_recorder") for c in found):
            msg += ("Use the existing recorder channel: connecting cameras as NVR channels keeps one login "
                    "and one place to manage streams. ")
        else:
            msg += "Use the existing camera. "
        msg += ("Add it anyway only for a genuine second stream (e.g. a main-stream close-up); the copy is "
                "then excluded from store totals so nobody is counted twice.")
        return {"code": "duplicate_camera", "message": msg, "existing": found, "new_description": new_desc,
                "hint": "Send allow_duplicate: true to add it anyway."}

    # -- operator actions --------------------------------------------------------------
    def dismiss(self, camera_ids: list[str], actor: str = "") -> None:
        ids = list(dict.fromkeys(str(c) for c in camera_ids))
        with self._lock:
            s = self.state()
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    s["dismissed"][_pair(a, b)] = {"at": _now_iso(), "by": actor}
            self._save_state(s)

    def undismiss(self, camera_ids: list[str]) -> None:
        ids = list(dict.fromkeys(str(c) for c in camera_ids))
        with self._lock:
            s = self.state()
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    s["dismissed"].pop(_pair(a, b), None)
            self._save_state(s)

    def set_primary(self, camera_id: str, group_ids: list[str]) -> None:
        with self._lock:
            s = self.state()
            for cid in group_ids:
                s["primary"].pop(cid, None)
            s["primary"][camera_id] = _now_iso()
            self._save_state(s)

    def forget_camera(self, camera_id: str) -> None:
        """Call before deleting a camera: drop its choices and dismissals.

        A camera removed while it was an excluded duplicate stays excluded
        from store totals (``retired``): its history rows describe the same
        people as its primary's, and would otherwise be counted again in past
        totals once the camera row is gone.
        """
        dup_of = self.snapshot().by_camera.get(camera_id, {}).get("duplicate_of")
        with self._lock:
            s = self.state()
            changed = s["primary"].pop(camera_id, None) is not None
            if dup_of:
                s.setdefault("retired", {})[camera_id] = {"duplicate_of": dup_of, "at": _now_iso()}
                changed = True
            for k in [k for k in s["dismissed"] if camera_id in k.split("|")]:
                s["dismissed"].pop(k, None)
                changed = True
            if changed:
                self._save_state(s)
            else:
                self.invalidate()

    # -- recorder device lists ------------------------------------------------------------
    async def refresh_recorder(self, host: str, cams: list[dict], *, http_port: Optional[int] = None) -> dict:
        """Read one recorder's connected-camera list and cache it. Never retries a rejected login."""
        from app.services import dahua_config as dc
        from app.services.recorder_substreams import recorder_credentials, recorder_substreams

        if host in self._refreshing:
            return {"host": host, "ok": False, "error": "already being read"}
        if recorder_substreams.is_running(host):
            return {"host": host, "ok": False, "error": "a sub-stream change is running on this recorder"}
        user, pw, _ = recorder_credentials(host, cams)
        entry: dict = {"read_at": _now_iso(), "ok": False, "channels": {}, "devices": None}
        if not pw:
            entry["error"] = "no sign-in saved for this recorder"
            self._save_recorder(host, entry)
            return {"host": host, **entry}
        self._refreshing.add(host)
        factory = self.client_factory or dc.DahuaHttpClient
        client = factory(host, user, pw, port=http_port or self.http_port)
        try:
            data = await asyncio.to_thread(read_recorder_devices, client)
            entry.update(ok=True, error=None, **data)
        except dc.DahuaAuthError as e:
            entry.update(error=f"{e}; not retried, to avoid locking the account", auth_failed=True)
        except dc.DahuaHttpError as e:
            entry.update(error=str(e))
        except Exception as e:  # noqa: BLE001
            entry.update(error=f"{type(e).__name__}")
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            self._refreshing.discard(host)
        self._save_recorder(host, entry)
        logger.info(f"Recorder {host} connected-camera list: "
                    f"{'%d channel(s) mapped' % len(entry['channels']) if entry['ok'] else entry.get('error')}")
        return {"host": host, **entry}

    def stale_recorders(self) -> list[str]:
        maps = self.recorder_maps()
        hosts = [r["host"] for r in self.snapshot().report.get("recorders", [])]
        out = []
        for h in hosts:
            age = _age_s((maps.get(h) or {}).get("read_at"))
            if (age is None or age > RECORDER_REFRESH_S) and h not in self._refreshing:
                out.append(h)
        return out

    def schedule_stale_refresh(self, cams_by_host: dict[str, list[dict]]) -> list[str]:
        """Daily: read the device list of recorders never read or read over 24 h ago (in the background).

        A failed read (including a rejected login) waits the same 24 h, so a
        wrong saved password costs at most one failed login a day.
        """
        if not self.auto_refresh:
            return []
        hosts = [h for h in self.stale_recorders() if h in cams_by_host]
        if not hosts:
            return []

        async def run():
            for h in hosts:
                try:
                    await self.refresh_recorder(h, cams_by_host[h])
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"recorder device list {h}: {e}")

        try:
            asyncio.get_running_loop().create_task(run(), name="recorder-device-lists")
        except RuntimeError:
            return []
        return hosts


duplicate_guard = DuplicateGuard()


def excluded_camera_ids() -> list[str]:
    """Store-total exclusion list for SQL ``notin_`` filters (never raises)."""
    try:
        return sorted(duplicate_guard.excluded_ids())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"duplicate exclusion unavailable: {e}")
        return []


# ------------------------------------------------------------- delete impact

async def delete_impact(db, camera_id: str) -> dict:
    """What removing a camera deletes and what it keeps (read-only, counts).

    Describes the existing delete (``DELETE /api/v1/cameras/{id}`` and
    ``DELETE /api/v1/layout/cameras/{id}``), which is not changed: the camera
    row goes (settings, map placement, calibration, role), with the rows the
    ORM cascades from it (security events, DVR segments, incident archives)
    and its stored sign-in. Observations keyed only by ``camera_id`` stay, as
    do its overlays in the JSON stores (they no longer apply to anything).
    """
    from sqlalchemy import func, select

    from app.models import db_models as m
    from app.services import camera_source

    cam = await db.get(m.CameraModel, camera_id)
    if cam is None:
        return {}

    async def count(model, col=None) -> int:
        col = col if col is not None else model.camera_id
        return int(await db.scalar(select(func.count()).select_from(model).where(col == camera_id)) or 0)

    overlays = {"tripwires": 0, "intrusion_zones": 0, "exclusion_masks": 0, "queue_zones": 0}
    try:
        from app.services.ai_zone_service import ai_zone_service

        zones = ai_zone_service.get_all_zones(camera_id)
        overlays = {k: len(zones.get(k) or []) for k in overlays}
    except Exception:  # noqa: BLE001
        pass
    shelf_zones = 0
    try:
        from app.services.shelf_interaction_service import shelf_interaction_service

        shelf_zones = len(shelf_interaction_service.get_zones(camera_id))
    except Exception:  # noqa: BLE001
        pass
    has_creds = False
    try:
        has_creds = camera_source.load_credentials(camera_id) is not None
    except Exception:  # noqa: BLE001
        pass
    evidence = int(await db.scalar(select(func.count()).select_from(m.TheftIncidentModel).where(
        m.TheftIncidentModel.camera_id == camera_id,
        (m.TheftIncidentModel.snapshot_path.isnot(None)) | (m.TheftIncidentModel.clip_path.isnot(None)))) or 0)

    deleted = [
        {"what": "camera settings, role, analytics switches", "count": 1},
        {"what": "placement on the store map", "count": 1 if (cam.floor_x or cam.floor_y) else 0},
        {"what": "calibration (image-to-floor mapping)", "count": 1 if cam.homography_matrix else 0},
        {"what": "saved camera sign-in", "count": 1 if has_creds else 0},
        {"what": "alert log entries (security events)", "count": await count(m.SecurityEventModel)},
        {"what": "legacy recording segments", "count": await count(m.DVRSegmentModel)},
        {"what": "incident archives", "count": await count(m.IncidentArchiveModel)},
    ]
    kept = [
        {"what": "theft incidents", "count": await count(m.TheftIncidentModel)},
        {"what": "incidents with evidence files (image / clip stay on disk)", "count": evidence},
        {"what": "counting-line crossings (footfall history)", "count": await count(m.TripwireEventModel)},
        {"what": "area visits (dwell history)", "count": await count(m.ZoneVisitModel)},
        {"what": "queue / checkout visits", "count": await count(m.QueueVisitModel)},
        {"what": "tracked paths", "count": await count(m.CustomerTrackModel)},
        {"what": "shelf reaches", "count": await count(m.ShelfInteractionModel)},
        {"what": "recorded camera heatmaps", "count": await count(m.HeatmapSnapshotModel)},
        {"what": "counting lines drawn on it", "count": overlays["tripwires"]},
        {"what": "restricted areas", "count": overlays["intrusion_zones"]},
        {"what": "privacy masks / ignore areas", "count": overlays["exclusion_masks"]},
        {"what": "checkout / queue areas", "count": overlays["queue_zones"]},
        {"what": "product shelf areas", "count": shelf_zones},
    ]
    return {
        "camera_id": camera_id, "name": cam.name,
        "deleted": deleted, "kept": kept,
        "note": ("Removing deletes the camera and the rows listed as deleted. History stays and still counts in "
                 "past totals; the camera's drawn lines and areas stay stored but no longer apply to anything."),
    }
