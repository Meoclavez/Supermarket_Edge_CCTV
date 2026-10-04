"""Privacy masks burned into direct (WebRTC) live video, fail closed.

The analysis pipeline burns a camera's privacy masks (services/privacy_mask.py:
BLUR, MOSAIC, BLACKOUT, COLOR; an unknown mode counts as BLACKOUT) into every
picture it shows or saves. Direct live video does not pass through that
pipeline: go2rtc pulls the camera's RTSP stream itself. So for a camera with
at least one privacy mask the go2rtc source of every session is an ffmpeg
pipeline that burns the masks in before encoding:

    camera ─decode─> scale to WxH ─split─┬──────────────────────────────────────────── overlay ... ─> H.264
                                         ├─ crop box, 1/4 size, box blur, back, alpha ─┘   (BLUR)
                                         └─ crop box, 1/S size, nearest back, alpha ───┘   (MOSAIC)
                                                    box-sized RGBA image ─────────────┘   (BLACKOUT, COLOR)

* Each polygon (normalised 0..1, or legacy pixels) is rendered into a PNG
  the size of its bounding box, at the camera's frame size
  (``frame_geometry.frame_sizes.native``, learned by the camera worker):
  RGBA for BLACKOUT (black) and COLOR (the mask colour), grey (255 inside
  the polygon) as the alpha of a BLUR or MOSAIC region. Only the boxes are
  filtered, so the cost follows the masked area, not the frame.
  Files are content addressed (``<camera>_<signature>_<n>_<kind>.png`` under
  ``STORAGE_DIR/go2rtc/masks``, 0600), so a mask edit gives new files and a
  new signature. The video is scaled to that size (a no-op when it already
  has it), so the masks always cover the same part of the picture.
* The filter step runs on the CPU. The encoder is probed at runtime the way
  the plain transcode is (go2rtc_manager.detect_transcode_engine: NVENC, then
  VA-API, else libx264; ``WEBRTC_TRANSCODE_ENCODER`` forces one): VA-API gets
  ``format=nv12,hwupload`` on a device created up front, NVENC takes system
  frames. Before the source is handed to go2rtc the exact argument list is
  run once on a synthetic input (cached); if the chosen engine fails, libx264
  is tried; if that fails too, the session is refused.
* go2rtc refuses an API-added source with whitespace ("may be insecure"), so
  every ffmpeg argument is its own ``#raw=<token>``; the graph contains no
  spaces, quotes or ``#`` (see :func:`go2rtc_source` for what go2rtc builds
  from it). go2rtc's ``#hardware`` is not used: it would add a ``-vf`` and
  move decoded frames to the GPU before the CPU filter.

Fail closed: if the zone store cannot be read, the frame size is unknown,
ffmpeg is missing, an image cannot be written or the probe fails, the session
is refused with :data:`REFUSAL` and the reason. The raw stream is never used
for a camera that has privacy masks. ``AI_IGNORE`` masks are analysis
exclusions and do not change the picture.

Mask changes: ai_zone_service calls :func:`notify_masks_changed`; the session
registry compares each session's mask signature with the current one and ends
(and cuts) sessions whose masks changed. The reaper does the same comparison
every few seconds as a backstop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from app.config import settings
from app.services.privacy_mask import IGNORE_MODE, PRIVACY_MODES, polygon_pixels

logger = logging.getLogger("edge.privacy_video")

REFUSAL = "Hidden: this camera has privacy masks and the masked live view could not be started"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9/._-]+$")
_FORBIDDEN_IN_TOKEN = re.compile(r"[\s#\"'`]")
# Blur: the region is shrunk to 1/4, box-blurred and scaled back (the same
# shape as privacy_mask._strong_blur). Mosaic: blocks of S pixels.
BLUR_DOWNSCALE = 4
DEFAULT_BLUR_KERNEL = 51
DEFAULT_MOSAIC_SCALE = 16
PROBE_TIMEOUT_S = 20.0

ENCODERS: dict[str, list[str]] = {
    # go2rtc's own defaults for these encoders (internal/ffmpeg), so browsers get what they already play.
    # NVENC's "-level:v auto" is not used: go2rtc 1.9.14 treats any argument equal to "auto" as
    # "use the native producer" and then rejects the source ("ffmpeg: unsupported params").
    "software": ["-c:v", "libx264", "-g", "50", "-profile:v", "high", "-level:v", "4.1", "-preset:v", "superfast",
                 "-tune:v", "zerolatency", "-pix_fmt:v", "yuv420p"],
    "vaapi": ["-c:v", "h264_vaapi", "-g", "50", "-bf", "0", "-profile:v", "high", "-level:v", "4.1", "-sei:v", "0"],
    "cuda": ["-c:v", "h264_nvenc", "-g", "50", "-bf", "0", "-profile:v", "high", "-level:v", "4.1",
             "-preset:v", "p2", "-tune:v", "ll"],
}
ENCODER_NAMES = {"software": "libx264", "vaapi": "h264_vaapi", "cuda": "h264_nvenc"}
VAAPI_DEVICE_NAME = "edgeva"


class MaskedVideoUnavailable(RuntimeError):
    """The masked pipeline cannot be built; the message says why (shown after REFUSAL)."""


# --------------------------------------------------------------------------- masks

def _mode(mask: dict) -> str:
    return str(mask.get("mask_mode") or "BLUR").upper()


def is_privacy(mask: dict) -> bool:
    return _mode(mask) != IGNORE_MODE


def privacy_masks(camera_id: str) -> list[dict]:
    """Enabled privacy masks of a camera. Raises MaskedVideoUnavailable if the zone store cannot be read.

    Same selection as privacy_mask.camera_masks (enabled, at least 3 points),
    but strict: that one treats an unreadable store as "no masks", which here
    would mean raw video.
    """
    try:
        from app.services.ai_zone_service import ai_zone_service

        masks = ai_zone_service.get_all_zones(camera_id).get("exclusion_masks", [])
    except Exception as exc:  # noqa: BLE001
        raise MaskedVideoUnavailable(f"the privacy masks could not be read ({exc.__class__.__name__})") from None
    return [m for m in masks
            if m.get("enabled", True) and len(m.get("points") or []) >= 3 and is_privacy(m)]


def _canonical(mask: dict) -> dict:
    mode = _mode(mask)
    if mode not in PRIVACY_MODES:
        mode = "BLACKOUT"
    out: dict[str, Any] = {"mode": mode,
                           "points": [[round(float(p["x"]), 5), round(float(p["y"]), 5)] for p in mask["points"]]}
    if mode == "COLOR":
        out["colour"] = [int(c) for c in (mask.get("mask_color_bgr") or (0, 0, 0))]
    if mode == "BLUR":
        out["kernel"] = int(mask.get("blur_kernel_size") or DEFAULT_BLUR_KERNEL)
    if mode == "MOSAIC":
        out["scale"] = int(mask.get("mosaic_scale") or DEFAULT_MOSAIC_SCALE)
    if any(max(abs(float(p["x"])), abs(float(p["y"]))) > 1.0 for p in mask["points"]):
        out["drawn"] = [mask.get("frame_width"), mask.get("frame_height")]
    return out


def signature(masks: list[dict], size: Optional[tuple[int, int]]) -> str:
    """What the picture depends on: modes, polygons, colours, strengths and the frame size ("" = no masks)."""
    if not masks:
        return ""
    try:
        canon = sorted(json.dumps(_canonical(m), sort_keys=True) for m in masks)
    except (KeyError, TypeError, ValueError):
        canon = ["unparseable:" + json.dumps(masks, sort_keys=True, default=str)]
    blob = json.dumps({"masks": canon, "size": list(size) if size else None}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def current_signature(camera_id: str, size: Optional[tuple[int, int]] = None) -> str:
    """The camera's mask signature now, at ``size`` (default: its native size).

    "" when it has no privacy masks; "unreadable" (never equal to a real
    signature) when the zone store cannot be read, so a session is then ended.
    """
    try:
        masks = privacy_masks(camera_id)
    except MaskedVideoUnavailable:
        return "unreadable"
    if not masks:
        return ""
    if not size:
        from app.services.frame_geometry import frame_sizes

        size = frame_sizes.native(camera_id)
    return signature(masks, size)


# --------------------------------------------------------------------------- plan + images

@dataclass
class MaskItem:
    """One privacy mask: its kind, its bounding box in the frame (even-aligned) and its strength."""
    kind: str                      # fill | blur | mosaic
    x: int
    y: int
    w: int
    h: int
    strength: int = 0              # blur: kernel (privacy_mask semantics); mosaic: block size in pixels
    image: Optional[np.ndarray] = None  # fill: BGRA; blur / mosaic: single channel 0/255 (bbox sized)

    @property
    def small_size(self) -> tuple[int, int]:
        """Blur: the region is shrunk to 1/4 (like privacy_mask._strong_blur); mosaic: one pixel per block."""
        if self.kind == "mosaic":
            s = max(2, self.strength)
            return max(1, self.w // s), max(1, self.h // s)
        return max(2, self.w // BLUR_DOWNSCALE), max(2, self.h // BLUR_DOWNSCALE)

    @property
    def blur_radii(self) -> Optional[tuple[int, int]]:
        """(luma, chroma) box radius on the 1/4-size copy, within boxblur's plane limits (None: too small to need it)."""
        sw, sh = self.small_size
        luma_max = min(sw, sh) // 2 - 1
        chroma_max = min(sw // 2, sh // 2) // 2 - 1
        if luma_max < 1 or chroma_max < 1:
            return None   # a 1/4 copy this small is already featureless when scaled back
        # Gaussian k/4 on the 1/4 copy (privacy_mask); a power-2 box of radius ~k/8 is at least as strong.
        r = max(2, int(self.strength or DEFAULT_BLUR_KERNEL) // 8)
        return min(r, luma_max), max(1, min(r // 2, chroma_max))


@dataclass
class MaskPlan:
    width: int
    height: int
    items: list[MaskItem] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return [it.kind for it in self.items]


def _bbox(poly: np.ndarray, width: int, height: int) -> tuple[int, int, int, int]:
    """Bounding box of a polygon, grown to even coordinates and sizes (4:2:0 chroma), inside the frame."""
    x0, y0 = int(poly[:, 0].min()), int(poly[:, 1].min())
    x1, y1 = int(poly[:, 0].max()) + 1, int(poly[:, 1].max()) + 1
    x0, y0 = x0 - x0 % 2, y0 - y0 % 2
    x1, y1 = min(width - width % 2, x1 + x1 % 2), min(height - height % 2, y1 + y1 % 2)
    x1, y1 = max(x1, x0 + 2), max(y1, y0 + 2)
    if x1 > width:
        x0, x1 = max(0, width - width % 2 - 2), width - width % 2
    if y1 > height:
        y0, y1 = max(0, height - height % 2 - 2), height - height % 2
    return x0, y0, x1 - x0, y1 - y0


def plan_for(masks: list[dict], width: int, height: int) -> MaskPlan:
    """One item per privacy mask, with its image cropped to its bounding box.

    Working on the box only (not the whole frame) keeps the per-tile CPU cost
    proportional to the masked area.
    """
    import cv2

    width, height = int(width), int(height)
    plan = MaskPlan(width=width, height=height)
    for m in masks:
        mode = _mode(m)
        poly = polygon_pixels(m, width, height)
        x, y, w, h = _bbox(poly, width, height)
        local = poly - np.array([x, y], dtype=np.int32)
        if mode == "BLUR":
            k = max(int(m.get("blur_kernel_size") or DEFAULT_BLUR_KERNEL), (min(w, h) // 4) | 1)
            img = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(img, [local], 255)
            plan.items.append(MaskItem("blur", x, y, w, h, k, img))
        elif mode == "MOSAIC":
            s = max(2, int(m.get("mosaic_scale") or DEFAULT_MOSAIC_SCALE))
            img = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(img, [local], 255)
            plan.items.append(MaskItem("mosaic", x, y, w, h, s, img))
        else:  # BLACKOUT, COLOR, and an unknown mode as BLACKOUT (fail closed, as privacy_mask)
            bgr = (0, 0, 0)
            if mode == "COLOR":
                bgr = tuple(max(0, min(255, int(c))) for c in (m.get("mask_color_bgr") or (0, 0, 0)))
            img = np.zeros((h, w, 4), dtype=np.uint8)
            cv2.fillPoly(img, [local], (*bgr, 255))
            plan.items.append(MaskItem("fill", x, y, w, h, 0, img))
    return plan


def masks_dir() -> Path:
    return Path(settings.STORAGE_DIR) / "go2rtc" / "masks"


def write_images(camera_id: str, masks: list[dict], plan: MaskPlan, directory: Optional[Path] = None) -> list[Path]:
    """Write one PNG per plan item (content addressed, 0600) and remove this camera's older ones."""
    import cv2

    directory = Path(directory or masks_dir())
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    cam_key = hashlib.sha256(camera_id.encode("utf-8")).hexdigest()[:12]
    sig = signature(masks, (plan.width, plan.height))
    paths: list[Path] = []
    for i, item in enumerate(plan.items):
        path = directory / f"{cam_key}_{sig}_{i}_{item.kind}.png"
        if not _SAFE_PATH.match(str(path)):
            raise MaskedVideoUnavailable("the mask folder's path contains characters ffmpeg's source cannot carry")
        if not path.is_file():
            ok, buf = cv2.imencode(".png", item.image)
            if not ok:
                raise MaskedVideoUnavailable("the mask image could not be encoded")
            tmp = path.with_name(f".{path.name}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, buf.tobytes())
            finally:
                os.close(fd)
            os.replace(tmp, path)
        paths.append(path)
    keep = {p.name for p in paths}
    for old in directory.glob(f"{cam_key}_*.png"):
        if old.name not in keep:
            try:
                old.unlink()
            except OSError:
                pass
    return paths


# --------------------------------------------------------------------------- ffmpeg arguments

def filter_graph(plan: MaskPlan, engine: str) -> str:
    """The -filter_complex graph (no whitespace). Inputs: 0 = camera, i+1 = image of plan.items[i].

    The camera picture is scaled to the plan's size (a no-op at the native
    size). Blur and mosaic regions are cut from the unmasked picture (split),
    filtered at their own size, given the polygon as alpha and overlaid at
    their box; fills are overlaid as RGBA. The images are single frames that
    overlay repeats (eof_action=repeat).
    """
    w, h = plan.width, plan.height
    regions = [i for i, it in enumerate(plan.items) if it.kind in ("blur", "mosaic")]
    parts: list[str] = []
    head = f"[0:v]scale={w}:{h}:flags=bilinear,setsar=1,format=yuv420p"
    if regions:
        head += f",split={len(regions) + 1}[base]" + "".join(f"[src{i}]" for i in regions)
    else:
        head += "[base]"
    parts.append(head)
    cur = "base"
    for i, it in enumerate(plan.items):
        inp = i + 1
        if it.kind == "fill":
            parts.append(f"[{inp}:v]format=rgba[img{i}]")
        else:
            sw, sh = it.small_size
            chain = f"[src{i}]crop={it.w}:{it.h}:{it.x}:{it.y},scale={sw}:{sh}:flags=area"
            if it.kind == "blur":
                radii = it.blur_radii
                if radii:
                    chain += f",boxblur={radii[0]}:2:{radii[1]}:2"
                chain += f",scale={it.w}:{it.h}:flags=bilinear"
            else:
                chain += f",scale={it.w}:{it.h}:flags=neighbor"
            parts.append(chain + f",format=yuva420p[reg{i}]")
            parts.append(f"[{inp}:v]format=gray[alpha{i}]")
            parts.append(f"[reg{i}][alpha{i}]alphamerge[img{i}]")
        parts.append(f"[{cur}][img{i}]overlay={it.x}:{it.y}:format=yuv420:eof_action=repeat[m{i}]")
        cur = f"m{i}"
    tail = "format=nv12,hwupload" if engine == "vaapi" else "format=yuv420p"
    parts.append(f"[{cur}]{tail}[v]")
    graph = ";".join(parts)
    if _FORBIDDEN_IN_TOKEN.search(graph):
        raise MaskedVideoUnavailable("internal error: the filter graph contains whitespace")
    return graph


def ffmpeg_args(plan: MaskPlan, engine: str, images: list[Path]) -> list[str]:
    """ffmpeg arguments that follow the camera input (go2rtc puts its own output after them)."""
    if not plan.items:
        raise MaskedVideoUnavailable("no privacy masks to burn in")
    if len(images) != len(plan.items) or not all(Path(p).is_file() for p in images):
        raise MaskedVideoUnavailable("a mask image is missing")
    args: list[str] = []
    if engine == "vaapi":
        args += ["-init_hw_device", f"vaapi={VAAPI_DEVICE_NAME}", "-filter_hw_device", VAAPI_DEVICE_NAME]
    for path in images:
        args += ["-i", str(path)]
    args += ["-filter_complex", filter_graph(plan, engine), "-map", "[v]", "-an"]
    args += ENCODERS.get(engine, ENCODERS["software"])
    for a in args:
        # "auto" as a whole argument makes go2rtc ignore the ffmpeg arguments (see ENCODERS).
        if not a or _FORBIDDEN_IN_TOKEN.search(a) or a == "auto":
            raise MaskedVideoUnavailable("an ffmpeg argument would contain whitespace or '#'")
    return args


def go2rtc_source(url: str, args: list[str]) -> str:
    """``ffmpeg:<camera url>#video=-an#raw=<arg>#raw=<arg>...``.

    What go2rtc 1.9.14 builds from it (internal/ffmpeg parseArgs, checked with
    the real binary): ``ffmpeg <global> -allowed_media_types video <rtsp input
    options> -i <url> <raw args in order> -an -an <rtsp output>``.
    * every ffmpeg argument is its own raw token: an API-added source must not
      contain whitespace;
    * without any ``#video`` go2rtc appends ``-c copy``, which ffmpeg refuses next
      to a filter graph ("Filtering and streamcopy cannot be used together").
      A ``#video`` value that is not one of go2rtc's templates is appended as
      is, so ``-an`` (a no-op here) stops that and limits the input to video.
    """
    if _FORBIDDEN_IN_TOKEN.search(url):
        raise MaskedVideoUnavailable("the camera address contains a space or '#'")
    return "ffmpeg:" + url + "#video=-an" + "".join(f"#raw={a}" for a in args)


# --------------------------------------------------------------------------- engine + probe

_probe_cache: dict[tuple, bool] = {}
_probe_lock = threading.Lock()


def _configured_engine() -> str:
    from app.services import go2rtc_manager as g2

    choice = (settings.WEBRTC_TRANSCODE_ENCODER or "auto").strip().lower()
    engine = g2.ENCODER_ENGINES.get(choice, "")
    if choice in ("auto", "") or choice not in g2.ENCODER_ENGINES:
        engine = g2.detect_transcode_engine()
    return engine if engine in ENCODERS else "software"


def probe(ffmpeg_bin: str, args: list[str], plan: MaskPlan) -> bool:
    """Run the exact arguments once on a synthetic input; cached per argument list."""
    key = (ffmpeg_bin, tuple(args))
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", "0.5",
           "-i", f"testsrc2=size={plan.width}x{plan.height}:rate=10", *args, "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, timeout=PROBE_TIMEOUT_S)
        ok = res.returncode == 0
        if not ok:
            logger.warning(f"Masked live video probe failed ({ENCODER_NAMES.get(_engine_of(args), '?')}): "
                           f"{(res.stderr or '').strip()[-300:]}")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"Masked live video probe could not run: {exc}")
        ok = False
    with _probe_lock:
        _probe_cache[key] = ok
    return ok


def _engine_of(args: list[str]) -> str:
    for engine, enc in ENCODERS.items():
        if enc[1] in args:
            return engine
    return "software"


@dataclass
class MaskedSource:
    source: str
    signature: str
    engine: str
    encoder: str
    width: int
    height: int
    kinds: list[str]


def masked_source(camera_id: str, url: str, masks: list[dict], *, ffmpeg_bin: Optional[str] = None,
                  size: Optional[tuple[int, int]] = None, engine: Optional[str] = None,
                  directory: Optional[Path] = None,
                  prober: Optional[Callable[[str, list[str], MaskPlan], bool]] = None) -> MaskedSource:
    """go2rtc source with this camera's privacy masks burned in. Raises MaskedVideoUnavailable."""
    if not masks:
        raise MaskedVideoUnavailable("no privacy masks")  # callers use the plain path then
    if ffmpeg_bin is None:
        from app.services.capture_backends import ffmpeg_binary

        ffmpeg_bin = ffmpeg_binary()
    if not ffmpeg_bin:
        raise MaskedVideoUnavailable("ffmpeg is not installed")
    if size is None:
        from app.services.frame_geometry import frame_sizes

        size = frame_sizes.native(camera_id)
    if not size:
        raise MaskedVideoUnavailable("the camera's picture size is not known yet (it is learned once the camera "
                                     "is online)")
    width, height = int(size[0]), int(size[1])
    try:
        plan = plan_for(masks, width, height)
        images = write_images(camera_id, masks, plan, directory)
    except MaskedVideoUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001  (malformed polygon, disk full, ...)
        raise MaskedVideoUnavailable(f"the mask images could not be made ({exc.__class__.__name__})") from None
    prober = prober or probe
    first = engine or _configured_engine()
    tried: list[str] = []
    for eng in dict.fromkeys([first, "software"]):
        args = ffmpeg_args(plan, eng, images)
        tried.append(ENCODER_NAMES.get(eng, eng))
        if prober(ffmpeg_bin, args, plan):
            if eng != first:
                logger.warning(f"Masked live video for {camera_id}: {ENCODER_NAMES.get(first, first)} failed; "
                               f"using {ENCODER_NAMES[eng]}")
            return MaskedSource(source=go2rtc_source(url, args), signature=signature(masks, (width, height)),
                                engine=eng, encoder=ENCODER_NAMES.get(eng, eng), width=width, height=height,
                                kinds=list(plan.kinds))
    raise MaskedVideoUnavailable(f"the masking filter could not run (tried {', '.join(tried)})")


# --------------------------------------------------------------------------- change notifications

_listeners: list[Any] = []   # weak references: a discarded session registry is not kept alive
_listeners_lock = threading.Lock()


def add_listener(fn: Callable[[], None]) -> None:
    import weakref

    ref = weakref.WeakMethod(fn) if hasattr(fn, "__self__") else (lambda f=fn: f)
    with _listeners_lock:
        _listeners[:] = [r for r in _listeners if r() is not None and r() != fn]
        _listeners.append(ref)


def notify_masks_changed() -> None:
    """Called by ai_zone_service after a privacy mask was added, changed or removed (any thread)."""
    with _listeners_lock:
        fns = [r() for r in _listeners]
    for fn in fns:
        if fn is None:
            continue
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.exception("privacy mask change listener failed")
