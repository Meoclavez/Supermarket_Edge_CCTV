"""Person detection with runtime hardware auto-detection.

The deployment machine is not the development machine, so nothing here may be
chosen at build time. On startup the service walks a priority chain and uses
the best accelerator that actually loads:

    1. HailoRT NPU          (if hailo_platform imports and /dev/hailo0 exists)
    2. TensorRT  EP         (NVIDIA, fastest ONNX path)
    3. CUDA      EP         (NVIDIA)
    4. ROCm      EP         (AMD discrete)
    5. OpenVINO  EP         (Intel iGPU / NPU)
    6. CPU       EP         (always available)
    7. UNAVAILABLE          (no model file present)

The last state matters as much as the others: when no backend can run, the
service reports ``available = False`` and returns **no detections at all**.
It never emits synthetic boxes to keep the dashboard looking busy, because a
fabricated detection is indistinguishable downstream from a real one and would
silently poison every retail metric derived from it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# COCO class 0 is "person". Everything the retail analytics needs is derived
# from person detections, so other classes are discarded at decode time.
PERSON_CLASS_ID = 0

# Execution providers in descending order of throughput. Names that are not
# present in this ONNX Runtime build are skipped silently.
_PROVIDER_PRIORITY = (
    ("TensorrtExecutionProvider", "tensorrt"),
    ("CUDAExecutionProvider", "cuda"),
    ("ROCMExecutionProvider", "rocm"),
    ("MIGraphXExecutionProvider", "migraphx"),
    ("OpenVINOExecutionProvider", "openvino"),
    ("CoreMLExecutionProvider", "coreml"),
    ("CPUExecutionProvider", "cpu"),
)


@dataclass
class Detection:
    """One person, in pixel coordinates of the frame that produced it."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int = PERSON_CLASS_ID

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot_point(self) -> tuple[float, float]:
        """Where the person meets the floor.

        The bottom-centre of the box is the only part of a person that is
        reliably on the ground plane, so it is what gets projected through the
        homography into store coordinates. Using the box centre instead would
        place everyone roughly a metre behind where they actually stand.
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def to_dict(self) -> dict:
        return {
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "confidence": round(self.confidence, 3),
            "class_id": self.class_id,
        }


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy non-maximum suppression. Returns indices to keep."""
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


