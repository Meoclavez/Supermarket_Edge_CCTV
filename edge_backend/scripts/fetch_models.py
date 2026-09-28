#!/usr/bin/env python3
"""Verify the ONNX models against models/manifest.json and restore any that are missing or damaged.

Every model is published by its upstream project (OpenMMLab) and fetched from
the ``download`` URL in its manifest entry, checked against the archive
sha256. With a ``transform`` block as well (RTMO: the published graph has
in-graph NMS and dynamic shapes), the archive is handed to a script of this
repository that rewrites it (scripts/export_rtmo_static.py), run in a small
isolated uv venv under a cache directory with only the packages the transform
lists; nothing is installed into the application venv.

A transform is expected to reproduce the manifest's sha256 exactly. If a
different onnx build makes it differ, the result is accepted only when its
ONNX IO signature (input/output names, shapes, dtypes) equals the one recorded
in the manifest; a warning is printed and the accepted hash is recorded in
models/.local_exports.json so the next verification treats it as good.

ONNX files in the models directory that the manifest does not list (for
example models of an earlier release) are deleted, each one logged, so the
installed product holds only the reviewed models (``--keep-unlisted`` keeps
them; ``--verify-only`` only lists them). Nothing outside the models
directory is touched.

Usage:
    python scripts/fetch_models.py                 # verify, restore what is needed, prune unlisted
    python scripts/fetch_models.py --verify-only   # verify only, exit 1 on problems
    python scripts/fetch_models.py --force rtmo-s-body7-640x640-static.onnx

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


def ensure_tools_env(cache: Path, transform: dict) -> Path:
    """Create (once) the isolated venv a model ``transform`` runs in and return its python."""
    uv = find_uv()
    if not uv:
        raise RuntimeError(
            "uv is required to build the isolated model-tools environment. Install it without sudo with "
            "`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `pipx install uv`), then re-run."
        )
    pyver = str(transform.get("python", "3.12"))
    venv = cache / f"venv-tools-py{pyver.replace('.', '')}"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".edge-tools-ready"
    reqs = [str(r) for r in transform.get("requirements", [])]
    want = "\n".join(reqs)
    if marker.exists() and marker.read_text().strip() == want and py.exists():
        return py
    say("fix", f"building isolated model-tools env {venv} (Python {pyver}: {', '.join(reqs)})")
    for cmd in ([uv, "venv", "--clear", "--python", pyver, str(venv)],
                [uv, "pip", "install", "--python", str(py), *reqs]):
        res = run(cmd)
        if res.returncode != 0:
            raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{(res.stderr or res.stdout)[-1500:]}")
    marker.write_text(want + "\n")
    return py


def transform_model(entry: dict, archive: Path, out: Path, cache: Path) -> None:
    """Rewrite a downloaded archive into the shipped model with ``entry["transform"]``."""
    tr = entry["transform"]
    script = EDGE_BACKEND_DIR / tr["script"]
    if not script.is_file():
        raise RuntimeError(f"transform script {script} not found")
    py = ensure_tools_env(cache, tr)
    if out.exists():
        out.unlink()
    cmd = [str(py), str(script), "--src", str(archive), "--out", str(out), *[str(a) for a in tr.get("args", [])]]
    say("fix", f"transforming {archive.name} -> {entry['file']}: {script.name} {' '.join(cmd[4:])}")
    res = run(cmd)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"transform of {archive.name} failed:\n{(res.stderr or res.stdout)[-1500:]}")


def download_model(entry: dict, cache: Path) -> Path:
    """Fetch a model that is published as a file, e.g. RTMPose, or as an
    archive that ``transform`` rewrites (RTMO).

    ``entry["download"]`` = {"url", "archive_sha256", "member"}: the archive is
    downloaded once into the cache, its sha256 checked, and ``member`` (the
    ONNX file inside the zip) extracted.
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
    if entry.get("transform"):
        transform_model(entry, archive, out, cache)
        return out
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


