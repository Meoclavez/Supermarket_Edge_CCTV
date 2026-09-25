"""Startup preflight: report what is wrong with this installation, and how to fix it.

``run_preflight()`` checks, without changing anything:

* every runtime dependency in requirements.txt imports;
* which onnxruntime distribution is installed, whether two conflicting ones
  are (they share one import package), which execution providers it offers,
  and whether a GPU is present while inference can only run on the CPU;
* the model files against ``models/manifest.json`` (size + sha256);
* that the storage directories are writable;
* ``.env`` keys this version does not read, and model settings that name
  files which do not exist.

It returns ``{"ok", "errors", "warnings", "checks", ...}``. Every error and
warning carries the exact command that fixes it. Fixing is the job of
``scripts/bootstrap.py`` (``./run.sh``); this module never installs or writes.

The module's top level imports only the standard library, so the bootstrap and
fetch_models scripts can load it by file path (to share the accelerator probe
and the manifest verification) before the app's dependencies exist. Anything
that needs the app is imported lazily inside the function that uses it.

Run standalone (systemd ExecStartPre, Docker entrypoint)::

    python -m app.services.preflight            # human report, exit 1 on errors
    python -m app.services.preflight --json     # machine-readable
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

EDGE_BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = EDGE_BACKEND_DIR.parent
MODELS_DIR = EDGE_BACKEND_DIR / "models"
MANIFEST_PATH = MODELS_DIR / "manifest.json"
LOCAL_EXPORTS_NAME = ".local_exports.json"
REQUIREMENTS_PATH = EDGE_BACKEND_DIR / "requirements.txt"
RUN_SH = REPO_DIR / "run.sh"

# Every distribution that installs the ``onnxruntime`` import package. Two of
# these in one environment overwrite each other's files.
ORT_DISTRIBUTIONS: dict[str, str] = {
    "onnxruntime": "cpu",
    "onnxruntime-gpu": "cuda",
    "onnxruntime-openvino": "openvino",
    "onnxruntime-directml": "directml",
    "onnxruntime-rocm": "rocm",
    "onnxruntime-migraphx": "migraphx",
    "onnxruntime-qnn": "qnn",
}

GPU_PROVIDERS = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "MIGraphXExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
)

# (import names tried in order, requirement name). All are in requirements.txt,
# so a missing one means the install is incomplete.
REQUIRED_MODULES: list[tuple[tuple[str, ...], str]] = [
    (("fastapi",), "fastapi"),
    (("uvicorn",), "uvicorn[standard]"),
    (("pydantic",), "pydantic"),
    (("pydantic_settings",), "pydantic-settings"),
    (("sqlalchemy",), "sqlalchemy[asyncio]"),
    (("aiosqlite",), "aiosqlite"),
    (("cryptography",), "cryptography"),
    (("zeroconf",), "zeroconf"),
    (("python_multipart", "multipart"), "python-multipart"),
    (("cv2",), "opencv-python-headless"),
    (("numpy",), "numpy"),
    (("aiofiles",), "aiofiles"),
    (("jwt",), "pyjwt"),
    (("httpx",), "httpx[http2]"),
    (("bcrypt",), "bcrypt"),
    (("psutil",), "psutil"),
    (("scipy",), "scipy"),
    (("onnxruntime",), "onnxruntime-gpu / onnxruntime"),
]

logger = logging.getLogger("edge.preflight")

_last_result: Optional[dict] = None

AMD_HELPER_PATH = Path(__file__).resolve().parent / "amd_migraphx.py"


def _amd():
    """app.services.amd_migraphx, or the same file loaded by path when this
    module runs standalone (bootstrap, before the app is importable)."""
    try:
        from app.services import amd_migraphx  # noqa: PLC0415
        return amd_migraphx
    except ImportError:
        mod = sys.modules.get("edge_amd_migraphx_standalone")
        if mod is None:
            spec = importlib.util.spec_from_file_location("edge_amd_migraphx_standalone", AMD_HELPER_PATH)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


# --------------------------------------------------------------------------- #
# Fix commands
# --------------------------------------------------------------------------- #

def _fix_run_sh() -> str:
    return f"{RUN_SH} --check-only"


def _fix_requirements(python: str) -> str:
    return f"uv pip install --python {python} -r {REQUIREMENTS_PATH}"


def _fix_ort_gpu(python: str, remove: list[str]) -> str:
    parts = []
    if remove:
        parts.append(f"uv pip uninstall --python {python} {' '.join(remove)}")
    parts.append(
        f"uv pip install --python {python} --reinstall-package onnxruntime-gpu "
        f"'onnxruntime-gpu[cuda,cudnn]>=1.30'"
    )
    return f"{_fix_run_sh()}   (or: {' && '.join(parts)})"


def _fix_ort_migraphx() -> str:
    return f"{sys.executable} {EDGE_BACKEND_DIR / 'scripts' / 'bootstrap.py'} --check-only --ort migraphx"


def _fix_models() -> str:
    return f"{sys.executable} {EDGE_BACKEND_DIR / 'scripts' / 'fetch_models.py'}"


def _os_release_id() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def nvidia_driver_install_command() -> str:
    """System-level fix for "NVIDIA GPU present, driver not loaded". Printed, never run."""
    if in_container():
        return (
            "on the HOST: install the NVIDIA driver and nvidia-container-toolkit, then start with "
            "`docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`"
        )
    distro = _os_release_id()
    if distro in ("arch", "endeavouros", "manjaro", "cachyos"):
        return "sudo pacman -S --needed nvidia-open nvidia-utils && sudo reboot"
    if distro in ("ubuntu", "pop", "linuxmint"):
        return "sudo ubuntu-drivers install && sudo reboot"
    if distro == "debian":
        return "sudo apt install nvidia-driver firmware-misc-nonfree && sudo reboot"
    if distro in ("fedora", "rhel", "rocky", "almalinux"):
        return "sudo dnf install akmod-nvidia && sudo reboot   (RPM Fusion)"
    return "install your distribution's NVIDIA driver package (>= 580 for CUDA 13), then reboot"


# --------------------------------------------------------------------------- #
# Accelerator probe (stdlib only; shared with scripts/bootstrap.py)
# --------------------------------------------------------------------------- #

_PCI_NVIDIA, _PCI_AMD, _PCI_INTEL, _PCI_HAILO = "0x10de", "0x1002", "0x8086", "0x1e60"


def _pci_devices() -> list[tuple[str, str]]:
    """(vendor, class) for every PCI function, from sysfs. Empty off Linux."""
    out: list[tuple[str, str]] = []
    root = Path("/sys/bus/pci/devices")
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for dev in entries:
        try:
            out.append(((dev / "vendor").read_text().strip(), (dev / "class").read_text().strip()))
        except OSError:
            continue
    return out


def _nvidia_smi() -> tuple[list[str], Optional[str]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], None
    try:
        res = subprocess.run(
            [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], None
    if res.returncode != 0:
        return [], None
    names, driver = [], None
    for line in res.stdout.strip().splitlines():
        name, _, ver = line.partition(",")
        names.append(name.strip())
        driver = ver.strip() or driver
    return names, driver


def detect_accelerators() -> dict[str, Any]:
    """What accelerators this machine has, probed at runtime. Never assumes."""
    pci = _pci_devices()
    display = [(v, c) for v, c in pci if c.startswith("0x03")]  # VGA / 3D controller

    smi_names, smi_driver = _nvidia_smi()
    nvidia_nodes = sorted(str(p) for p in Path("/dev").glob("nvidia[0-9]*"))
    nvidia_pci = any(v == _PCI_NVIDIA for v, _ in display)
    # In a container the host's /proc/driver/nvidia is visible even when the GPU
    # was not passed in, so there only device nodes / nvidia-smi count.
    nvidia_driver = bool(smi_names) or bool(nvidia_nodes) or (
        Path("/proc/driver/nvidia/version").exists() and not in_container())
    if not nvidia_pci and not nvidia_driver and sys.platform == "win32":
        nvidia_driver = bool(smi_names)

    render_nodes = sorted(str(p) for p in Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []

    hailo_node = Path("/dev/hailo0").exists()
    return {
        "platform": f"{sys.platform}/{platform.machine()}",
        "container": in_container(),
        "nvidia": {
            "present": nvidia_pci or nvidia_driver,
            "pci": nvidia_pci,
            "driver_loaded": nvidia_driver,
            "gpus": smi_names,
            "driver_version": smi_driver,
            "device_nodes": nvidia_nodes,
        },
        "amd": {
            "present": any(v == _PCI_AMD for v, _ in display),
            "rocm_kfd": Path("/dev/kfd").exists(),
            # Read from the KFD topology at run time (e.g. gfx1200), primary first.
            "gfx_targets": _amd().gfx_targets() if Path("/dev/kfd").exists() else [],
        },
        "intel": {"present": any(v == _PCI_INTEL for v, _ in display)},
        "render_nodes": render_nodes,
        "hailo": {
            "present": hailo_node or any(v == _PCI_HAILO for v, _ in pci),
            "device_node": hailo_node,
            "runtime_installed": importlib.util.find_spec("hailo_platform") is not None,
        },
        "apple_silicon": sys.platform == "darwin" and platform.machine() == "arm64",
    }


def preferred_gpu_providers(accel: dict[str, Any]) -> list[str]:
    """ORT providers worth asking for on this hardware, best first (CPU implied)."""
    wanted: list[str] = []
    if accel.get("nvidia", {}).get("driver_loaded"):
        wanted.append("CUDAExecutionProvider")
    if accel.get("amd", {}).get("rocm_kfd") and not accel.get("nvidia", {}).get("driver_loaded"):
        # Only without a working NVIDIA GPU, which the engine prefers (and an
        # AMD APU's iGPU also exposes /dev/kfd). ROCMExecutionProvider was
        # removed from ONNX Runtime in 1.23.
        wanted.append("MIGraphXExecutionProvider")
    if accel.get("intel", {}).get("present") and accel.get("render_nodes"):
        wanted.append("OpenVINOExecutionProvider")
    if sys.platform == "win32":
        wanted.append("DmlExecutionProvider")
    if accel.get("apple_silicon"):
        wanted.append("CoreMLExecutionProvider")
    return wanted


# --------------------------------------------------------------------------- #
# Model manifest verification (shared with scripts/fetch_models.py)
# --------------------------------------------------------------------------- #

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> dict:
    return json.loads(Path(manifest_path).read_text())


def load_local_exports(models_dir: Path) -> dict:
    """Hashes of locally re-exported models that fetch_models accepted (IO signature matched)."""
    try:
        return json.loads((Path(models_dir) / LOCAL_EXPORTS_NAME).read_text())
    except (OSError, ValueError):
        return {}


def read_io_signature(path: Path) -> Optional[dict]:
    """Input/output names, shapes and types of an ONNX model, via a CPU session."""
    try:
        import onnxruntime as ort  # noqa: PLC0415

        so = ort.SessionOptions()
        so.log_severity_level = 3
        sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        return {
            "inputs": [{"name": i.name, "shape": list(i.shape), "type": i.type} for i in sess.get_inputs()],
            "outputs": [{"name": o.name, "shape": list(o.shape), "type": o.type} for o in sess.get_outputs()],
        }
    except Exception:  # noqa: BLE001 - unreadable/corrupt model or no ORT
        return None


def io_signature_matches(actual: Optional[dict], expected: Optional[dict]) -> bool:
    if not actual or not expected:
        return False

    def norm(sig: dict) -> list:
        return [
            [(t["name"], [str(d) for d in t["shape"]], t["type"]) for t in sig.get(k, [])]
            for k in ("inputs", "outputs")
        ]

    return norm(actual) == norm(expected)


def verify_model(entry: dict, models_dir: Path, local_exports: Optional[dict] = None,
                 check_io_on_mismatch: bool = True) -> dict:
    """Verify one manifest entry. ``status`` is one of:

    ok, local_export (re-exported here, IO signature verified by fetch_models),
    missing, size_mismatch, hash_mismatch.
    """
    path = Path(models_dir) / entry["file"]
    result: dict[str, Any] = {
        "file": entry["file"], "task": entry.get("task"), "required": bool(entry.get("required", True)),
        "path": str(path), "status": "missing",
    }
    if not path.is_file():
        return result
    size = path.stat().st_size
    result["size"] = size
    digest = sha256_file(path)
    result["sha256"] = digest
    if digest == entry.get("sha256"):
        result["status"] = "ok"
        return result
    accepted = (local_exports or {}).get(entry["file"])
    if accepted and accepted.get("sha256") == digest:
        result["status"] = "local_export"
        return result
    result["status"] = "size_mismatch" if size != entry.get("size") else "hash_mismatch"
    if check_io_on_mismatch:
        result["io_signature_matches"] = io_signature_matches(read_io_signature(path), entry.get("io"))
    return result


def verify_models(manifest_path: Path = MANIFEST_PATH, models_dir: Optional[Path] = None,
                  check_io_on_mismatch: bool = True) -> list[dict]:
    manifest = load_manifest(manifest_path)
    models_dir = Path(models_dir) if models_dir else Path(manifest_path).parent
    local = load_local_exports(models_dir)
    return [verify_model(e, models_dir, local, check_io_on_mismatch) for e in manifest.get("models", [])]


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def _issue(check: str, message: str, fix: Optional[str] = None) -> dict:
    return {"check": check, "message": message, "fix": fix}


def check_modules() -> tuple[dict, list, list]:
    status: dict[str, Any] = {}
    missing: list[str] = []
    for names, requirement in REQUIRED_MODULES:
        err: Optional[str] = None
        for name in names:
            try:
                importlib.import_module(name)
                status[names[0]] = True
                err = None
                break
            except Exception as exc:  # noqa: BLE001 - ImportError or a broken native lib
                err = f"{type(exc).__name__}: {exc}"
        if err is not None:
            status[names[0]] = err
            missing.append(f"{requirement} ({err})")
    errors = [_issue(
        "modules", f"required packages not importable: {'; '.join(missing)}",
        f"{_fix_run_sh()}   (or: {_fix_requirements(sys.executable)})",
    )] if missing else []
    return status, errors, []


def installed_ort_distributions() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in ORT_DISTRIBUTIONS:
        try:
            found[dist] = importlib_metadata.version(dist)
        except importlib_metadata.PackageNotFoundError:
            continue
    return found


def _ort_runtime() -> dict[str, Any]:
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"importable": False, "error": f"{type(exc).__name__}: {exc}", "providers": []}
    try:
        plugin = _amd().plugin_status()
    except Exception as exc:  # noqa: BLE001
        plugin = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "importable": True,
        "version": getattr(ort, "__version__", None),
        "providers": list(ort.get_available_providers()),
        "has_preload_dlls": hasattr(ort, "preload_dlls"),
        # Plugin EPs are not in get_available_providers() until registered;
        # this is what is installed, not proof that it runs (see the probe).
        "plugin_eps": {"migraphx": plugin},
    }


def evaluate_ort(dists: dict[str, str], runtime: dict[str, Any], accel: dict[str, Any],
                 python: Optional[str] = None) -> tuple[dict, list, list]:
    """Pure decision logic for the onnxruntime checks (unit-tested with fakes)."""
    python = python or sys.executable
    errors: list = []
    warnings: list = []
    providers = list(runtime.get("providers") or [])
    gpu_providers = [p for p in providers if p in GPU_PROVIDERS]
    nvidia = accel.get("nvidia", {})
    mgx = (runtime.get("plugin_eps") or {}).get("migraphx") or {}
    mgx_usable = bool(mgx.get("installed")) and not mgx.get("missing_symlinks") \
        and mgx.get("onnxruntime_compatible") is not False
    if mgx_usable and "MIGraphXExecutionProvider" not in gpu_providers:
        gpu_providers.append("MIGraphXExecutionProvider")
    flavour = ",".join(ORT_DISTRIBUTIONS[d] for d in dists) or None
    if mgx.get("installed"):
        flavour = f"{flavour or '?'}+migraphx-plugin"
    info: dict[str, Any] = {
        "distributions": dists,
        "flavour": flavour,
        "plugin_eps": {"migraphx": mgx} if mgx else {},
        "version": runtime.get("version"),
        "available_providers": providers,
        "gpu_providers_available": gpu_providers,
        "conflict": len(dists) > 1,
        "gpu_mismatch": False,
    }

    if not runtime.get("importable", False):
        errors.append(_issue("onnxruntime", f"onnxruntime is not importable ({runtime.get('error')})",
                             _fix_ort_gpu(python, []) if nvidia.get("present") else _fix_run_sh()))
        return info, errors, warnings

    if len(dists) > 1:
        errors.append(_issue(
            "onnxruntime",
            f"conflicting onnxruntime distributions installed: {', '.join(f'{k} {v}' for k, v in dists.items())}. "
            "They share one import package, so whichever was installed last overwrote the other's files",
            _fix_ort_gpu(python, [d for d in dists if d != "onnxruntime-gpu"])
            if nvidia.get("present") else _fix_run_sh(),
        ))

    if nvidia.get("present") and not nvidia.get("driver_loaded"):
        info["gpu_mismatch"] = True
        warnings.append(_issue(
            "gpu",
            ("an NVIDIA GPU is on the host but was not passed into this container (no /dev/nvidia*); "
             if in_container() else
             "an NVIDIA GPU is on the PCI bus but its driver is not loaded (no /dev/nvidia*, nvidia-smi fails); ")
            + "inference will run on the CPU",
            nvidia_driver_install_command(),
        ))
    elif nvidia.get("driver_loaded") and "CUDAExecutionProvider" not in providers:
        info["gpu_mismatch"] = True
        installed = ", ".join(dists) or "unknown"
        warnings.append(_issue(
            "gpu",
            f"NVIDIA GPU present ({', '.join(nvidia.get('gpus') or ['driver loaded'])}) but the installed "
            f"onnxruntime ({installed}) offers only {providers}: inference is CPU-only",
            _fix_ort_gpu(python, [d for d in dists if d != "onnxruntime-gpu"]),
        ))
    elif accel.get("amd", {}).get("rocm_kfd") and not nvidia.get("present") and not mgx_usable \
            and "MIGraphXExecutionProvider" not in providers:
        info["gpu_mismatch"] = True
        gfx = ", ".join(accel.get("amd", {}).get("gfx_targets") or []) or "target unknown"
        if not mgx.get("installed"):
            why = "the MIGraphX plugin EP (onnxruntime-ep-migraphx) is not installed"
        elif mgx.get("missing_symlinks"):
            why = (f"MIGraphX is installed but {', '.join(mgx['missing_symlinks'])} is missing in "
                   f"{mgx.get('libs_dir')} (wheels cannot ship symlinks)")
        else:
            why = (f"the MIGraphX plugin needs {mgx.get('onnxruntime_required')} but onnxruntime "
                   f"{(mgx.get('versions') or {}).get('onnxruntime')} is installed")
        warnings.append(_issue(
            "gpu", f"AMD GPU with ROCm (/dev/kfd, {gfx}) present but {why}: inference is CPU-only",
            _fix_ort_migraphx(),
        ))

    hailo = accel.get("hailo", {})
    if hailo.get("device_node") and not hailo.get("runtime_installed"):
        warnings.append(_issue(
            "hailo",
            "/dev/hailo0 present but the HailoRT Python runtime (hailo_platform) is not installed",
            f"download the hailort wheel for this Python from https://hailo.ai/developer-zone/ and run "
            f"uv pip install --python {python} ./hailort-*.whl",
        ))
    elif hailo.get("present") and not hailo.get("device_node"):
        warnings.append(_issue(
            "hailo", "Hailo PCIe device found but /dev/hailo0 is missing (driver not loaded)",
            "install the hailort-pcie-driver package from https://hailo.ai/developer-zone/ and reboot",
        ))
    return info, errors, warnings


def check_onnxruntime(accel: dict[str, Any]) -> tuple[dict, list, list]:
    return evaluate_ort(installed_ort_distributions(), _ort_runtime(), accel)


def evaluate_models(results: list[dict]) -> tuple[list, list]:
    errors: list = []
    warnings: list = []
    for r in results:
        status = r["status"]
        if status in ("ok", "local_export"):
            continue
        if status == "missing":
            (errors if r["required"] else warnings).append(_issue(
                "models", f"model {r['file']} is missing ({r['path']})", _fix_models()))
        else:
            detail = "same ONNX IO signature" if r.get("io_signature_matches") else "IO signature differs or unreadable"
            warnings.append(_issue(
                "models", f"model {r['file']} does not match manifest.json ({status}; {detail})", _fix_models()))
    return errors, warnings


def check_models() -> tuple[list, list, list]:
    if not MANIFEST_PATH.exists():
        return [], [_issue("models", f"{MANIFEST_PATH} not found", None)], []
    results = verify_models(MANIFEST_PATH, MODELS_DIR)
    errors, warnings = evaluate_models(results)
    return results, errors, warnings


def check_storage() -> tuple[list, list, list]:
    results: list[dict] = []
    errors: list = []
    warnings: list = []
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return results, [_issue("storage", f"cannot load app.config ({type(exc).__name__}: {exc})", None)], []

    names = ("STORAGE_DIR", "SNAPSHOTS_DIR", "CLIPS_DIR", "DVR_DIR", "ARCHIVES_DIR", "DATA_DIR", "BACKUPS_DIR")
    for name in names:
        value = getattr(settings, name, None)
        if value is None:
            continue
        path = Path(value)
        entry = {"name": name, "path": str(path)}
        if not path.exists():
            # A configured directory that does not exist yet is normal on a
            # fresh install or after a path override; the service creates it
            # on first use. It is only a problem if its nearest existing
            # ancestor is not writable (this module never writes).
            ancestor = next((p for p in path.parents if p.exists()), None)
            if ancestor is not None and ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK):
                results.append({**entry, "writable": True, "exists": False, "note": "will be created"})
                continue
            entry["note"] = f"cannot be created: {ancestor} is not writable"
        writable = path.is_dir() and os.access(path, os.W_OK | os.X_OK)
        results.append({**entry, "writable": writable})
        if not writable:
            why = f" and {entry['note']}" if entry.get("note") else ""
            errors.append(_issue("storage", f"{name} {path} is not a writable directory{why}",
                                 f"sudo chown -R $(id -un) {path}   (or fix the service user's ReadWritePaths)"))
    db = getattr(settings, "DATABASE_PATH", None)
    if db is not None:
        db = Path(db)
        if db.exists():
            ok = os.access(db, os.W_OK)
        else:
            # The database (and possibly its directory) is created on first start.
            parent = next((p for p in db.parents if p.exists()), None)
            ok = parent is not None and os.access(parent, os.W_OK | os.X_OK)
        results.append({"name": "DATABASE_PATH", "path": str(db), "writable": ok})
        if not ok:
            errors.append(_issue("storage", f"database {db} is not writable", f"sudo chown $(id -un) {db}"))
    root = str(getattr(settings, "STORAGE_DIR", ""))
    try:
        from app.services.evidence_storage import network_mount_problem  # noqa: PLC0415

        problem = network_mount_problem(Path(root)) if root else None
    except Exception:  # noqa: BLE001 - a broken check must not stop preflight
        problem = None
    if problem:
        warnings.append(_issue("storage", f"STORAGE_DIR {problem}; the evidence storage limit will not run",
                               "set STORAGE_DIR in .env to a directory on this device's own disk"))
    if root.startswith("/tmp/") and "cctv_test_" not in root:
        warnings.append(_issue("storage", f"storage fell back to {root}; recordings and the database "
                                          "will be lost on reboot", f"make {REPO_DIR / 'storage'} writable"))
    return results, errors, warnings


# --------------------------------------------------------------------------- #
# Session probe (optional; runs in a child process so nothing is loaded here)
# --------------------------------------------------------------------------- #

_PROBE_SCRIPT = r"""
import json, sys, time
res = {"ok": False}
try:
    import onnxruntime as ort
    res["version"] = ort.__version__
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as exc:
            res["preload_error"] = str(exc)
    avail = ort.get_available_providers()
    res["available"] = avail
    requested = json.loads(sys.argv[2])
    wanted = [p for p in requested if p in avail]
    wanted = list(dict.fromkeys(wanted + ["CPUExecutionProvider"]))
    so = ort.SessionOptions()
    so.log_severity_level = 3
    plugin = False
    if "MIGraphXExecutionProvider" in requested and "MIGraphXExecutionProvider" not in avail and len(sys.argv) > 3:
        # Plugin EP: requested by name it is ignored silently, so register it
        # and attach it through its device objects (app/services/amd_migraphx.py).
        import importlib.util
        spec = importlib.util.spec_from_file_location("edge_amd_migraphx_probe", sys.argv[3])
        amd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(amd)
        info = amd.prepare(ort, sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None)
        res["migraphx"] = {k: info.get(k) for k in ("ok", "reason", "gfx", "devices", "cache_dir")}
        plugin = bool(info.get("ok")) and amd.add_to_session_options(so)
        if plugin:
            wanted = ["MIGraphXExecutionProvider", "CPUExecutionProvider"]
    t0 = time.perf_counter()
    if plugin:
        sess = ort.InferenceSession(sys.argv[1], so)
    else:
        sess = ort.InferenceSession(sys.argv[1], so, providers=wanted)
    res["session_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    res["requested"] = wanted
    res["providers"] = sess.get_providers()
    res["provider"] = res["providers"][0]
    import numpy as np
    inp = sess.get_inputs()[0]
    shape = [d if isinstance(d, int) else 1 for d in inp.shape]
    x = np.zeros(shape, dtype=np.float32)
    sess.run(None, {inp.name: x})
    t0 = time.perf_counter()
    for _ in range(3):
        sess.run(None, {inp.name: x})
    res["infer_ms"] = round((time.perf_counter() - t0) * 1000 / 3, 2)
    res["ok"] = True
except Exception as exc:
    res["error"] = f"{type(exc).__name__}: {exc}"
print("@@PROBE@@" + json.dumps(res))
"""


def probe_session(python: str, model_path: Path, providers: list[str], timeout: Optional[float] = None,
                  migraphx_cache: Optional[str] = None) -> dict:
    """Create a real ORT session with ``python`` (any venv) and report ``get_providers()[0]``.

    With MIGraphX requested the model may have to be compiled for the GPU
    first (1-3 min unless the cache under ``migraphx_cache`` has it), so the
    default timeout is longer then.
    """
    if timeout is None:
        timeout = 420 if "MIGraphXExecutionProvider" in providers else 180
    try:
        res = subprocess.run(
            [python, "-c", _PROBE_SCRIPT, str(model_path), json.dumps(providers), str(AMD_HELPER_PATH),
             str(migraphx_cache or "")],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    for line in res.stdout.splitlines():
        if line.startswith("@@PROBE@@"):
            data = json.loads(line[len("@@PROBE@@"):])
            if "falling back" in res.stdout.lower():
                # ORT's own notice: the requested EP failed and a later one took the session.
                data["fell_back"] = True
            # ORT prints why a provider failed (e.g. a missing libcudnn) on stderr.
            tail = [ln for ln in res.stderr.strip().splitlines() if ln.strip()][-3:]
            if tail:
                data["stderr_tail"] = tail
            return data
    return {"ok": False, "error": (res.stderr or res.stdout).strip()[-600:] or f"exit {res.returncode}"}


def probe_model_path() -> Optional[Path]:
    """Smallest verified model from the manifest, for a quick session probe."""
    try:
        entries = load_manifest().get("models", [])
    except (OSError, ValueError):
        return None
    paths = [MODELS_DIR / e["file"] for e in entries if (MODELS_DIR / e["file"]).is_file()]
    return min(paths, key=lambda p: p.stat().st_size) if paths else None


def evaluate_probe(probe: dict, accel: dict[str, Any]) -> list:
    """Warnings for a GPU machine whose real session ended up on the CPU."""
    wanted = preferred_gpu_providers(accel)
    if not wanted:
        return []
    if probe.get("ok") and probe.get("provider") in GPU_PROVIDERS and not probe.get("fell_back"):
        return []
    reason = (probe.get("error") or (probe.get("migraphx") or {}).get("reason")
              or "; ".join(probe.get("stderr_tail") or []) or "provider fell back to CPU")
    fix = _fix_run_sh()
    if in_container():
        fix = nvidia_driver_install_command() if "CUDAExecutionProvider" in wanted else "rebuild the image"
    elif "CUDAExecutionProvider" in wanted:
        drv = accel.get("nvidia", {}).get("driver_version")
        fix += (f"; if it persists the NVIDIA driver ({drv or 'unknown'}) may be too old for the CUDA 13 wheels "
                f"(needs >= 580): {nvidia_driver_install_command()}")
    return [_issue("gpu", f"GPU present ({', '.join(wanted)}) but a real session ran on "
                          f"{probe.get('provider') or 'nothing'}: {reason}", fix)]


def evaluate_live_status(status: dict, accel: dict[str, Any]) -> list:
    """Same check against the running detector's own status (no extra session)."""
    wanted = preferred_gpu_providers(accel)
    if not wanted or not status:
        return []
    provider = status.get("execution_provider") or status.get("provider") or ""
    if status.get("backend") == "hailo" or provider in GPU_PROVIDERS or "hailo" in str(provider).lower():
        return []
    compile_state = (status.get("gpu_compile") or {}).get("state")
    if compile_state == "compiling":
        models = ", ".join((status.get("gpu_compile") or {}).get("models") or []) or "the models"
        return [_issue(
            "gpu",
            f"the GPU is still being compiled for ({models}; first start with a cold cache, 1-3 min per "
            f"model); the person detector runs on {provider or 'nothing'} until it is done",
            None,
        )]
    return [_issue(
        "gpu",
        f"GPU present ({', '.join(wanted)}) but the person detector runs on "
        f"{provider or 'nothing'} ({status.get('error') or 'no GPU provider took'})",
        _fix_run_sh(),
    )]


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

# .env keys earlier releases read and this one does not. Settings accepts
# unknown keys silently (extra="allow"), so without this a stale line such as
# PERSON_MODEL_PATH=./models/yolov5n.onnx looks like it selects a model.
RETIRED_ENV_KEYS = {
    "PERSON_MODEL_PATH": "not read: the pose model is chosen per accelerator from POSE_MODEL_LADDER_GPU/_CPU "
                         "(then POSE_MODEL_GPU/_CPU); set POSE_MODEL_PATH only to pin one file",
}
# Read by the launcher, bootstrap or native libraries rather than Settings.
_EXTERNAL_ENV_PREFIXES = ("EDGE_", "ORT_", "MIGRAPHX_", "HIP_", "ROCM_", "HSA_", "AMD_", "MIOPEN_", "CUDA_",
                          "OMP_", "PYTHON", "XDG_", "UV_", "PIP_")


def _read_env_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            keys.append(key)
    return keys


def _model_file(value: str, relative_to_models: bool) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (MODELS_DIR / p) if relative_to_models else (EDGE_BACKEND_DIR / p)


def check_env() -> tuple[dict, list, list]:
    """Stale .env keys and model settings that point at files which do not exist."""
    warnings: list = []
    info: dict[str, Any] = {"env_file": None, "unused_keys": [], "missing_models": []}
    try:
        from app.config import Settings, settings  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return info, [], [_issue("config", f"cannot load app.config ({type(exc).__name__}: {exc})", None)]

    env_file = Path(str(Settings.model_config.get("env_file") or EDGE_BACKEND_DIR / ".env"))
    if env_file.is_file():
        info["env_file"] = str(env_file)
        try:
            keys = _read_env_keys(env_file)
        except OSError as exc:
            keys = []
            warnings.append(_issue("config", f"cannot read {env_file} ({exc.strerror or type(exc).__name__})", None))
        known = set(Settings.model_fields)
        for key in dict.fromkeys(keys):
            if key in RETIRED_ENV_KEYS:
                info["unused_keys"].append(key)
                warnings.append(_issue("config", f"{env_file.name}: {key} is {RETIRED_ENV_KEYS[key]}",
                                       f"delete the {key}= line from {env_file}"))
            elif key not in known and not key.startswith(_EXTERNAL_ENV_PREFIXES):
                info["unused_keys"].append(key)
                warnings.append(_issue("config", f"{env_file.name}: {key} is not a setting this version reads "
                                                 "(typo or retired key); it has no effect",
                                       f"check the spelling against .env.example, or delete the {key}= line"))

    # Model files the configuration names. Pose names resolve under MODELS_DIR
    # like the engine does; POSE_MODEL_PATH / OBJECT_MODEL_PATH are opened as
    # given (relative to edge_backend/, the service's working directory).
    named: list[tuple[str, str, bool]] = []
    for key in ("POSE_MODEL_GPU", "POSE_MODEL_CPU", "POSE_REFINER_MODEL"):
        named.append((key, getattr(settings, key, "") or "", True))
    for key in ("POSE_MODEL_LADDER_GPU", "POSE_MODEL_LADDER_CPU"):
        for name in (getattr(settings, key, "") or "").split(","):
            named.append((key, name.strip(), True))
    for key in ("POSE_MODEL_PATH", "OBJECT_MODEL_PATH"):
        named.append((key, getattr(settings, key, "") or "", False))
    for key, value, under_models in named:
        if not value:
            continue
        path = _model_file(value, under_models)
        if not path.is_file():
            info["missing_models"].append({"setting": key, "value": value})
            if key == "POSE_REFINER_MODEL" and str(getattr(settings, "POSE_REFINER", "auto") or "").strip().lower() \
                    in ("off", "0", "false", "no", ""):
                continue
            warnings.append(_issue("config", f"{key}={value} names a model file that does not exist ({path})",
                                   f"use one of the files in {MODELS_DIR} (see models/manifest.json)"))
    return info, [], warnings


def run_preflight(session_probe: bool = False) -> dict:
    """Run every check. Never installs, deletes or writes anything."""
    global _last_result
    t0 = time.perf_counter()
    errors: list = []
    warnings: list = []
    checks: dict[str, Any] = {}

    modules, e, w = check_modules()
    checks["modules"] = modules
    errors += e
    warnings += w

    accel = detect_accelerators()
    checks["accelerators"] = accel

    ort_info, e, w = check_onnxruntime(accel)
    checks["onnxruntime"] = ort_info
    errors += e
    warnings += w

    models, e, w = check_models()
    checks["models"] = models
    errors += e
    warnings += w

    storage, e, w = check_storage()
    checks["storage"] = storage
    errors += e
    warnings += w

    env_info, e, w = check_env()
    checks["config"] = env_info
    errors += e
    warnings += w

    if session_probe:
        model = probe_model_path()
        if model is not None:
            try:
                from app.config import settings  # noqa: PLC0415
                mgx_cache = str(settings.MIGRAPHX_CACHE_DIR)
            except Exception:  # noqa: BLE001 - standalone: the helper's default
                mgx_cache = None
            probe = probe_session(sys.executable, model, preferred_gpu_providers(accel), migraphx_cache=mgx_cache)
            probe["model"] = model.name
            checks["session_probe"] = probe
            if not ort_info.get("gpu_mismatch"):
                warnings += evaluate_probe(probe, accel)

    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "python": sys.executable,
        "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
        "timestamp": time.time(),
    }
    _last_result = result
    return result


