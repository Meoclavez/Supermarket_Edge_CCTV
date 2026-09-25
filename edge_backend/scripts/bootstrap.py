#!/usr/bin/env python3
"""Startup fixer for the edge backend: make the environment right, prove it, then start.

Each step checks first and only acts when something is actually wrong, so a
second run on a healthy machine changes nothing:

  1. venv          locate it (--venv / EDGE_VENV / <repo>/.venv), create or recreate if missing/broken
  2. installer     uv if available, else the venv's pip
  3. requirements  install requirements.txt (onnxruntime lines are handled by step 5)
  4. accelerators  nvidia-smi and /dev/nvidia*, /dev/kfd, /dev/dri, /dev/hailo0
  5. onnxruntime   pick the flavour, remove conflicting ones, reinstall the winner.
                   Flavours: gpu (NVIDIA, onnxruntime-gpu), migraphx (AMD: pinned
                   onnxruntime + ROCm + MIGraphX plugin EP from AMD's indexes, for
                   the GPU target read from /sys/class/kfd at run time, plus the
                   unversioned libmigraphx_*.so symlinks wheels cannot ship),
                   openvino, cpu
  6. models        scripts/fetch_models.py (verify; export missing ones in an isolated venv)
  6a. pre-warm     migraphx only: scripts/prewarm_inference.py loads the models the
                   service will load and compiles them for the GPU into the cache
                   (1-3 min per model the first time; seconds once cached)
  7. verify        create a real ORT session on a model, check get_providers()[0]
  8. preflight     app.services.preflight (read-only checks)
  8a. tunnel       only with --with-tunnel or when remote access is enabled in
                   Settings: fetch cloudflared for this OS/arch from the official
                   GitHub release into <repo>/bin/, verified against the
                   published SHA-256 (skipped when cloudflared is on PATH)
  9. start         exec uvicorn (skipped with --check-only)

It never uses sudo and never touches system packages. When the fix is at the
system level (for example an NVIDIA GPU without a loaded driver) it prints the
exact command for you to run.

Needs only the Python standard library, so it runs before the venv exists.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EDGE_BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = EDGE_BACKEND_DIR.parent
REQUIREMENTS = EDGE_BACKEND_DIR / "requirements.txt"
REQUIREMENTS_DEV = EDGE_BACKEND_DIR / "requirements-dev.txt"
IS_WINDOWS = os.name == "nt"

# ORT flavour -> (distribution, requirement spec). "migraphx" pins more than
# this; see ensure_migraphx() and app/services/amd_migraphx.py.
ORT_FLAVOURS = {
    "gpu": ("onnxruntime-gpu", "onnxruntime-gpu[cuda,cudnn]>=1.30"),
    "cpu": ("onnxruntime", "onnxruntime>=1.30"),
    "openvino": ("onnxruntime-openvino", "onnxruntime-openvino"),
    "migraphx": ("onnxruntime", "onnxruntime==1.29.0"),
}


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "edge_preflight_standalone", EDGE_BACKEND_DIR / "app" / "services" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


pf = _load_preflight()
amd = pf._amd()  # app/services/amd_migraphx.py, loaded by path (stdlib only)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.fixed: list[str] = []
        self.t0 = time.perf_counter()

    def line(self, tag: str, msg: str) -> None:
        print(f"[{tag:<4}] {msg}", flush=True)

    def ok(self, msg: str) -> None:
        self.line("ok", msg)

    def info(self, msg: str) -> None:
        self.line("info", msg)

    def fix(self, msg: str) -> None:
        self.fixed.append(msg)
        self.line("fix", msg)

    def warn(self, msg: str, cmd: str | None = None) -> None:
        self.warnings.append(msg)
        self.line("warn", msg)
        if cmd:
            print(f"       run: {cmd}", flush=True)

    def fail(self, msg: str, cmd: str | None = None) -> None:
        self.errors.append(msg)
        self.line("FAIL", msg)
        if cmd:
            print(f"       run: {cmd}", flush=True)


R = Report()


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def tail(res: subprocess.CompletedProcess, n: int = 12) -> str:
    text = (res.stderr or "") + (res.stdout or "")
    return "\n".join(text.strip().splitlines()[-n:])


# --------------------------------------------------------------------------- #
# 1. venv
# --------------------------------------------------------------------------- #

def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def python_works(py: Path) -> bool:
    if not py.exists():
        return False
    try:
        return sh([str(py), "-c", "import sys, encodings; sys.exit(0)"], timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_venv(venv: Path, uv: str | None) -> Path:
    py = venv_python(venv)
    if python_works(py):
        ver = sh([str(py), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"]).stdout.strip()
        R.ok(f"venv {venv} (Python {ver})")
        _warn_stale_scripts(venv)
        return py
    if venv.exists():
        # Typical cause on a rolling distro: the system Python it was built on was upgraded away.
        R.fix(f"venv {venv} exists but its interpreter does not run; recreating it")
    else:
        R.fix(f"creating venv {venv}")
    if uv:
        cmd = [uv, "venv", "--clear", "--python", sys.executable, str(venv)]
    else:
        cmd = [sys.executable, "-m", "venv", "--clear", str(venv)]
    res = sh(cmd)
    if res.returncode != 0 or not python_works(py):
        R.fail(f"could not create venv:\n{tail(res)}",
               "sudo pacman -S python   (Arch) / sudo apt install python3-venv   (Debian/Ubuntu)")
        finish(exit_code=1)
    return py


def _warn_stale_scripts(venv: Path) -> None:
    """A venv copied from elsewhere keeps console scripts whose shebang runs the OLD interpreter."""
    if IS_WINDOWS:
        return
    script = venv / "bin" / "uvicorn"
    try:
        head = script.read_text(errors="replace").splitlines()[:3]
    except OSError:
        return
    if not head or not head[0].startswith("#!"):
        return
    interp = head[0][2:].strip().split()[0] if head[0][2:].strip() else ""
    if interp == "/bin/sh" and len(head) > 1:
        # Long-path form written by pip/uv: #!/bin/sh + '''exec' '/path/python' "$0" "$@"
        m = re.search(r"exec'?\s+'?([^'\s]+)", head[1])
        interp = m.group(1) if m else ""
    if interp and not interp.startswith(str(venv)):
        R.warn(f"{script} runs {interp}, not this venv (the venv was copied or moved). "
               f"This launcher uses `{venv_python(venv)} -m uvicorn`, which is correct; do not call "
               f"{script} directly")


# --------------------------------------------------------------------------- #
# 2/3. installer + requirements
# --------------------------------------------------------------------------- #

def find_uv() -> str | None:
    if os.environ.get("EDGE_NO_UV"):
        return None
    for cand in (shutil.which("uv"), Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


class Installer:
    def __init__(self, py: Path, uv: str | None) -> None:
        self.py, self.uv = py, uv
        self.name = "uv" if uv else "pip"

    def ensure_pip(self) -> None:
        if self.uv:
            return
        if sh([str(self.py), "-m", "pip", "--version"]).returncode != 0:
            R.fix("venv has no pip; bootstrapping it with ensurepip")
            res = sh([str(self.py), "-m", "ensurepip", "--upgrade"])
            if res.returncode != 0:
                R.fail(f"ensurepip failed:\n{tail(res)}",
                       "curl -LsSf https://astral.sh/uv/install.sh | sh   (installs uv to ~/.local/bin, no sudo)")
                finish(exit_code=1)

    def pending(self, args: list[str]) -> list[str] | None:
        """Packages an install would add/change; [] if satisfied; None if the check itself failed."""
        if self.uv:
            res = sh([self.uv, "pip", "install", "--dry-run", "--python", str(self.py), *args])
            if res.returncode != 0:
                return None
            out = res.stderr + res.stdout
            if "Would make no changes" in out:
                return []
            return [m.group(1) for m in re.finditer(r"^\s*[+~]\s*(\S+)", out, re.M)] or ["(changes)"]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            report = fh.name
        try:
            res = sh([str(self.py), "-m", "pip", "install", "--dry-run", "--quiet", "--report", report, *args])
            if res.returncode != 0:
                return None
            data = json.loads(Path(report).read_text() or "{}")
            return [f"{i['metadata']['name']}=={i['metadata']['version']}" for i in data.get("install", [])]
        finally:
            Path(report).unlink(missing_ok=True)

    def install(self, args: list[str], reinstall: str | None = None) -> subprocess.CompletedProcess:
        if self.uv:
            extra = ["--reinstall-package", reinstall] if reinstall else []
            return sh([self.uv, "pip", "install", "--python", str(self.py), *extra, *args])
        if reinstall:
            res = sh([str(self.py), "-m", "pip", "install", "--force-reinstall", "--no-deps", *args])
            if res.returncode != 0:
                return res
        return sh([str(self.py), "-m", "pip", "install", *args])

    def uninstall(self, dists: list[str]) -> subprocess.CompletedProcess:
        if self.uv:
            return sh([self.uv, "pip", "uninstall", "--python", str(self.py), *dists])
        return sh([str(self.py), "-m", "pip", "uninstall", "-y", *dists])


def is_ort_line(line: str) -> bool:
    return bool(re.match(r"\s*onnxruntime", line))


def filtered_requirements(venv: Path, include_dev: bool) -> Path:
    """requirements.txt minus the onnxruntime lines (step 5 owns the flavour)."""
    lines = [ln for ln in REQUIREMENTS.read_text().splitlines() if not is_ort_line(ln)]
    if include_dev and REQUIREMENTS_DEV.exists():
        lines += [ln for ln in REQUIREMENTS_DEV.read_text().splitlines()
                  if not ln.strip().startswith("-r") and not is_ort_line(ln)]
    out = venv / ".edge-bootstrap" / "requirements.resolved.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


def ensure_requirements(inst: Installer, venv: Path, include_dev: bool) -> None:
    req = filtered_requirements(venv, include_dev)
    pending = inst.pending(["-r", str(req)])
    if pending == []:
        R.ok(f"requirements satisfied ({REQUIREMENTS.name}{' + dev' if include_dev else ''}, checked with {inst.name})")
        return
    R.fix(f"installing requirements with {inst.name}"
          + (f": {', '.join(pending[:12])}{' ...' if len(pending) > 12 else ''}" if pending else ""))
    res = inst.install(["-r", str(req)])
    if res.returncode != 0:
        R.fail(f"requirements install failed (offline? see below):\n{tail(res)}")


# --------------------------------------------------------------------------- #
# 4/5. accelerators + onnxruntime flavour
# --------------------------------------------------------------------------- #

def ort_default_flavour() -> str:
    """Mirror of the environment markers in requirements.txt."""
    x86 = platform.machine().lower() in ("x86_64", "amd64")
    return "gpu" if sys.platform in ("linux", "win32") and x86 else "cpu"


def describe_accelerators(accel: dict) -> None:
    nv = accel["nvidia"]
    parts = []
    if nv["present"]:
        parts.append(f"{', '.join(nv['gpus']) or 'NVIDIA GPU (driver not loaded)'}"
                     + (f" driver {nv['driver_version']}" if nv["driver_version"] else ""))
    if accel["amd"]["present"] or accel["amd"]["rocm_kfd"]:
        parts.append("AMD GPU" + (" with ROCm /dev/kfd" if accel["amd"]["rocm_kfd"] else ""))
    if accel["intel"]["present"]:
        parts.append("Intel GPU")
    if accel["render_nodes"]:
        parts.append(f"render nodes {' '.join(accel['render_nodes'])}")
    if accel["hailo"]["present"]:
        parts.append("Hailo NPU" + (" /dev/hailo0" if accel["hailo"]["device_node"] else " (no /dev/hailo0)"))
    if accel["apple_silicon"]:
        parts.append("Apple Silicon")
    R.info(f"accelerators: {'; '.join(parts) or 'none (CPU only)'}  [{accel['platform']}]")


def system_level_advice(accel: dict, py: Path) -> None:
    nv, hailo = accel["nvidia"], accel["hailo"]
    if nv["present"] and not nv["driver_loaded"]:
        R.warn("NVIDIA GPU on the PCI bus but no driver loaded: inference will run on the CPU until it is",
               pf.nvidia_driver_install_command())
    if hailo["present"] and not hailo["device_node"]:
        R.warn("Hailo PCIe device found but /dev/hailo0 is missing (driver not loaded)",
               "install hailort-pcie-driver from https://hailo.ai/developer-zone/ and reboot")
    elif hailo["device_node"] and not hailo["runtime_installed"]:
        R.warn("/dev/hailo0 present but HailoRT's Python runtime is not installed (it is not on PyPI)",
               f"uv pip install --python {py} ./hailort-<ver>-cp<py>-linux_x86_64.whl   "
               "(wheel from https://hailo.ai/developer-zone/)")
    if accel["amd"]["rocm_kfd"] and not nv["present"] and not accel["amd"].get("gfx_targets"):
        R.warn("AMD ROCm device (/dev/kfd) found but no GPU target could be read from "
               "/sys/class/kfd/kfd/topology (and rocm_agent_enumerator is absent): the MIGraphX flavour "
               "cannot pick its device libraries",
               "EDGE_ROCM_GFX=gfxNNNN <this command> --ort migraphx   (target from `rocminfo | grep gfx`)")
    elif accel["amd"]["present"] and not accel["amd"]["rocm_kfd"] and not nv["present"]:
        R.info("AMD GPU on the PCI bus but no /dev/kfd (amdgpu compute not available): CPU inference")


def installed_ort(py: Path) -> dict[str, str]:
    code = ("import json, importlib.metadata as m\nout={}\n"
            f"for d in {list(pf.ORT_DISTRIBUTIONS)!r}:\n"
            "    try: out[d]=m.version(d)\n    except m.PackageNotFoundError: pass\nprint(json.dumps(out))")
    res = sh([str(py), "-c", code])
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def ort_imports(py: Path) -> str | None:
    res = sh([str(py), "-c", "import onnxruntime as o; print(o.__version__)"])
    return None if res.returncode != 0 else res.stdout.strip()


def amd_flavour_applicable(accel: dict) -> bool:
    """An AMD GPU with ROCm compute and a readable target, and no NVIDIA GPU."""
    x86 = platform.machine().lower() in ("x86_64", "amd64")
    a = accel.get("amd", {})
    return (sys.platform.startswith("linux") and x86 and not accel["nvidia"]["present"]
            and bool(a.get("rocm_kfd")) and bool(a.get("gfx_targets")))


def choose_flavour(requested: str, accel: dict) -> str:
    if requested != "auto":
        return requested
    if amd_flavour_applicable(accel):
        R.info(f"AMD GPU {accel['amd']['gfx_targets'][0]} with ROCm and no NVIDIA GPU: onnxruntime flavour "
               f"migraphx (onnxruntime {amd.ORT_VERSION} + MIGraphX {amd.MIGRAPHX_VERSION} plugin EP, ~3.8 GB). "
               "`--ort cpu` keeps the small CPU-only build")
        return "migraphx"
    flavour = ort_default_flavour()
    if flavour == "gpu" and not accel["nvidia"]["present"]:
        R.info("no NVIDIA GPU: keeping onnxruntime-gpu as declared (its CPU provider is used; a GPU fitted later "
               "works without reinstalling). `--ort cpu` installs the plain build and saves ~1 GB of CUDA wheels")
        if accel["intel"]["present"] and accel["render_nodes"]:
            R.info("Intel GPU found: `--ort openvino` switches to onnxruntime-openvino (unverified on this build)")
    return flavour


def _migraphx_libs_dir(py: Path) -> Path | None:
    res = sh([str(py), "-c", "import importlib.util as u; s = u.find_spec('migraphx_libs'); "
                             "print(list(s.submodule_search_locations)[0] if s else '')"])
    out = res.stdout.strip().splitlines()
    return Path(out[-1]) if res.returncode == 0 and out and out[-1] else None


def ensure_migraphx(inst: Installer, py: Path, accel: dict) -> bool:
    """AMD: pinned onnxruntime + ROCm + MIGraphX + plugin EP, then the symlinks."""
    targets = accel.get("amd", {}).get("gfx_targets") or amd.gfx_targets()
    if not targets:
        R.fail("--ort migraphx: no AMD GPU target found (no /sys/class/kfd topology, no rocm_agent_enumerator)",
               "EDGE_ROCM_GFX=gfxNNNN <this command> --ort migraphx")
        return False
    gfx = targets[0]
    specs = amd.install_specs(gfx)
    idx = amd.index_args(bool(inst.uv))
    have = installed_ort(py)
    others = [d for d in have if d != "onnxruntime"]
    if others:
        R.fix(f"conflicting onnxruntime builds {', '.join(f'{d} {have[d]}' for d in others)}: uninstalling "
              f"(the MIGraphX plugin EP loads into plain onnxruntime {amd.ORT_VERSION})")
        res = inst.uninstall(others)
        if res.returncode != 0:
            R.fail(f"uninstall failed:\n{tail(res)}")
            return False
        # Removing the loser deleted files the winner shares; reinstall so its files are complete.
        res = inst.install([f"onnxruntime=={amd.ORT_VERSION}", *idx], reinstall="onnxruntime")
        if res.returncode != 0:
            R.fail(f"install of onnxruntime=={amd.ORT_VERSION} failed:\n{tail(res)}")
            return False
    pending = inst.pending([*specs, *idx])
    if pending == [] and ort_imports(py):
        R.ok(f"AMD MIGraphX stack for {gfx} installed ({', '.join(specs)})")
    else:
        R.fix(f"installing the AMD MIGraphX stack for {gfx} from AMD's indexes (~3.8 GB, several minutes)"
              + (f": {', '.join(pending[:8])}{' ...' if len(pending) > 8 else ''}" if pending else ""))
        res = inst.install([*specs, *idx])
        if res.returncode != 0:
            R.fail(f"install of the MIGraphX stack for {gfx} failed (no wheels for this Python/target, or "
                   f"offline):\n{tail(res)}", f"{REPO_DIR / 'run.sh'} --check-only --ort cpu   (CPU-only)")
            return False
    ldir = _migraphx_libs_dir(py)
    if ldir is None:
        R.fail("migraphx_libs is not importable after the install")
        return False
    for link in amd.ensure_symlinks(ldir):
        R.fix(f"symlink {ldir / link.split(' -> ')[0]} -> {link.split(' -> ')[1]} (MIGraphX dlopens the "
              "unversioned name; wheels cannot ship symlinks)")
    missing = amd.missing_symlinks(ldir)
    if missing:
        R.fail(f"{', '.join(missing)} still missing in {ldir}")
        return False
    version = ort_imports(py)
    if version != amd.ORT_VERSION:
        R.fail(f"onnxruntime {version} imports, but the MIGraphX plugin needs {amd.ORT_VERSION}")
        return False
    R.ok(f"onnxruntime {version} + MIGraphX plugin EP for {gfx}; unversioned libraries linked in {ldir.name}")
    return True


def ensure_onnxruntime(inst: Installer, py: Path, flavour: str, accel: dict | None = None) -> None:
    if flavour == "migraphx":
        if ensure_migraphx(inst, py, accel or pf.detect_accelerators()) or ort_imports(py):
            return
        R.warn("installing the CPU build instead so the service can run (on the CPU) meanwhile")
        flavour = "cpu"
    want_dist, want_spec = ORT_FLAVOURS[flavour]
    have = installed_ort(py)
    others = [d for d in have if d != want_dist]
    if others:
        R.fix(f"conflicting onnxruntime builds {', '.join(f'{d} {have[d]}' for d in others)}: uninstalling "
              f"(they share the onnxruntime package with {want_dist})")
        res = inst.uninstall(others)
        if res.returncode != 0:
            R.fail(f"uninstall failed:\n{tail(res)}")
            return
        # Removing the loser deleted files the winner shares; reinstall so its files are complete.
        R.fix(f"reinstalling {want_spec} so its files win")
        res = inst.install([want_spec], reinstall=want_dist)
        if res.returncode != 0:
            R.fail(f"install of {want_spec} failed:\n{tail(res)}")
            return
    else:
        pending = inst.pending([want_spec])
        if pending == [] and ort_imports(py):
            R.ok(f"onnxruntime flavour {want_dist} {have.get(want_dist, '')} (no conflicting builds)")
            return
        R.fix(f"installing {want_spec}" + (f": {', '.join(pending[:8])}" if pending else ""))
        res = inst.install([want_spec], reinstall=want_dist if want_dist in have else None)
        if res.returncode != 0:
            R.fail(f"install of {want_spec} failed:\n{tail(res)}")
            return
    version = ort_imports(py)
    if version:
        R.ok(f"onnxruntime {version} ({want_dist}) imports")
    else:
        R.fail("onnxruntime still does not import after reinstall", f"{REPO_DIR / 'run.sh'} --check-only")


# --------------------------------------------------------------------------- #
# 6/7. models + session verification
# --------------------------------------------------------------------------- #

def ensure_models(py: Path, cache_dir: str | None) -> None:
    cmd = [str(py), str(EDGE_BACKEND_DIR / "scripts" / "fetch_models.py"), "--quiet"]
    if cache_dir:
        cmd += ["--cache-dir", cache_dir]
    res = subprocess.run(cmd, text=True)  # streams its own [fix]/[warn] lines
    if res.returncode == 0:
        try:
            n = len(pf.load_manifest().get("models", []))
        except (OSError, ValueError):
            n = 0
        R.ok(f"models verified against manifest.json ({n} files)")
    else:
        R.fail("one or more models could not be verified or restored (see above)",
               f"{py} {EDGE_BACKEND_DIR / 'scripts' / 'fetch_models.py'}")


def prewarm_gpu(py: Path) -> dict:
    """Compile the models the service will load for the AMD GPU, as the service would."""
    script = EDGE_BACKEND_DIR / "scripts" / "prewarm_inference.py"
    R.info("pre-compiling the models the service will load for the AMD GPU (MIGraphX): 1-3 min per model "
           "the first time, seconds when cached")
    started = time.perf_counter()
    summary: dict = {}
    try:
        proc = subprocess.Popen([str(py), str(script), "--require-gpu"], cwd=EDGE_BACKEND_DIR, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        R.fail(f"pre-warm could not start: {exc}")
        return summary
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("@@PREWARM@@"):
            try:
                summary = json.loads(line[len("@@PREWARM@@"):])
            except ValueError:
                pass
        elif (re.search(r"Compiling|GPU|MIGraphX|model chosen|ready|ERROR|WARNING", line)
              and "/AMDMIGraphX/" not in line):  # MIGraphX's own compiler chatter
            print(f"       {line.rstrip()}", flush=True)
    rc = proc.wait()
    secs = time.perf_counter() - started
    gc = summary.get("gpu_compile") or {}
    if rc == 0 and summary.get("provider") == "migraphx":
        R.ok(f"GPU pre-warm: {summary.get('model')} {summary.get('input_size')} on {summary.get('device')} "
             f"({summary.get('warmup_steady_ms')} ms/frame), refiner "
             f"{'on' if (summary.get('refiner') or {}).get('enabled') else 'off'}, objects on "
             f"{summary.get('object_provider')}; compiled {gc.get('compiled') or 'nothing new (cache warm)'} "
             f"in {secs:.0f} s; cache {(summary.get('migraphx') or {}).get('cache_dir')}")
    else:
        R.fail(f"GPU pre-warm ended on {summary.get('provider') or 'nothing'} (exit {rc}): "
               + "; ".join(f"{a.get('provider')}: {a.get('reason')}" for a in summary.get("provider_attempts") or []
                           if not a.get("ok"))[:600])
    return summary


def verify_session(py: Path, accel: dict, migraphx_cache: str | None = None) -> dict:
    model = pf.probe_model_path()
    if model is None:
        R.fail("no model file available to verify inference with")
        return {}
    wanted = pf.preferred_gpu_providers(accel)
    probe = pf.probe_session(str(py), model, wanted, migraphx_cache=migraphx_cache)
    provider = probe.get("provider")
    if not probe.get("ok"):
        R.fail(f"inference session could not be created on {model.name}: {probe.get('error')}")
        return probe
    detail = f"{provider} on {model.name} (session {probe.get('session_ms')} ms, {probe.get('infer_ms')} ms/inference)"
    if provider in pf.GPU_PROVIDERS and not probe.get("fell_back"):
        R.ok(f"inference verified: {detail}")
    elif wanted:
        reason = ((probe.get("migraphx") or {}).get("reason") or "; ".join(probe.get("stderr_tail") or [])
                  or f"requested {wanted}, available {probe.get('available')}")
        cmd = None
        if "CUDAExecutionProvider" in wanted:
            drv = accel["nvidia"].get("driver_version")
            cmd = (f"nvidia-smi   # driver {drv or '?'}; the CUDA 13 wheels need >= 580. "
                   f"If older: {pf.nvidia_driver_install_command()}")
        R.fail(f"GPU present but the session fell back to {detail}. Reason: {reason}", cmd)
    else:
        R.ok(f"inference verified: {detail} (no GPU provider applicable on this machine)")
    return probe


# --------------------------------------------------------------------------- #
# 8. preflight
# --------------------------------------------------------------------------- #

def run_preflight(py: Path) -> dict:
    res = sh([str(py), "-m", "app.services.preflight", "--json"], cwd=EDGE_BACKEND_DIR)
    try:
        start = res.stdout.index("{")
        result = json.loads(res.stdout[start:])
    except ValueError:
        R.fail(f"preflight did not run:\n{tail(res)}")
        return {}
    for issue in result.get("errors", []):
        R.fail(f"preflight [{issue['check']}] {issue['message']}", issue.get("fix"))
    for issue in result.get("warnings", []):
        R.warn(f"preflight [{issue['check']}] {issue['message']}", issue.get("fix"))
    if result.get("ok") and not result.get("warnings"):
        R.ok(f"preflight clean ({result.get('duration_ms')} ms)")
    return result


# --------------------------------------------------------------------------- #
# Remote access: cloudflared binary (optional)
# --------------------------------------------------------------------------- #

CLOUDFLARED_RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
BIN_DIR = REPO_DIR / "bin"


def remote_access_enabled() -> bool:
    """Read Settings -> Remote access from the app database (read-only)."""
    import sqlite3

    candidates = [os.environ.get("DATABASE_PATH"), str(REPO_DIR / "storage" / "cctv_core.db"), "/app/cctv_core.db"]
    for path in filter(None, candidates):
        if not Path(path).is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute("SELECT value FROM system_setup WHERE key = 'remote_access'").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row:
            try:
                return bool(json.loads(row[0]).get("enabled"))
            except (ValueError, AttributeError):
                return False
        return False
    return False


def cloudflared_asset_name() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "armhf", "armv6l": "arm", "i386": "386", "i686": "386", "x86": "386"}.get(machine)
    if not arch:
        return None
    if system == "linux":
        return f"cloudflared-linux-{arch}"
    if system == "darwin" and arch in ("amd64", "arm64"):
        return f"cloudflared-darwin-{arch}.tgz"
    if system == "windows" and arch in ("amd64", "386"):
        return f"cloudflared-windows-{arch}.exe"
    return None


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "edge-cctv-bootstrap",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 (https only)
        return res.read()


def published_sha256(release: dict, asset_name: str) -> tuple[str | None, str | None]:
    """(checksum listed in the release notes, digest GitHub reports for the asset)."""
    listed = None
    m = re.search(rf"^\s*{re.escape(asset_name)}:\s*([0-9a-f]{{64}})\s*$", release.get("body") or "", re.M)
    if m:
        listed = m.group(1)
    api = None
    for asset in release.get("assets") or []:
        if asset.get("name") == asset_name and str(asset.get("digest") or "").startswith("sha256:"):
            api = asset["digest"].split(":", 1)[1]
    return listed, api


def ensure_cloudflared(requested: bool) -> None:
    import hashlib

    wanted = requested or remote_access_enabled()
    on_path = shutil.which("cloudflared")
    exe = BIN_DIR / ("cloudflared.exe" if IS_WINDOWS else "cloudflared")
    if not wanted:
        if exe.is_file() or on_path:
            R.ok(f"cloudflared present ({on_path or exe})")
        return
    if on_path:
        R.ok(f"cloudflared on PATH: {on_path}")
        return
    if exe.is_file():
        res = sh([str(exe), "--version"])
        if res.returncode == 0:
            R.ok(f"cloudflared: {res.stdout.strip().splitlines()[0] if res.stdout.strip() else exe}")
            return
        R.warn(f"{exe} does not run; downloading it again")
    asset = cloudflared_asset_name()
    if not asset:
        R.warn(f"No cloudflared build for {platform.system()} {platform.machine()}; remote access "
               "cannot use a Cloudflare tunnel on this machine",
               "see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        return
    try:
        release = json.loads(_http_get(CLOUDFLARED_RELEASE_API, timeout=20))
        listed, api_digest = published_sha256(release, asset)
        url = next((a["browser_download_url"] for a in release.get("assets") or [] if a.get("name") == asset), None)
        if not url or not url.startswith("https://github.com/cloudflare/cloudflared/releases/download/"):
            R.warn(f"cloudflared release {release.get('tag_name')} has no asset {asset}")
            return
        R.info(f"downloading {asset} ({release.get('tag_name')})")
        data = _http_get(url, timeout=300)
    except Exception as exc:  # offline, rate-limited, ...
        R.warn(f"cloudflared download failed: {exc}", f"{REPO_DIR / 'run.sh'} --with-tunnel --check-only")
        return
    digest = hashlib.sha256(data).hexdigest()
    expected = [d for d in (listed, api_digest) if d]
    if not expected:
        R.warn(f"no published checksum for {asset}; installed unverified (sha256 {digest})")
    elif any(d != digest for d in expected):
        R.fail(f"cloudflared checksum mismatch for {asset}: got {digest}, published {expected[0]}. Not installed.")
        return
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".tgz"):
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"), None)
            if member is None:
                R.fail(f"{asset} does not contain a cloudflared binary")
                return
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
    tmp = exe.with_suffix(".download")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, exe)
    res = sh([str(exe), "--version"])
    if res.returncode != 0:
        R.fail(f"downloaded cloudflared does not run: {tail(res)}")
        return
    R.fix(f"cloudflared installed: {exe} ({res.stdout.strip().splitlines()[0] if res.stdout.strip() else 'ok'}"
          f"; sha256 verified against {'release notes + GitHub digest' if len(expected) == 2 else 'published checksum'})"
          if expected else f"cloudflared installed: {exe}")


# --------------------------------------------------------------------------- #

def finish(exit_code: int | None = None, provider: str | None = None) -> None:
    secs = time.perf_counter() - R.t0
    state = "READY" if not R.errors else "NOT READY"
    print(f"---- {state}: {len(R.fixed)} fixed, {len(R.warnings)} warnings, {len(R.errors)} errors"
          + (f", inference on {provider}" if provider else "") + f" ({secs:.1f}s)", flush=True)
    if exit_code is not None:
        sys.exit(exit_code)


def main() -> None:
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Fix, verify and start the edge CCTV backend.",
                                 epilog="Arguments after `--` are passed to uvicorn.")
    ap.add_argument("--venv", default=os.environ.get("EDGE_VENV") or str(REPO_DIR / ".venv"),
                    help="virtualenv to use/create (env EDGE_VENV; default <repo>/.venv)")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", default=os.environ.get("PORT", "8000"))
    ap.add_argument("--check-only", action="store_true", help="fix and verify, but do not start uvicorn")
    ap.add_argument("--ort", choices=["auto", *ORT_FLAVOURS], default=os.environ.get("EDGE_ORT", "auto"),
                    help="onnxruntime flavour (env EDGE_ORT; default auto)")
    ap.add_argument("--dev", action="store_true", help="also install requirements-dev.txt (pytest)")
    ap.add_argument("--model-cache-dir", default=None,
                    help="where models are exported (default ~/.cache/edge-cctv/model-export)")
    ap.add_argument("--skip-models", action="store_true", help="do not verify/export models")
    ap.add_argument("--skip-prewarm", action="store_true",
                    help="migraphx: do not pre-compile the models for the GPU (the service then compiles "
                         "them in the background on its first start, running on the CPU meanwhile)")
    ap.add_argument("--force", action="store_true", help="start uvicorn even if checks report errors")
    ap.add_argument("--with-tunnel", action="store_true",
                    help="fetch cloudflared into <repo>/bin for remote access (done automatically when "
                         "remote access is enabled in Settings)")
    args = ap.parse_args(argv)

    venv = Path(args.venv).expanduser().absolute()
    uv = find_uv()
    print(f"== edge-cctv bootstrap: {REPO_DIR}", flush=True)
    py = ensure_venv(venv, uv)
    inst = Installer(py, uv)
    inst.ensure_pip()
    R.info(f"installer: {inst.name}" + (f" ({uv})" if uv else " (uv not found; install it for faster, "
                                          "more reliable installs: curl -LsSf https://astral.sh/uv/install.sh | sh)"))
    ensure_requirements(inst, venv, args.dev)

    accel = pf.detect_accelerators()
    describe_accelerators(accel)
    system_level_advice(accel, py)
    flavour = choose_flavour(args.ort, accel)
    if flavour != "gpu" and accel["nvidia"]["driver_loaded"]:
        R.warn(f"--ort {flavour} on a machine with an NVIDIA GPU: inference will not use the GPU")
    ensure_onnxruntime(inst, py, flavour, accel)

    if not args.skip_models:
        ensure_models(py, args.model_cache_dir)
    mgx_cache = None
    if flavour == "migraphx" and not args.skip_prewarm:
        warm = prewarm_gpu(py)
        cache_dir = (warm.get("migraphx") or {}).get("cache_dir")
        mgx_cache = str(Path(cache_dir).parent) if cache_dir else None
    probe = verify_session(py, accel, mgx_cache)
    run_preflight(py)
    ensure_cloudflared(args.with_tunnel)

    if args.check_only:
        finish(exit_code=1 if R.errors else 0, provider=probe.get("provider"))
    if R.errors and not args.force:
        print("Not starting: fix the errors above (or pass --force to start anyway).", flush=True)
        finish(exit_code=1, provider=probe.get("provider"))
    finish(provider=probe.get("provider"))

    # Backstop for a response that never ends: uvicorn otherwise waits for
    # every open connection before running the app's shutdown (see
    # app/services/shutdown_signal.py for the primary fix).
    graceful = [] if any(a.startswith("--timeout-graceful-shutdown") for a in passthrough) \
        else ["--timeout-graceful-shutdown", "5"]
    # No "server: uvicorn" banner on every response.
    banner = [] if "--server-header" in passthrough or "--no-server-header" in passthrough \
        else ["--no-server-header"]
    cmd = [str(py), "-m", "uvicorn", "app.main:app", "--host", str(args.host), "--port", str(args.port),
           *graceful, *banner, *passthrough]
    print(f"== exec {' '.join(cmd)}  (cwd {EDGE_BACKEND_DIR})", flush=True)
    os.chdir(EDGE_BACKEND_DIR)
    if IS_WINDOWS:
        sys.exit(subprocess.call(cmd))
    os.execv(str(py), cmd)


if __name__ == "__main__":
    main()
