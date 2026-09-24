"""Preflight flags .env keys the app ignores and model settings naming missing files.

Settings accepts unknown keys silently (extra="allow"), so a stale line such as
PERSON_MODEL_PATH=./models/yolov5n.onnx looked like it chose the model while
the service actually picked a YOLO26 pose model on its own.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, settings
from app.services import preflight as pf

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _run(tmp_path, monkeypatch, text: str):
    env = tmp_path / ".env"
    env.write_text(text)
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    return pf.check_env()


def test_retired_person_model_path_is_flagged(tmp_path, monkeypatch):
    info, errors, warnings = _run(tmp_path, monkeypatch, "DEBUG=false\nPERSON_MODEL_PATH=./models/yolov5n.onnx\n")
    assert errors == []
    assert info["unused_keys"] == ["PERSON_MODEL_PATH"]
    assert any("PERSON_MODEL_PATH is not read" in w["message"] for w in warnings)


def test_typo_is_flagged_but_known_and_external_keys_are_not(tmp_path, monkeypatch):
    info, _, warnings = _run(tmp_path, monkeypatch,
                             "# comment\nPORT=8000\nexport HOST=0.0.0.0\nPERSN_CONF_THRESHOLD=0.4\n"
                             "EDGE_ORT=auto\nMIGRAPHX_DISABLE_WINOGRAD=1\nHIP_VISIBLE_DEVICES=0\n")
    assert info["unused_keys"] == ["PERSN_CONF_THRESHOLD"]
    assert len([w for w in warnings if "PERSN_CONF_THRESHOLD" in w["message"]]) == 1


def test_model_setting_naming_a_missing_file_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "yolov5n-pose.onnx")
    info, _, warnings = _run(tmp_path, monkeypatch, "")
    assert {"setting": "POSE_MODEL_GPU", "value": "yolov5n-pose.onnx"} in info["missing_models"]
    assert any("POSE_MODEL_GPU=yolov5n-pose.onnx" in w["message"] for w in warnings)


def test_shipped_defaults_and_example_are_clean(tmp_path, monkeypatch):
    """Every model the defaults name ships in models/, and .env.example has no dead keys."""
    info, errors, warnings = _run(tmp_path, monkeypatch, EXAMPLE.read_text())
    assert errors == [] and info["unused_keys"] == [], warnings
    assert info["missing_models"] == [], info["missing_models"]