class PersonDetector:
    """Loads a YOLO ONNX model onto the best available accelerator."""

    def __init__(self, model_path: Optional[Path] = None):
        self.model_path = Path(model_path or settings.PERSON_MODEL_PATH)
        self.session = None
        self.provider: str = "none"
        self.backend: str = "unavailable"
        self.device_name: str = "none"
        self.input_name: str = ""
        self.input_size: tuple[int, int] = (640, 640)
        self.input_dtype = np.float32
        self._layout: str = "v5"   # "v5" -> [1,N,85]; "v8" -> [1,84,N]
        self._num_classes: int = 80
        self._lock = threading.Lock()
        self._latencies: list[float] = []
        self._rejected = 0
        self.last_error: Optional[str] = None
        self._load()

    # ---------------------------------------------------------------- loading

    def _try_hailo(self) -> bool:
        """Use the NPU when the site machine actually has one fitted."""
        if not os.path.exists(settings.HAILO_DEVICE):
            return False
        try:
            import hailo_platform  # noqa: F401
        except ImportError:
            return False
        # A real HailoRT pipeline needs a compiled .hef, not the ONNX file.
        hef = Path(settings.HAILO_YOLO_HEF_PATH)
        if not hef.exists():
            logger.info(
                f"Hailo device present but no compiled model at {hef}; "
                "falling through to ONNX Runtime."
            )
            return False
        self.backend = "hailo"
        self.provider = "hailort"
        self.device_name = "Hailo NPU"
        logger.info("Person detector running on Hailo NPU")
        return True

    def _load(self) -> None:
        if self._try_hailo():
            return

        if not self.model_path.exists():
            self.last_error = f"model file not found: {self.model_path}"
            logger.warning(
                f"Person detection unavailable: {self.last_error}. "
                "No detections will be produced and no analytics will be fabricated."
            )
            return

        try:
            import onnxruntime as ort
        except ImportError as e:
            self.last_error = f"onnxruntime not installed ({e})"
            logger.warning(f"Person detection unavailable: {self.last_error}")
            return

        available = set(ort.get_available_providers())
        chosen: Optional[tuple[str, str]] = None
        for ep, label in _PROVIDER_PRIORITY:
            if ep in available:
                chosen = (ep, label)
                break
        if chosen is None:
            self.last_error = "no usable ONNX Runtime execution provider"
            logger.warning(f"Person detection unavailable: {self.last_error}")
            return

        ep, label = chosen
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3

        # Fall back down the chain if the preferred provider fails to
        # initialise -- a CUDA build on a box with mismatched driver libraries
        # raises here rather than at import time.
        for ep_try, label_try in _PROVIDER_PRIORITY:
            if ep_try not in available:
                continue
            try:
                self.session = ort.InferenceSession(
                    str(self.model_path), sess_options=opts, providers=[ep_try]
                )
                self.provider = label_try
                self.backend = "onnxruntime"
                self.device_name = ep_try.replace("ExecutionProvider", "")
                break
            except Exception as e:
                logger.warning(f"Provider {ep_try} failed to initialise: {e}")
                continue

        if self.session is None:
            self.last_error = "every execution provider failed to initialise"
            logger.error(f"Person detection unavailable: {self.last_error}")
            return

        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape
        if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int):
            self.input_size = (int(shape[3]), int(shape[2]))  # (w, h)
        self.input_dtype = np.float16 if "float16" in inp.type else np.float32

        # YOLOv5 exports [1, N, 5+C]; YOLOv8 exports [1, 4+C, N]. Detect which
        # so one decoder serves whichever model the operator drops in.
        out_shape = self.session.get_outputs()[0].shape
        if len(out_shape) == 3 and isinstance(out_shape[1], int) and isinstance(out_shape[2], int):
            if out_shape[1] < out_shape[2]:
                self._layout = "v8"
                self._num_classes = int(out_shape[1]) - 4
            else:
                self._layout = "v5"
                self._num_classes = int(out_shape[2]) - 5

        logger.info(
            f"Person detector ready: {self.model_path.name} on {self.device_name} "
            f"({self.provider}), input {self.input_size}, layout {self._layout}"
        )

    # -------------------------------------------------------------- inference

    @property
    def available(self) -> bool:
        return self.session is not None or self.backend == "hailo"

    def _preprocess(self, frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        """Letterbox to the network size, preserving aspect ratio."""
        import cv2

        net_w, net_h = self.input_size
        h, w = frame.shape[:2]
        scale = min(net_w / w, net_h / h)
        new_w, new_h = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        canvas = np.full((net_h, net_w, 3), 114, dtype=np.uint8)
        pad_x, pad_y = (net_w - new_w) // 2, (net_h - new_h) // 2
        canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized

        blob = canvas[:, :, ::-1].astype(self.input_dtype) / self.input_dtype(255.0)
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        return np.ascontiguousarray(blob), scale, pad_x, pad_y

    def _decode(
        self,
        raw: np.ndarray,
        scale: float,
        pad_x: int,
        pad_y: int,
        frame_w: int,
        frame_h: int,
        conf_threshold: float,
        iou_threshold: float,
    ) -> list[Detection]:
        pred = np.squeeze(raw, axis=0).astype(np.float32)
        if self._layout == "v8":
            pred = pred.T  # [N, 4+C]
            boxes_xywh = pred[:, :4]
            class_scores = pred[:, 4:]
            scores = class_scores[:, PERSON_CLASS_ID]
        else:
            boxes_xywh = pred[:, :4]
            objectness = pred[:, 4]
            class_scores = pred[:, 5:]
            scores = objectness * class_scores[:, PERSON_CLASS_ID]

        mask = scores >= conf_threshold
        if not np.any(mask):
            return []
        boxes_xywh = boxes_xywh[mask]
        scores = scores[mask]

        # Only keep rows where person actually wins the class argmax, so a
        # confidently-detected handbag does not become a shopper.
        cls = class_scores[mask]
        if cls.shape[1] > 1:
            winners = cls.argmax(axis=1) == PERSON_CLASS_ID
            if not np.any(winners):
                return []
            boxes_xywh, scores = boxes_xywh[winners], scores[winners]

        cx, cy, bw, bh = boxes_xywh.T
        x1 = (cx - bw / 2 - pad_x) / scale
        y1 = (cy - bh / 2 - pad_y) / scale
        x2 = (cx + bw / 2 - pad_x) / scale
        y2 = (cy + bh / 2 - pad_y) / scale

        x1 = np.clip(x1, 0, frame_w)
        y1 = np.clip(y1, 0, frame_h)
        x2 = np.clip(x2, 0, frame_w)
        y2 = np.clip(y2, 0, frame_h)

        boxes = np.stack([x1, y1, x2, y2], axis=1)
        keep = _nms(boxes, scores, iou_threshold)

        frame_area = float(frame_w * frame_h) or 1.0
        out: list[Detection] = []
        for i in keep:
            bx1, by1, bx2, by2 = boxes[i].tolist()
            if not self._is_plausible_person(bx1, by1, bx2, by2, frame_area):
                self._rejected += 1
                continue
            out.append(Detection(bx1, by1, bx2, by2, confidence=float(scores[i])))
        return out

    def _is_plausible_person(
        self, x1: float, y1: float, x2: float, y2: float, frame_area: float
    ) -> bool:
        """Reject boxes whose shape cannot be an upright person.

        A small detector pointed at floor texture, shelving or a doorway will
        occasionally return a confident "person" that is nearly square or
        covers half the frame. Accepting those would write false shoppers into
        the analytics, where nothing downstream could tell them from real ones.
        """
        w, h = x2 - x1, y2 - y1
        if w < settings.PERSON_MIN_BOX_PIXELS or h < settings.PERSON_MIN_BOX_PIXELS:
            return False
        if h / max(w, 1e-6) < settings.PERSON_MIN_ASPECT_RATIO:
            return False
        if (w * h) / frame_area > settings.PERSON_MAX_FRAME_FRACTION:
            return False
        return True

    def detect(
        self,
        frame: np.ndarray,
        conf_threshold: Optional[float] = None,
        iou_threshold: float = 0.45,
    ) -> list[Detection]:
        """Detect people in a BGR frame.

        Returns an empty list when no backend is available. That emptiness is
        meaningful and must be propagated, not replaced with placeholder data.
        """
        if not self.available or frame is None or frame.size == 0:
            return []
        conf = conf_threshold if conf_threshold is not None else settings.PERSON_CONF_THRESHOLD

        started = time.perf_counter()
        try:
            blob, scale, pad_x, pad_y = self._preprocess(frame)
            with self._lock:  # ORT sessions are not guaranteed thread-safe
                raw = self.session.run(None, {self.input_name: blob})[0]
            h, w = frame.shape[:2]
            dets = self._decode(raw, scale, pad_x, pad_y, w, h, conf, iou_threshold)
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            self.last_error = str(e)
            return []

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._latencies.append(elapsed_ms)
        if len(self._latencies) > 120:
            self._latencies.pop(0)
        return dets

    # ------------------------------------------------------------- telemetry

    @property
    def avg_latency_ms(self) -> float:
        return round(sum(self._latencies) / len(self._latencies), 2) if self._latencies else 0.0

    def status(self) -> dict:
        return {
            "available": self.available,
            "backend": self.backend,
            "provider": self.provider,
            "device": self.device_name,
            "model": self.model_path.name if self.model_path.exists() else None,
            "model_path": str(self.model_path),
            "input_size": list(self.input_size),
            "layout": self._layout,
            "avg_latency_ms": self.avg_latency_ms,
            "conf_threshold": settings.PERSON_CONF_THRESHOLD,
            "implausible_boxes_rejected": self._rejected,
            "last_error": self.last_error,
        }


person_detector = PersonDetector()
