"""Runtime hardware probe.

Two different questions are answered here and must not be confused:

* ``decoder_type`` / ``decoder_capability`` -- what video-decode hardware the
  box *has* (probed from nvidia-smi, /dev/dri, vainfo). This is a capability,
  not a statement that any stream is currently decoded on it.
* ``inference_backend`` / ``inference_provider`` -- what the person detector
  *actually initialised*. This is read from ``person_detector.status()`` and is
  never inferred from the presence of a GPU; a box with an NVIDIA card whose
  ONNX Runtime has no CUDA provider reports ``onnxruntime`` / ``cpu``.

Nothing here has a fabricated fallback: when RAM cannot be read it is
reported as unknown (``None``), not as a plausible number.
"""

import os
import shutil
import subprocess
from typing import Optional, Tuple

from ..models.schemas import HardwareProfile


def get_ram_info() -> Tuple[Optional[float], Optional[float]]:
    """(total_gb, available_gb), each None when it cannot be measured."""
    try:
        import psutil

        mem = psutil.virtual_memory()
        return round(mem.total / (1024 ** 3), 2), round(mem.available / (1024 ** 3), 2)
    except Exception:
        pass

    total_gb: Optional[float] = None
    avail_gb: Optional[float] = None
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_gb = round(int(line.split()[1]) / (1024 ** 2), 2)
                elif line.startswith("MemAvailable:"):
                    avail_gb = round(int(line.split()[1]) / (1024 ** 2), 2)
    except Exception:
        pass
    return total_gb, avail_gb


def probe_nvidia_gpu_name() -> Optional[str]:
    if not (shutil.which("nvidia-smi") or os.path.exists("/dev/nvidia0")):
        return None
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return None


def probe_nvidia_gpu_utilisation() -> Optional[float]:
    """Current GPU utilisation percent from nvidia-smi, or None when unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None


def probe_amd_gpu_busy() -> Optional[float]:
    """Busiest AMD GPU's ``gpu_busy_percent`` from the amdgpu driver, or None."""
    import glob

    values = []
    for path in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            with open(os.path.join(os.path.dirname(path), "vendor")) as f:
                if f.read().strip() != "0x1002":
                    continue
            with open(path) as f:
                values.append(float(f.read().strip()))
        except (OSError, ValueError):
            continue
    return max(values) if values else None


def probe_gpu_utilisation() -> Optional[float]:
    """NVIDIA (nvidia-smi) or AMD (sysfs) GPU utilisation percent, or None."""
    value = probe_nvidia_gpu_utilisation()
    return value if value is not None else probe_amd_gpu_busy()


def _probe_decoder() -> Tuple[str, str]:
    """(decoder_capability, device_name) from what the box physically has."""
    gpu_name = probe_nvidia_gpu_name()
    if gpu_name:
        return "cuda", gpu_name

    if os.path.exists("/dev/dri"):
        try:
            render_nodes = [f for f in os.listdir("/dev/dri") if f.startswith("renderD")]
        except OSError:
            render_nodes = []
        if render_nodes:
            vendor = None
            if shutil.which("vainfo"):
                try:
                    res = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=2)
                    output = res.stdout + res.stderr
                    if "iHD" in output or "Intel" in output:
                        vendor = "intel"
                    elif "radeonsi" in output or "AMD" in output:
                        vendor = "amd"
                except Exception:
                    pass
            if vendor is None:
                try:
                    with open("/proc/cpuinfo", "r") as f:
                        cpuinfo = f.read()
                    if "GenuineIntel" in cpuinfo:
                        vendor = "intel"
                    elif "AuthenticAMD" in cpuinfo:
                        vendor = "amd"
                except Exception:
                    vendor = None
            if vendor == "intel":
                return "vaapi_intel", "Intel iGPU (VA-API render node present)"
            if vendor == "amd":
                return "vaapi_amd", "AMD GPU/APU (VA-API render node present)"

    return "cpu", "CPU (no hardware decoder detected)"


def _inference_status() -> dict:
    """What the detector actually runs, read lazily on every call.

    ``person_detector.status()`` never loads a model; loading happens once in
    ``initialise_inference()`` from the application lifespan. Before that has
    run the backend is reported as ``not_initialised`` rather than guessed.
    """
    try:
        from .inference_backend import person_detector

        return person_detector.status()
    except Exception as exc:  # detector import failed entirely
        return {"available": False, "backend": "unavailable", "provider": "none",
                "device": None, "last_error": str(exc)}


class HardwareDetector:
    @staticmethod
    def detect_hardware() -> HardwareProfile:
        total_ram_gb, available_ram_gb = get_ram_info()
        cpu_cores = os.cpu_count()

        decoder, device_name = _probe_decoder()
        infer = _inference_status()

        # Ring-buffer / camera-count sizing is a recommendation derived from
        # measured RAM. Unknown RAM gets the most conservative tier.
        if total_ram_gb is None or total_ram_gb < 6.0:
            ring_buffer_seconds, max_cams = 3, 6
        elif total_ram_gb <= 16.0:
            ring_buffer_seconds, max_cams = 5, 12
        else:
            ring_buffer_seconds, max_cams = 10, 20

        return HardwareProfile(
            decoder_type=decoder,
            decoder_capability=decoder,
            inference_backend=infer.get("backend", "unavailable"),
            inference_provider=infer.get("provider", "none"),
            inference_available=bool(infer.get("available", False)),
            inference_device=infer.get("device"),
            inference_error=infer.get("last_error"),
            inference_execution_provider=infer.get("execution_provider"),
            inference_model=infer.get("model"),
            inference_gpu_compile=(infer.get("gpu_compile") or {}).get("state"),
            device_name=device_name,
            total_ram_gb=total_ram_gb,
            available_ram_gb=available_ram_gb,
            ring_buffer_seconds=ring_buffer_seconds,
            cpu_cores=cpu_cores,
            max_recommended_cameras=max_cams,
        )


def current_hardware_profile() -> HardwareProfile:
    """Re-probe on every call so the inference fields track the live detector."""
    return HardwareDetector.detect_hardware()


def __getattr__(name: str):
    """``hardware_profile`` is probed on first access, not at import time.

    Importing this module used to run nvidia-smi/vainfo and snapshot the
    detector status before the models were loaded, freezing an
    ``unavailable`` inference backend into the profile. Prefer
    ``current_hardware_profile()``, which re-probes on every call.
    """
    if name == "hardware_profile":
        return current_hardware_profile()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
