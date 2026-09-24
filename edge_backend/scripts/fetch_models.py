#!/usr/bin/env python3
"""Verify the ONNX models against models/manifest.json and restore any that are missing or damaged.

A model with a ``download`` block (RTMPose, published as ONNX) is fetched
from its URL and checked against the archive sha256. Any other model that is
missing, or whose sha256 does not match, is re-exported from its Ultralytics
``.pt`` source in an isolated, throwaway uv virtualenv under a
cache directory (Python 3.12, CPU-only torch). ultralytics and torch are never
installed into the application venv.

Re-exporting is NOT byte-identical: the export embeds a timestamp (and a
different ultralytics/onnx build can reorder the graph). So after a re-export,
a hash mismatch is accepted when the model's ONNX IO signature (input/output
names, shapes, dtypes) equals the one recorded in the manifest; a warning is
printed and the accepted hash is recorded in models/.local_exports.json so the
next verification treats it as good instead of re-exporting on every start.

Usage:
    python scripts/fetch_models.py                 # verify, export what is needed
    python scripts/fetch_models.py --verify-only   # verify only, exit 1 on problems
    python scripts/fetch_models.py --force yolo26n.onnx

Run it with the app venv's python so the IO signature check can use onnxruntime.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EDGE_BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = EDGE_BACKEND_DIR / "models" / "manifest.json"
DEFAULT_CACHE = Path(os.environ.get("EDGE_MODEL_EXPORT_CACHE", Path.home() / ".cache" / "edge-cctv" / "model-export"))


def _load_preflight():
    """Load app/services/preflight.py by path: shares the verification code without importing the app."""
    path = EDGE_BACKEND_DIR / "app" / "services" / "preflight.py"
    spec = importlib.util.spec_from_file_location("edge_preflight_standalone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


pf = _load_preflight()


def say(tag: str, msg: str) -> None:
    print(f"[{tag:<4}] {msg}", flush=True)


def find_uv() -> str | None:
    for cand in (shutil.which("uv"), Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def ensure_export_env(cache: Path, exporter: dict) -> Path:
    """Create (once) the isolated export venv and return its python."""
    uv = find_uv()
    if not uv:
        raise RuntimeError(
            "uv is required to build the isolated export environment. Install it without sudo with "
            "`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `pipx install uv`), then re-run."
        )
    pyver = str(exporter.get("python", "3.12"))
    venv = cache / f"venv-py{pyver.replace('.', '')}"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".edge-export-ready"
    requirement = exporter.get("requirement", "ultralytics>=8.4,<8.5")
    if marker.exists() and marker.read_text().strip() == requirement and py.exists():
        return py

    say("fix", f"building isolated export env {venv} (Python {pyver}, CPU torch, {requirement})")
    steps = [
        [uv, "venv", "--clear", "--python", pyver, str(venv)],
        [uv, "pip", "install", "--python", str(py), "--index-url",
         exporter.get("torch_index_url", "https://download.pytorch.org/whl/cpu"), "torch", "torchvision"],
        [uv, "pip", "install", "--python", str(py), requirement, *exporter.get("extra_packages", ["onnx", "onnxslim"])],
    ]
    for cmd in steps:
        res = run(cmd)
        if res.returncode != 0:
            raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{(res.stderr or res.stdout)[-1500:]}")
    marker.write_text(requirement + "\n")
    return py


def export_model(entry: dict, cache: Path, exporter: dict) -> Path:
    py = ensure_export_env(cache, exporter)
    work = cache / "work"
    work.mkdir(parents=True, exist_ok=True)
    source = entry["source"]
    out = work / (Path(source).stem + ".onnx")
    if out.exists():
        out.unlink()
    args = [f"model={source}"] + [
        f"{k}={v if not isinstance(v, bool) else str(v)}" for k, v in entry.get("export", {}).items()
    ]
    cli = py.parent / ("yolo.exe" if os.name == "nt" else "yolo")
    env = dict(os.environ, YOLO_CONFIG_DIR=str(cache / "ultralytics-config"))
    say("fix", f"exporting {entry['file']}: yolo export {' '.join(args)}")
    res = subprocess.run([str(cli), "export", *args], cwd=work, env=env, text=True, capture_output=True)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"export of {source} failed:\n{(res.stderr or res.stdout)[-1500:]}")
    return out


def download_model(entry: dict, cache: Path) -> Path:
    """Fetch a model that is published as a file (not exported), e.g. RTMPose.

    ``entry["download"]`` = {"url", "archive_sha256", "member"}: the archive is
    downloaded once into the cache, its sha256 checked, and ``member`` (the
    ONNX file inside the zip) extracted. No export environment is needed.
    """
    import hashlib
    import urllib.request
    import zipfile

    dl = entry["download"]
    work = cache / "downloads"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / Path(dl["url"].split("?", 1)[0]).name

    def _sha(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    want = dl.get("archive_sha256")
    if not archive.exists() or (want and _sha(archive) != want):
        say("fix", f"downloading {dl['url']}")
        tmp = archive.with_suffix(archive.suffix + ".part")
        req = urllib.request.Request(dl["url"], headers={"User-Agent": "edge-cctv-fetch-models/1"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, archive)
    if want and _sha(archive) != want:
        raise RuntimeError(f"{archive.name}: sha256 does not match manifest archive_sha256")
    out = work / entry["file"]
    member = dl.get("member")
    if member:
        with zipfile.ZipFile(archive) as z, z.open(member) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copy2(archive, out)
    return out


def install(exported: Path, dest: Path, cache: Path) -> None:
    if dest.exists():
        backup = cache / "replaced" / f"{dest.name}.{int(time.time())}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, backup)
        say("info", f"previous {dest.name} kept at {backup}")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copy2(exported, tmp)
    os.replace(tmp, dest)


def record_local_export(models_dir: Path, entry: dict, digest: str, size: int) -> None:
    path = models_dir / pf.LOCAL_EXPORTS_NAME
    data = pf.load_local_exports(models_dir)
    data[entry["file"]] = {"sha256": digest, "size": size, "manifest_sha256": entry.get("sha256"),
                           "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": "io_signature_match"}
    path.write_text(json.dumps(data, indent=2) + "\n")


def restore(entry: dict, models_dir: Path, cache: Path, exporter: dict) -> bool:
    exported = download_model(entry, cache) if entry.get("download") else export_model(entry, cache, exporter)
    digest = pf.sha256_file(exported)
    size = exported.stat().st_size
    if digest == entry["sha256"]:
        verdict = "exact"
    elif pf.io_signature_matches(pf.read_io_signature(exported), entry.get("io")):
        verdict = "io"
    else:
        say("FAIL", f"{entry['file']}: re-export has a different IO signature than manifest.json; "
                    "not installed (the app's decoder expects the manifest layout)")
        return False

    dest = models_dir / entry["file"]
    install(exported, dest, cache)
    installed = pf.sha256_file(dest)
    if installed != digest:
        say("FAIL", f"{entry['file']}: installed file hash {installed[:12]} != exported {digest[:12]} (disk problem?)")
        return False
    if verdict == "exact":
        say("ok", f"{entry['file']}: re-exported, sha256 matches manifest")
    else:
        record_local_export(models_dir, entry, digest, size)
        say("warn", f"{entry['file']}: re-exported; sha256 {digest[:12]}... differs from manifest "
                    f"{entry['sha256'][:12]}... (exports embed a timestamp) but the ONNX IO signature matches. "
                    f"Accepted and recorded in {models_dir / pf.LOCAL_EXPORTS_NAME}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--models-dir", type=Path, default=None, help="default: the manifest's directory")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                    help=f"export venv + work dir (default {DEFAULT_CACHE}; env EDGE_MODEL_EXPORT_CACHE)")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--force", nargs="*", default=[], metavar="FILE", help="re-export these even if they verify")
    ap.add_argument("--quiet", action="store_true", help="only print problems and fixes")
    args = ap.parse_args(argv)

    manifest = pf.load_manifest(args.manifest)
    models_dir = args.models_dir or args.manifest.parent
    models_dir.mkdir(parents=True, exist_ok=True)
    local = pf.load_local_exports(models_dir)
    exporter = manifest.get("exporter", {})

    failures = 0
    for entry in manifest.get("models", []):
        res = pf.verify_model(entry, models_dir, local)
        status = res["status"]
        forced = entry["file"] in args.force
        if status in ("ok", "local_export") and not forced:
            if not args.quiet:
                note = " (local re-export, IO signature verified)" if status == "local_export" else ""
                say("ok", f"{entry['file']}: sha256 verified{note}")
            continue
        label = "forced" if forced else status
        if args.verify_only:
            say("FAIL" if entry.get("required", True) else "warn", f"{entry['file']}: {label}")
            failures += 1 if entry.get("required", True) or status != "missing" else 0
            continue
        say("fix", f"{entry['file']}: {label}; restoring from {entry['source']}")
        try:
            if not restore(entry, models_dir, args.cache_dir.expanduser(), exporter):
                failures += 1
        except RuntimeError as exc:
            say("FAIL", f"{entry['file']}: {exc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
