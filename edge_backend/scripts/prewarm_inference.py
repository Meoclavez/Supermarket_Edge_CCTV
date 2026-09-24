#!/usr/bin/env python3
"""Load the models exactly as the service does, compiling them for the GPU now.

Run with the service's venv, as the service user, from anywhere::

    /opt/edge-cctv/.venv/bin/python edge_backend/scripts/prewarm_inference.py

It calls the same ``PersonDetector.initialise()`` the application lifespan
calls (same provider chain, pose-model ladder, latency budget, refiner and
object model), but with ``MIGRAPHX_COMPILE_MODE=foreground`` so a cold AMD
MIGraphX cache is compiled here, where waiting is expected, instead of at the
next service start. Programs land in ``MIGRAPHX_CACHE_DIR`` and are recorded
in its ``edge_warm.json``. On any other provider this is just a load test.

The run-time scheduler (app/services/inference_scheduler.py) may later step to
any rung of the pose ladder as cameras come and go, so on an accelerator every
other ladder rung is compiled and measured too (``--no-ladder`` skips that; the
service's own background compile uses it to reach the GPU sooner, and compiles
a rung in a child process of this script when the scheduler first needs it).

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--require-gpu", action="store_true",
                    help="exit 2 unless the person model ends up on an accelerator")
    ap.add_argument("--no-ladder", action="store_true",
                    help="compile only the models start-up selects, not the other pose-ladder rungs")
    args = ap.parse_args()

    os.environ["MIGRAPHX_COMPILE_MODE"] = "foreground"   # before app.config is imported
    sys.path.insert(0, str(EDGE_BACKEND_DIR))
    os.chdir(EDGE_BACKEND_DIR)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")

    from app.services.inference_backend import PersonDetector  # noqa: PLC0415

    started = time.perf_counter()
    det = PersonDetector()
    st = det.initialise()
    ladder = [] if args.no_ladder else prewarm_ladder(det)
    if ladder:
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
        "object_provider": (st.get("object_detection") or {}).get("provider"),
        "object_model": st.get("object_model"),
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
