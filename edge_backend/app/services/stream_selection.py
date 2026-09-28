"""Which stream of a recorder channel each camera is analysed from.

A Dahua recorder channel offers a main stream (``subtype=0``) and one or two
sub-streams (``subtype=1``, ``subtype=2``). Person and pose detection need
roughly D1 (704x576) to see far shoppers; a main stream of 2560x1440 or more
costs several times the decode for no detection gain. So each camera has a
"Stream quality" setting (``features.stream_quality``):

* ``auto`` (default, also ``None``): the smallest sub-stream of at least D1
  among ``subtype=1`` and ``subtype=2``. When neither reaches D1, the
  camera's current sub-stream is kept. The main stream is never picked
  automatically. A camera that was added on its main stream stays on it.
* ``sub``: always ``subtype=1``.
* ``main``: always ``subtype=0``.

Choosing needs each sub-stream's real size. It is measured once per camera
and stream (one frame, read-only, in a child process) by a background pass
that runs a while after start-up, one camera at a time, and remembered in
``STORAGE_DIR/stream_selection.json`` keyed by the camera's stored URL, so a
changed URL is measured again; the operator can re-measure from the camera's
Settings. Until a camera is measured it stays on its configured stream.

:meth:`StreamSelection.decide` is cheap and never touches the network: the
pipeline supervisor calls it on every reconcile to get the URL a worker
opens. Non-Dahua URLs are opened exactly as configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.config import settings
from app.services.camera_drivers import redact_url

logger = logging.getLogger(__name__)

QUALITIES = ("auto", "sub", "main")
STREAM_NAMES = {0: "Main stream", 1: "Sub-stream", 2: "Sub-stream 2"}
_DAHUA_RE = re.compile(r"/cam/realmonitor\?(?:[^#]*&)?channel=(\d+)", re.IGNORECASE)
_SUBTYPE_RE = re.compile(r"([?&]subtype=)(\d+)", re.IGNORECASE)
# A failed measurement is tried again after this long (the recorder may have
# been down); a successful one is kept until the URL changes or on request.
PROBE_ERROR_RETRY_S = 24 * 3600.0
PROBE_TIMEOUT_S = 8.0
PROBE_GAP_S = 2.0


def parse_dahua(url: Optional[str]) -> Optional[tuple[int, int]]:
    """(channel, subtype) of a Dahua ``/cam/realmonitor`` URL, else None."""
    if not url or not url.lower().startswith(("rtsp://", "rtsps://")):
        return None
    m = _DAHUA_RE.search(url)
    if not m:
        return None
    s = _SUBTYPE_RE.search(url)
    return int(m.group(1)), (int(s.group(2)) if s else 0)


def with_subtype(url: str, subtype: int) -> str:
    if _SUBTYPE_RE.search(url):
        return _SUBTYPE_RE.sub(lambda m: f"{m.group(1)}{int(subtype)}", url, count=1)
    return f"{url}&subtype={int(subtype)}"


def is_d1_or_higher(w: Optional[int], h: Optional[int]) -> bool:
    return bool(w and h) and w >= 704 and w * h >= 704 * 480


def size_text(w: Optional[int], h: Optional[int]) -> str:
    if not (w and h):
        return "size not measured"
    name = {(352, 288): "CIF", (352, 240): "CIF", (704, 576): "D1", (704, 480): "D1"}.get((w, h))
    return f"{w}×{h}" + (f" ({name})" if name == "D1" else "")


def _base_key(stored_url: str) -> str:
    """Identity of the camera's source, independent of the subtype chosen."""
    norm = _SUBTYPE_RE.sub(lambda m: f"{m.group(1)}x", redact_url(stored_url or ""))
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:16]


def normalise_quality(value: Any) -> str:
    v = str(value or "auto").strip().lower()
    return v if v in QUALITIES else "auto"


@dataclass
class Decision:
    camera_id: str
    mode: str                     # auto | sub | main
    applies: bool                 # a Dahua channel URL the setting can act on
    subtype: Optional[int]
    stream: str                   # "Sub-stream", "Main stream", "As configured"
    width: Optional[int]
    height: Optional[int]
    label: str                    # e.g. "Sub-stream 704×576 (D1)"
    reason: str
    state: str                    # chosen | pending | fixed | not_applicable
    url: str = ""                 # credential-free URL to open (not serialised)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("url", None)
        return d