def add_live_status(result: dict, status: dict) -> dict:
    """Fold the loaded detector's real provider into a preflight result (in place)."""
    accel = result.get("checks", {}).get("accelerators") or {}
    ort_info = result.get("checks", {}).get("onnxruntime") or {}
    result.setdefault("checks", {})["inference"] = {
        k: status.get(k) for k in ("available", "backend", "provider", "execution_provider", "model", "warmup_ms", "error")
    }
    if not ort_info.get("gpu_mismatch"):
        new = evaluate_live_status(status, accel)
        if new:
            ort_info["gpu_mismatch"] = True
            result["warnings"].extend(new)
    return result


def last_result() -> Optional[dict]:
    return _last_result


def summary(result: Optional[dict]) -> Optional[dict]:
    """Compact form for /api/v1/system/hardware."""
    if not result:
        return None
    checks = result.get("checks", {})
    ort_info = checks.get("onnxruntime", {})
    models = checks.get("models", [])
    return {
        "ok": result.get("ok"),
        "error_count": len(result.get("errors", [])),
        "warning_count": len(result.get("warnings", [])),
        "errors": [i["message"] for i in result.get("errors", [])],
        "warnings": [i["message"] for i in result.get("warnings", [])],
        "onnxruntime": ort_info.get("distributions"),
        "available_providers": ort_info.get("available_providers"),
        "flavour": ort_info.get("flavour"),
        "gpu_providers": ort_info.get("gpu_providers_available"),
        "gpu_mismatch": ort_info.get("gpu_mismatch"),
        "models_verified": sum(1 for m in models if m.get("status") in ("ok", "local_export")),
        "models_total": len(models),
        "timestamp": result.get("timestamp"),
    }


