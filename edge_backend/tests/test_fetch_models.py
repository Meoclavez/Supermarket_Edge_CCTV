"""scripts/fetch_models.py: only manifest-listed models stay in models/.

The product may ship only reviewed (non-AGPL) models, so ONNX files in the
models directory that models/manifest.json does not list (e.g. the YOLO
exports of an earlier release) are deleted, each one logged, and their stale
.local_exports.json records dropped. ``--keep-unlisted`` keeps them and
``--verify-only`` only reports them. Every test works on a tmp_path copy of a
models directory; the real models/ is never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_models.py"


@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("edge_fetch_models_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _models_dir(tmp_path: Path, listed: dict[str, bytes], unlisted: dict[str, bytes]) -> tuple[Path, dict]:
    """A models directory with a manifest listing ``listed`` (hashes match) plus ``unlisted`` files."""
    models = tmp_path / "models"
    models.mkdir()
    entries = []
    for name, data in listed.items():
        (models / name).write_bytes(data)
        entries.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                        "task": "pose", "required": True, "licence": "Apache-2.0",
                        "download": {"url": f"https://example.invalid/{name}.zip",
                                     "archive_sha256": "0" * 64, "member": "end2end.onnx"},
                        "io": {"inputs": [], "outputs": []}})
    for name, data in unlisted.items():
        (models / name).write_bytes(data)
    manifest = {"schema": 1, "models": entries}
    (models / "manifest.json").write_text(json.dumps(manifest))
    return models, manifest


LISTED = {"rtmo-s-body7-640x640-static.onnx": b"rtmo", "rtmpose-s-256x192.onnx": b"rtmpose"}
OLD = {"yolo26n-pose.onnx": b"old-a", "yolo26s-pose-544x960.onnx": b"old-b", "yolo26n.onnx": b"old-c"}


def test_unlisted_models_are_only_onnx_files_directly_in_the_models_dir(fm, tmp_path):
    models, manifest = _models_dir(tmp_path, LISTED, OLD)
    (models / "notes.txt").write_text("kept")
    (models / "sub").mkdir()
    (models / "sub" / "nested.onnx").write_bytes(b"nested")
    assert [p.name for p in fm.unlisted_models(manifest, models)] == sorted(OLD)


def test_prune_deletes_unlisted_models_and_logs_each(fm, tmp_path, capsys):
    models, manifest = _models_dir(tmp_path, LISTED, OLD)
    (models / "notes.txt").write_text("kept")
    (models / "sub").mkdir()
    (models / "sub" / "nested.onnx").write_bytes(b"nested")
    removed = fm.prune_unlisted(manifest, models)
    assert sorted(removed) == sorted(OLD)
    assert sorted(p.name for p in models.glob("*.onnx")) == sorted(LISTED)
    assert (models / "manifest.json").exists() and (models / "notes.txt").exists()
    assert (models / "sub" / "nested.onnx").exists()                 # only the models dir itself
    out = capsys.readouterr().out
    for name in OLD:
        assert f"deleted {models / name} (not listed in manifest.json)" in out
    assert fm.unlisted_models(manifest, models) == []
    assert fm.prune_unlisted(manifest, models) == []                  # idempotent


def test_prune_removes_a_symlink_but_not_its_target(fm, tmp_path):
    models, manifest = _models_dir(tmp_path, LISTED, {})
    target = tmp_path / "elsewhere.onnx"
    target.write_bytes(b"outside")
    (models / "linked.onnx").symlink_to(target)
    assert fm.prune_unlisted(manifest, models) == ["linked.onnx"]
    assert not (models / "linked.onnx").is_symlink() and target.read_bytes() == b"outside"


def test_dry_run_only_reports(fm, tmp_path, capsys):
    models, manifest = _models_dir(tmp_path, LISTED, OLD)
    local = {"yolo26n-pose.onnx": {"sha256": "a" * 64}}
    (models / ".local_exports.json").write_text(json.dumps(local))
    assert fm.prune_unlisted(manifest, models, dry_run=True) == []
    assert all((models / n).exists() for n in OLD)
    assert json.loads((models / ".local_exports.json").read_text()) == local
    out = capsys.readouterr().out
    for name in OLD:
        assert f"{name}: not listed in manifest.json (would be deleted)" in out


def test_prune_drops_stale_local_export_records(fm, tmp_path):
    models, manifest = _models_dir(tmp_path, LISTED, OLD)
    keep = {"rtmo-s-body7-640x640-static.onnx": {"sha256": "b" * 64}}
    (models / ".local_exports.json").write_text(json.dumps({**keep, "yolo26n-pose.onnx": {"sha256": "a" * 64}}))
    fm.prune_unlisted(manifest, models)
    assert json.loads((models / ".local_exports.json").read_text()) == keep


def test_prune_removes_a_local_exports_file_with_only_stale_records(fm, tmp_path):
    models, manifest = _models_dir(tmp_path, LISTED, {})
    (models / ".local_exports.json").write_text(json.dumps({"yolo26n-pose.onnx": {"sha256": "a" * 64}}))
    fm.prune_unlisted(manifest, models)
    assert not (models / ".local_exports.json").exists()


def _main(fm, models: Path, *extra: str) -> int:
    return fm.main(["--manifest", str(models / "manifest.json"), "--models-dir", str(models),
                    "--cache-dir", str(models.parent / "cache"), "--quiet", *extra])


def test_main_prunes_by_default(fm, tmp_path):
    models, _ = _models_dir(tmp_path, LISTED, OLD)
    assert _main(fm, models) == 0                                      # listed models verify: no download
    assert sorted(p.name for p in models.glob("*.onnx")) == sorted(LISTED)


def test_main_keep_unlisted_keeps_them(fm, tmp_path):
    models, _ = _models_dir(tmp_path, LISTED, OLD)
    assert _main(fm, models, "--keep-unlisted") == 0
    assert all((models / n).exists() for n in OLD)


def test_main_verify_only_reports_but_keeps_them(fm, tmp_path, capsys):
    models, _ = _models_dir(tmp_path, LISTED, OLD)
    assert _main(fm, models, "--verify-only") == 0
    assert all((models / n).exists() for n in OLD)
    assert "would be deleted" in capsys.readouterr().out


def test_restore_needs_a_download_source(fm, tmp_path):
    entry = {"file": "legacy.onnx", "sha256": "0" * 64, "source": "legacy.pt"}
    with pytest.raises(RuntimeError, match="no download source"):
        fm.restore(entry, tmp_path, tmp_path / "cache")


def test_no_ultralytics_export_path_is_left(fm):
    for name in ("export_model", "ensure_export_env"):
        assert not hasattr(fm, name), name
    assert "ultralytics" not in SCRIPT.read_text().lower()


def test_docker_build_context_ships_only_manifest_models():
    """.dockerignore excludes models/* and re-includes exactly the manifest's files."""
    root = Path(__file__).resolve().parents[1]
    lines = [ln.strip() for ln in (root / ".dockerignore").read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert "models/*" in lines
    exclude_at = lines.index("models/*")
    allowed = {ln[len("!models/"):] for ln in lines[exclude_at + 1:] if ln.startswith("!models/")}
    manifest = json.loads((root / "models" / "manifest.json").read_text())
    assert allowed == {m["file"] for m in manifest["models"]} | {"manifest.json"}
    # Nothing earlier re-includes a model the later exclusion would then drop again.
    assert not any(ln.startswith("!models/") for ln in lines[:exclude_at])
