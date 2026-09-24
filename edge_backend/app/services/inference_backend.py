"""Person pose estimation and retail-object detection with runtime hardware
auto-detection.

The deployment machine is not the development machine, so nothing here may be
chosen at build time. ``initialise_inference()`` (called once from the
application lifespan) walks a priority chain and keeps the first accelerator
that *really* takes the model:

    1. HailoRT NPU      reported honestly: this build has no HEF runner, so a
                        fitted Hailo device is listed but never selected
    2. TensorRT  EP     (NVIDIA; fp16, engine cache under storage/)
    3. CUDA      EP     (NVIDIA)
    4. MIGraphX  EP     (AMD; ONNX Runtime plugin EP, see amd_migraphx.py.
                        A cold compiled-program cache is compiled in the
                        background while the CPU serves, then swapped in)
    5. OpenVINO  EP     (Intel iGPU / NPU / CPU)
    6. DirectML / CoreML
    7. CPU       EP     (always present in a working ONNX Runtime)
    8. UNAVAILABLE      (no model file, or ONNX Runtime missing)

A provider only counts when ``session.get_providers()[0]`` is that provider
and ONNX Runtime did not print its "Falling back" notice. ONNX Runtime
silently falls back (for example TensorRT -> CUDA when libnvinfer is missing,
or any plugin EP requested by name), so asking for a provider proves nothing;
the reason every provider was passed over is recorded in
``status()["provider_attempts"]``.

Sessions on the CPU provider share ``INFERENCE_CPU_THREADS`` (default half the
CPUs) and do not spin idle threads, so CPU inference cannot starve decoding.

Model size and input resolution are chosen the same way, by measurement:
on the selected provider the pose model ladder (``POSE_MODEL_LADDER_*``, most
accurate first) is warmed up in turn and the first model whose steady latency
fits the per-frame budget is kept (``latency_budget()``: derived from the
number of analytics cameras unless ``POSE_LATENCY_BUDGET_MS`` is set). An
optional top-down refiner (RTMPose, ``POSE_REFINER``) re-estimates the
keypoints of each person crop; ``auto`` enables it only on an accelerator and
only when it fits in half the budget. Everything measured is in
``status()["model_selection"]`` and ``status()["keypoint_refiner"]``.

That start-up choice only sees the cameras that exist at start-up. At run time
``inference_scheduler`` shares the measured capacity between the cameras that
are really analysing and re-fits the choice when that set or the load changes:
it switches the refiner (``auto``) and the ladder rung through
``set_refiner()`` / ``switch_pose_model()``, which load the new session off the
capture threads and swap it in atomically. On an accelerator every
``session.run`` is serialised by one device lock, so cameras queue in Python
(bounded by the scheduler) rather than inside the driver, and the time spent in
it is the measured device load (``load_metrics()``).

The last state matters as much as the others: when no backend can run, the
service reports ``available = False`` and returns **no detections at all**.
It never emits synthetic boxes, because a fabricated detection is
indistinguishable downstream from a real one.

Importing this module loads nothing. Models are loaded by
``initialise_inference()``; ``detect()`` will also trigger it on first use as
a safety net, so a caller that forgets the lifespan hook still gets real
results rather than a silently idle detector.
"""

from __future__ import annotations

import ast
import contextlib
import io
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.config import settings
from app.services import amd_migraphx as amd

logger = logging.getLogger(__name__)

# COCO class 0 is "person".
PERSON_CLASS_ID = 0

KEYPOINT_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)
# Limb pairs for drawing a COCO skeleton.
SKELETON_EDGES = (
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),          # shoulders, arms
    (5, 11), (6, 12), (11, 12),                       # torso
    (11, 13), (13, 15), (12, 14), (14, 16),           # legs
    (0, 1), (0, 2), (1, 3), (2, 4),                   # face
)

# COCO names, used only when a model carries no ``names`` metadata.
_COCO_FALLBACK_NAMES = {
    0: "person", 24: "backpack", 26: "handbag", 28: "suitcase", 39: "bottle", 67: "cell phone",
}

# (execution provider, label, is_accelerator). Accelerators get the larger
# pose model by default; CPU-class providers get the small one.
_PROVIDER_PRIORITY: tuple[tuple[str, str, bool], ...] = (
    ("TensorrtExecutionProvider", "tensorrt", True),
    ("CUDAExecutionProvider", "cuda", True),
    ("MIGraphXExecutionProvider", "migraphx", True),
    # Removed from ONNX Runtime in 1.23; only a legacy build still offers it.
    ("ROCMExecutionProvider", "rocm", True),
    ("OpenVINOExecutionProvider", "openvino", False),
    ("DmlExecutionProvider", "directml", True),
    ("CoreMLExecutionProvider", "coreml", True),
    ("CPUExecutionProvider", "cpu", False),
)


# ---------------------------------------------------------------- data types


@dataclass
class Detection:
    """One person, in pixel coordinates of the frame that produced it."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int = PERSON_CLASS_ID
    # (17, 3): x_px, y_px, visibility 0..1, in original frame coordinates.
    # None when the loaded model is a plain detector.
    keypoints: Optional[np.ndarray] = None

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def foot_point(self) -> tuple[float, float]:
        """Where the person meets the floor.

        The bottom-centre of the box is the only part of a person that is
        reliably on the ground plane, so it is what gets projected through the
        homography into store coordinates.
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def to_dict(self) -> dict:
        out = {
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "confidence": round(self.confidence, 3),
            "class_id": self.class_id,
        }
        if self.keypoints is not None:
            out["keypoints"] = keypoints_to_list(self.keypoints)
        return out


@dataclass
class ObjectDetection:
    """A non-person COCO object (bag, bottle, phone) in original frame pixels."""

    bbox: tuple[float, float, float, float]
    class_id: int
    label: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "bbox": [round(float(v), 1) for v in self.bbox],
            "class_id": self.class_id,
            "label": self.label,
            "confidence": round(self.confidence, 3),
        }


def keypoints_to_list(kpts: Optional[np.ndarray]) -> Optional[list[list[float]]]:
    if kpts is None:
        return None
    return [[round(float(x), 1), round(float(y), 1), round(float(v), 3)] for x, y, v in kpts[:, :3]]


@dataclass
class ModelSpec:
    """What a loaded ONNX model is, decided from its metadata and output shape."""

    path: Path
    task: str                         # "pose" | "detect"
    layout: str                       # "cf" [1,C,N] | "cl" [1,N,C] | "v5" [1,N,5+C] | "e2e" [1,K,6(+kpts)]
    num_classes: int
    names: dict[int, str]
    kpt_shape: Optional[tuple[int, int]]
    input_name: str
    input_size: tuple[int, int]       # (w, h)
    input_dtype: Any = np.float32


# ------------------------------------------------------------------ helpers


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy non-maximum suppression. Returns indices to keep, best first."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[order[1:]] - inter
        iou = inter / np.maximum(union, 1e-9)
        order = order[1:][iou <= iou_threshold]
    return keep


def _ewma(prev: Optional[float], value: float, alpha: float = 0.1) -> float:
    """Exponentially weighted mean; the first sample seeds it."""
    return float(value) if prev is None else prev + alpha * (float(value) - prev)


def _parse_literal(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None


def build_model_spec(path: Path, meta: dict[str, str], inputs, outputs) -> ModelSpec:
    """Decide task and output layout from ONNX metadata first, shape second.

    Ultralytics exports carry ``kpt_shape`` and ``names`` in the custom
    metadata map; those are authoritative. The channel count is only used to
    pick between layouts, and to recognise legacy YOLOv5/v8 detect exports
    that carry no metadata.
    """
    inp = inputs[0]
    shape = inp.shape
    input_size = (640, 640)
    if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int):
        input_size = (int(shape[3]), int(shape[2]))
    dtype = np.float16 if "float16" in (inp.type or "") else np.float32

    names_raw = _parse_literal(meta.get("names"))
    names: dict[int, str] = {}
    if isinstance(names_raw, dict):
        names = {int(k): str(v) for k, v in names_raw.items()}
    elif isinstance(names_raw, (list, tuple)):
        names = {i: str(v) for i, v in enumerate(names_raw)}
    kpt = _parse_literal(meta.get("kpt_shape"))
    kpt_shape = (int(kpt[0]), int(kpt[1])) if isinstance(kpt, (list, tuple)) and len(kpt) == 2 else None
    end2end = str(meta.get("end2end", "")).lower() == "true"

    out_shape = outputs[0].shape
    if len(out_shape) != 3 or not all(isinstance(d, int) for d in out_shape[1:]):
        raise ValueError(f"unsupported output shape {out_shape}; expected a static [1, C, N] tensor")
    d1, d2 = int(out_shape[1]), int(out_shape[2])
    kpt_len = kpt_shape[0] * kpt_shape[1] if kpt_shape else 0

    if end2end:
        # [1, max_det, 6 + kpts]: x1, y1, x2, y2, score, class, (kpts...)
        if d2 != 6 + kpt_len:
            raise ValueError(f"end2end output width {d2} does not match kpt_shape {kpt_shape}")
        nc = max(len(names), 1)
        task = "pose" if kpt_shape else "detect"
        return ModelSpec(path, task, "e2e", nc, names, kpt_shape, inp.name, input_size, dtype)

    if names:
        # Metadata says exactly how many channels an anchor has.
        c_exp = 4 + len(names) + kpt_len
        task = "pose" if kpt_shape else "detect"
        if d1 == c_exp:
            layout = "cf"                  # [1, C, N]: YOLOv8 / YOLO11 / YOLO26
        elif d2 == c_exp:
            layout = "cl"                  # [1, N, C]: same, anchor-major
        elif not kpt_shape and d2 == 5 + len(names):
            layout = "v5"                  # [1, N, 5+C]: objectness column
        else:
            raise ValueError(
                f"output {out_shape} is inconsistent with metadata: {len(names)} class name(s)"
                + (f", kpt_shape {kpt_shape}" if kpt_shape else "")
                + f" -> expected {c_exp} channels"
            )
        return ModelSpec(path, task, layout, len(names), names, kpt_shape, inp.name, input_size, dtype)

    # No class names: fall back to the shape, which is unambiguous for real
    # exports (thousands of anchors vs tens of channels).
    channels, layout = (d1, "cf") if d1 < d2 else (d2, "cl")
    if kpt_shape:
        nc = channels - 4 - kpt_len
        if nc < 1:
            raise ValueError(f"output {out_shape} cannot hold kpt_shape {kpt_shape}")
        return ModelSpec(path, "pose", layout, nc, {0: "person"}, kpt_shape, inp.name, input_size, dtype)
    if layout == "cf":
        if channels == 4 + 1 + 17 * 3:
            # A pose export stripped of metadata: 1 class + 17x3 keypoints.
            return ModelSpec(path, "pose", "cf", 1, {0: "person"}, (17, 3), inp.name, input_size, dtype)
        return ModelSpec(path, "detect", "cf", channels - 4, {}, None, inp.name, input_size, dtype)
    # [1, N, 5 + C] without metadata: legacy YOLOv5 detect export.
    nc = d2 - 5
    if nc < 1:
        raise ValueError(f"unsupported output shape {out_shape}")
    return ModelSpec(path, "detect", "v5", nc, {}, None, inp.name, input_size, dtype)


