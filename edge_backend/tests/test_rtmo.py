"""RTMO (mmpose one-stage pose) as a person model, and the object model's default.

* ``build_model_spec`` recognises the static RTMO export by its two outputs
  (dets [1,K,5], keypoints [1,K,17,3]) and refuses the dynamic end2end graph.
* Its input convention differs from Ultralytics: BGR 0-255, no /255.
* Decoding maps boxes and keypoints back to frame pixels through the same
  letterbox (pad top/bottom) or pillarbox (pad left/right) as the YOLO path,
  and the host NMS replaces the graph's.
* With the model file present, the static export reproduces the original
  end2end graph's people on the bundled pictures (tests/fixtures/rtmo_reference.json,
  written from the original ONNX with its in-graph NMS).
* The object model is off by default and says why.
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


def test_yolo_exports_keep_their_input_convention():
    spec = build_model_spec(Path("y.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                            [_io("images", [1, 3, 640, 640])], [_io("output0", [1, 56, 8400])])
    assert spec.layout == "cf" and spec.input_rgb is True and spec.input_scale == pytest.approx(1 / 255)


def test_rtmo_blob_is_bgr_0_255_and_yolo_blob_is_rgb_unit():
    frame = np.zeros((640, 640, 3), np.uint8)
    frame[..., 0], frame[..., 1], frame[..., 2] = 10, 20, 200          # B, G, R
    bgr, *_ = letterbox(frame, (640, 640), rgb=False, scale_to=1.0)
    rgb, *_ = letterbox(frame, (640, 640))
    assert bgr.shape == (1, 3, 640, 640) and bgr.dtype == np.float32
    assert tuple(bgr[0, :, 5, 5]) == (10.0, 20.0, 200.0)
    assert np.allclose(rgb[0, :, 5, 5], np.array([200, 20, 10]) / 255.0)


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
    boxes, scores, cls, kpts = decode_output(raw, spec, 0.25, class_ids={0})
    assert len(boxes) == 2 and set(cls) == {0}                     # zero-score padding dropped
    keep = _nms(boxes, scores, 0.45)
    assert keep == [0]
    b, k = unletterbox(boxes[keep], kpts[keep], scale, px, py, w, h)
    assert np.allclose(b[0], [fx1, fy1, fx2, fy2], atol=0.01)
    assert np.allclose(k[0], kp_frame, atol=0.01)


def test_rtmo_goes_through_the_same_person_gates_as_yolo():
    spec = _spec(k=8)
    d = PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")
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
        boxes, scores, _cls, kpts = decode_output(raw, spec, float(ref["min_score"]), class_ids={0})
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


# ------------------------------------------------------------ object model default


def test_object_model_is_off_by_default():
    from app.config import Settings

    assert Settings.model_fields["OBJECT_DETECT_EVERY_N"].default == 0


def test_disabled_object_model_is_reported_as_disabled_by_configuration(monkeypatch):
    from app.services import preflight

    monkeypatch.setattr(settings, "OBJECT_DETECT_EVERY_N", 0)
    d = PersonDetector(model_path="/nonexistent/model.onnx")
    st = d.status()["object_detection"]
    assert st["available"] is False and st["state"] == "disabled"
    info, _errors, warnings = preflight.check_env()
    assert info["object_model"]["enabled"] is False
    assert "disabled by configuration" in info["object_model"]["reason"]
    assert not any("OBJECT_MODEL_PATH" in w["message"] for w in warnings)


@pytest.mark.skipif(not (MODELS / "yolo26n-pose.onnx").exists(), reason="yolo26n-pose.onnx not present")
def test_loaded_detector_skips_the_object_model_and_says_why(monkeypatch):
    monkeypatch.setattr(settings, "OBJECT_DETECT_EVERY_N", 0)
    monkeypatch.setattr(settings, "INFERENCE_DISABLED_PROVIDERS", "tensorrt,cuda,migraphx,rocm,openvino,directml")
    monkeypatch.setattr(settings, "POSE_REFINER", "off")
    d = PersonDetector(model_path=MODELS / "yolo26n-pose.onnx")
    st = d.initialise()
    assert st["available"]
    obj = st["object_detection"]
    assert obj["state"] == "disabled" and obj["error"] == "disabled by configuration (OBJECT_DETECT_EVERY_N=0)"
    assert d.object_session is None and not d.objects_available
    assert ib.settings.OBJECT_DETECT_EVERY_N == 0
