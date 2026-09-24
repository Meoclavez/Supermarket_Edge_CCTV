"""AMD GPU inference (ONNX Runtime MIGraphX plugin EP): detection, install
choices and honest provider reporting. ONNX Runtime and ROCm are faked; the
real GPU path is verified on hardware (see scripts/prewarm_inference.py)."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services import amd_migraphx as amd
from app.services import inference_backend as ib
from app.services import preflight as pf
from app.services.inference_backend import PersonDetector

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.py"


# --------------------------------------------------------------------------- #
# GPU target detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,name", [
    (120000, "gfx1200"), (120001, "gfx1201"), (110501, "gfx1151"), (100300, "gfx1030"),
    (90010, "gfx90a"), (90402, "gfx942"), (0, None), ("junk", None),
])
def test_gfx_target_version_to_llvm_name(value, name):
    assert amd.gfx_name(value) == name


def _node(root: Path, idx: int, gfx_version: int, simd: int) -> None:
    d = root / str(idx)
    d.mkdir(parents=True)
    (d / "properties").write_text(
        f"cpu_cores_count {0 if gfx_version else 16}\nsimd_count {simd}\n"
        f"gfx_target_version {gfx_version}\nvendor_id {4098 if gfx_version else 0}\n")


def test_topology_skips_cpu_and_puts_the_largest_gpu_first(tmp_path):
    _node(tmp_path, 0, 0, 0)             # CPU agent
    _node(tmp_path, 1, 100306, 4)        # small iGPU (gfx1036)
    _node(tmp_path, 2, 120000, 64)       # discrete RDNA4
    agents = amd.detect_gfx_targets(tmp_path)
    assert [a["gfx"] for a in agents] == ["gfx1200", "gfx1036"]


def test_gfx_override_and_missing_topology(tmp_path, monkeypatch):
    assert amd.detect_gfx_targets(tmp_path / "absent") == []
    monkeypatch.setenv("EDGE_ROCM_GFX", "gfx1101, gfx1030")
    assert amd.gfx_targets() == ["gfx1101", "gfx1030"]


def test_install_specs_follow_the_detected_target():
    specs = amd.install_specs("gfx1201")
    assert specs[0] == f"rocm[libraries,device-gfx1201]=={amd.ROCM_VERSION}"
    assert f"onnxruntime=={amd.ORT_VERSION}" in specs
    assert any(s.startswith("onnxruntime-ep-migraphx==") for s in specs)
    with pytest.raises(ValueError):
        amd.install_specs("rm -rf /")
    assert "--index-strategy" in amd.index_args(uv=True)
    assert "--index-strategy" not in amd.index_args(uv=False)


def test_compatible_release_check():
    assert amd._compatible_release_ok("onnxruntime~=1.29.0", "1.29.3") is True
    assert amd._compatible_release_ok("onnxruntime~=1.29.0", "1.30.0") is False
    assert amd._compatible_release_ok("onnxruntime~=1.29.0", "1.28.9") is False
    assert amd._compatible_release_ok("onnxruntime>=1.29", "1.30.0") is None


# --------------------------------------------------------------------------- #
# Symlinks and the compiled-program cache
# --------------------------------------------------------------------------- #

def test_symlinks_point_at_the_highest_plain_version(tmp_path):
    for name in ("libmigraphx_gpu.so.2017000", "libmigraphx_gpu.so.2016000",
                 "libmigraphx_gpu.so.2017000.0.0.hipv4-amdgcn-amd-amdhsa--gfx1200",
                 "libmigraphx_ref.so.2017000"):
        (tmp_path / name).write_bytes(b"x")
    assert amd.missing_symlinks(tmp_path) == ["libmigraphx_gpu.so", "libmigraphx_ref.so"]
    created = amd.ensure_symlinks(tmp_path)
    assert created == ["libmigraphx_gpu.so -> libmigraphx_gpu.so.2017000",
                       "libmigraphx_ref.so -> libmigraphx_ref.so.2017000"]
    assert (tmp_path / "libmigraphx_gpu.so").is_symlink()
    assert (tmp_path / "libmigraphx_gpu.so").resolve().name == "libmigraphx_gpu.so.2017000"
    assert amd.missing_symlinks(tmp_path) == []
    assert amd.ensure_symlinks(tmp_path) == []   # idempotent


def test_warm_manifest_tracks_model_content_and_shape(tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"weights-1")
    cache = tmp_path / "cache"
    cache.mkdir()
    assert not amd.is_warm(cache, model)
    assert amd.mark_warm(cache, model, "default", 12.5)
    assert amd.is_warm(cache, model)
    assert not amd.is_warm(cache, model, "b4")       # another input shape is another program
    model.write_bytes(b"weights-2")                  # re-exported model: cold again
    assert not amd.is_warm(cache, model)
    assert not amd.mark_warm(None, model)


def test_cache_key_separates_precision_and_versions():
    v = {"migraphx": "2.17.0+rocm10.0.0", "onnxruntime-ep-migraphx": "1.0.0+rocm10.0.0", "onnxruntime": "1.29.0"}
    assert amd.cache_key("gfx1200", False, v) != amd.cache_key("gfx1200", True, v)
    assert amd.cache_key("gfx1200", False, v) != amd.cache_key("gfx1201", False, v)
    assert "/" not in amd.cache_key("gfx1200", False, v) and "+" not in amd.cache_key("gfx1200", False, v)


# --------------------------------------------------------------------------- #
# Bootstrap flavour selection
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("edge_bootstrap_under_test", BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _accel(nvidia=False, kfd=False, gfx=()):
    return {
        "platform": "linux/x86_64", "container": False,
        "nvidia": {"present": nvidia, "pci": nvidia, "driver_loaded": nvidia, "gpus": ["RTX"] if nvidia else [],
                   "driver_version": None, "device_nodes": []},
        "amd": {"present": kfd, "rocm_kfd": kfd, "gfx_targets": list(gfx)},
        "intel": {"present": False}, "render_nodes": [],
        "hailo": {"present": False, "device_node": False, "runtime_installed": False},
        "apple_silicon": False,
    }


@pytest.fixture
def linux_x86(monkeypatch, bootstrap):
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")


def test_auto_picks_migraphx_for_an_amd_gpu_without_nvidia(bootstrap, linux_x86):
    assert bootstrap.choose_flavour("auto", _accel(kfd=True, gfx=["gfx1200"])) == "migraphx"


def test_auto_keeps_cuda_when_an_nvidia_gpu_is_present(bootstrap, linux_x86):
    assert bootstrap.choose_flavour("auto", _accel(nvidia=True, kfd=True, gfx=["gfx1200"])) == "gpu"
    assert bootstrap.choose_flavour("auto", _accel(nvidia=True)) == "gpu"


def test_auto_without_a_readable_amd_target_stays_default(bootstrap, linux_x86):
    assert bootstrap.choose_flavour("auto", _accel(kfd=True, gfx=[])) == "gpu"
    assert bootstrap.choose_flavour("auto", _accel()) == "gpu"


def test_ort_override_wins(bootstrap, linux_x86):
    assert bootstrap.choose_flavour("cpu", _accel(kfd=True, gfx=["gfx1200"])) == "cpu"
    assert "migraphx" in bootstrap.ORT_FLAVOURS


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def test_preferred_providers_for_amd_are_migraphx_only():
    assert pf.preferred_gpu_providers(_accel(kfd=True, gfx=["gfx1200"])) == ["MIGraphXExecutionProvider"]


def _runtime(plugin):
    return {"importable": True, "version": "1.29.0", "providers": ["CPUExecutionProvider"],
            "has_preload_dlls": False, "plugin_eps": {"migraphx": plugin}}


def test_amd_without_plugin_is_a_gpu_mismatch_with_the_install_command():
    info, errors, warnings = pf.evaluate_ort({"onnxruntime-gpu": "1.30.0"}, _runtime({"installed": False}),
                                             _accel(kfd=True, gfx=["gfx1200"]))
    assert info["gpu_mismatch"] is True and not errors
    assert "gfx1200" in warnings[0]["message"] and "--ort migraphx" in warnings[0]["fix"]


def test_amd_with_plugin_missing_symlinks_is_flagged():
    plugin = {"installed": True, "versions": {"onnxruntime": "1.29.0"}, "libs_dir": "/x/migraphx_libs",
              "missing_symlinks": ["libmigraphx_gpu.so"], "onnxruntime_compatible": True}
    _, _, warnings = pf.evaluate_ort({"onnxruntime": "1.29.0"}, _runtime(plugin), _accel(kfd=True, gfx=["gfx1200"]))
    assert "libmigraphx_gpu.so" in warnings[0]["message"]


def test_amd_with_working_plugin_lists_migraphx_and_no_warning():
    plugin = {"installed": True, "versions": {"onnxruntime": "1.29.0", "onnxruntime-ep-migraphx": "1.0.0"},
              "missing_symlinks": [], "onnxruntime_compatible": True}
    info, errors, warnings = pf.evaluate_ort({"onnxruntime": "1.29.0"}, _runtime(plugin),
                                             _accel(kfd=True, gfx=["gfx1200"]))
    assert not errors and not warnings
    assert "MIGraphXExecutionProvider" in info["gpu_providers_available"]
    assert info["flavour"] == "cpu+migraphx-plugin"


def test_probe_that_fell_back_is_not_a_gpu():
    accel = _accel(kfd=True, gfx=["gfx1200"])
    assert pf.evaluate_probe({"ok": True, "provider": "MIGraphXExecutionProvider"}, accel) == []
    assert pf.evaluate_probe({"ok": True, "provider": "MIGraphXExecutionProvider", "fell_back": True}, accel)
    w = pf.evaluate_probe({"ok": True, "provider": "CPUExecutionProvider",
                           "migraphx": {"ok": False, "reason": "no /dev/kfd"}}, accel)
    assert "no /dev/kfd" in w[0]["message"]


def test_live_status_while_compiling_says_so():
    status = {"provider": "cpu", "execution_provider": "CPUExecutionProvider", "backend": "onnxruntime",
              "gpu_compile": {"state": "compiling", "models": ["yolo26m-pose-544x960.onnx"]}}
    issue = pf.evaluate_live_status(status, _accel(kfd=True, gfx=["gfx1200"]))[0]
    assert "compiled" in issue["message"] and "runs on CPUExecutionProvider" in issue["message"]


# --------------------------------------------------------------------------- #
# Engine: device-path sessions, honest fallback, thread cap, background compile
# --------------------------------------------------------------------------- #

class _IO:
    def __init__(self, name, shape):
        self.name, self.shape, self.type = name, shape, "tensor(float)"


class _Session:
    def __init__(self, providers, so):
        self._providers, self.so = providers, so

    def get_providers(self):
        return list(self._providers)

    def get_modelmeta(self):
        class M:
            custom_metadata_map = {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"}
        return M()

    def get_inputs(self):
        return [_IO("images", [1, 3, 64, 64])]

    def get_outputs(self):
        return [_IO("output0", [1, 56, 84])]

    def run(self, _names, feed):
        return [np.zeros((1, 56, 84), np.float32)]


class _SessionOptions:
    def __init__(self):
        self.graph_optimization_level = None
        self.log_severity_level = 0
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.devices = None
        self.config: dict = {}

    def add_provider_for_devices(self, devices, options):
        self.devices = (list(devices), dict(options))

    def add_session_config_entry(self, key, value):
        self.config[key] = value


class _FakeOrt:
    """ORT stand-in that behaves like the real thing for plugin EPs: by name
    they are ignored, through the device path they take the session."""

    __version__ = "1.29.0"
    SessionOptions = _SessionOptions

    class GraphOptimizationLevel:
        ORT_ENABLE_ALL = 99

    def __init__(self, gpu_works=True, print_fallback=False, gate: threading.Event | None = None):
        self.gpu_works, self.print_fallback, self.gate = gpu_works, print_fallback, gate
        self.created: list[dict] = []

    def get_available_providers(self):
        return ["CPUExecutionProvider"]

    def set_default_logger_severity(self, _level):
        pass

    def InferenceSession(self, path, sess_options=None, providers=None):
        so = sess_options
        if providers is None and so is not None and so.devices:
            if self.gate is not None:
                self.gate.wait(10)           # "compiling"
            if self.print_fallback:
                print("EP Error something\nFalling back to ['CPUExecutionProvider'] and retrying.")
            provs = (["MIGraphXExecutionProvider", "CPUExecutionProvider"] if self.gpu_works
                     else ["CPUExecutionProvider"])
        else:
            names = [p[0] if isinstance(p, tuple) else p for p in (providers or ["CPUExecutionProvider"])]
            provs = [n for n in names if n != amd.PLUGIN_EP] or ["CPUExecutionProvider"]
        self.created.append({"model": Path(path).name, "providers": provs, "so": so,
                             "by_name": providers is not None})
        return _Session(provs, so)


class _Device:
    ep_name = "MIGraphXExecutionProvider"


@pytest.fixture
def amd_box(monkeypatch, tmp_path):
    """Models on disk, the plugin 'registered', cache under tmp_path."""
    for name in ("big-pose.onnx", "small-pose.onnx"):
        (tmp_path / name).write_bytes(name.encode())
    cache = tmp_path / "cache"
    cache.mkdir()
    for key, value in {"MODELS_DIR": tmp_path, "POSE_MODEL_GPU": "big-pose.onnx",
                       "POSE_MODEL_CPU": "small-pose.onnx", "POSE_MODEL_PATH": "",
                       "POSE_MODEL_LADDER_GPU": "", "POSE_MODEL_LADDER_CPU": "",
                       "INFERENCE_DISABLED_PROVIDERS": "", "POSE_REFINER": "off",
                       "OBJECT_DETECT_EVERY_N": 0, "POSE_LATENCY_BUDGET_MS": 1000.0,
                       "INFERENCE_CPU_THREADS": 6, "INFERENCE_CPU_SPINNING": False,
                       "MIGRAPHX_COMPILE_MODE": "background", "MIGRAPHX_CACHE_DIR": tmp_path}.items():
        monkeypatch.setattr(settings, key, value)
    info = {"ok": True, "reason": None, "devices": 1, "gfx": "gfx1200", "cache_dir": str(cache),
            "cache_warning": None}

    def fake_prepare(ort, cache_base=None, fp16=False):
        monkeypatch.setattr(amd, "_devices", [_Device()])
        monkeypatch.setattr(amd, "_prepared", dict(info))
        return dict(info)

    monkeypatch.setattr(amd, "applicable", lambda: True)
    monkeypatch.setattr(amd, "prepare", fake_prepare)
    # The real one runs scripts/prewarm_inference.py in a child process.
    monkeypatch.setattr(PersonDetector, "_compile_in_child", lambda self: (True, None))
    return tmp_path


def _init(monkeypatch, ort) -> PersonDetector:
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    d = PersonDetector(object_model_path="")
    d.initialise()
    return d


def _join_compile_thread():
    for t in threading.enumerate():
        if t.name == "migraphx-compile":
            t.join(10)


def test_migraphx_session_uses_the_device_path_not_the_name(amd_box, monkeypatch):
    monkeypatch.setattr(settings, "MIGRAPHX_COMPILE_MODE", "foreground")
    ort = _FakeOrt()
    d = _init(monkeypatch, ort)
    assert d.provider == "migraphx" and d.execution_provider == "MIGraphXExecutionProvider"
    assert d.device_name == "MIGRAPHX:0 gfx1200"
    gpu = [c for c in ort.created if c["providers"][0] == "MIGraphXExecutionProvider"]
    assert gpu and not any(c["by_name"] for c in gpu)
    assert isinstance(gpu[0]["so"].devices[0][0], _Device)
    assert d.status()["gpu_compile"]["state"] == "done"
    # Compiled once, recorded, so the next start knows the cache is warm.
    assert amd.is_warm(amd_box / "cache", amd_box / "big-pose.onnx")


def test_migraphx_that_lands_on_cpu_is_reported_as_cpu(amd_box, monkeypatch):
    monkeypatch.setattr(settings, "MIGRAPHX_COMPILE_MODE", "foreground")
    d = _init(monkeypatch, _FakeOrt(gpu_works=False))
    assert d.provider == "cpu" and d.model_path.name == "small-pose.onnx"
    mgx = next(a for a in d.provider_attempts if a["provider"] == "migraphx")
    assert mgx["ok"] is False and "landed on CPUExecutionProvider" in mgx["reason"]
    assert d.status()["provider"] == "cpu"


def test_fallback_notice_is_a_failure_even_if_the_provider_list_looks_right(amd_box, monkeypatch):
    monkeypatch.setattr(settings, "MIGRAPHX_COMPILE_MODE", "foreground")
    d = _init(monkeypatch, _FakeOrt(print_fallback=True))
    assert d.provider == "cpu"
    mgx = next(a for a in d.provider_attempts if a["provider"] == "migraphx")
    assert "fell back" in mgx["reason"]


def test_cpu_sessions_are_capped_and_do_not_spin(amd_box, monkeypatch):
    monkeypatch.setattr(settings, "INFERENCE_DISABLED_PROVIDERS", "migraphx")
    ort = _FakeOrt()
    d = _init(monkeypatch, ort)
    assert d.provider == "cpu"
    so = ort.created[-1]["so"]
    assert so.intra_op_num_threads == 4 and so.inter_op_num_threads == 1   # 6 - 6 // 3
    assert so.config["session.intra_op.allow_spinning"] == "0"
    assert d.status()["cpu_threads"]["budget"] == 6
    assert ib.cpu_threads_for("object") == 2


def test_thread_budget_defaults_to_half_the_usable_cpus(monkeypatch):
    monkeypatch.setattr(settings, "INFERENCE_CPU_THREADS", 0)
    monkeypatch.setattr(ib.os, "sched_getaffinity", lambda _pid: set(range(16)))
    assert ib.cpu_thread_budget() == 8
    assert ib.cpu_threads_for("pose") + ib.cpu_threads_for("object") <= 8


def test_cold_cache_serves_on_cpu_then_swaps_to_the_gpu(amd_box, monkeypatch):
    gate = threading.Event()
    ort = _FakeOrt(gate=gate)
    d = _init(monkeypatch, ort)
    # While compiling: honest CPU, with the state visible.
    st = d.status()
    assert st["provider"] == "cpu" and st["gpu_compile"]["state"] == "compiling"
    assert st["gpu_compile"]["models"] == ["big-pose.onnx"]
    assert d.detect(np.zeros((64, 64, 3), np.uint8)) == []   # CPU session answers meanwhile
    gate.set()
    _join_compile_thread()
    st = d.status()
    assert st["provider"] == "migraphx" and st["gpu_compile"]["state"] == "done"
    assert st["model"] == "big-pose.onnx" and st["device"] == "MIGRAPHX:0 gfx1200"
    assert d.detect(np.zeros((64, 64, 3), np.uint8)) == []
    assert amd.is_warm(amd_box / "cache", amd_box / "big-pose.onnx")


def test_warm_cache_loads_on_the_gpu_directly(amd_box, monkeypatch):
    amd.mark_warm(amd_box / "cache", amd_box / "big-pose.onnx")
    d = _init(monkeypatch, _FakeOrt())
    assert d.provider == "migraphx" and d.status()["gpu_compile"]["state"] == "done"
    assert not [t for t in threading.enumerate() if t.name == "migraphx-compile"]


def test_background_compile_failure_keeps_cpu_and_reports_it(amd_box, monkeypatch):
    ort = _FakeOrt(gpu_works=False)
    d = _init(monkeypatch, ort)
    _join_compile_thread()
    st = d.status()
    assert st["provider"] == "cpu" and st["gpu_compile"]["state"] == "failed"
    assert "CPUExecutionProvider" in st["gpu_compile"]["error"]


def test_cuda_path_is_untouched_by_the_plugin_code(monkeypatch, tmp_path):
    """No plugin on an NVIDIA box: CUDA sessions keep their options and no thread cap."""
    (tmp_path / "big-pose.onnx").write_bytes(b"x")
    monkeypatch.setattr(settings, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "big-pose.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_PATH", "")
    monkeypatch.setattr(amd, "_devices", [])

    captured = {}

    class Ort(_FakeOrt):
        def get_available_providers(self):
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        def InferenceSession(self, path, sess_options=None, providers=None):
            captured["providers"], captured["so"] = providers, sess_options
            return _Session([p[0] if isinstance(p, tuple) else p for p in providers], sess_options)

    d = PersonDetector(object_model_path="")
    sess, reason = d._create_session(Ort(), tmp_path / "big-pose.onnx", "CUDAExecutionProvider")
    assert reason is None
    assert captured["providers"][0][0] == "CUDAExecutionProvider"
    assert captured["providers"][0][1]["cudnn_conv_algo_search"] == "HEURISTIC"
    assert captured["so"].intra_op_num_threads == 0 and captured["so"].config == {}


def test_background_child_failure_keeps_cpu_and_reports_why(amd_box, monkeypatch):
    monkeypatch.setattr(PersonDetector, "_compile_in_child",
                        lambda self: (False, "compiler process ended on cpu (exit 2): migraphx: HIP failure"))
    d = _init(monkeypatch, _FakeOrt())
    _join_compile_thread()
    st = d.status()
    assert st["provider"] == "cpu" and st["gpu_compile"]["state"] == "failed"
    assert "HIP failure" in st["gpu_compile"]["error"]
    assert any("background compile failed" in (a.get("reason") or "") for a in st["provider_attempts"])


class _FakePopen:
    lines: list[str] = []
    rc = 0
    last_env: dict = {}

    def __init__(self, cmd, env=None, **_kw):
        type(self).last_env = env or {}
        self.cmd, self.pid, self.stdout = cmd, 4242, iter(self.lines)

    def wait(self):
        return self.rc


def test_compile_in_child_runs_the_prewarm_script_and_parses_its_verdict(monkeypatch, tmp_path):
    import json
    import subprocess

    monkeypatch.setattr(settings, "MIGRAPHX_CACHE_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    ok_summary = {"provider": "migraphx", "gpu_compile": {"compiled": ["a.onnx"]}}
    _FakePopen.lines = ["x WARNING Compiling a.onnx for the AMD GPU (MIGraphX...)\n",
                        "[WARN] [/app/AMDMIGraphX/src/x.cpp:1] chatter\n",
                        "@@PREWARM@@" + json.dumps(ok_summary) + "\n"]
    _FakePopen.rc = 0
    d = PersonDetector(model_path=tmp_path / "p.onnx", object_model_path="")
    assert d._compile_in_child() == (True, None)
    assert d.gpu_compile["compiled"] == ["a.onnx"] and "current" not in d.gpu_compile
    assert _FakePopen.last_env["MIGRAPHX_CACHE_DIR"] == str(tmp_path)
    assert _FakePopen.last_env["POSE_MODEL_PATH"] == str(tmp_path / "p.onnx")
    assert _FakePopen.last_env["OBJECT_MODEL_PATH"] == ""

    bad = {"provider": "cpu", "provider_attempts": [{"provider": "migraphx", "ok": False, "reason": "HIP failure"}]}
    _FakePopen.lines = ["@@PREWARM@@" + json.dumps(bad) + "\n"]
    _FakePopen.rc = 2
    ok, why = d._compile_in_child()
    assert not ok and "ended on cpu" in why and "HIP failure" in why