def letterbox(frame: np.ndarray, size: tuple[int, int], dtype=np.float32):
    """Resize preserving aspect ratio and pad to the network size.

    Returns (blob [1,3,H,W], scale, pad_x, pad_y).
    """
    import cv2

    net_w, net_h = size
    h, w = frame.shape[:2]
    scale = min(net_w / w, net_h / h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    canvas = np.full((net_h, net_w, 3), 114, dtype=np.uint8)
    pad_x, pad_y = (net_w - new_w) // 2, (net_h - new_h) // 2
    canvas[pad_y: pad_y + new_h, pad_x: pad_x + new_w] = resized

    blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(dtype)
    blob *= dtype(1.0 / 255.0)
    return np.ascontiguousarray(blob), scale, pad_x, pad_y


def decode_output(
    raw: np.ndarray,
    spec: ModelSpec,
    conf_threshold: float,
    class_ids: Optional[set[int]] = None,
    max_candidates: int = 1000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """Raw network output -> candidate (boxes_xyxy, scores, class_ids, keypoints).

    Everything stays in network (letterbox) space; NMS and the mapping back to
    frame pixels happen afterwards. ``class_ids`` restricts which classes are
    returned (the person path passes {0}).
    """
    pred = np.asarray(raw)
    if pred.ndim == 3:
        pred = pred[0]
    pred = pred.astype(np.float32, copy=False)
    k = spec.kpt_shape
    kpt_len = k[0] * k[1] if k else 0
    empty = (np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, np.int64),
             np.zeros((0, k[0], k[1]), np.float32) if k else None)

    if spec.layout == "e2e":
        boxes = pred[:, :4]
        scores = pred[:, 4]
        cls = pred[:, 5].astype(np.int64)
        kpts = pred[:, 6: 6 + kpt_len] if k else None
    else:
        if spec.layout in ("cf", "cl"):
            if spec.layout == "cf":
                pred = pred.T                     # [N, C]
            boxes_xywh = pred[:, :4]
            class_scores = pred[:, 4: 4 + spec.num_classes]
            kpts = pred[:, 4 + spec.num_classes: 4 + spec.num_classes + kpt_len] if k else None
        else:                                     # v5
            boxes_xywh = pred[:, :4]
            class_scores = pred[:, 5: 5 + spec.num_classes] * pred[:, 4:5]
            kpts = None
        cls = class_scores.argmax(axis=1)
        scores = class_scores[np.arange(len(cls)), cls]
        cx, cy, bw, bh = boxes_xywh.T
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

    mask = scores >= conf_threshold
    if class_ids is not None:
        mask &= np.isin(cls, list(class_ids))
    if not np.any(mask):
        return empty
    idx = np.nonzero(mask)[0]
    if len(idx) > max_candidates:
        idx = idx[np.argsort(scores[idx])[::-1][:max_candidates]]
    kp = kpts[idx].reshape(len(idx), k[0], k[1]) if (k and kpts is not None) else None
    return boxes[idx], scores[idx], cls[idx], kp


def unletterbox(
    boxes: np.ndarray,
    kpts: Optional[np.ndarray],
    scale: float,
    pad_x: int,
    pad_y: int,
    frame_w: int,
    frame_h: int,
) -> tuple[np.ndarray, Optional[np.ndarray]]:
    """Map network-space boxes/keypoints back to original frame pixels."""
    b = boxes.copy()
    b[:, [0, 2]] = np.clip((b[:, [0, 2]] - pad_x) / scale, 0, frame_w)
    b[:, [1, 3]] = np.clip((b[:, [1, 3]] - pad_y) / scale, 0, frame_h)
    k = None
    if kpts is not None:
        k = kpts.astype(np.float32).copy()
        k[..., 0] = np.clip((k[..., 0] - pad_x) / scale, 0, frame_w)
        k[..., 1] = np.clip((k[..., 1] - pad_y) / scale, 0, frame_h)
        if k.shape[-1] == 2:  # (x, y) only: visibility is unknown, not 1
            k = np.concatenate([k, np.full(k.shape[:-1] + (1,), np.nan, np.float32)], axis=-1)
    return b, k


def _class_aware_nms(boxes, scores, cls, iou_threshold) -> list[int]:
    if len(boxes) == 0:
        return []
    offset = cls.astype(np.float32)[:, None] * 8192.0
    return _nms(boxes + offset, scores, iou_threshold)


def _parse_class_filter(spec: str, names: dict[int, str]) -> set[int]:
    wanted: set[int] = set()
    by_name = {v.lower(): k for k, v in names.items()}
    for token in (t.strip() for t in (spec or "").split(",")):
        if not token:
            continue
        if token.isdigit():
            wanted.add(int(token))
        elif token.lower() in by_name:
            wanted.add(by_name[token.lower()])
        else:
            logger.warning(f"Object class '{token}' is not in the object model's class list; ignored")
    wanted.discard(PERSON_CLASS_ID)  # people come from the pose model
    return wanted


def _short(msg: str, limit: int = 420) -> str:
    """Keep an ORT error readable: drop C++ template noise, keep head and tail."""
    msg = " ".join(re.sub(r"std::conditional_t<[^>]*>[^\]]*\]", "", msg).split())
    if len(msg) <= limit:
        return msg
    return msg[: limit // 3] + " ... " + msg[-(2 * limit // 3):]


def _probe_tensorrt_libs() -> str:
    """Why libnvinfer could not be loaded, for the TensorRT fallback reason."""
    import ctypes

    errors = []
    for name in ("libnvinfer.so.10", "libnvinfer.so.8", "nvinfer_10.dll", "nvinfer.dll"):
        try:
            ctypes.CDLL(name)
            return f"{name} loads"
        except OSError as e:
            errors.append(str(e))
    return errors[0] if errors else "libnvinfer not found"


def cpu_thread_budget() -> int:
    """ORT threads all CPU-provider sessions may use together.

    ``INFERENCE_CPU_THREADS`` > 0 is taken as is; otherwise half the CPUs this
    process may run on (its affinity mask, so a cpuset/container limit counts).
    """
    fixed = int(settings.INFERENCE_CPU_THREADS)
    if fixed > 0:
        return fixed
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 2
    return max(1, n // 2)


def cpu_threads_for(role: str) -> int:
    """Share of the budget per session. Pose and object inference can run at
    the same time (separate locks), so their shares add up to the budget."""
    budget = cpu_thread_budget()
    if role == "pose":
        return max(1, budget - budget // 3)
    return max(1, budget // 3)


def _nvidia_device_name(device_id: int) -> Optional[str]:
    import shutil
    import subprocess

    if not shutil.which("nvidia-smi"):
        return None
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    target = str(device_id)
    if visible:
        ids = [v.strip() for v in visible.split(",") if v.strip()]
        if device_id < len(ids):
            target = ids[device_id]
    try:
        res = subprocess.run(
            ["nvidia-smi", "-i", target, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


# ------------------------------------------------- top-down keypoint refiner

_RTM_MEAN = np.array([123.675, 116.28, 103.53], np.float32)
_RTM_STD = np.array([58.395, 57.12, 57.375], np.float32)


def refiner_crop(frame: np.ndarray, box, size_hw: tuple[int, int], padding: float = 1.25):
    """Affine crop of one person for a top-down (RTMPose/SimCC) model.

    The box is padded by ``padding`` and widened/heightened to the model's
    aspect ratio (mmpose TopDownGetBboxCenterScale + TopDownAffine, no
    rotation). Returns (crop HxWx3 BGR, (cx, cy, crop_w, crop_h)).
    """
    import cv2

    h, w = size_hw
    x1, y1, x2, y2 = (float(v) for v in box)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    bw, bh = max(x2 - x1, 1.0) * padding, max(y2 - y1, 1.0) * padding
    ar = w / h
    if bw > bh * ar:
        bh = bw / ar
    else:
        bw = bh * ar
    s = w / bw
    m = np.array([[s, 0.0, w / 2.0 - cx * s], [0.0, s, h / 2.0 - cy * s]], np.float32)
    crop = cv2.warpAffine(frame, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    return crop, (cx, cy, bw, bh)


def refiner_decode(simcc_x: np.ndarray, simcc_y: np.ndarray, metas, size_hw, frame_w: int, frame_h: int,
                   split_ratio: float = 2.0) -> np.ndarray:
    """SimCC argmax decode -> (N, K, 3) keypoints in frame pixels.

    The third column is the raw SimCC confidence (min of the x and y peaks,
    as mmpose), not yet a visibility; see ``POSE_REFINER_SCORE_SCALE``.
    """
    h, w = size_hw
    xi = simcc_x.argmax(-1).astype(np.float32) / split_ratio
    yi = simcc_y.argmax(-1).astype(np.float32) / split_ratio
    conf = np.minimum(simcc_x.max(-1), simcc_y.max(-1)).astype(np.float32)
    out = np.zeros(xi.shape + (3,), np.float32)
    for n, (cx, cy, bw, bh) in enumerate(metas):
        out[n, :, 0] = np.clip(xi[n] * (bw / w) + cx - bw / 2.0, 0, frame_w)
        out[n, :, 1] = np.clip(yi[n] * (bh / h) + cy - bh / 2.0, 0, frame_h)
        out[n, :, 2] = conf[n]
    return out


# ------------------------------------------------------------ the detector


class PersonDetector:
    """YOLO pose (or plain detect) person model plus an optional object model.

    Construction is free; ``initialise()`` does the probing and loading.
    """

    def __init__(
        self,
        model_path: Optional[Path | str] = None,
        object_model_path: Optional[Path | str] = None,
    ):
        forced = model_path or (settings.POSE_MODEL_PATH or None)
        self._forced_model: Optional[Path] = Path(forced) if forced else None
        if object_model_path is None:
            object_model_path = settings.OBJECT_MODEL_PATH
        self._object_model_path: Optional[Path] = Path(object_model_path) if object_model_path else None

        self.session = None
        self.spec: Optional[ModelSpec] = None
        self.model_path: Optional[Path] = self._forced_model
        self.provider: str = "none"
        self.execution_provider: Optional[str] = None
        self.backend: str = "not_initialised"
        self.device_name: str = "none"
        self.last_error: Optional[str] = None
        self.provider_attempts: list[dict] = []
        self.hailo: dict = {}
        self.available_providers: list[str] = []
        self.ort_version: Optional[str] = None
        self.dlls_preloaded: Optional[str] = None
        self.warmup_ms: Optional[float] = None
        self.warmup_steady_ms: Optional[float] = None
        self.load_ms: Optional[float] = None
        # AMD MIGraphX plugin EP: prepare() result, and the state of compiling
        # the models for the GPU (idle | compiling | done | failed).
        self.migraphx: dict = {}
        self.gpu_compile: dict = {"state": "idle"}
        self.cpu_threads: dict = {}
        # How the pose model was chosen (budget, candidates measured).
        self.selection: dict = {}

        self.object_session = None
        self.object_spec: Optional[ModelSpec] = None
        self.object_class_ids: set[int] = set()
        self.object_provider: Optional[str] = None
        self.object_error: Optional[str] = None
        self.object_warmup_ms: Optional[float] = None
        self._object_label: Optional[str] = None

        # Optional top-down keypoint refiner (RTMPose, Apache-2.0).
        self.refiner_session = None
        self.refiner_path: Optional[Path] = None
        self.refiner_input_hw: tuple[int, int] = (256, 192)
        self.refiner_enabled = False
        self.refiner_reason: str = "not initialised"
        self.refiner_batch_ms: Optional[float] = None
        self._refiner_lock = threading.Lock()
        self._refine_ms: deque[float] = deque(maxlen=120)

        self._initialised = False
        self._init_lock = threading.Lock()
        # One lock per session; only session.run() is held under it so
        # pre/post-processing of other cameras proceeds in parallel.
        self._run_lock = threading.Lock()
        self._obj_run_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._infer_ms: deque[float] = deque(maxlen=120)
        self._total_ms: deque[float] = deque(maxlen=120)
        self._obj_infer_ms: deque[float] = deque(maxlen=120)
        self._frames = 0
        self._rejected = 0
        # One run at a time on an accelerator (MIGraphX/CUDA serialise anyway;
        # queueing here keeps the measured run times pure device time), and the
        # load measurements the scheduler reads (load_metrics()).
        self._device_lock = threading.Lock()
        self._busy_ms = 0.0
        self._pose_ewma: Optional[float] = None
        self._refiner_frame_ewma: Optional[float] = None

    # ------------------------------------------------------------- probing

    def _probe_hailo(self) -> dict:
        """Report the NPU as it is. There is no HEF runner in this build."""
        present = os.path.exists(settings.HAILO_DEVICE)
        info: dict = {"device": settings.HAILO_DEVICE, "device_present": present,
                      "hailo_platform": False, "hef": None, "runner": False, "selected": False}
        if not present:
            info["reason"] = "no Hailo device node"
            return info
        try:
            import hailo_platform  # noqa: F401
            info["hailo_platform"] = True
        except ImportError:
            pass
        hef = Path(getattr(settings, "HAILO_POSE_HEF_PATH", "") or "models_hef/yolo26_pose.hef")
        info["hef"] = str(hef) if hef.exists() else None
        info["reason"] = (
            "Hailo device present but this build has no HailoRT pose runner"
            + ("" if info["hef"] else f" and no compiled HEF at {hef}")
            + ("" if info["hailo_platform"] else "; hailo_platform not importable")
            + "; falling through to ONNX Runtime"
        )
        logger.info(info["reason"])
        return info

    def _model_candidates(self, accelerator: bool) -> list[Path]:
        """Pose models to consider on this provider class, most accurate first.

        The ladder (``POSE_MODEL_LADDER_GPU`` / ``_CPU``) lists larger or
        higher-resolution exports; ``_fit_to_budget`` walks down it until one
        meets the measured latency budget. ``POSE_MODEL_GPU`` / ``_CPU`` stay
        as the floor, then the other class's model as the last resort.
        """
        if self._forced_model is not None:
            return [self._forced_model]
        d = Path(settings.MODELS_DIR)
        ladder = settings.POSE_MODEL_LADDER_GPU if accelerator else settings.POSE_MODEL_LADDER_CPU
        names = [n.strip() for n in (ladder or "").split(",") if n.strip()]
        primary, other = ((settings.POSE_MODEL_GPU, settings.POSE_MODEL_CPU) if accelerator
                          else (settings.POSE_MODEL_CPU, settings.POSE_MODEL_GPU))
        out: list[Path] = []
        for n in [*names, primary, other]:
            p = d / n
            if p not in out:
                out.append(p)
        return out

    @staticmethod
    def _count_ai_cameras() -> Optional[int]:
        """Enabled analytics cameras in the database (read-only), or None."""
        import sqlite3

        db = Path(settings.DATABASE_PATH)
        if not db.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM cameras WHERE is_ai_enabled = 1 "
                    "AND rtsp_url IS NOT NULL AND rtsp_url != ''"
                ).fetchone()
            finally:
                con.close()
            return int(row[0]) if row else None
        except Exception:
            return None

    def latency_budget(self) -> tuple[float, str]:
        """Per-frame pose latency the box can afford, and how it was derived.

        ``POSE_LATENCY_BUDGET_MS`` > 0 is taken as is. Otherwise: one shared
        session serves every analytics camera, each analysed at
        RECORDING_FPS / ANALYTICS_DETECT_EVERY_N_FRAMES frames per second
        (capped at ANALYTICS_TARGET_DETECT_FPS while the scheduler is on, the
        rate ``inference_scheduler`` re-fits for), and only
        ``POSE_BUDGET_UTILISATION`` of the accelerator is planned for.
        """
        fixed = float(settings.POSE_LATENCY_BUDGET_MS)
        if fixed > 0:
            return fixed, "POSE_LATENCY_BUDGET_MS"
        streams = int(settings.POSE_BUDGET_STREAMS)
        source = "POSE_BUDGET_STREAMS"
        if streams <= 0:
            counted = self._count_ai_cameras()
            streams, source = (counted, "enabled cameras") if counted else (4, "default (no cameras yet)")
        per_stream = max(float(settings.RECORDING_FPS) / max(1, int(settings.ANALYTICS_DETECT_EVERY_N_FRAMES)), 0.5)
        if settings.ANALYTICS_SCHEDULER:
            per_stream = min(per_stream, max(float(settings.ANALYTICS_TARGET_DETECT_FPS),
                                             float(settings.ANALYTICS_MIN_DETECT_FPS), 0.1))
        budget = 1000.0 * float(settings.POSE_BUDGET_UTILISATION) / (max(1, streams) * per_stream)
        budget = min(max(budget, 5.0), 150.0)
        return round(budget, 1), f"auto: {streams} stream(s) [{source}] x {per_stream:g} fps"

    def _fit_to_budget(self, ort) -> None:
        """Walk down the model ladder until the measured latency fits the budget.

        The first (most accurate) loadable candidate is already running and
        warmed up. If its steady latency exceeds the budget, the next ones are
        loaded on the same provider and measured the same way; the first that
        fits wins, else the fastest measured. Every measurement is reported.
        """
        budget, source = self.latency_budget()
        if self.refiner_enabled and self.refiner_batch_ms:
            # The refiner runs after the pose model on every analysed frame.
            source += f"; minus refiner {self.refiner_batch_ms} ms"
            budget = round(max(budget - self.refiner_batch_ms, 1.0), 1)
        accel = self.provider not in ("cpu", "openvino")
        cands = [p for p in self._model_candidates(accel) if p.exists()]
        tried = [{"model": self.model_path.name, "input_size": list(self.spec.input_size),
                  "steady_ms": self.warmup_steady_ms}]
        self.selection = {"budget_ms": budget, "budget_source": source, "tried": tried,
                          "chosen": self.model_path.name, "reason": "most accurate candidate fits"}
        if self._forced_model is not None:
            self.selection["reason"] = "pinned by POSE_MODEL_PATH"
            return
        if (self.warmup_steady_ms or 0.0) <= budget:
            return
        best = (self.warmup_steady_ms or float("inf"), self.session, self.spec, self.model_path,
                self.warmup_ms, self.warmup_steady_ms)
        try:
            start = cands.index(self.model_path) + 1
        except ValueError:
            start = len(cands)
        for path in cands[start:]:
            sess, reason = self._create_session(ort, path, self.execution_provider)
            if sess is None:
                tried.append({"model": path.name, "error": reason})
                continue
            try:
                spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                        sess.get_inputs(), sess.get_outputs())
                if spec.task != "pose":
                    raise ValueError(f"{path.name} is a {spec.task} model")
                first, steady = self._warm_up(sess, spec, threading.Lock())
            except Exception as e:
                tried.append({"model": path.name, "error": _short(str(e))})
                continue
            self._mark_compiled(path)
            tried.append({"model": path.name, "input_size": list(spec.input_size), "steady_ms": steady})
            if (steady or float("inf")) < best[0]:
                best = (steady or float("inf"), sess, spec, path, first, steady)
            if steady is not None and steady <= budget:
                break
        _, self.session, self.spec, self.model_path, self.warmup_ms, self.warmup_steady_ms = best
        fits = (self.warmup_steady_ms or float("inf")) <= budget
        self.selection["chosen"] = self.model_path.name
        self.selection["reason"] = ("largest candidate within budget" if fits
                                    else "no candidate fits the budget; fastest measured")
        logger.info(
            f"Pose model chosen for a {budget} ms budget ({source}): {self.model_path.name} "
            f"steady {self.warmup_steady_ms} ms; tried "
            + ", ".join(f"{t['model']}={t.get('steady_ms', t.get('error'))}" for t in tried)
        )

    @staticmethod
    def _provider_options(ep: str) -> dict:
        dev = int(settings.INFERENCE_DEVICE_ID)
        if ep == "TensorrtExecutionProvider":
            cache = Path(settings.TRT_ENGINE_CACHE_DIR)
            try:
                cache.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return {
                "device_id": dev,
                "trt_fp16_enable": bool(settings.TRT_FP16),
                "trt_engine_cache_enable": True,
                "trt_engine_cache_path": str(cache),
                "trt_timing_cache_enable": True,
                "trt_timing_cache_path": str(cache),
            }
        if ep == "CUDAExecutionProvider":
            return {
                "device_id": dev,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "do_copy_in_default_stream": True,
            }
        if ep in ("ROCMExecutionProvider", "MIGraphXExecutionProvider", "DmlExecutionProvider"):
            return {"device_id": dev}
        return {}

    def _providers_for(self, ep: str) -> list:
        chain: list = [(ep, self._provider_options(ep))]
        if ep == "TensorrtExecutionProvider":
            chain.append(("CUDAExecutionProvider", self._provider_options("CUDAExecutionProvider")))
        if ep != "CPUExecutionProvider":
            chain.append("CPUExecutionProvider")
        return chain

    @staticmethod
    def _is_plugin(ep: Optional[str]) -> bool:
        """``ep`` is the MIGraphX plugin EP, registered in this process."""
        return ep == amd.PLUGIN_EP and bool(amd.devices())

    def _limit_cpu_threads(self, opts, role: str) -> None:
        n = cpu_threads_for(role)
        opts.intra_op_num_threads = n
        opts.inter_op_num_threads = 1
        if not settings.INFERENCE_CPU_SPINNING and hasattr(opts, "add_session_config_entry"):
            # A spinning pool keeps every worker at 100 % between frames.
            opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.cpu_threads = {**self.cpu_threads, "budget": cpu_thread_budget(), role: n,
                            "spinning": bool(settings.INFERENCE_CPU_SPINNING)}

    def _mark_compiled(self, path: Path, tag: str = "default", ep: Optional[str] = None) -> None:
        """Record in the MIGraphX cache manifest that ``path`` is compiled (after a real run)."""
        if not self._is_plugin(ep or self.execution_provider) or amd.is_warm(amd.cache_dir(), path, tag):
            return
        if amd.mark_warm(amd.cache_dir(), path, tag):
            done = self.gpu_compile.setdefault("compiled", [])
            if path.name not in done:
                done.append(path.name)

    def _create_session(self, ort, path: Path, ep: str, role: str = "pose", tag: str = "default"):
        """Create a session on ``ep`` and return (session | None, reason | None).

        ORT prints its own fallback notice to stdout and silently lands on the
        next provider, so success is decided by ``get_providers()[0]`` *and*
        the absence of that notice. The MIGraphX plugin EP is attached through
        its device objects: requested by name it would be ignored silently.
        ``role`` picks the CPU thread share; ``tag`` names the input shape
        for the MIGraphX compiled-program manifest.
        """
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        plugin = self._is_plugin(ep)
        if ep == "CPUExecutionProvider" or plugin:
            # A GPU session keeps a CPU pool for the few nodes left on the CPU.
            self._limit_cpu_threads(opts, role)
        if plugin and not amd.is_warm(amd.cache_dir(), path, tag):
            self.gpu_compile["current"] = path.name
            logger.warning(f"Compiling {path.name} for the AMD GPU (MIGraphX, first use of this model and "
                           f"shape; typically 1-3 min). Cached in {amd.cache_dir()}")
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                if plugin:
                    amd.add_to_session_options(opts, int(settings.INFERENCE_DEVICE_ID), bool(settings.MIGRAPHX_FP16))
                    sess = ort.InferenceSession(str(path), sess_options=opts)
                else:
                    sess = ort.InferenceSession(str(path), sess_options=opts, providers=self._providers_for(ep))
        except Exception as e:
            return None, _short(f"{type(e).__name__}: {e}")
        finally:
            self.gpu_compile.pop("current", None)
        got = (sess.get_providers() or ["none"])[0]
        fell_back = "falling back" in captured.getvalue().lower()
        if got == ep and not fell_back:
            return sess, None
        # ORT's banner: "*** EP Error *** EP Error <what> when using [...]".
        detail = " ".join(re.sub(r"\*+|EP Error", " ", captured.getvalue()).split())
        detail = re.sub(r"\s*when using \[.*$", "", detail)   # drop the echoed provider options
        reason = (f"session landed on {got}, not {ep}" if got != ep
                  else f"ONNX Runtime fell back from {ep} while creating the session")
        if ep == "TensorrtExecutionProvider":
            reason += f" ({_probe_tensorrt_libs()})"
        if detail:
            reason += f": {detail}"
        return None, _short(reason)

    @staticmethod
    def _disabled_labels() -> set[str]:
        return {t.strip().lower() for t in settings.INFERENCE_DISABLED_PROVIDERS.split(",") if t.strip()}

    def _first_selectable_ep(self) -> Optional[str]:
        disabled = self._disabled_labels()
        in_build = set(self.available_providers)
        return next((ep for ep, label, _ in _PROVIDER_PRIORITY if ep in in_build and label not in disabled), None)

    def _select(self, ort, skip: frozenset = frozenset(), only: Optional[set] = None) -> None:
        """Walk the provider chain; sets session/provider or leaves last_error.

        ``skip`` leaves providers out (a GPU still compiling in the
        background); ``only`` restricts the walk (the background compile).
        """
        disabled = self._disabled_labels()
        in_build = set(self.available_providers)
        for ep, label, accel in _PROVIDER_PRIORITY:
            if ep not in in_build or ep in skip or (only is not None and ep not in only):
                continue
            if label in disabled:
                self.provider_attempts.append({"provider": label, "ok": False,
                                               "reason": "disabled by INFERENCE_DISABLED_PROVIDERS"})
                continue
            path = next((p for p in self._model_candidates(accel) if p.exists()), None)
            if path is None:
                reason = "model file not found: " + ", ".join(str(p) for p in self._model_candidates(accel))
                self.provider_attempts.append({"provider": label, "ok": False, "reason": reason})
                self.last_error = reason
                continue
            started = time.perf_counter()
            sess, reason = self._create_session(ort, path, ep)
            if sess is None:
                self.provider_attempts.append({"provider": label, "ok": False, "model": path.name,
                                               "reason": reason})
                logger.warning(f"Inference provider {label} passed over: {reason}")
                continue
            try:
                spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                        sess.get_inputs(), sess.get_outputs())
            except Exception as e:
                reason = f"model {path.name} not usable: {e}"
                self.provider_attempts.append({"provider": label, "ok": False, "model": path.name,
                                               "reason": reason})
                self.last_error = reason
                logger.error(reason)
                continue
            if spec.task == "detect" and spec.names and spec.names.get(PERSON_CLASS_ID, "person") != "person":
                reason = f"model {path.name} has no person class"
                self.provider_attempts.append({"provider": label, "ok": False, "reason": reason})
                self.last_error = reason
                continue
            self.load_ms = round((time.perf_counter() - started) * 1000.0, 1)
            self.provider_attempts.append({"provider": label, "ok": True, "model": path.name})
            self.session, self.spec, self.model_path = sess, spec, path
            self.provider, self.execution_provider = label, ep
            self.backend = "onnxruntime"
            self.last_error = None
            if ep in ("TensorrtExecutionProvider", "CUDAExecutionProvider"):
                name = _nvidia_device_name(int(settings.INFERENCE_DEVICE_ID))
                self.device_name = f"{label.upper()}:{settings.INFERENCE_DEVICE_ID}" + (f" {name}" if name else "")
            elif self._is_plugin(ep):
                gfx = self.migraphx.get("gfx")
                self.device_name = f"MIGRAPHX:{settings.INFERENCE_DEVICE_ID}" + (f" {gfx}" if gfx else "")
            else:
                self.device_name = ep.replace("ExecutionProvider", "")
            return

    def _warm_up(self, session, spec: ModelSpec, lock: threading.Lock) -> tuple[float, Optional[float]]:
        """Run the model on a blank input: first call pays kernel/graph setup."""
        w, h = spec.input_size
        x = np.zeros((1, 3, h, w), dtype=spec.input_dtype)
        runs = max(1, int(settings.INFERENCE_WARMUP_RUNS))
        times = []
        for _ in range(runs + 1):
            with lock:
                t = time.perf_counter()
                session.run(None, {spec.input_name: x})
                times.append((time.perf_counter() - t) * 1000.0)
        self._account(sum(times), self.provider)
        steady = round(sum(times[1:]) / len(times[1:]), 2) if len(times) > 1 else None
        return round(times[0], 1), steady

    def _load_refiner(self, ort) -> None:
        """Load the optional top-down refiner and decide whether it runs.

        ``POSE_REFINER``: "off"; "on" (any provider); "auto" (default): only
        on an accelerator, and only when a batch of
        ``POSE_REFINER_BUDGET_PERSONS`` crops costs at most half the latency
        budget. On a CPU-only box auto leaves it off.
        """
        mode = (settings.POSE_REFINER or "auto").strip().lower()
        if mode in ("off", "0", "false", "no", ""):
            self.refiner_reason = "disabled (POSE_REFINER=off)"
            return
        accel = self.provider not in ("cpu", "openvino")
        if mode == "auto" and not accel:
            self.refiner_reason = f"auto: off on the {self.provider} provider"
            return
        path = Path(settings.POSE_REFINER_MODEL)
        if not path.is_absolute():
            path = Path(settings.MODELS_DIR) / path
        if not path.exists():
            self.refiner_reason = f"model file not found: {path}"
            return
        n = max(1, int(settings.POSE_REFINER_BUDGET_PERSONS))
        sess, reason = self._create_session(ort, path, self.execution_provider, role="refiner", tag=f"b{n}")
        if sess is None:
            self.refiner_reason = f"could not load on {self.provider}: {reason}"
            return
        inp = sess.get_inputs()[0]
        outs = [o.name for o in sess.get_outputs()]
        if len(inp.shape) != 4 or not all(isinstance(v, int) for v in inp.shape[2:]) or len(outs) != 2:
            self.refiner_reason = f"{path.name}: unexpected IO {inp.shape} -> {outs}"
            return
        h, w = int(inp.shape[2]), int(inp.shape[3])
        x = np.zeros((n, 3, h, w), np.float32)
        times = []
        for _ in range(max(1, int(settings.INFERENCE_WARMUP_RUNS)) + 1):
            t = time.perf_counter()
            sess.run(None, {inp.name: x})
            times.append((time.perf_counter() - t) * 1000.0)
        batch_ms = round(sum(times[1:]) / len(times[1:]), 2) if len(times) > 1 else round(times[0], 2)
        self._mark_compiled(path, f"b{n}")
        self.refiner_session, self.refiner_path, self.refiner_input_hw = sess, path, (h, w)
        self.refiner_batch_ms = batch_ms
        budget, _ = self.latency_budget()
        if mode == "auto" and batch_ms > 0.5 * budget:
            # Loaded but idle: the scheduler may switch it on when the load drops.
            self.refiner_reason = (f"auto: {n} crops take {batch_ms} ms, over half the "
                                   f"{budget} ms budget")
            return
        self.refiner_enabled = True
        self.refiner_reason = f"{mode}: {n} crops in {batch_ms} ms on {self.provider}"
        logger.info(f"Keypoint refiner {path.name} enabled ({self.refiner_reason})")

    def _refine(self, frame: np.ndarray, dets: list["Detection"]) -> float:
        """Replace each detection's keypoints with the top-down estimate.

        Returns the milliseconds spent in the refiner session (device time).

        Runs on the ``POSE_REFINER_MAX_PERSONS`` most confident people in one
        batch. Visibility becomes ``max(pose-model visibility, refiner
        confidence x POSE_REFINER_SCORE_SCALE)`` clipped to 1, so a joint the
        pose model saw clearly never loses its visibility.
        """
        with self._refiner_lock:
            sess, hw = self.refiner_session, self.refiner_input_hw
        if sess is None or not dets:
            return 0.0
        chosen = sorted((d for d in dets if d.keypoints is not None),
                        key=lambda d: d.confidence, reverse=True)[: max(1, int(settings.POSE_REFINER_MAX_PERSONS))]
        if not chosen:
            return 0.0
        started = time.perf_counter()
        crops, metas = [], []
        for d in chosen:
            c, m = refiner_crop(frame, d.bbox, hw)
            crops.append(c)
            metas.append(m)
        x = np.stack(crops)[..., ::-1].astype(np.float32)
        x = ((x - _RTM_MEAN) / _RTM_STD).transpose(0, 3, 1, 2)
        # Always run the batch size used at warm-up: on CUDA every change of
        # input shape re-plans the graph (~45 ms measured on an RTX 2050, vs
        # 5.6 ms for a batch of 4), so crops are padded to fixed-size chunks.
        b = max(1, int(settings.POSE_REFINER_BUDGET_PERSONS))
        n = x.shape[0]
        pad = (-n) % b
        if pad:
            x = np.concatenate([x, np.zeros((pad,) + x.shape[1:], np.float32)])
        name = sess.get_inputs()[0].name
        outs_x, outs_y = [], []
        run_ms = 0.0
        with self._refiner_lock:
            for i in range(0, x.shape[0], b):
                with self._gate(self.provider):
                    t = time.perf_counter()
                    ox, oy = sess.run(None, {name: np.ascontiguousarray(x[i: i + b])})
                    run_ms += (time.perf_counter() - t) * 1000.0
                outs_x.append(ox)
                outs_y.append(oy)
        self._account(run_ms, self.provider)
        sx, sy = np.concatenate(outs_x)[:n], np.concatenate(outs_y)[:n]
        fh, fw = frame.shape[:2]
        refined = refiner_decode(sx, sy, metas, hw, fw, fh)
        scale = float(settings.POSE_REFINER_SCORE_SCALE)
        for d, r in zip(chosen, refined):
            k = d.keypoints
            if k.shape != r.shape:
                continue
            out = r.copy()
            out[:, 2] = np.clip(np.maximum(k[:, 2], r[:, 2] * scale), 0.0, 1.0)
            d.keypoints = out
        with self._stats_lock:
            self._refine_ms.append((time.perf_counter() - started) * 1000.0)
        return run_ms

    def _load_object_model(self, ort) -> None:
        path = self._object_model_path
        if path is None:
            self.object_error = "disabled (OBJECT_MODEL_PATH empty)"
            return
        if not path.exists():
            self.object_error = f"model file not found: {path}"
            logger.info(f"Object detection disabled: {self.object_error}")
            return
        # Same provider as the pose model, then CPU as the honest fallback.
        eps = [self.execution_provider] if self.execution_provider else []
        if "CPUExecutionProvider" not in eps:
            eps.append("CPUExecutionProvider")
        reasons = []
        for ep in eps:
            sess, reason = self._create_session(ort, path, ep, role="object")
            if sess is None:
                reasons.append(f"{ep}: {reason}")
                continue
            try:
                spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                        sess.get_inputs(), sess.get_outputs())
                if spec.task != "detect":
                    raise ValueError(f"expected a detect model, got {spec.task}")
                if not spec.names:
                    spec.names = {i: _COCO_FALLBACK_NAMES.get(i, str(i)) for i in range(spec.num_classes)}
            except Exception as e:
                self.object_error = f"model {path.name} not usable: {e}"
                logger.error(self.object_error)
                return
            self.object_session, self.object_spec = sess, spec
            self.object_provider = ep.replace("ExecutionProvider", "").lower()
            self._object_label = next((label for e, label, _ in _PROVIDER_PRIORITY if e == ep), self.object_provider)
            self.object_class_ids = _parse_class_filter(settings.OBJECT_CLASSES, spec.names)
            self.object_warmup_ms, _ = self._warm_up(sess, spec, self._obj_run_lock)
            self._mark_compiled(path, ep=ep)
            self.object_error = None
            return
        self.object_error = "; ".join(reasons) or "no provider"

    def initialise(self) -> dict:
        """Probe, load and warm up. Idempotent and thread-safe."""
        with self._init_lock:
            if self._initialised:
                return self.status()
            self._initialised = True
            self.hailo = self._probe_hailo()
            self.backend = "unavailable"
            try:
                import onnxruntime as ort
            except ImportError as e:
                self.last_error = f"onnxruntime not installed ({e})"
                logger.warning(f"Person detection unavailable: {self.last_error}")
                return self.status()

            self.ort_version = getattr(ort, "__version__", None)
            if hasattr(ort, "preload_dlls"):
                try:
                    ort.preload_dlls()
                    self.dlls_preloaded = "ok"
                except Exception as e:  # CPU-only wheel or missing nvidia pip libs
                    self.dlls_preloaded = f"failed: {e}"
            else:
                self.dlls_preloaded = "not supported by this onnxruntime"
            self.available_providers = list(ort.get_available_providers())
            self._prepare_plugin_eps(ort)

            quiet = hasattr(ort, "set_default_logger_severity")
            if quiet:
                ort.set_default_logger_severity(4)  # our own log carries the reasons
            background = False
            try:
                cold = self._migraphx_cold_models()
                background = bool(cold) and settings.MIGRAPHX_COMPILE_MODE.strip().lower() != "foreground"
                if background:
                    # Compiling takes minutes per model: serve on the next
                    # provider (normally the CPU) meanwhile, and say so.
                    reason = (f"compiling {', '.join(cold)} for the GPU in the background (cold cache); "
                              "the next provider serves until it is done")
                    self.provider_attempts.append({"provider": "migraphx", "ok": False, "reason": reason})
                    self.gpu_compile.update({"state": "compiling", "provider": "migraphx", "models": cold,
                                             "started_at": time.time(), "mode": "background"})
                    logger.warning(f"AMD GPU: {reason}")
                    self._select(ort, skip=frozenset({amd.PLUGIN_EP}))
                else:
                    self._select(ort)
                if self.session is not None:
                    self._finish_loading(ort)
            finally:
                if quiet:
                    ort.set_default_logger_severity(3)
            if self._is_plugin(self.execution_provider) and self.gpu_compile.get("state") == "idle":
                self.gpu_compile.update({"state": "done", "provider": "migraphx", "mode": "foreground"})
            if background:
                threading.Thread(target=self._compile_gpu_in_background, args=(ort,),
                                 name="migraphx-compile", daemon=True).start()
            if self.session is None:
                self.backend = "unavailable"
                self.last_error = self.last_error or "every execution provider failed to initialise"
                logger.warning(
                    f"Person detection unavailable: {self.last_error}. "
                    "No detections will be produced and no analytics will be fabricated."
                )
            return self.status()

    def _prepare_plugin_eps(self, ort) -> None:
        """Register ONNX Runtime plugin EPs (AMD MIGraphX) that apply to this machine."""
        if not amd.applicable() or amd.PLUGIN_EP in self.available_providers:
            return  # nothing to register, or a build with MIGraphX compiled in (named path works)
        if "migraphx" in self._disabled_labels():
            self.migraphx = {"ok": False, "reason": "disabled by INFERENCE_DISABLED_PROVIDERS"}
            return
        self.migraphx = amd.prepare(ort, settings.MIGRAPHX_CACHE_DIR, bool(settings.MIGRAPHX_FP16))
        if self.migraphx.get("ok"):
            self.available_providers.append(amd.PLUGIN_EP)
        else:
            self.provider_attempts.append({"provider": "migraphx", "ok": False,
                                           "reason": f"plugin EP not usable: {self.migraphx.get('reason')}"})

    def _migraphx_cold_models(self) -> list[str]:
        """Models the MIGraphX start would load that are not compiled in the cache yet."""
        if not self._is_plugin(amd.PLUGIN_EP) or self._first_selectable_ep() != amd.PLUGIN_EP:
            return []
        needed: list[tuple[Path, str]] = []
        pose = next((p for p in self._model_candidates(True) if p.exists()), None)
        if pose is not None:
            needed.append((pose, "default"))
        if (settings.POSE_REFINER or "auto").strip().lower() not in ("off", "0", "false", "no", ""):
            ref = Path(settings.POSE_REFINER_MODEL)
            ref = ref if ref.is_absolute() else Path(settings.MODELS_DIR) / ref
            if ref.exists():
                needed.append((ref, f"b{max(1, int(settings.POSE_REFINER_BUDGET_PERSONS))}"))
        if settings.OBJECT_DETECT_EVERY_N > 0 and self._object_model_path and self._object_model_path.exists():
            needed.append((self._object_model_path, "default"))
        cache = amd.cache_dir()
        return [p.name for p, tag in needed if not amd.is_warm(cache, p, tag)]

    # Everything a loaded detector consists of; swapped as one unit by _adopt().
    _ADOPT_FIELDS = (
        "session", "spec", "model_path", "provider", "execution_provider", "backend", "device_name",
        "last_error", "warmup_ms", "warmup_steady_ms", "load_ms", "selection",
        "object_session", "object_spec", "object_class_ids", "object_provider", "object_error",
        "object_warmup_ms", "_object_label", "refiner_session", "refiner_path", "refiner_input_hw", "refiner_enabled",
        "refiner_reason", "refiner_batch_ms",
    )

    def _compile_in_child(self, pose_model: Optional[Path] = None,
                          expect: str = "migraphx") -> tuple[bool, Optional[str]]:
        """Compile the GPU programs in a child process (scripts/prewarm_inference.py).

        Without ``pose_model`` it compiles what the start-up selection loads
        (``--no-ladder``: the other ladder rungs are compiled on demand, or by
        scripts/bootstrap.py at install). With it, just that pose model (the
        scheduler's next rung), plus the refiner and object model from cache.

        Creating an ONNX Runtime session holds the GIL for the whole MIGraphX
        compile (measured: the process stood still for 142 s), so compiling
        in a thread of this process would freeze every camera worker and the
        API. The child runs the same selection code with the same settings and
        fills the same cache; this process then loads from it in about a
        second per model.
        """
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[2] / "scripts" / "prewarm_inference.py"
        env = dict(os.environ, MIGRAPHX_CACHE_DIR=str(settings.MIGRAPHX_CACHE_DIR),
                   POSE_MODEL_PATH=str(pose_model or self._forced_model or ""),
                   OBJECT_MODEL_PATH=str(self._object_model_path or ""))
        summary: dict = {}
        tail: deque[str] = deque(maxlen=5)
        try:
            proc = subprocess.Popen([sys.executable, str(script), "--require-gpu", "--no-ladder"], env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            return False, f"could not start the compiler process: {e}"
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("@@PREWARM@@"):
                try:
                    import json

                    summary = json.loads(line[len("@@PREWARM@@"):])
                except ValueError:
                    pass
                continue
            if "/AMDMIGraphX/" in line:  # MIGraphX's own compiler chatter
                continue
            tail.append(line.strip())
            m = re.search(r"Compiling (\S+) for the AMD GPU", line)
            if m:
                self.gpu_compile["current"] = m.group(1)
                logger.info(f"AMD GPU: compiling {m.group(1)} (background process {proc.pid})")
        rc = proc.wait()
        self.gpu_compile.pop("current", None)
        self.gpu_compile["compiled"] = list((summary.get("gpu_compile") or {}).get("compiled") or [])
        if rc == 0 and summary.get("provider") == expect:
            return True, None
        why = "; ".join(f"{a.get('provider')}: {a.get('reason')}" for a in summary.get("provider_attempts") or []
                        if not a.get("ok")) or " | ".join(tail) or f"exit {rc}"
        return False, _short(f"compiler process ended on {summary.get('provider') or 'nothing'} (exit {rc}): {why}")

    def _compile_gpu_in_background(self, ort) -> None:
        """Compile out of process, then load on MIGraphX in a separate detector and swap it in."""
        started = time.perf_counter()
        ok, error = self._compile_in_child()
        if not ok:
            seconds = round(time.perf_counter() - started, 1)
            self.gpu_compile.update({"state": "failed", "finished_at": time.time(), "seconds": seconds,
                                     "error": error})
            with self._stats_lock:
                self.provider_attempts.append({"provider": "migraphx", "ok": False,
                                               "reason": f"background compile failed: {error}"})
            logger.error(f"AMD GPU background compile failed after {seconds} s, staying on {self.provider}: {error}")
            return
        staging = PersonDetector(model_path=self._forced_model, object_model_path=self._object_model_path or "")
        staging._initialised = True
        staging.hailo, staging.migraphx = self.hailo, self.migraphx
        staging.available_providers = list(self.available_providers)
        staging.ort_version, staging.dlls_preloaded = self.ort_version, self.dlls_preloaded
        staging.gpu_compile = self.gpu_compile  # shared: status() shows the model being compiled
        error = None
        try:
            staging._select(ort, only={amd.PLUGIN_EP})
            if staging.session is not None:
                staging._finish_loading(ort)
        except Exception as e:  # noqa: BLE001 - keep serving on the CPU
            error = _short(f"{type(e).__name__}: {e}")
        seconds = round(time.perf_counter() - started, 1)
        if error is None and staging.available and staging.execution_provider == amd.PLUGIN_EP:
            self._adopt(staging)
            self.gpu_compile.update({"state": "done", "finished_at": time.time(), "seconds": seconds})
            logger.info(f"AMD GPU ready after {seconds} s of background compiling: person model "
                        f"{self.model_path.name} now runs on {self.device_name} (was on the CPU)")
            return
        error = error or staging.last_error or "; ".join(
            a.get("reason", "") for a in staging.provider_attempts if not a.get("ok")) or "unknown"
        self.gpu_compile.update({"state": "failed", "finished_at": time.time(), "seconds": seconds,
                                 "error": error})
        with self._stats_lock:
            self.provider_attempts.append({"provider": "migraphx", "ok": False,
                                           "reason": f"background compile failed: {error}"})
        logger.error(f"AMD GPU background compile failed after {seconds} s, staying on {self.provider}: {error}")

    def _adopt(self, other: "PersonDetector") -> None:
        """Swap in another detector's loaded sessions atomically w.r.t. inference."""
        with self._run_lock, self._obj_run_lock, self._refiner_lock, self._stats_lock:
            for name in self._ADOPT_FIELDS:
                setattr(self, name, getattr(other, name))
            self.provider_attempts = self.provider_attempts + [
                {**a, "note": "background compile"} for a in other.provider_attempts]
            self.cpu_threads = {**self.cpu_threads, **other.cpu_threads}
            # Timings measured on the previous provider no longer describe this one.
            for q in (self._infer_ms, self._total_ms, self._obj_infer_ms, self._refine_ms):
                q.clear()
            self._pose_ewma = self._refiner_frame_ewma = None

    def _finish_loading(self, ort) -> None:
        """Warm up the chosen session, then load the optional object model."""
        try:
            self.warmup_ms, self.warmup_steady_ms = self._warm_up(self.session, self.spec, self._run_lock)
        except Exception as e:
            self.last_error = f"warm-up failed: {e}"
            logger.error(self.last_error)
            self.session, self.spec = None, None
            self.backend = "unavailable"
            return
        self._mark_compiled(self.model_path)
        try:
            self._load_refiner(ort)
        except Exception as e:
            self.refiner_session, self.refiner_enabled = None, False
            self.refiner_reason = f"failed to load: {_short(str(e))}"
            logger.error(f"Keypoint refiner failed to load: {e}")
        try:
            self._fit_to_budget(ort)
        except Exception as e:  # keep the model that already works
            logger.error(f"Pose model budget selection failed, keeping {self.model_path.name}: {e}")
        ladder = self.pose_ladder()
        if (self.refiner_enabled and settings.ANALYTICS_SCHEDULER and self._refiner_mode() == "auto"
                and ladder and self.model_path != ladder[0]):
            # Under load the refiner is shed before the model is: with the top
            # rung out of budget it waits (loaded) for the scheduler.
            self.refiner_enabled = False
            self.refiner_reason = (f"auto: off while the pose model is below the top of the ladder "
                                   f"({self.model_path.name}, not {ladder[0].name})")

        if settings.OBJECT_DETECT_EVERY_N > 0:
            try:
                self._load_object_model(ort)
            except Exception as e:
                self.object_error = str(e)
                logger.error(f"Object model failed to load: {e}")
        else:
            self.object_error = "disabled (OBJECT_DETECT_EVERY_N <= 0)"

        logger.info(
            f"Person {self.spec.task} model ready: {self.model_path.name} on {self.device_name} "
            f"({self.provider}), warm-up {self.warmup_ms} ms, steady {self.warmup_steady_ms} ms"
            + (f"; objects: {self.object_spec.path.name} on {self.object_provider}"
               if self.object_session is not None else f"; objects: {self.object_error}")
        )

    # ------------------------------------------------------------ inference

    @property
    def available(self) -> bool:
        return self.session is not None and self.spec is not None

    @property
    def initialised(self) -> bool:
        return self._initialised

    @property
    def keypoints_supported(self) -> bool:
        return self.spec is not None and self.spec.kpt_shape is not None

    @property
    def objects_available(self) -> bool:
        return self.object_session is not None

    # Kept for callers of the previous API.
    @property
    def input_size(self) -> tuple[int, int]:
        return self.spec.input_size if self.spec else (640, 640)

    def _is_plausible_person(
        self, x1: float, y1: float, x2: float, y2: float, frame_area: float,
        keypoints: Optional[np.ndarray] = None, max_frame_fraction: Optional[float] = None,
    ) -> bool:
        """Reject boxes whose shape cannot be a person.

        A small detector pointed at floor texture, shelving or a doorway will
        occasionally return a confident "person" that is nearly square or
        covers half the frame. The aspect gate is relaxed only when the pose
        head found a coherent body (``_has_coherent_torso``: a torso, or head +
        shoulders + an elbow): that is direct evidence of a person who is
        bending or reaching, which is exactly when a box goes square.

        The size gate is ``max_frame_fraction`` (the camera's own setting) or
        the global ``PERSON_MAX_FRAME_FRACTION``. A box above it is still a
        person when the pose head found a coherent skeleton (both shoulders
        plus the hips or the head, ``_has_coherent_skeleton``) and the box is
        within ``PERSON_MAX_FRAME_FRACTION_WITH_SKELETON``: a shopper close to
        the camera has one, a false giant box over shelving does not.
        """
        w, h = x2 - x1, y2 - y1
        if w < settings.PERSON_MIN_BOX_PIXELS or h < settings.PERSON_MIN_BOX_PIXELS:
            return False
        limit = settings.PERSON_MAX_FRAME_FRACTION if max_frame_fraction is None else float(max_frame_fraction)
        frac = (w * h) / frame_area
        if frac > limit:
            relaxed = max(limit, float(settings.PERSON_MAX_FRAME_FRACTION_WITH_SKELETON))
            if frac > relaxed or not _has_coherent_skeleton(keypoints):
                return False
        if h / max(w, 1e-6) < settings.PERSON_MIN_ASPECT_RATIO:
            return _has_coherent_torso(keypoints)
        return True

    def _run(self, session, spec, lock, frame, provider: Optional[str] = None):
        blob, scale, px, py = letterbox(frame, spec.input_size, spec.input_dtype)
        provider = provider or self.provider
        with lock, self._gate(provider):
            # Timed inside the locks: contention between cameras is not model time.
            t = time.perf_counter()
            raw = session.run(None, {spec.input_name: blob})[0]
            infer_ms = (time.perf_counter() - t) * 1000.0
        self._account(infer_ms, provider)
        return raw, scale, px, py, infer_ms

    @staticmethod
    def _is_accelerator(provider: Optional[str]) -> bool:
        return bool(provider) and provider not in ("cpu", "openvino", "none")

    def _gate(self, provider: Optional[str]):
        """The device lock for a run on an accelerator; nothing on the CPU,
        where pose and object sessions run side by side on their thread shares."""
        return self._device_lock if self._is_accelerator(provider) else contextlib.nullcontext()

    def _account(self, ms: float, provider: Optional[str]) -> None:
        """Add a run to the device-busy total when it ran where the pose model
        runs (an object model that fell back to the CPU is not GPU load)."""
        if provider == self.provider:
            with self._stats_lock:
                self._busy_ms += ms

    def detect(
        self,
        frame: np.ndarray,
        conf_threshold: Optional[float] = None,
        iou_threshold: Optional[float] = None,
        max_frame_fraction: Optional[float] = None,
    ) -> list[Detection]:
        """Detect people (with keypoints when the model has them) in a BGR frame.

        ``max_frame_fraction`` overrides ``PERSON_MAX_FRAME_FRACTION`` for this
        call (the camera's ``person_max_frame_fraction`` setting).

        Returns an empty list when no backend is available. That emptiness is
        meaningful and must be propagated, not replaced with placeholder data.
        """
        if not self._initialised:
            self.initialise()
        if not self.available or frame is None or frame.size == 0:
            return []
        conf = settings.PERSON_CONF_THRESHOLD if conf_threshold is None else conf_threshold
        iou = settings.PERSON_NMS_IOU if iou_threshold is None else iou_threshold
        # One consistent (session, spec) pair: a background GPU compile may swap both.
        with self._run_lock:
            session, spec = self.session, self.spec
        if session is None or spec is None:
            return []

        started = time.perf_counter()
        try:
            raw, scale, px, py, infer_ms = self._run(session, spec, self._run_lock, frame)
            h, w = frame.shape[:2]
            boxes, scores, _cls, kpts = decode_output(raw, spec, conf, class_ids={PERSON_CLASS_ID})
            if len(boxes) == 0:
                dets: list[Detection] = []
            else:
                keep = list(range(len(boxes))) if spec.layout == "e2e" else _nms(boxes, scores, iou)
                boxes, kpts = unletterbox(boxes, kpts, scale, px, py, w, h)
                frame_area = float(w * h) or 1.0
                dets = []
                rejected = 0
                for i in keep:
                    bx1, by1, bx2, by2 = (float(v) for v in boxes[i])
                    kp = kpts[i] if kpts is not None else None
                    if not self._is_plausible_person(bx1, by1, bx2, by2, frame_area, kp, max_frame_fraction):
                        rejected += 1
                        continue
                    dets.append(Detection(bx1, by1, bx2, by2, confidence=float(scores[i]), keypoints=kp))
                if rejected:
                    with self._stats_lock:
                        self._rejected += rejected
            refine_ms = 0.0
            if self.refiner_enabled and dets:
                try:
                    refine_ms = self._refine(frame, dets)
                except Exception as e:  # the pose model's own keypoints stand
                    logger.error(f"Keypoint refinement failed: {e}")
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            self.last_error = str(e)
            return []

        total_ms = (time.perf_counter() - started) * 1000.0
        with self._stats_lock:
            self._infer_ms.append(infer_ms)
            self._total_ms.append(total_ms)
            self._frames += 1
            if session is self.session:     # not a result of a model swapped out meanwhile
                self._pose_ewma = _ewma(self._pose_ewma, infer_ms)
                if self.refiner_enabled:
                    # Per analysed frame, 0 when nobody was in view: what the refiner really costs.
                    self._refiner_frame_ewma = _ewma(self._refiner_frame_ewma, refine_ms)
        return dets

    def detect_objects(self, frame: np.ndarray, conf_threshold: Optional[float] = None) -> list[ObjectDetection]:
        """Retail-context objects (configured COCO classes). Empty when unavailable."""
        if not self._initialised:
            self.initialise()
        with self._obj_run_lock:
            session, spec, class_ids = self.object_session, self.object_spec, self.object_class_ids
        if session is None or spec is None or frame is None or frame.size == 0 or not class_ids:
            return []
        conf = settings.OBJECT_CONF_THRESHOLD if conf_threshold is None else conf_threshold
        try:
            raw, scale, px, py, infer_ms = self._run(session, spec, self._obj_run_lock, frame,
                                                     provider=self._object_label)
            boxes, scores, cls, _ = decode_output(raw, spec, conf, class_ids=class_ids)
            if len(boxes) == 0:
                out: list[ObjectDetection] = []
            else:
                keep = (list(range(len(boxes))) if spec.layout == "e2e"
                        else _class_aware_nms(boxes, scores, cls, settings.OBJECT_NMS_IOU))
                h, w = frame.shape[:2]
                boxes, _ = unletterbox(boxes, None, scale, px, py, w, h)
                out = [
                    ObjectDetection(
                        bbox=tuple(float(v) for v in boxes[i]),
                        class_id=int(cls[i]),
                        label=spec.names.get(int(cls[i]), str(int(cls[i]))),
                        confidence=float(scores[i]),
                    )
                    for i in keep
                ]
        except Exception as e:
            logger.error(f"Object inference failed: {e}")
            self.object_error = str(e)
            return []
        with self._stats_lock:
            self._obj_infer_ms.append(infer_ms)
        return out

    # ----------------------------------------------- run-time load control

    @staticmethod
    def _refiner_mode() -> str:
        mode = (settings.POSE_REFINER or "auto").strip().lower()
        return "off" if mode in ("off", "0", "false", "no", "") else mode

    def pose_ladder(self) -> list[Path]:
        """Pose models the scheduler may step between on this provider, most
        accurate first (existing files only; one entry when pinned)."""
        if not self.available:
            return []
        if self._forced_model is not None:
            return [self._forced_model]
        return [p for p in self._model_candidates(self._is_accelerator(self.provider)) if p.exists()]

    def load_metrics(self) -> dict:
        """What the scheduler measures from: cumulative device-busy ms and
        analysed frames (it differences them over time), plus per-part costs."""
        with self._stats_lock:
            obj = self._avg(self._obj_infer_ms)
            return {
                "busy_ms": self._busy_ms,
                "frames": self._frames,
                "provider": self.provider,
                "model": self.model_path.name if (self.model_path and self.available) else None,
                "pose_ms": round(self._pose_ewma, 2) if self._pose_ewma is not None else self.warmup_steady_ms,
                "pose_ms_measured": self._pose_ewma is not None,
                "object_ms": obj if obj is not None else None,
                "objects_on_device": self.object_session is not None and self._object_label == self.provider,
                "refiner_available": self.refiner_session is not None,
                "refiner_enabled": self.refiner_enabled,
                "refiner_frame_ms": (round(self._refiner_frame_ewma, 2)
                                     if self._refiner_frame_ewma is not None else None),
                "refiner_batch_ms": self.refiner_batch_ms,
            }

    def set_refiner(self, on: bool, reason: str) -> bool:
        """Switch the loaded refiner on or off (the scheduler's first lever)."""
        with self._refiner_lock:
            if on and self.refiner_session is None:
                return False
            changed = self.refiner_enabled != bool(on)
            self.refiner_enabled = bool(on)
            self.refiner_reason = reason
        if changed:
            logger.info(f"Keypoint refiner {'on' if on else 'off'}: {reason}")
        return True

    def _needs_child_compile(self, path: Path) -> bool:
        """Creating this session in-process could compile for minutes with the
        GIL held (MIGraphX cold cache; a TensorRT engine build), freezing every
        camera thread, so it is compiled in a child process first."""
        if self._is_plugin(self.execution_provider):
            return not amd.is_warm(amd.cache_dir(), path)
        return self.execution_provider == "TensorrtExecutionProvider"

    def switch_pose_model(self, path: Path, reason: str, progress=None) -> tuple[bool, dict]:
        """Load ``path`` on the current provider, measure it, and swap it in.

        Runs on the caller's (the scheduler's refit) thread; the running model
        keeps serving every camera until the swap, which happens under the
        pose run lock, so no frame sees a half-switched detector. Returns
        (ok, {"steady_ms", "warmup_ms", "from"} or {"error"}).
        """
        path = Path(path)
        if not self.available:
            return False, {"error": "no pose model is loaded"}
        if path == self.model_path:
            return True, {"steady_ms": self.warmup_steady_ms, "from": path.name}
        try:
            import onnxruntime as ort
        except ImportError as e:
            return False, {"error": f"onnxruntime not importable: {e}"}
        ep = self.execution_provider
        if self._needs_child_compile(path):
            if progress:
                progress("compiling")
            ok, error = self._compile_in_child(pose_model=path, expect=self.provider)
            if not ok:
                return False, {"error": error}
        if progress:
            progress("loading")
        sess, why = self._create_session(ort, path, ep)
        if sess is None:
            return False, {"error": why}
        try:
            spec = build_model_spec(path, sess.get_modelmeta().custom_metadata_map or {},
                                    sess.get_inputs(), sess.get_outputs())
            if spec.task != "pose":
                raise ValueError(f"{path.name} is a {spec.task} model")
            # Warm-up takes turns with the live cameras on the device lock.
            lock = self._device_lock if self._is_accelerator(self.provider) else threading.Lock()
            first, steady = self._warm_up(sess, spec, lock)
        except Exception as e:  # noqa: BLE001 - keep the model that works
            return False, {"error": _short(f"{type(e).__name__}: {e}")}
        self._mark_compiled(path, ep=ep)
        with self._run_lock, self._stats_lock:
            if self.execution_provider != ep:
                # The background GPU compile swapped providers meanwhile.
                return False, {"error": f"the provider changed to {self.provider} during the switch"}
            old = self.model_path
            self.session, self.spec, self.model_path = sess, spec, path
            self.warmup_ms, self.warmup_steady_ms = first, steady
            self._infer_ms.clear()
            self._total_ms.clear()
            self._pose_ewma = None
            tried = [t for t in self.selection.get("tried", []) if t.get("model") != path.name]
            tried.append({"model": path.name, "input_size": list(spec.input_size), "steady_ms": steady})
            self.selection = {**self.selection, "chosen": path.name, "reason": reason, "tried": tried,
                              "refitted_at": time.time()}
        return True, {"steady_ms": steady, "warmup_ms": first, "from": old.name if old else None}

    # ------------------------------------------------------------ telemetry

    @staticmethod
    def _avg(values) -> Optional[float]:
        return round(sum(values) / len(values), 2) if values else None

    @property
    def avg_latency_ms(self) -> float:
        with self._stats_lock:
            return self._avg(self._total_ms) or 0.0

    def status(self) -> dict:
        """What is actually running. Never loads anything."""
        with self._stats_lock:
            avg_infer = self._avg(self._infer_ms)
            avg_total = self._avg(self._total_ms)
            avg_obj = self._avg(self._obj_infer_ms)
            avg_refine = self._avg(self._refine_ms)
            frames, rejected = self._frames, self._rejected
        spec = self.spec
        return {
            "initialised": self._initialised,
            "available": self.available,
            "backend": self.backend,
            "provider": self.provider,
            "execution_provider": self.execution_provider,
            "device": self.device_name,
            "model": self.model_path.name if (self.model_path and self.available) else None,
            "model_path": str(self.model_path) if self.model_path else None,
            "task": spec.task if spec else None,
            "layout": spec.layout if spec else None,
            "input_size": list(spec.input_size) if spec else None,
            "input_dtype": np.dtype(spec.input_dtype).name if spec else None,
            "keypoints_supported": self.keypoints_supported,
            "num_keypoints": spec.kpt_shape[0] if (spec and spec.kpt_shape) else 0,
            "load_ms": self.load_ms,
            "warmup_ms": self.warmup_ms,
            "warmup_steady_ms": self.warmup_steady_ms,
            "model_selection": dict(self.selection),
            # Measured on real frames only; None until one has been inferred.
            "avg_infer_ms": avg_infer,
            "avg_latency_ms": avg_total if avg_total is not None else 0.0,
            "frames_inferred": frames,
            "conf_threshold": settings.PERSON_CONF_THRESHOLD,
            "implausible_boxes_rejected": rejected,
            "error": self.last_error,
            "last_error": self.last_error,
            "provider_attempts": list(self.provider_attempts),
            "available_providers": list(self.available_providers),
            "onnxruntime_version": self.ort_version,
            "dlls_preloaded": self.dlls_preloaded,
            "hailo": dict(self.hailo),
            # AMD MIGraphX: whether the plugin EP loaded, and whether the GPU
            # is still being compiled for (``provider`` above is the truth of
            # what runs *now*; while compiling that is the CPU).
            "gpu_compile": dict(self.gpu_compile),
            "migraphx": {k: self.migraphx.get(k) for k in ("ok", "reason", "gfx", "devices", "cache_dir",
                                                          "cache_warning")} if self.migraphx else None,
            "cpu_threads": dict(self.cpu_threads),
            # Run-time budget: target vs measured accelerator load, the rate
            # each camera really gets, and every model / refiner re-fit.
            "load_control": _load_control_status() if self is globals().get("person_detector") else None,
            "keypoint_refiner": {
                "enabled": self.refiner_enabled,
                "model": self.refiner_path.name if self.refiner_path else None,
                "reason": self.refiner_reason,
                "batch_ms": self.refiner_batch_ms,
                "avg_refine_ms": avg_refine,
            },
            "object_model": self.object_spec.path.name if self.object_session is not None else None,
            "object_detection": {
                "available": self.object_session is not None,
                "provider": self.object_provider,
                "classes": sorted(
                    self.object_spec.names.get(c, str(c)) for c in self.object_class_ids
                ) if self.object_spec else [],
                "every_n_detection_frames": settings.OBJECT_DETECT_EVERY_N,
                "warmup_ms": self.object_warmup_ms,
                "avg_infer_ms": avg_obj,
                "error": self.object_error,
            },
        }


def _load_control_status() -> Optional[dict]:
    try:
        from app.services.inference_scheduler import inference_scheduler

        return inference_scheduler.status()
    except Exception as e:  # noqa: BLE001 - status must never fail
        return {"error": f"{type(e).__name__}: {e}"}


def _has_coherent_torso(kpts: Optional[np.ndarray]) -> bool:
    """Keypoint evidence that a box which is wider than a standing person is one.

    Either a torso (both shoulders and a hip, shoulders above the hips), or an
    upper body: both shoulders, a head point (nose, eye or ear) above the
    shoulder line, and an elbow. The second form matters for shoppers: a
    person reaching sideways or up to a shelf gets a wide box, and the hips
    are very often hidden by the shelf edge, a trolley or the frame bottom.
    Measured on 609 COCO raised/extended-arm persons, the torso-only rule
    rejected ~6.5 % of them; a shelf or floor texture does not produce a
    confident head + shoulders + elbow arrangement.
    """
    if kpts is None or kpts.shape[0] < 13 or kpts.shape[1] < 3:
        return False
    thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
    vis = kpts[:, 2] >= thr
    if not (vis[5] and vis[6]):
        return False
    shoulder_y = (kpts[5, 1] + kpts[6, 1]) / 2.0
    hips = [kpts[i, 1] for i in (11, 12) if vis[i]]
    if hips and shoulder_y < (sum(hips) / len(hips)):
        return True
    head = [kpts[i, 1] for i in (0, 1, 2, 3, 4) if vis[i]]
    if not head or min(head) >= shoulder_y:
        return False
    if hips:
        return False  # hips seen *above* the shoulders: not an upright body
    return bool(vis[7] or vis[8])


def _has_coherent_skeleton(kpts: Optional[np.ndarray]) -> bool:
    """Keypoint evidence that an unusually large box is a real, close person.

    Both shoulders, plus either a hip below the shoulder line or a head point
    (nose, eye or ear) above it. A person near the camera shows at least that
    much of a body; a false detection spanning shelving or a doorway almost
    never produces two shoulders with a head or hips in the right place.
    """
    if kpts is None or kpts.shape[0] < 13 or kpts.shape[1] < 3:
        return False
    thr = settings.KEYPOINT_VISIBILITY_THRESHOLD
    vis = kpts[:, 2] >= thr
    if not (vis[5] and vis[6]):
        return False
    shoulder_y = (kpts[5, 1] + kpts[6, 1]) / 2.0
    if any(vis[i] and kpts[i, 1] > shoulder_y for i in (11, 12)):
        return True
    return any(vis[i] and kpts[i, 1] < shoulder_y for i in (0, 1, 2, 3, 4))


person_detector = PersonDetector()


def initialise_inference() -> dict:
    """Load and warm up the models on the best available accelerator.

    Called from the application lifespan. Idempotent and thread-safe; returns
    ``person_detector.status()``.
    """
    return person_detector.initialise()
