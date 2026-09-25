#!/usr/bin/env python3
"""Check that the accelerator's skeletons match the CPU's, model by model.

A pose model compiled for a GPU (MIGraphX, TensorRT, CUDA) runs a different
implementation of every graph node than the CPU provider. A wrong strided
slice or reshape in the keypoint branch would leave the boxes right and turn
the skeletons on their side, so this runs the same frames through both and
compares, for each person, the box and every visible joint. Read-only: it
loads models, it changes no settings or cameras.

Run it on the edge box with the service's venv (a cold MIGraphX cache is
compiled first, which takes minutes per model)::

    /opt/edge-cctv/.venv/bin/python edge_backend/scripts/pose_parity_check.py
    ... --models yolo26n-pose.onnx,yolo26m-pose-544x960.onnx --image some_frame.jpg
    ... --refiner          # the RTMPose top-down refiner on both providers too

For every model and frame size it prints the people found by each provider,
the median joint distance between them as a fraction of the person's height,
and the anatomical checks (shoulders above hips, head above hips, upright
torso) per provider. Exit status: 0 when every model agrees within
``--tolerance``, 3 when one does not, 1 when nothing could run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

EDGE_BACKEND_DIR = Path(__file__).resolve().parents[1]
FRAME_SIZES = "352x288,704x576,1280x720,2560x1440,3072x2048"
CPU_ONLY = "tensorrt,cuda,migraphx,rocm,openvino,directml,coreml"


def _fit(img, w, h):
    import cv2

    H, W = img.shape[:2]
    if W / H > w / h:
        nw = int(round(H * w / h))
        img = img[:, (W - nw) // 2:(W - nw) // 2 + nw]
    else:
        nh = int(round(W * h / w))
        img = img[(H - nh) // 2:(H - nh) // 2 + nh]
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def _iou(a, b) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    union = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
    return inter / union if union > 0 else 0.0


def _anatomy(d, thr: float) -> list[str]:
    import numpy as np

    k = d.keypoints
    v = k[:, 2] >= thr
    out = []
    sh = [i for i in (5, 6) if v[i]]
    hp = [i for i in (11, 12) if v[i]]
    if sh and hp and k[sh, 1].mean() >= k[hp, 1].mean():
        out.append("shoulders below hips")
    if v[0] and hp and k[0, 1] >= k[hp, 1].mean():
        out.append("nose below hips")
    if len(sh) == 2 and len(hp) == 2:
        dx, dy = np.asarray(k[[11, 12], :2].mean(0) - k[[5, 6], :2].mean(0))
        if abs(dx) > abs(dy):
            out.append("torso horizontal")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default="", help="comma-separated model files (default: the pose ladder)")
    ap.add_argument("--image", default=str(EDGE_BACKEND_DIR / "tests" / "fixtures" / "bus.jpg"))
    ap.add_argument("--sizes", default=FRAME_SIZES, help="frame sizes WxH to test")
    ap.add_argument("--conf", type=float, default=0.4)
    ap.add_argument("--refiner", action="store_true", help="also run the RTMPose refiner on both providers")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="max median joint distance, as a fraction of the person's height")
    args = ap.parse_args()

    os.chdir(EDGE_BACKEND_DIR)
    sys.path.insert(0, str(EDGE_BACKEND_DIR))
    os.environ.setdefault("MIGRAPHX_COMPILE_MODE", "foreground")
    import cv2
    import numpy as np

    from app.config import settings
    from app.services.inference_backend import PersonDetector

    settings.POSE_REFINER = "on" if args.refiner else "off"
    settings.LOW_LIGHT_ENHANCE = "off"
    settings.OBJECT_DETECT_EVERY_N = 0
    img = cv2.imread(args.image)
    if img is None:
        print(f"cannot read {args.image}")
        return 1
    sizes = [tuple(int(v) for v in s.split("x")) for s in args.sizes.split(",") if s]

    probe = PersonDetector(object_model_path="")
    probe.initialise()
    if not probe.available:
        print(f"no backend: {probe.last_error}")
        return 1
    accel = probe.provider
    names = [n.strip() for n in args.models.split(",") if n.strip()] or [p.name for p in probe.pose_ladder()]
    print(f"accelerator: {probe.device_name} ({accel}); models: {', '.join(names)}")
    if accel in ("cpu", "openvino"):
        print("the selected provider is the CPU: nothing to compare")
        return 0

    worst = 0.0
    thr = float(settings.KEYPOINT_VISIBILITY_THRESHOLD)
    for name in names:
        path = Path(settings.MODELS_DIR) / name
        dev = PersonDetector(model_path=path, object_model_path="")
        dev.initialise()
        disabled = settings.INFERENCE_DISABLED_PROVIDERS
        settings.INFERENCE_DISABLED_PROVIDERS = CPU_ONLY
        try:
            cpu = PersonDetector(model_path=path, object_model_path="")
            cpu.initialise()
        finally:
            settings.INFERENCE_DISABLED_PROVIDERS = disabled
        if not (dev.available and cpu.available) or dev.provider != accel:
            print(f"{name}: skipped (device {dev.provider}: {dev.last_error}; cpu: {cpu.last_error})")
            continue
        for w, h in sizes:
            frame = _fit(img, w, h)
            a = dev.detect(frame, conf_threshold=args.conf)
            b = cpu.detect(frame, conf_threshold=args.conf)
            dists, unmatched = [], 0
            for p in a:
                q = max(b, key=lambda x: _iou(p, x), default=None)
                if q is None or _iou(p, q) < 0.7:
                    # A person near the threshold may fall either side of it.
                    unmatched += p.confidence >= args.conf + 0.1
                    continue
                v = (p.keypoints[:, 2] >= thr) & (q.keypoints[:, 2] >= thr)
                if v.any():
                    d = np.hypot(*(p.keypoints[v, :2] - q.keypoints[v, :2]).T) / max(p.y2 - p.y1, 1.0)
                    dists.append(float(np.median(d)))
            med = max(dists) if dists else 0.0
            worst = max(worst, med, float("inf") if unmatched else 0.0)
            bad_dev = sum(1 for p in a if _anatomy(p, thr))
            bad_cpu = sum(1 for p in b if _anatomy(p, thr))
            flag = "OK " if med <= args.tolerance and not unmatched else "BAD"
            label = name + (" +refiner" if dev.refiner_enabled else "")
            print(f"{flag} {label:36s} {w:>4d}x{h:<4d} people {accel}={len(a)} cpu={len(b)} unmatched={unmatched} "
                  f"joint-distance/height={med:.3f} anatomy-failures {accel}={bad_dev} cpu={bad_cpu}")
    return 0 if worst <= args.tolerance else 3


if __name__ == "__main__":
    sys.exit(main())