def restore(entry: dict, models_dir: Path, cache: Path) -> bool:
    if not entry.get("download"):
        raise RuntimeError(f"{entry['file']} has no download source in manifest.json")
    exported = download_model(entry, cache)
    digest = pf.sha256_file(exported)
    size = exported.stat().st_size
    if digest == entry["sha256"]:
        verdict = "exact"
    elif pf.io_signature_matches(pf.read_io_signature(exported), entry.get("io")):
        verdict = "io"
    else:
        say("FAIL", f"{entry['file']}: rebuilt model has a different IO signature than manifest.json; "
                    "not installed (the app's decoder expects the manifest layout)")
        return False

    dest = models_dir / entry["file"]
    install(exported, dest, cache)
    installed = pf.sha256_file(dest)
    if installed != digest:
        say("FAIL", f"{entry['file']}: installed file hash {installed[:12]} != exported {digest[:12]} (disk problem?)")
        return False
    if verdict == "exact":
        say("ok", f"{entry['file']}: restored, sha256 matches manifest")
    else:
        record_local_export(models_dir, entry, digest, size)
        say("warn", f"{entry['file']}: restored; sha256 {digest[:12]}... differs from manifest "
                    f"{entry['sha256'][:12]}... (different onnx build?) but the ONNX IO signature matches. "
                    f"Accepted and recorded in {models_dir / pf.LOCAL_EXPORTS_NAME}")
    return True


def unlisted_models(manifest: dict, models_dir: Path) -> list[Path]:
    """ONNX files directly inside ``models_dir`` that the manifest does not list."""
    listed = {e["file"] for e in manifest.get("models", [])}
    return sorted(p for p in models_dir.glob("*.onnx")
                  if p.name not in listed and (p.is_file() or p.is_symlink()))


def prune_unlisted(manifest: dict, models_dir: Path, dry_run: bool = False) -> list[str]:
    """Delete (or with ``dry_run`` only report) the unlisted ONNX files, and
    drop their stale records from models/.local_exports.json."""
    removed: list[str] = []
    for path in unlisted_models(manifest, models_dir):
        if dry_run:
            say("warn", f"{path.name}: not listed in manifest.json (would be deleted)")
            continue
        try:
            path.unlink()
        except OSError as exc:
            say("warn", f"{path.name}: not listed in manifest.json; could not delete it ({exc})")
            continue
        removed.append(path.name)
        say("fix", f"deleted {path} (not listed in manifest.json)")
    local_path = models_dir / pf.LOCAL_EXPORTS_NAME
    local = pf.load_local_exports(models_dir)
    listed = {e["file"] for e in manifest.get("models", [])}
    stale = [k for k in local if k not in listed]
    if stale and not dry_run:
        for k in stale:
            local.pop(k, None)
        try:
            if local:
                local_path.write_text(json.dumps(local, indent=2) + "\n")
            else:
                local_path.unlink()
            say("fix", f"removed stale {pf.LOCAL_EXPORTS_NAME} record(s): {', '.join(stale)}")
        except OSError as exc:
            say("warn", f"could not update {local_path}: {exc}")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--models-dir", type=Path, default=None, help="default: the manifest's directory")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                    help=f"downloads + tools venv (default {DEFAULT_CACHE}; env EDGE_MODEL_EXPORT_CACHE)")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--force", nargs="*", default=[], metavar="FILE", help="restore these even if they verify")
    ap.add_argument("--keep-unlisted", action="store_true",
                    help="do not delete ONNX files in the models directory that manifest.json does not list")
    ap.add_argument("--quiet", action="store_true", help="only print problems and fixes")
    args = ap.parse_args(argv)

    manifest = pf.load_manifest(args.manifest)
    models_dir = args.models_dir or args.manifest.parent
    models_dir.mkdir(parents=True, exist_ok=True)
    if not args.keep_unlisted:
        prune_unlisted(manifest, models_dir, dry_run=args.verify_only)
    local = pf.load_local_exports(models_dir)

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
        say("fix", f"{entry['file']}: {label}; restoring from {(entry.get('download') or {}).get('url') or entry.get('source')}")
        required = entry.get("required", True)
        try:
            ok = restore(entry, models_dir, args.cache_dir.expanduser())
            error = None if ok else "not restored"
        except RuntimeError as exc:
            ok, error = False, str(exc)
        if not ok:
            if required:
                say("FAIL", f"{entry['file']}: {error}")
                failures += 1
            else:
                # An optional model (the keypoint refiner) must not fail an
                # install; whatever uses it reports it missing.
                say("warn", f"{entry['file']} (optional) could not be restored: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