def log_report(result: dict, log: logging.Logger = logger) -> None:
    for issue in result.get("errors", []):
        log.error("PREFLIGHT ERROR [%s] %s%s", issue["check"], issue["message"],
                  f" | fix: {issue['fix']}" if issue.get("fix") else "")
    for issue in result.get("warnings", []):
        log.warning("PREFLIGHT WARNING [%s] %s%s", issue["check"], issue["message"],
                    f" | fix: {issue['fix']}" if issue.get("fix") else "")
    s = summary(result) or {}
    log.info(
        "Preflight %s in %.0f ms: onnxruntime=%s providers=%s models=%s/%s errors=%d warnings=%d",
        "ok" if result.get("ok") else "FAILED", result.get("duration_ms", 0), s.get("onnxruntime"),
        s.get("available_providers"), s.get("models_verified"), s.get("models_total"),
        s.get("error_count", 0), s.get("warning_count", 0),
    )


def format_report(result: dict) -> str:
    s = summary(result) or {}
    checks = result.get("checks", {})
    nv = checks.get("accelerators", {}).get("nvidia", {})
    amd = checks.get("accelerators", {}).get("amd", {})
    mgx = (checks.get("onnxruntime", {}).get("plugin_eps") or {}).get("migraphx") or {}
    lines = [
        f"preflight: {'OK' if result.get('ok') else 'FAILED'} ({result.get('duration_ms')} ms, {result.get('python')})",
        f"  onnxruntime : {s.get('onnxruntime')}  providers={s.get('available_providers')}",
        f"  nvidia      : {', '.join(nv.get('gpus') or []) or ('present, driver not loaded' if nv.get('present') else 'none')}",
        "  amd         : " + (("ROCm " + (", ".join(amd.get("gfx_targets") or []) or "target unknown")
                                + ("; MIGraphX plugin " + (mgx.get("versions") or {}).get("onnxruntime-ep-migraphx", "?")
                                   if mgx.get("installed") else "; no MIGraphX plugin"))
                               if amd.get("rocm_kfd") else ("present, no /dev/kfd" if amd.get("present") else "none")),
        f"  models      : {s.get('models_verified')}/{s.get('models_total')} verified",
    ]
    probe = checks.get("session_probe")
    if probe:
        lines.append(f"  session     : {probe.get('provider') or probe.get('error')} on {probe.get('model')}"
                     + (f" ({probe.get('infer_ms')} ms/inference)" if probe.get("infer_ms") else ""))
    for issue in result.get("errors", []):
        lines.append(f"  ERROR   [{issue['check']}] {issue['message']}")
        if issue.get("fix"):
            lines.append(f"          fix: {issue['fix']}")
    for issue in result.get("warnings", []):
        lines.append(f"  WARNING [{issue['check']}] {issue['message']}")
        if issue.get("fix"):
            lines.append(f"          fix: {issue['fix']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Check this edge CCTV installation (read-only).")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--session-probe", action="store_true",
                        help="also create a real ORT session in a child process and report its provider")
    args = parser.parse_args(argv)
    result = run_preflight(session_probe=args.session_probe)
    print(json.dumps(result, indent=2, default=str) if args.json else format_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if str(EDGE_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(EDGE_BACKEND_DIR))
    sys.exit(main())
