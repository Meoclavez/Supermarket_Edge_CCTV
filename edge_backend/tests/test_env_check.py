"""Preflight flags .env keys the app ignores, model settings naming missing
files, and model files the manifest does not list.

Settings accepts unknown keys silently (extra="allow"), so a stale line such as
PERSON_MODEL_PATH=./models/<old model>.onnx looked like it chose the model
while the service actually picked its pose model on its own. The same holds
for the retired object-model keys (OBJECT_*, THEFT_BAG_CLASS_IDS) and
HAILO_YOLO_HEF_PATH.
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
    info, errors, warnings = _run(tmp_path, monkeypatch, "DEBUG=false\nPERSON_MODEL_PATH=./models/person.onnx\n")
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
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "missing-pose.onnx")
    info, _, warnings = _run(tmp_path, monkeypatch, "")
    assert {"setting": "POSE_MODEL_GPU", "value": "missing-pose.onnx"} in info["missing_models"]
    assert any("POSE_MODEL_GPU=missing-pose.onnx" in w["message"] for w in warnings)
    assert "object_model" not in info


def test_retired_object_model_and_hailo_yolo_keys_are_flagged(tmp_path, monkeypatch):
    keys = ("OBJECT_MODEL_PATH", "OBJECT_DETECT_EVERY_N", "OBJECT_CLASSES", "OBJECT_CONF_THRESHOLD",
            "OBJECT_NMS_IOU", "THEFT_BAG_CLASS_IDS", "HAILO_YOLO_HEF_PATH")
    assert set(keys) <= set(pf.RETIRED_ENV_KEYS)
    info, errors, warnings = _run(tmp_path, monkeypatch, "".join(f"{k}=x\n" for k in keys))
    assert errors == [] and sorted(info["unused_keys"]) == sorted(keys)
    for k in keys:
        assert any(f"{k} is not read" in w["message"] for w in warnings), k
    assert any("object model" in w["message"] for w in warnings if "OBJECT_MODEL_PATH" in w["message"])


def test_shipped_defaults_and_example_are_clean(tmp_path, monkeypatch):
    """Every model the defaults name ships in models/, and .env.example has no dead keys."""
    info, errors, warnings = _run(tmp_path, monkeypatch, EXAMPLE.read_text())
    assert errors == [] and info["unused_keys"] == [], warnings
    assert info["missing_models"] == [], info["missing_models"]
    assert info["unlisted_models"] == [], info["unlisted_models"]


def test_check_env_warns_about_a_model_the_manifest_does_not_list(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "rtmo-s-body7-640x640-static.onnx").write_bytes(b"x")
    (models / "other-pose.onnx").write_bytes(b"x")
    monkeypatch.setattr(settings, "MODELS_DIR", models)
    monkeypatch.setattr(pf, "MODELS_DIR", models, raising=False)
    monkeypatch.setattr(settings, "POSE_MODEL_GPU", "other-pose.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_CPU", "rtmo-s-body7-640x640-static.onnx")
    monkeypatch.setattr(settings, "POSE_MODEL_LADDER_GPU", "")
    monkeypatch.setattr(settings, "POSE_MODEL_LADDER_CPU", "")
    monkeypatch.setattr(settings, "POSE_MODEL_PATH", "")
    monkeypatch.setattr(settings, "POSE_REFINER", "off")
    monkeypatch.setattr(settings, "SHADOW_POSE_MODEL", "", raising=False)
    info, _errors, warnings = pf.check_env()
    assert "object_model" not in info
    assert info["unlisted_models"] == [{"setting": "POSE_MODEL_GPU", "value": "other-pose.onnx"}]
    assert any("POSE_MODEL_GPU=other-pose.onnx is not listed in models/manifest.json" in w["message"]
               for w in warnings)
    assert not any("rtmo-s-body7" in w["message"] for w in warnings)
