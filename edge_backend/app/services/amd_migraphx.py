"""AMD GPU inference through ONNX Runtime's MIGraphX *plugin* execution provider.

Since ONNX Runtime 1.23 there is no ROCm EP, and AMD ships MIGraphX as a
plugin EP (``onnxruntime-ep-migraphx``) that is loaded into a stock
``onnxruntime`` wheel at run time. Three things about it are easy to get
wrong, and each one silently ends up on the CPU:

* A plugin EP must be *registered* (``ort.register_execution_provider_library``)
  and then attached to a session through its device objects
  (``SessionOptions.add_provider_for_devices``). Asking for
  ``providers=["MIGraphXExecutionProvider"]`` by name is ignored without an
  error and the session runs on the CPU.
* The plugin's RUNPATH points at AMD's build machine, so ``libmigraphx_c`` and
  ``libonnxruntime`` must already be loaded (RTLD_GLOBAL) before registration,
  and the ROCm runtime must be initialised through ``rocm_sdk`` first.
* MIGraphX ``dlopen``s ``libmigraphx_gpu.so`` / ``libmigraphx_ref.so`` by their
  unversioned names, which a wheel cannot ship (no symlinks in wheels). The
  installer (scripts/bootstrap.py) creates them; this module only reports
  when they are missing.

Compiling a model for the GPU takes 1-3 minutes per model and input shape,
so compiled programs are cached (``ORT_MIGRAPHX_CACHE_DIR``) in one
directory per GPU target / library versions / precision: the plugin's cache
key ignores fp16 and ``MIGRAPHX_*`` settings, so mixing them in one directory
would load the wrong program. A small manifest (``edge_warm.json``) records
which model files have been compiled there, so the service can tell a cold
cache (compile in the background, run on the CPU meanwhile) from a warm one.

Nothing about the machine is assumed: the GPU target (e.g. ``gfx1200``) is
read from the kernel's KFD topology at run time, with rocm_agent_enumerator as
a fallback.

This module imports only the standard library at top level, so the bootstrap
script and the preflight session probe can load it by file path before the
application's dependencies exist.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLUGIN_EP = "MIGraphXExecutionProvider"
PLUGIN_PACKAGE = "onnxruntime_ep_migraphx"

# The pinned, verified set (Ubuntu 26.04, RX 9060 XT gfx1200, Python 3.14).
# The plugin requires onnxruntime~=1.29.0, so this flavour cannot use the
# >=1.30 builds the CUDA/CPU flavours install.
ROCM_VERSION = "10.0.0"
ORT_VERSION = "1.29.0"
MIGRAPHX_VERSION = "2.17.0+rocm10.0.0"
PLUGIN_VERSION = "1.0.0+rocm10.0.0"
AMD_INDEX_URLS = (
    "https://stable.repo.amd.com/rocm/whl-next/",
    "https://stable.repo.amd.com/rocm/migraphx/whl-next/",
    "https://stable.repo.amd.com/rocm/onnxruntime/whl-next/",
)
# Distributions this flavour installs (for status reporting).
DISTRIBUTIONS = ("onnxruntime", "migraphx", "migraphx-libs", "onnxruntime-ep-migraphx",
                 "rocm", "rocm-sdk-core", "rocm-sdk-libraries")
# Libraries MIGraphX dlopens by their unversioned name.
UNVERSIONED_LIBS = ("libmigraphx_gpu", "libmigraphx_ref")
PRELOAD_SHORTNAMES = ["amd_comgr", "amdhip64", "hiprtc"]

KFD_NODES = Path("/sys/class/kfd/kfd/topology/nodes")
WARM_MANIFEST = "edge_warm.json"


# --------------------------------------------------------------------------- #
# GPU target detection
# --------------------------------------------------------------------------- #

def gfx_name(gfx_target_version: int) -> Optional[str]:
    """KFD ``gfx_target_version`` -> LLVM target name.

    The value is major*10000 + minor*100 + stepping, and LLVM spells minor and
    stepping as one hex digit each: 120000 -> gfx1200, 90010 -> gfx90a,
    110501 -> gfx1151. 0 is a CPU node.
    """
    try:
        v = int(gfx_target_version)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    major, minor, step = v // 10000, (v // 100) % 100, v % 100
    if minor > 15 or step > 15:
        return None
    return f"gfx{major}{minor:x}{step:x}"


def _read_properties(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


def detect_gfx_targets(nodes_dir: Path = KFD_NODES) -> list[dict[str, Any]]:
    """GPU agents from the KFD topology, largest (most compute units) first."""
    agents: list[dict[str, Any]] = []
    try:
        nodes = sorted(nodes_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0)
    except OSError:
        return agents
    for node in nodes:
        props = _read_properties(node / "properties")
        name = gfx_name(props.get("gfx_target_version", 0))
        if not name:
            continue
        agents.append({
            "node": int(node.name) if node.name.isdigit() else node.name,
            "gfx": name,
            "simd_count": props.get("simd_count", 0),
            "device_id": props.get("device_id"),
            "vendor_id": props.get("vendor_id"),
        })
    agents.sort(key=lambda a: -int(a.get("simd_count") or 0))
    return agents


def gfx_from_enumerator() -> list[str]:
    """Fallback: ``rocm_agent_enumerator`` (a system ROCm install), minus the CPU agent."""
    exe = shutil.which("rocm_agent_enumerator")
    if not exe:
        return []
    try:
        res = subprocess.run([exe], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [t for t in dict.fromkeys(res.stdout.split()) if re.fullmatch(r"gfx[0-9a-f]+", t) and t != "gfx000"]


def gfx_targets() -> list[str]:
    """Distinct GPU targets on this machine, primary (largest) first.

    ``EDGE_ROCM_GFX`` (comma separated) overrides detection, for a box whose
    topology cannot be read.
    """
    forced = [t.strip() for t in os.environ.get("EDGE_ROCM_GFX", "").split(",") if t.strip()]
    if forced:
        return forced
    found = [a["gfx"] for a in detect_gfx_targets()] or gfx_from_enumerator()
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------------- #
# Install-time helpers (used by scripts/bootstrap.py)
# --------------------------------------------------------------------------- #

def install_specs(gfx: str) -> list[str]:
    """Pinned requirement specs for this GPU target."""
    if not re.fullmatch(r"gfx[0-9a-f]+", gfx or ""):
        raise ValueError(f"not a GPU target name: {gfx!r}")
    return [
        f"rocm[libraries,device-{gfx}]=={ROCM_VERSION}",
        f"onnxruntime=={ORT_VERSION}",
        f"migraphx=={MIGRAPHX_VERSION}",
        f"onnxruntime-ep-migraphx=={PLUGIN_VERSION}",
    ]


def index_args(uv: bool) -> list[str]:
    args: list[str] = []
    for url in AMD_INDEX_URLS:
        args += ["--extra-index-url", url]
    if uv:
        # uv stops at the first index that has a name; these packages are split
        # across PyPI and three AMD indexes.
        args += ["--index-strategy", "unsafe-best-match"]
    return args


def _versioned(libs_dir: Path, stem: str) -> Optional[Path]:
    """Highest ``<stem>.so.<N>`` (plain numeric suffix only) in ``libs_dir``."""
    best: tuple[int, Optional[Path]] = (-1, None)
    for p in libs_dir.glob(f"{stem}.so.*"):
        m = re.fullmatch(re.escape(stem) + r"\.so\.(\d+)", p.name)
        if m and int(m.group(1)) > best[0]:
            best = (int(m.group(1)), p)
    return best[1]


def missing_symlinks(libs_dir: Optional[Path]) -> list[str]:
    """Unversioned library names MIGraphX will dlopen that do not resolve."""
    if libs_dir is None:
        return []
    out = []
    for stem in UNVERSIONED_LIBS:
        link = libs_dir / f"{stem}.so"
        if not link.exists() and _versioned(libs_dir, stem) is not None:
            out.append(link.name)
    return out


def ensure_symlinks(libs_dir: Path) -> list[str]:
    """Create ``libmigraphx_{gpu,ref}.so -> .so.<N>`` next to the versioned files."""
    created = []
    for stem in UNVERSIONED_LIBS:
        link = libs_dir / f"{stem}.so"
        target = _versioned(libs_dir, stem)
        if target is None or link.exists():
            continue
        if link.is_symlink():  # dangling: points at a version that was replaced
            link.unlink()
        os.symlink(target.name, link)
        created.append(f"{link.name} -> {target.name}")
    return created


def libs_dir() -> Optional[Path]:
    spec = importlib.util.find_spec("migraphx_libs")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


def _dist_version(name: str) -> Optional[str]:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _compatible_release_ok(required: str, have: Optional[str]) -> Optional[bool]:
    """``~=a.b.c`` check without the packaging module. None when unparseable."""
    m = re.search(r"~=\s*(\d+)\.(\d+)(?:\.(\d+))?", required or "")
    if not m or not have:
        return None
    hv = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", have)
    if not hv:
        return None
    req = tuple(int(x or 0) for x in m.groups())
    got = tuple(int(x or 0) for x in hv.groups())
    if m.group(3) is not None:        # ~=a.b.c: same a.b, >= c
        return got[:2] == req[:2] and got >= req
    return got[0] == req[0] and got >= req  # ~=a.b: same a, >= a.b


def plugin_status() -> dict[str, Any]:
    """What is installed, without loading any GPU library (cheap; for preflight)."""
    versions = {d: v for d in DISTRIBUTIONS if (v := _dist_version(d))}
    installed = importlib.util.find_spec(PLUGIN_PACKAGE) is not None
    ldir = libs_dir() if installed else None
    ort_req = None
    try:
        for req in importlib_metadata.requires("onnxruntime-ep-migraphx") or []:
            if re.match(r"onnxruntime\b(?![-_])", req):
                ort_req = req
                break
    except importlib_metadata.PackageNotFoundError:
        pass
    return {
        "installed": installed,
        "versions": versions,
        "libs_dir": str(ldir) if ldir else None,
        "missing_symlinks": missing_symlinks(ldir),
        "onnxruntime_required": ort_req,
        "onnxruntime_compatible": _compatible_release_ok(ort_req or "", versions.get("onnxruntime")),
    }


# --------------------------------------------------------------------------- #
# Compiled-program cache
# --------------------------------------------------------------------------- #

def default_cache_base() -> Path:
    env = os.environ.get("MIGRAPHX_CACHE_DIR")
    if env:
        return Path(env)
    # Mirrors app.config.get_storage_root() for a checkout (the service's case).
    return Path(__file__).resolve().parents[3] / "storage" / "migraphx_cache"


def cache_key(gfx: Optional[str], fp16: bool, versions: dict[str, str]) -> str:
    parts = [
        gfx or "gfx-unknown",
        f"migraphx{versions.get('migraphx', '?')}",
        f"ep{versions.get('onnxruntime-ep-migraphx', '?')}",
        f"ort{versions.get('onnxruntime', '?')}",
        "fp16" if fp16 else "fp32",
    ]
    if os.environ.get("MIGRAPHX_DISABLE_WINOGRAD", "") not in ("", "0"):
        parts.append("nowino")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", "-".join(parts))


_sha_cache: dict[tuple[str, int, int], str] = {}


def model_digest(path: Path) -> Optional[str]:
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


def _warm_entries(cache_dir: Optional[Path]) -> dict:
    if cache_dir is None:
        return {}
    try:
        return json.loads((Path(cache_dir) / WARM_MANIFEST).read_text())
    except (OSError, ValueError):
        return {}


def is_warm(cache_dir: Optional[Path], model: Path, tag: str = "default") -> bool:
    digest = model_digest(Path(model))
    return bool(digest) and f"{digest}:{tag}" in _warm_entries(cache_dir)


def mark_warm(cache_dir: Optional[Path], model: Path, tag: str = "default",
              seconds: Optional[float] = None) -> bool:
    """Record that ``model`` (at ``tag``) is compiled in ``cache_dir``. False if unwritable."""
    digest = model_digest(Path(model))
    if cache_dir is None or not digest:
        return False
    entries = _warm_entries(cache_dir)
    entries[f"{digest}:{tag}"] = {"model": Path(model).name, "tag": tag, "compile_s": seconds,
                                  "at": round(time.time())}
    path = Path(cache_dir) / WARM_MANIFEST
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(entries, indent=1, sort_keys=True))
        os.replace(tmp, path)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


# --------------------------------------------------------------------------- #
# Runtime: prepare once per process, then hand out devices
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_prepared: Optional[dict[str, Any]] = None
_devices: list = []


def applicable() -> bool:
    """Worth trying at all: Linux, a ROCm compute node, and the plugin installed."""
    return (sys.platform.startswith("linux") and os.path.exists("/dev/kfd")
            and importlib.util.find_spec(PLUGIN_PACKAGE) is not None)


def _set_cache_env(base: Path, key: str) -> tuple[Optional[Path], Optional[str]]:
    """Point every compiler/program cache at ``base`` (the service's $HOME is read-only)."""
    cache_dir = base / key
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / f".write-test-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        return None, f"compiled-program cache {cache_dir} is not writable ({exc}); every start recompiles"
    # Always ours: one directory per target/versions/precision (see module docstring).
    os.environ["ORT_MIGRAPHX_CACHE_DIR"] = str(cache_dir)
    os.environ.setdefault("AMD_COMGR_CACHE_DIR", str(base / "comgr"))
    os.environ.setdefault("MIOPEN_USER_DB_PATH", str(base / "miopen"))
    os.environ.setdefault("MIOPEN_CUSTOM_CACHE_DIR", str(base / "miopen"))
    return cache_dir, None


def prepare(ort, cache_base: Optional[Path | str] = None, fp16: bool = False) -> dict[str, Any]:
    """Load ROCm + the plugin and register it with ``ort``. Once per process.

    Returns ``{"ok": bool, "reason": str|None, "devices": int, "gfx": ...,
    "cache_dir": ...}``. Never raises: every failure is a reason string.
    """
    global _prepared, _devices
    with _lock:
        if _prepared is not None:
            return dict(_prepared)
        info: dict[str, Any] = {"ok": False, "reason": None, "devices": 0, "gfx": None,
                                "cache_dir": None, "cache_warning": None}
        started = time.perf_counter()
        try:
            _prepare(ort, info, Path(cache_base) if cache_base else default_cache_base(), fp16)
        except Exception as exc:  # noqa: BLE001 - a broken ROCm install must not take the service down
            info["reason"] = f"{type(exc).__name__}: {exc}"
        info["prepare_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        _prepared = info
        if info["ok"]:
            logger.info(f"MIGraphX plugin EP ready: {info['devices']} device(s), {info['gfx']}, "
                        f"cache {info['cache_dir']} ({info['prepare_ms']} ms)")
        else:
            logger.warning(f"MIGraphX plugin EP not usable: {info['reason']}")
        return dict(info)


def _prepare(ort, info: dict, cache_base: Path, fp16: bool) -> None:
    if not sys.platform.startswith("linux"):
        info["reason"] = "MIGraphX plugin EP is Linux-only"
        return
    if not os.path.exists("/dev/kfd"):
        info["reason"] = "no /dev/kfd (amdgpu KFD not loaded or not passed into this sandbox)"
        return
    if not os.access("/dev/kfd", os.R_OK | os.W_OK):
        info["reason"] = "/dev/kfd is not accessible to this user (needs the render/video group)"
        return
    status = plugin_status()
    info["versions"] = status["versions"]
    if not status["installed"]:
        info["reason"] = "onnxruntime-ep-migraphx is not installed (scripts/bootstrap.py --ort migraphx)"
        return
    if status["onnxruntime_compatible"] is False:
        info["reason"] = (f"installed onnxruntime {status['versions'].get('onnxruntime')} does not satisfy the "
                          f"plugin's {status['onnxruntime_required']}")
        return
    ldir = Path(status["libs_dir"]) if status["libs_dir"] else None
    if ldir is None:
        info["reason"] = "migraphx_libs package not found (install migraphx from AMD's index)"
        return
    if status["missing_symlinks"]:
        info["reason"] = (f"{', '.join(status['missing_symlinks'])} missing in {ldir} (wheels cannot ship "
                          "symlinks; scripts/bootstrap.py creates them)")
        return

    targets = gfx_targets()
    info["gfx"] = targets[0] if targets else None
    # Winograd tuning made the first compile ~8x slower (809 s vs ~100 s) for
    # the same or faster inference on RDNA4.
    os.environ.setdefault("MIGRAPHX_DISABLE_WINOGRAD", "1")
    cache_dir, warning = _set_cache_env(cache_base, cache_key(info["gfx"], fp16, status["versions"]))
    info["cache_dir"] = str(cache_dir) if cache_dir else None
    info["cache_warning"] = warning

    import rocm_sdk  # noqa: PLC0415

    rocm_sdk.initialize_process(preload_shortnames=list(PRELOAD_SHORTNAMES))
    c_lib = next((p for p in sorted(ldir.glob("libmigraphx_c.so.*"))
                  if re.fullmatch(r"libmigraphx_c\.so\.\d+", p.name)), None)
    if c_lib is None:
        info["reason"] = f"libmigraphx_c.so.* not found in {ldir}"
        return
    # The plugin's RUNPATH points at AMD's build tree (AMDMIGraphX#5235):
    # preload what it links against so the loader finds it by SONAME.
    ctypes.CDLL(str(c_lib), mode=ctypes.RTLD_GLOBAL)
    capi = Path(ort.__file__).parent / "capi"
    ort_lib = next(iter(sorted(capi.glob("libonnxruntime.so.1.*"))), None)
    if ort_lib is None:
        info["reason"] = f"libonnxruntime.so.1.* not found in {capi}"
        return
    ctypes.CDLL(str(ort_lib), mode=ctypes.RTLD_GLOBAL)

    import importlib  # noqa: PLC0415

    epm = importlib.import_module(PLUGIN_PACKAGE)
    for name, path in zip(epm.get_ep_names(), epm.get_library_paths()):
        try:
            ort.register_execution_provider_library(name, path)
        except Exception as exc:  # noqa: BLE001
            if "already" not in str(exc).lower():
                raise
    devices = [d for d in ort.get_ep_devices() if "MIGraphX" in str(getattr(d, "ep_name", ""))]
    if not devices:
        info["reason"] = "plugin registered but it exposes no AMD GPU device"
        return
    _devices[:] = devices
    info["devices"] = len(devices)
    info["ok"] = True


def devices() -> list:
    """The MIGraphX EP devices registered by ``prepare()`` (empty if it failed)."""
    return list(_devices)


def pick_device(device_id: int = 0) -> list:
    """One device for a session: ``device_id`` when it exists, else the first."""
    devs = devices()
    if not devs:
        return []
    return [devs[device_id]] if 0 <= device_id < len(devs) else [devs[0]]


def provider_options(fp16: bool = False) -> dict[str, str]:
    return {"migraphx_fp16_enable": "1"} if fp16 else {}


def add_to_session_options(so, device_id: int = 0, fp16: bool = False) -> bool:
    """Attach the MIGraphX EP to ``so`` through the device path. False if none."""
    devs = pick_device(device_id)
    if not devs:
        return False
    so.add_provider_for_devices(devs, provider_options(fp16))
    return True


def cache_dir() -> Optional[Path]:
    """The compiled-program cache chosen by ``prepare()``, or None."""
    return Path(_prepared["cache_dir"]) if _prepared and _prepared.get("cache_dir") else None


def reset_for_tests() -> None:
    global _prepared
    with _lock:
        _prepared = None
        _devices.clear()
