"""Tests for the read-only startup preflight (app/services/preflight.py)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services import preflight as pf


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

def _accel(nvidia_driver=False, nvidia_pci=False, kfd=False, hailo_node=False, hailo_rt=False):
    return {
        "platform": "linux/x86_64",
        "container": False,
        "nvidia": {"present": nvidia_driver or nvidia_pci, "pci": nvidia_pci, "driver_loaded": nvidia_driver,
                   "gpus": ["Test GPU"] if nvidia_driver else [], "driver_version": "600.0" if nvidia_driver else None,
                   "device_nodes": ["/dev/nvidia0"] if nvidia_driver else []},
        "amd": {"present": kfd, "rocm_kfd": kfd},
        "intel": {"present": False},
        "render_nodes": [],
        "hailo": {"present": hailo_node, "device_node": hailo_node, "runtime_installed": hailo_rt},
        "apple_silicon": False,
    }


def _runtime(providers, importable=True):
    return {"importable": importable, "version": "1.30.0", "providers": providers, "has_preload_dlls": True}


def _checks(issues):
    return [i["check"] for i in issues]


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #

def test_run_preflight_structure():
    result = pf.run_preflight()
    assert set(("ok", "errors", "warnings", "checks", "duration_ms", "python")) <= set(result)
    assert result["ok"] is (len(result["errors"]) == 0)
    for issue in result["errors"] + result["warnings"]:
        assert set(issue) == {"check", "message", "fix"}
        assert isinstance(issue["message"], str) and issue["message"]
    for key in ("modules", "accelerators", "onnxruntime", "models", "storage"):
        assert key in result["checks"], key
    assert pf.last_result() is result
    json.dumps(result, default=str)  # must be serialisable for the API


def test_summary_shape():
    s = pf.summary(pf.run_preflight())
    for key in ("ok", "error_count", "warning_count", "onnxruntime", "available_providers",
                "gpu_mismatch", "models_verified", "models_total"):
        assert key in s
    assert pf.summary(None) is None


def test_preflight_does_not_create_files():
    before = sorted(p.name for p in pf.MODELS_DIR.iterdir())
    pf.run_preflight()
    assert sorted(p.name for p in pf.MODELS_DIR.iterdir()) == before


# --------------------------------------------------------------------------- #
# onnxruntime flavour / GPU mismatch
# --------------------------------------------------------------------------- #

def test_gpu_present_but_cpu_only_provider_is_flagged_with_fix():
    info, errors, warnings = pf.evaluate_ort(
        {"onnxruntime": "1.30.0"}, _runtime(["AzureExecutionProvider", "CPUExecutionProvider"]),
        _accel(nvidia_driver=True), python="/venv/bin/python")
    assert info["gpu_mismatch"] is True
    assert errors == []
    gpu = [w for w in warnings if w["check"] == "gpu"]
    assert len(gpu) == 1
    assert "CPU-only" in gpu[0]["message"]
    fix = gpu[0]["fix"]
    assert "run.sh --check-only" in fix
    assert "uv pip uninstall --python /venv/bin/python onnxruntime" in fix
    assert "onnxruntime-gpu[cuda,cudnn]" in fix


def test_gpu_with_cuda_provider_is_not_flagged():
    info, errors, warnings = pf.evaluate_ort(
        {"onnxruntime-gpu": "1.30.0"},
        _runtime(["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]),
        _accel(nvidia_driver=True))
    assert info["gpu_mismatch"] is False
    assert errors == [] and warnings == []


def test_no_gpu_cpu_only_is_fine():
    info, errors, warnings = pf.evaluate_ort({"onnxruntime": "1.30.0"}, _runtime(["CPUExecutionProvider"]), _accel())
    assert not info["gpu_mismatch"] and errors == [] and warnings == []


def test_conflicting_distributions_are_an_error():
    info, errors, _ = pf.evaluate_ort(
        {"onnxruntime": "1.30.0", "onnxruntime-gpu": "1.30.0"}, _runtime(["CPUExecutionProvider"]),
        _accel(nvidia_driver=True), python="/venv/bin/python")
    assert info["conflict"] is True
    assert _checks(errors) == ["onnxruntime"]
    assert "conflicting" in errors[0]["message"]
    assert "uninstall --python /venv/bin/python onnxruntime " in errors[0]["fix"] + " "


def test_nvidia_on_pci_without_driver_prints_system_command():
    info, _, warnings = pf.evaluate_ort({"onnxruntime-gpu": "1.30.0"}, _runtime(["CPUExecutionProvider"]),
                                        _accel(nvidia_pci=True))
    assert info["gpu_mismatch"] is True
    assert any("driver is not loaded" in w["message"] for w in warnings)
    driver = [w for w in warnings if "driver is not loaded" in w["message"]][0]
    assert "nvidia" in driver["fix"].lower()   # printed for the user, never executed


def test_ort_not_importable_is_an_error():
    _, errors, _ = pf.evaluate_ort({}, _runtime([], importable=False), _accel())
    assert _checks(errors) == ["onnxruntime"]


def test_hailo_node_without_runtime_warns():
    _, _, warnings = pf.evaluate_ort({"onnxruntime-gpu": "1.30.0"}, _runtime(["CPUExecutionProvider"]),
                                     _accel(hailo_node=True))
    assert "hailo" in _checks(warnings)


def test_run_preflight_detects_mismatch_with_mocked_providers(monkeypatch):
    monkeypatch.setattr(pf, "detect_accelerators", lambda: _accel(nvidia_driver=True))
    monkeypatch.setattr(pf, "_ort_runtime", lambda: _runtime(["CPUExecutionProvider"]))
    monkeypatch.setattr(pf, "installed_ort_distributions", lambda: {"onnxruntime": "1.30.0"})
    result = pf.run_preflight()
    assert result["checks"]["onnxruntime"]["gpu_mismatch"] is True
    assert "gpu" in _checks(result["warnings"])
    assert pf.summary(result)["gpu_mismatch"] is True


def test_live_status_on_cpu_with_gpu_present_is_flagged():
    result = {"ok": True, "errors": [], "warnings": [],
              "checks": {"accelerators": _accel(nvidia_driver=True), "onnxruntime": {"gpu_mismatch": False}}}
    pf.add_live_status(result, {"available": True, "backend": "onnxruntime", "provider": "CPUExecutionProvider",
                                "execution_provider": "CPUExecutionProvider", "model": "yolo26n-pose.onnx",
                                "error": None})
    assert _checks(result["warnings"]) == ["gpu"]
    assert result["checks"]["inference"]["provider"] == "CPUExecutionProvider"

    ok = {"ok": True, "errors": [], "warnings": [],
          "checks": {"accelerators": _accel(nvidia_driver=True), "onnxruntime": {"gpu_mismatch": False}}}
    pf.add_live_status(ok, {"available": True, "execution_provider": "CUDAExecutionProvider"})
    assert ok["warnings"] == []


def test_probe_fallback_to_cpu_is_flagged():
    warnings = pf.evaluate_probe({"ok": True, "provider": "CPUExecutionProvider"}, _accel(nvidia_driver=True))
    assert _checks(warnings) == ["gpu"]
    assert pf.evaluate_probe({"ok": True, "provider": "CUDAExecutionProvider"}, _accel(nvidia_driver=True)) == []
    assert pf.evaluate_probe({"ok": True, "provider": "CPUExecutionProvider"}, _accel()) == []


# --------------------------------------------------------------------------- #
# Manifest verification
# --------------------------------------------------------------------------- #

def _write_manifest(tmp: Path, files: dict[str, bytes], required=True, io=None) -> Path:
    models = []
    for name, data in files.items():
        (tmp / name).write_bytes(data)
        models.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                       "task": "pose", "required": required, "source": name.replace(".onnx", ".pt"),
                       "io": io})
    path = tmp / "manifest.json"
    path.write_text(json.dumps({"schema": 1, "models": models}))
    return path


def test_manifest_verification_ok(tmp_path):
    manifest = _write_manifest(tmp_path, {"a.onnx": b"model-a", "b.onnx": b"model-b"})
    results = pf.verify_models(manifest)
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert pf.evaluate_models(results) == ([], [])


def test_manifest_verification_hash_mismatch_and_missing(tmp_path):
    manifest = _write_manifest(tmp_path, {"a.onnx": b"model-a", "b.onnx": b"model-b"})
    (tmp_path / "a.onnx").write_bytes(b"model-X")     # same size, different bytes
    (tmp_path / "b.onnx").unlink()
    results = {r["file"]: r for r in pf.verify_models(manifest)}
    assert results["a.onnx"]["status"] == "hash_mismatch"
    assert results["a.onnx"]["io_signature_matches"] is False   # not a real ONNX file
    assert results["b.onnx"]["status"] == "missing"
    errors, warnings = pf.evaluate_models(list(results.values()))
    assert len(errors) == 1 and "b.onnx" in errors[0]["message"] and "fetch_models.py" in errors[0]["fix"]
    assert len(warnings) == 1 and "a.onnx" in warnings[0]["message"]


def test_manifest_size_mismatch_and_optional_missing(tmp_path):
    manifest = _write_manifest(tmp_path, {"a.onnx": b"model-a"}, required=False)
    (tmp_path / "a.onnx").write_bytes(b"longer model")
    assert pf.verify_models(manifest)[0]["status"] == "size_mismatch"
    (tmp_path / "a.onnx").unlink()
    errors, warnings = pf.evaluate_models(pf.verify_models(manifest))
    assert errors == [] and len(warnings) == 1   # optional model missing is only a warning


def test_manifest_accepts_recorded_local_export(tmp_path):
    manifest = _write_manifest(tmp_path, {"a.onnx": b"model-a"})
    (tmp_path / "a.onnx").write_bytes(b"model-Z")
    digest = hashlib.sha256(b"model-Z").hexdigest()
    (tmp_path / pf.LOCAL_EXPORTS_NAME).write_text(json.dumps({"a.onnx": {"sha256": digest}}))
    assert pf.verify_models(manifest)[0]["status"] == "local_export"


def test_shipped_manifest_matches_shipped_models():
    manifest = json.loads(pf.MANIFEST_PATH.read_text())
    names = {m["file"] for m in manifest["models"]}
    assert {"yolo26s-pose.onnx", "yolo26n-pose.onnx", "yolo26n.onnx"} <= names
    required = {m["file"] for m in manifest["models"] if m.get("required", True)}
    assert required == {"yolo26s-pose.onnx", "yolo26n-pose.onnx"}
    for m in manifest["models"]:
        assert len(m["sha256"]) == 64 and m.get("io")
        if m["file"].startswith("yolo26"):
            assert m["licence"] == "AGPL-3.0" and m["source"].endswith(".pt")
        else:  # published ONNX (RTMPose): downloaded, not exported
            assert m["licence"].startswith("Apache-2.0") and m["download"]["archive_sha256"]
    results = pf.verify_models()
    # Required models must verify; optional ladder/refiner models may be absent.
    assert all(r["status"] in ("ok", "local_export") or (not r["required"] and r["status"] == "missing")
               for r in results), results


def test_io_signature_comparison():
    sig = {"inputs": [{"name": "images", "shape": [1, 3, 640, 640], "type": "tensor(float)"}],
           "outputs": [{"name": "output0", "shape": [1, 56, 8400], "type": "tensor(float)"}]}
    other = json.loads(json.dumps(sig))
    assert pf.io_signature_matches(sig, other)
    other["outputs"][0]["shape"] = [1, 84, 8400]
    assert not pf.io_signature_matches(sig, other)
    assert not pf.io_signature_matches(None, sig)


def test_real_model_io_signature_matches_manifest():
    pytest.importorskip("onnxruntime")
    entry = json.loads(pf.MANIFEST_PATH.read_text())["models"][1]
    sig = pf.read_io_signature(pf.MODELS_DIR / entry["file"])
    assert pf.io_signature_matches(sig, entry["io"])


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def test_preflight_endpoint_and_hardware_summary():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        res = client.get("/api/v1/system/preflight")
        assert res.status_code == 200, res.text
        body = res.json()
        assert {"ok", "errors", "warnings", "checks"} <= set(body)
        hw = client.get("/api/v1/system/hardware")
        assert hw.status_code == 200, hw.text
        assert "preflight" in hw.json() and hw.json()["preflight"]["models_total"] == len(json.loads(pf.MANIFEST_PATH.read_text())["models"])