ProbeFn = Callable[[str], Awaitable[dict]]


async def _default_probe(open_target: str) -> dict:
    from app.services.camera_source import probe_stream_isolated

    return await probe_stream_isolated(open_target, "rtsp", PROBE_TIMEOUT_S)


class StreamSelection:
    FILE_NAME = "stream_selection.json"

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._decisions: dict[str, Decision] = {}
        self.probe_fn: ProbeFn = _default_probe
        self._task: Optional[asyncio.Task] = None
        self._queue: dict[str, tuple[str, bool]] = {}   # camera -> (stored url, force)
        self._first_pass_done = False
        self.initial_delay_s: Optional[float] = None    # None = settings value
        self.gap_s = PROBE_GAP_S
        self.probing: Optional[str] = None
        # Share sizes with the decoder's cache (capture_backends.stream_sizes):
        # a sub-stream the live worker already measured needs no probe.
        self.decoder_cache = True

    # -- persistence -----------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path or (Path(settings.STORAGE_DIR) / self.FILE_NAME)

    def _load(self) -> dict:
        if self._data is None:
            data: dict = {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                logger.warning(f"Ignoring unreadable {self.path.name}: {e}")
            self._data = data
        return self._data

    def _save_locked(self) -> None:
        path = self.path
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data or {}, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"Could not save {path.name}: {e}")

    def reset(self) -> None:
        with self._lock:
            self._data = None
            self._decisions.clear()
            self._queue.clear()
            self._first_pass_done = False

    def _entry(self, camera_id: str, stored_url: str) -> dict:
        """The camera's measurements for this URL (dropped when the URL changed)."""
        data = self._load()
        entry = data.get(camera_id)
        key = _base_key(stored_url)
        if not isinstance(entry, dict) or entry.get("base") != key:
            entry = {"base": key, "streams": {}}
            data[camera_id] = entry
        entry.setdefault("streams", {})
        return entry

    def measured(self, camera_id: str, stored_url: str, subtype: int) -> Optional[dict]:
        with self._lock:
            data = self._load()
            entry = data.get(camera_id)
            if not isinstance(entry, dict) or entry.get("base") != _base_key(stored_url):
                return None
            got = (entry.get("streams") or {}).get(str(int(subtype)))
            return dict(got) if isinstance(got, dict) else None

    def record(self, camera_id: str, stored_url: str, subtype: int, *, width: Optional[int] = None,
               height: Optional[int] = None, error: Optional[str] = None, source: str = "probe") -> None:
        """Remember one stream's measured size (or why it could not be measured)."""
        item: dict[str, Any] = {"at": round(time.time(), 1), "source": source}
        if width and height:
            item.update(w=int(width), h=int(height))
        else:
            item["error"] = (error or "no picture")[:200]
        with self._lock:
            self._entry(camera_id, stored_url)["streams"][str(int(subtype))] = item
            self._save_locked()

    def forget(self, camera_id: str, subtypes: Optional[tuple[int, ...]] = None) -> None:
        with self._lock:
            data = self._load()
            entry = data.get(camera_id)
            if not isinstance(entry, dict):
                return
            if subtypes is None:
                data.pop(camera_id, None)
            else:
                for st in subtypes:
                    (entry.get("streams") or {}).pop(str(int(st)), None)
            self._save_locked()

    # -- decision (no I/O besides the small JSON) --------------------------------
    def decide(self, camera_id: str, stored_url: str, quality: Any = None) -> Decision:
        mode = normalise_quality(quality)
        parsed = parse_dahua(stored_url)
        if parsed is None:
            d = Decision(camera_id, mode, False, None, "As configured", None, None,
                         "Stream as configured",
                         "Stream quality applies to Dahua recorder channels; this camera's address is opened as entered.",
                         "not_applicable", stored_url)
            return self._keep(d)
        _, stored_sub = parsed

        def size_of(st: int) -> tuple[Optional[int], Optional[int], Optional[str]]:
            m = self.measured(camera_id, stored_url, st)
            if not m:
                return None, None, None
            return m.get("w"), m.get("h"), m.get("error")

        def make(subtype: int, reason: str, state: str, suffix: Optional[str] = None) -> Decision:
            w, h, _ = size_of(subtype)
            name = STREAM_NAMES.get(subtype, f"Stream {subtype}")
            if w and h:
                label = f"{name} {size_text(w, h)}"
                if suffix:
                    label += f" — {suffix}"
            else:
                label = name
            return Decision(camera_id, mode, True, subtype, name, w, h, label, reason, state,
                            with_subtype(stored_url, subtype))

        if mode == "main":
            return self._keep(make(0, "Set to Main stream in this camera's settings.", "fixed"))
        if mode == "sub":
            return self._keep(make(1, "Set to Sub-stream in this camera's settings.", "fixed"))
        if stored_sub == 0:
            return self._keep(make(0, "This camera was added on its main stream; Auto only chooses between "
                                      "sub-streams. Choose Sub-stream to analyse a smaller stream.", "fixed"))

        s1, s2 = size_of(1), size_of(2)
        known = {1: s1, 2: s2}
        candidates = [(w * h, st, w, h) for st, (w, h, err) in known.items()
                      if not err and is_d1_or_higher(w, h)]
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1] != stored_sub, c[1]))
            _, st, w, h = candidates[0]
            other = 2 if st == 1 else 1
            ow, oh, oerr = known[other]
            if st == 2 and ow and oh and not is_d1_or_higher(ow, oh):
                why = f"Sub-stream is only {size_text(ow, oh)}; sub-stream 2 is {size_text(w, h)}."
            elif ow and oh and is_d1_or_higher(ow, oh):
                why = f"Smallest stream of at least D1 ({STREAM_NAMES[other].lower()} is {size_text(ow, oh)})."
            else:
                why = "Smallest stream of at least D1."
            return self._keep(make(st, "Auto: " + why, "chosen"))

        # Nothing of D1 or more known (yet).
        keep_sub = stored_sub if stored_sub in (1, 2) else 1
        w1, h1, e1 = known[keep_sub]
        other = 2 if keep_sub == 1 else 1
        _, _, eo = known[other]
        measured_all = self.measured(camera_id, stored_url, keep_sub) is not None and \
            self.measured(camera_id, stored_url, other) is not None
        if measured_all:
            if eo:
                other_txt = f"{STREAM_NAMES[other].lower()} not offered ({eo})"
            else:
                wo, ho, _ = known[other]
                other_txt = f"{STREAM_NAMES[other].lower()} is {size_text(wo, ho)}"
            return self._keep(make(keep_sub, f"Auto: D1 not available ({other_txt}); keeping the current "
                                             "sub-stream. The main stream is never chosen automatically.",
                                   "chosen", suffix="D1 not available"))
        return self._keep(make(keep_sub, "Auto: checking which sub-streams this camera offers; "
                                         "staying on the configured stream until then.", "pending"))

    def _keep(self, d: Decision) -> Decision:
        with self._lock:
            self._decisions[d.camera_id] = d
        return d

    def status(self, camera_id: str) -> Optional[dict]:
        with self._lock:
            d = self._decisions.get(camera_id)
        if d is None:
            return None
        out = d.to_dict()
        out["measuring"] = self.probing == camera_id
        return out

    # -- measuring (read-only) ----------------------------------------------------
    def _needs_probe(self, camera_id: str, stored_url: str, quality: Any) -> list[int]:
        if normalise_quality(quality) != "auto":
            return []
        parsed = parse_dahua(stored_url)
        if parsed is None or parsed[1] == 0:
            return []
        need = []
        for st in (1, 2):
            m = self.measured(camera_id, stored_url, st)
            if m is None or (m.get("error") and time.time() - float(m.get("at") or 0) > PROBE_ERROR_RETRY_S):
                need.append(st)
        return need

    async def measure_camera(self, camera_id: str, stored_url: str, *, subtypes: tuple[int, ...] = (1, 2),
                             open_target_for: Optional[Callable[[str], str]] = None) -> dict:
        """Measure the camera's sub-streams (one frame each). Read-only on the camera."""
        if parse_dahua(stored_url) is None:
            return {"camera_id": camera_id, "measured": {}, "error": "not a Dahua recorder channel"}
        if open_target_for is None:
            from app.services.camera_source import stream_source_for

            def open_target_for(u: str) -> str:
                return stream_source_for(camera_id, u)

        results: dict[str, Any] = {}
        self.probing = camera_id
        try:
            for st in subtypes:
                url = with_subtype(stored_url, st)
                target = open_target_for(url)
                if st == 1 and self.decoder_cache:
                    cached = _size_from_decoder_cache(camera_id, target)
                    if cached:
                        self.record(camera_id, stored_url, st, width=cached[0], height=cached[1], source="stream")
                        results[str(st)] = {"w": cached[0], "h": cached[1], "source": "stream"}
                        continue
                try:
                    res = await self.probe_fn(target)
                except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
                    res = {"success": False, "error": f"{type(exc).__name__}"}
                if res.get("success") and res.get("width") and res.get("height"):
                    self.record(camera_id, stored_url, st, width=int(res["width"]), height=int(res["height"]))
                    results[str(st)] = {"w": int(res["width"]), "h": int(res["height"]), "source": "probe"}
                    if self.decoder_cache:
                        _remember_decoder_size(camera_id, target, int(res["width"]), int(res["height"]))
                else:
                    err = str(res.get("error_code") or res.get("error") or "no picture")
                    self.record(camera_id, stored_url, st, error=err)
                    results[str(st)] = {"error": err}
                if len(subtypes) > 1 and st != subtypes[-1] and self.gap_s:
                    await asyncio.sleep(self.gap_s)
        finally:
            self.probing = None
        return {"camera_id": camera_id, "measured": results}

    def schedule(self, cameras: list[tuple[str, str, Any]],
                 on_change: Optional[Callable[[], Awaitable[None]]] = None, *, force: bool = False) -> int:
        """Queue measurements for cameras on Auto that lack them; runs in the background.

        ``cameras``: (camera id, stored URL, stream_quality). Returns how many
        cameras were queued. The first pass after start-up waits
        STREAM_AUTO_SELECT_DELAY_S so it does not compete with every worker's
        first connection.
        """
        if not settings.STREAM_AUTO_SELECT and not force:
            return 0
        queued = 0
        for cam_id, url, quality in cameras:
            if force or self._needs_probe(cam_id, url, quality):
                if parse_dahua(url) is None:
                    continue
                self._queue[cam_id] = (url, force)
                queued += 1
        if queued and (self._task is None or self._task.done()):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return queued
            self._task = loop.create_task(self._run_queue(on_change), name="stream-selection-probe")
        return queued

    async def _run_queue(self, on_change) -> None:
        if not self._first_pass_done:
            delay = self.initial_delay_s if self.initial_delay_s is not None else settings.STREAM_AUTO_SELECT_DELAY_S
            if delay > 0:
                await asyncio.sleep(delay)
            self._first_pass_done = True
        changed = False
        while self._queue:
            cam_id, (url, force) = next(iter(self._queue.items()))
            self._queue.pop(cam_id, None)
            before = self._decisions.get(cam_id)
            try:
                subtypes = (1, 2) if force else tuple(self._needs_probe(cam_id, url, "auto")) or ()
                if subtypes:
                    await self.measure_camera(cam_id, url, subtypes=subtypes)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Stream measurement for {cam_id} failed: {type(exc).__name__}: {exc}")
            after = self.decide(cam_id, url, _quality_of(cam_id))
            if before is None or before.subtype != after.subtype:
                changed = True
            if self._queue and self.gap_s:
                await asyncio.sleep(self.gap_s)
        if changed and on_change is not None:
            try:
                await on_change()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"stream selection reconcile: {exc}")

    async def wait_idle(self, timeout: float = 30.0) -> None:
        if self._task is not None and not self._task.done():
            await asyncio.wait_for(asyncio.shield(self._task), timeout)


def _quality_of(camera_id: str) -> Any:
    try:
        from app.services.feature_manager import feature_manager

        return feature_manager.get_setting(camera_id, "stream_quality")
    except Exception:  # noqa: BLE001
        return None


def _size_from_decoder_cache(camera_id: str, target: str) -> Optional[tuple[int, int]]:
    """The size the live worker measured for this exact stream, if any."""
    try:
        from app.services.capture_backends import stream_sizes

        return stream_sizes.get(camera_id, target)
    except Exception:  # noqa: BLE001
        return None


def _remember_decoder_size(camera_id: str, target: str, w: int, h: int) -> None:
    try:
        from app.services.capture_backends import stream_sizes

        stream_sizes.put(camera_id, target, w, h)
    except Exception:  # noqa: BLE001
        pass


stream_selection = StreamSelection()
