#!/usr/bin/env python3
"""Load the models exactly as the service does, compiling them for the GPU now.

Run with the service's venv, as the service user, from anywhere::

    /opt/edge-cctv/.venv/bin/python edge_backend/scripts/prewarm_inference.py

It calls the same ``PersonDetector.initialise()`` the application lifespan
calls (same provider chain, pose-model ladder, latency budget and keypoint
refiner), but with ``MIGRAPHX_COMPILE_MODE=foreground`` so a cold AMD
MIGraphX cache is compiled here, where waiting is expected, instead of at the
next service start. Programs land in ``MIGRAPHX_CACHE_DIR`` and are recorded
in its ``edge_warm.json``. On any other provider this is just a load test.

The run-time scheduler (app/services/inference_scheduler.py) may later step to
any rung of the pose ladder as cameras come and go, so on an accelerator every
other ladder rung is compiled and measured too (``--no-ladder`` skips that; the
service's own background compile uses it to reach the GPU sooner, and compiles
a rung in a child process of this script when the scheduler first needs it).

The shadow-trial model (``SHADOW_POSE_MODEL``, services/shadow_trial.py) is
compiled too when it is set, so the trial starts without a cold compile.
``--only-model FILE`` compiles just that one model on the first selectable
provider without loading the others (the service runs it in a child process
when the trial's model is not in the cache yet).

The last line is ``@@PREWARM@@{json}`` for scripts/bootstrap.py. Exit status:
0 when the person model runs on a GPU/NPU provider, 2 when it runs on the
CPU although a GPU provider was expected (``--require-gpu``), 1 when no
backend could load at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

EDGE_BACKEND_DIR = Path(__file__).resolve().parents[1]


def prewarm_ladder(det) -> list[dict]:
    """Compile and measure every pose-ladder rung the scheduler may step to."""
    import onnxruntime as ort  # noqa: PLC0415
    from app.services.inference_backend import build_model_spec  # noqa: PLC0415

    if not det.available or not det._is_accelerator(det.provider):
        return []
    out: list[dict] = []
    for path in det.pose_ladder():
        if path == det.model_path:
            out.append({"model": path.name, "steady_ms": det.warmup_steady_ms, "loaded": True})
            continue
        sess, reason = det._create_session(ort, path, det.execution_provider)
        if sess is None:
            out.append({"model": path.name, "error": reason})
            continue
        try:
            spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                    sess.get_inputs(), sess.get_outputs())
            _first, steady = det._warm_up(sess, spec, det._device_lock)
            det._mark_compiled(path)
            out.append({"model": path.name, "steady_ms": steady})
        except Exception as exc:  # noqa: BLE001 - report and carry on with the next rung
            out.append({"model": path.name, "error": f"{type(exc).__name__}: {exc}"})
        del sess
        logging.getLogger("prewarm").info(f"ladder rung {out[-1]}")
    return out


def prewarm_extra(det, path: Path) -> dict:
    """Compile and measure one more model on the provider the detector selected."""
    import onnxruntime as ort  # noqa: PLC0415
    from app.services.inference_backend import build_model_spec  # noqa: PLC0415

    if not path.is_file():
        return {"model": path.name, "error": f"model file not found: {path}"}
    sess, reason = det._create_session(ort, path, det.execution_provider, role="shadow")
    if sess is None:
        return {"model": path.name, "error": reason}
    try:
        spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                sess.get_inputs(), sess.get_outputs())
        _first, steady = det._warm_up(sess, spec, det._device_lock)
        det._mark_compiled(path, ep=det.execution_provider)
        return {"model": path.name, "layout": spec.layout, "steady_ms": steady}
    except Exception as exc:  # noqa: BLE001 - report it; the live models are unaffected
        return {"model": path.name, "error": f"{type(exc).__name__}: {exc}"}


def shadow_model_path() -> Path | None:
    from app.config import settings  # noqa: PLC0415

    name = (settings.SHADOW_POSE_MODEL or "").strip()
    if not name:
        return None
    p = Path(name)
    return p if p.is_absolute() else Path(settings.MODELS_DIR) / p


def only_model(path: Path) -> int:
    """``--only-model``: compile one file on the first selectable provider."""
    import onnxruntime as ort  # noqa: PLC0415
    from app.services.inference_backend import PersonDetector, _PROVIDER_PRIORITY  # noqa: PLC0415

    det = PersonDetector()
    det._initialised = True
    det.available_providers = list(ort.get_available_providers())
    det._prepare_plugin_eps(ort)
    ep = det._first_selectable_ep()
    det.execution_provider = ep
    det.provider = next((label for e, label, _ in _PROVIDER_PRIORITY if e == ep), "none")
    started = time.perf_counter()
    out = prewarm_extra(det, path) if ep else {"model": path.name, "error": "no execution provider"}
    out.update(provider=det.provider, seconds=round(time.perf_counter() - started, 1),
               gpu_compile=det.gpu_compile)
    print("@@PREWARM@@" + json.dumps(out, default=str), flush=True)
    return 1 if out.get("error") else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--require-gpu", action="store_true",
                    help="exit 2 unless the person model ends up on an accelerator")
    ap.add_argument("--no-ladder", action="store_true",
                    help="compile only the models start-up selects, not the other pose-ladder rungs")
    ap.add_argument("--only-model", type=Path, default=None, metavar="FILE",
                    help="compile just this model file on the first selectable provider")
    args = ap.parse_args()

    os.environ["MIGRAPHX_COMPILE_MODE"] = "foreground"   # before app.config is imported
    sys.path.insert(0, str(EDGE_BACKEND_DIR))
    os.chdir(EDGE_BACKEND_DIR)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")

    if args.only_model is not None:
        return only_model(args.only_model.resolve())

    from app.services.inference_backend import PersonDetector  # noqa: PLC0415

    started = time.perf_counter()
    det = PersonDetector()
    st = det.initialise()
    ladder = [] if args.no_ladder else prewarm_ladder(det)
    shadow = shadow_model_path()
    extra = ([prewarm_extra(det, shadow)] if (shadow is not None and not args.no_ladder and det.available
                                                and det._is_accelerator(det.provider)) else [])
    for e in extra:
        logging.getLogger("prewarm").info(f"shadow-trial model {e}")
    if ladder or extra:
        st = det.status()
    seconds = round(time.perf_counter() - started, 1)
    summary = {
        "available": st.get("available"),
        "provider": st.get("provider"),
        "execution_provider": st.get("execution_provider"),
        "device": st.get("device"),
        "model": st.get("model"),
        "input_size": st.get("input_size"),
        "warmup_steady_ms": st.get("warmup_steady_ms"),
        "model_selection": st.get("model_selection"),
        "refiner": st.get("keypoint_refiner"),
        "ladder": ladder,
        "shadow_model": extra[0] if extra else None,
        "gpu_compile": st.get("gpu_compile"),
        "migraphx": st.get("migraphx"),
        "cpu_threads": st.get("cpu_threads"),
        "provider_attempts": st.get("provider_attempts"),
        "error": st.get("error"),
        "seconds": seconds,
    }
    print("@@PREWARM@@" + json.dumps(summary, default=str), flush=True)
    if not st.get("available"):
        return 1
    if args.require_gpu and st.get("provider") in ("cpu", "openvino", "none"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
