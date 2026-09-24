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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--require-gpu", action="store_true",
                    help="exit 2 unless the person model ends up on an accelerator")
    args = ap.parse_args()

    os.environ["MIGRAPHX_COMPILE_MODE"] = "foreground"   # before app.config is imported
    sys.path.insert(0, str(EDGE_BACKEND_DIR))
    os.chdir(EDGE_BACKEND_DIR)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")

    from app.services.inference_backend import PersonDetector  # noqa: PLC0415

    started = time.perf_counter()
    st = PersonDetector().initialise()
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
