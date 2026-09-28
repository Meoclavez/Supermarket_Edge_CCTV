"""RTMO (mmpose one-stage pose): the only person model the backend runs.

* ``build_model_spec`` recognises the static RTMO export by its two outputs
  (dets [1,K,5], keypoints [1,K,17,3]), refuses the dynamic end2end graph and
  rejects any other output signature (YOLO [1,56,8400], [1,84,8400], ...).
* Its input convention: BGR 0-255, no /255.
* Decoding maps boxes and keypoints back to frame pixels through the
  letterbox (pad top/bottom) or pillarbox (pad left/right), and the host NMS
  replaces the graph's.
* With the model file present, the static export reproduces the original
  end2end graph's people on the bundled pictures (tests/fixtures/rtmo_reference.json,
  written from the original ONNX with its in-graph NMS).
* The object model (and its settings/status fields) is gone, and the default
  configuration names only models listed in models/manifest.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.services import inference_backend as ib
from app.services.inference_backend import (
    PersonDetector,
    _nms,
    build_model_spec,
    decode_output,
    letterbox,
    unletterbox,
)

FIXTURES = Path(__file__).parent / "fixtures"
MODELS = Path(__file__).resolve().parents[1] / "models"
RTMO = MODELS / "rtmo-s-body7-640x640-static.onnx"


def _io(name, shape, typ="tensor(float)"):
    return SimpleNamespace(name=name, shape=shape, type=typ)


def _spec(k=300, swapped=False, meta=None):
    outs = [_io("dets", [1, k, 5]), _io("keypoints", [1, k, 17, 3])]
    if swapped:
        outs.reverse()
    return build_model_spec(Path("rtmo.onnx"), meta or {}, [_io("input", [1, 3, 640, 640])], outs)


def test_static_rtmo_export_is_recognised_as_a_pose_model():
    spec = _spec()
    assert (spec.task, spec.layout, spec.kpt_shape, spec.input_size) == ("pose", "rtmo", (17, 3), (640, 640))
    assert spec.input_rgb is False and spec.input_scale == 1.0
    assert spec.output_order == (0, 1)
    assert _spec(swapped=True).output_order == (1, 0)    # matched by shape, never by position


def test_dynamic_rtmo_graph_is_refused():
    with pytest.raises(ValueError, match="not static"):
        _spec(k="N")
    with pytest.raises(ValueError, match="expected dets"):
        build_model_spec(Path("x.onnx"), {"model_family": "rtmo"}, [_io("input", [1, 3, 640, 640])],
                         [_io("dets", [1, 300, 6]), _io("keypoints", [1, 300, 17, 3])])


@pytest.mark.parametrize("outputs", [
    [[1, 56, 8400]],                        # YOLO pose, channels first
    [[1, 84, 8400]],                        # YOLO detect (COCO 80 classes)
    [[1, 8400, 56]],                        # channels last
    [[1, 300, 57]],                         # end-to-end export
    [[1, 25200, 85]],                       # YOLOv5 detect
])
@pytest.mark.parametrize("meta", [{}, {"kpt_shape": "[17, 3]", "names": "{0: 'person'}", "end2end": "True"}])
def test_non_rtmo_output_signatures_are_rejected(outputs, meta):
    with pytest.raises(ValueError, match="unsupported model outputs"):
        build_model_spec(Path("y.onnx"), meta, [_io("images", [1, 3, 640, 640])],
                         [_io(f"output{i}", s) for i, s in enumerate(outputs)])


def test_model_spec_defaults_are_the_rtmo_input_convention():
    spec = ib.ModelSpec(Path("m.onnx"), "pose", "rtmo", 1, {0: "person"}, (17, 3), "input", (640, 640))
    assert spec.input_rgb is False and spec.input_scale == 1.0


def test_rtmo_blob_is_bgr_0_255():
    frame = np.zeros((640, 640, 3), np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 10, 20, 200          # B, G, R
    bgr, *_ = letterbox(frame, (640, 640), rgb=False, scale_to=1.0)
    assert bgr.shape == (1, 3, 640, 640) and bgr.dtype == np.float32
    assert tuple(bgr[0, :, 5, 5]) == (10.0, 20.0, 200.0)


def _raw(boxes640, kpts640, scores, k=8):
    """Synthetic RTMO outputs: rows beyond the given ones are zero-score padding."""
    dets = np.zeros((1, k, 5), np.float32)
    kps = np.zeros((1, k, 17, 3), np.float32)
    for i, (b, kp, s) in enumerate(zip(boxes640, kpts640, scores)):
        dets[0, i, :4], dets[0, i, 4] = b, s
        kps[0, i] = kp
    return [dets, kps]


@pytest.mark.parametrize("w,h", [(1280, 720), (720, 1280)])     # letterbox, then pillarbox
def test_rtmo_decode_maps_back_to_frame_pixels(w, h):
    spec = _spec(k=8)
    frame = np.full((h, w, 3), 90, np.uint8)
    _blob, scale, px, py = letterbox(frame, spec.input_size, rgb=False, scale_to=1.0)
    assert (px > 0) == (w < h) and (py > 0) == (w > h)
    # A person at a known place in the frame, expressed in network pixels.
    fx1, fy1, fx2, fy2 = 300.0, 200.0, 420.0, 560.0
    net = [fx1 * scale + px, fy1 * scale + py, fx2 * scale + px, fy2 * scale + py]
    kp_frame = np.stack([np.linspace(fx1 + 10, fx2 - 10, 17), np.linspace(fy1 + 10, fy2 - 10, 17),
                         np.linspace(0.3, 0.95, 17)], axis=1).astype(np.float32)
    kp_net = kp_frame.copy()
    kp_net[:, 0] = kp_frame[:, 0] * scale + px
    kp_net[:, 1] = kp_frame[:, 1] * scale + py
    dup = [v + 1.0 for v in net]                                   # the same person again: NMS removes it
    raw = _raw([net, dup], [kp_net, kp_net], [0.9, 0.8])
    boxes, scores, cls, kpts = decode_output(raw, spec, 0.25)
    assert len(boxes) == 2 and set(cls) == {0}                     # zero-score padding dropped
    keep = _nms(boxes, scores, 0.45)
    assert keep == [0]
    b, k = unletterbox(boxes[keep], kpts[keep], scale, px, py, w, h)
    assert np.allclose(b[0], [fx1, fy1, fx2, fy2], atol=0.01)
    assert np.allclose(k[0], kp_frame, atol=0.01)


def test_rtmo_goes_through_the_person_gates():
    spec = _spec(k=8)
    d = PersonDetector(model_path="/nonexistent/model.onnx")
    frame = np.full((720, 1280, 3), 100, np.uint8)
    _blob, scale, px, py = letterbox(frame, spec.input_size, rgb=False, scale_to=1.0)
    person = [400 * scale + px, 100 * scale + py, 520 * scale + px, 500 * scale + py]
    square = [700 * scale + px, 100 * scale + py, 1100 * scale + px, 500 * scale + py]  # no skeleton: rejected
    kp = np.zeros((17, 3), np.float32)
    dets, rejected = d.postprocess(_raw([person, square], [kp, kp], [0.9, 0.9]), spec, frame, scale, px, py,
                                   0.25, 0.45, per_box=True)
    assert rejected == 1 and len(dets) == 1
    assert dets[0].bbox == pytest.approx((400, 100, 520, 500), abs=0.01)
    assert dets[0].pose_keypoints is dets[0].keypoints            # before any refiner


@pytest.mark.skipif(not RTMO.exists(), reason="rtmo-s-body7-640x640-static.onnx not present (fetch_models.py)")
def test_static_export_reproduces_the_original_graph_on_bundled_pictures():
    import cv2
    import onnxruntime as ort

    sess = ort.InferenceSession(str(RTMO), providers=["CPUExecutionProvider"])
    spec = build_model_spec(RTMO, sess.get_modelmeta().custom_metadata_map, sess.get_inputs(), sess.get_outputs())
    assert spec.layout == "rtmo"
    meta = sess.get_modelmeta().custom_metadata_map
    assert meta.get("model_family") == "rtmo" and meta.get("weights", "").endswith(".pth")
    ref = json.loads((FIXTURES / "rtmo_reference.json").read_text())
    for name, want in ref["images"].items():
        img = cv2.imread(str(FIXTURES / name))
        h, w = img.shape[:2]
        blob, scale, px, py = letterbox(img, spec.input_size, rgb=spec.input_rgb, scale_to=spec.input_scale)
        assert [px, py] == want["pad_xy"] and scale == pytest.approx(want["scale"])
        raw = sess.run(None, {spec.input_name: blob})
        # The original graph's post-processing: score > 0.15, NMS at IoU 0.65.
        boxes, scores, _cls, kpts = decode_output(raw, spec, float(ref["min_score"]))
        keep = _nms(boxes, scores, 0.65)
        b, k = unletterbox(boxes[keep], kpts[keep], scale, px, py, w, h)
        persons = want["persons"]
        assert len(keep) == len(persons), name
        rb = np.array([p["box"] for p in persons], np.float32)
        rk = np.array([p["keypoints"] for p in persons], np.float32)
        rk[..., 0] = np.clip(rk[..., 0], 0, w)                     # unletterbox clips to the frame
        rk[..., 1] = np.clip(rk[..., 1], 0, h)
        assert np.abs(b - rb).max() < 0.02, name                    # reference rounded to 0.01 px
        assert np.abs(k[..., :2] - rk[..., :2]).max() < 0.02, name
        assert np.abs(scores[keep] - np.array([p["score"] for p in persons])).max() < 0.006, name
        assert np.abs(k[..., 2] - rk[..., 2]).max() < 0.006, name


# ------------------------------------------------- no object model, manifest-only defaults


def test_object_model_and_yolo_settings_are_gone():
    from app.config import Settings

    for key in ("OBJECT_MODEL_PATH", "OBJECT_DETECT_EVERY_N", "OBJECT_CLASSES", "OBJECT_CONF_THRESHOLD",
                "OBJECT_NMS_IOU", "THEFT_BAG_CLASS_IDS", "HAILO_YOLO_HEF_PATH"):
        assert key not in Settings.model_fields, key
    assert Settings.model_fields["HAILO_POSE_HEF_PATH"].default == ""
    for name in ("ObjectDetection", "_COCO_FALLBACK_NAMES", "_class_aware_nms", "_parse_class_filter"):
        assert not hasattr(ib, name), name


def test_detector_has_no_object_model_api():
    d = PersonDetector(model_path="/nonexistent/model.onnx")
    for name in ("detect_objects", "objects_available", "object_enabled", "_load_object_model",
                 "object_session", "object_spec", "_obj_run_lock", "_obj_infer_ms"):
        assert not hasattr(d, name), name
    st = d.status()
    assert "object_model" not in st and "object_detection" not in st
    m = d.load_metrics()
    assert "object_ms" not in m and "objects_on_device" not in m


def test_default_config_names_only_manifest_listed_models():
    from app.config import Settings
    from app.services.preflight import load_manifest

    listed = {e["file"] for e in load_manifest()["models"]}
    assert "rtmo-s-body7-640x640-static.onnx" in listed
    assert not any("yolo" in f.lower() for f in listed)
    defaults = {k: Settings.model_fields[k].default for k in (
        "POSE_MODEL_GPU", "POSE_MODEL_CPU", "POSE_MODEL_LADDER_GPU", "POSE_MODEL_LADDER_CPU",
        "POSE_REFINER_MODEL")}
    assert defaults["POSE_MODEL_GPU"] == defaults["POSE_MODEL_CPU"] == "rtmo-s-body7-640x640-static.onnx"
    assert defaults["POSE_MODEL_LADDER_GPU"] == defaults["POSE_MODEL_LADDER_CPU"] == ""
    named = [n.strip() for v in defaults.values() for n in str(v or "").split(",") if n.strip()]
    assert named and all(Path(n).name in listed for n in named), named

