"""Skeletons stay upright and on the person for every camera shape on site.

The live cameras deliver 352x288 and 704x576 (PAL, aspect 1.22), 1280x720 and
2560x1440 (16:9) and 3072x2048 (3:2) frames into square (640x640) and
landscape (960x544) networks, so some frames are letterboxed and others
pillarboxed. A swapped axis anywhere between the frame and the drawn skeleton
(letterbox padding, keypoint decode, the RTMPose SimCC heads, the crop warp)
turns a standing person's skeleton on its side. These tests pin each step.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services.inference_backend import (
    PersonDetector,
    letterbox,
    refiner_crop,
    refiner_decode,
    simcc_output_order,
    unletterbox,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODELS = Path(settings.MODELS_DIR)
# (width, height) of every stream shape on site, plus a portrait one.
FRAME_SIZES = [(352, 288), (704, 576), (1280, 720), (2560, 1440), (3072, 2048), (576, 1024)]
NET_SIZES = [(640, 640), (960, 544)]


def _fit(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Centre-crop to the w:h aspect, then resize: people are not distorted."""
    import cv2

    H, W = img.shape[:2]
    if W / H > w / h:
        nw = int(round(H * w / h))
        img = img[:, (W - nw) // 2:(W - nw) // 2 + nw]
    else:
        nh = int(round(W * h / w))
        img = img[(H - nh) // 2:(H - nh) // 2 + nh]
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


# ------------------------------------------------------ letterbox geometry


@pytest.mark.parametrize("net", NET_SIZES)
@pytest.mark.parametrize("frame_wh", FRAME_SIZES)
def test_letterbox_round_trip_keeps_x_and_y(frame_wh, net):
    """A bright dot found in the network input maps back to where it was.

    The dot is placed off the diagonal (x != y, in a non-square frame), so a
    transposed axis or a pad applied to the wrong side moves it measurably.
    """
    w, h = frame_wh
    frame = np.zeros((h, w, 3), np.uint8)
    x, y = int(w * 0.8), int(h * 0.25)
    r = max(2, w // 100)
    frame[y - r:y + r + 1, x - r:x + r + 1] = 255
    blob, scale, px, py = letterbox(frame, net)
    assert blob.shape == (1, 3, net[1], net[0])
    plane = blob[0, 0]
    ys, xs = np.nonzero(plane > 0.5)
    found = np.array([[[xs.mean(), ys.mean(), 1.0]]], np.float32)
    box = np.array([[xs.min(), ys.min(), xs.max(), ys.max()]], np.float32)
    b, k = unletterbox(box, found, scale, px, py, w, h)
    tol = 1.5 / scale + 1.0
    assert abs(k[0, 0, 0] - x) <= tol and abs(k[0, 0, 1] - y) <= tol
    assert b[0, 0] <= x <= b[0, 2] + tol and b[0, 1] <= y <= b[0, 3] + tol
    # Letterboxed or pillarboxed, never both, and the pad is centred.
    assert px == 0 or py == 0


# ----------------------------------------------------- RTMPose SimCC heads


class _Out:
    def __init__(self, name, shape):
        self.name, self.shape = name, shape


def test_simcc_outputs_are_matched_by_name_not_position():
    x, y = _Out("simcc_x", ["batch", 17, 384]), _Out("simcc_y", ["batch", 17, 512])
    assert simcc_output_order([x, y], (256, 192)) == (0, 1)
    assert simcc_output_order([y, x], (256, 192)) == (1, 0)


def test_simcc_outputs_fall_back_to_their_lengths():
    """Unnamed heads: the x head has input_w x split bins, the y head input_h x split."""
    a, b = _Out("output0", ["batch", 17, 384]), _Out("output1", ["batch", 17, 512])
    assert simcc_output_order([a, b], (256, 192)) == (0, 1)
    assert simcc_output_order([b, a], (256, 192)) == (1, 0)
    with pytest.raises(ValueError):
        simcc_output_order([_Out("o0", [1, 17, 256]), _Out("o1", [1, 17, 256])], (128, 128))


def test_refiner_crop_and_decode_keep_orientation():
    """A peak placed at a known off-centre crop position decodes to the right frame pixel."""
    frame = np.zeros((288, 352, 3), np.uint8)
    box = (100, 40, 140, 200)                     # tall person, 40 x 160
    crop, meta = refiner_crop(frame, box, (256, 192))
    cx, cy, bw, bh = meta
    sx = np.zeros((1, 17, 384), np.float32)
    sy = np.zeros((1, 17, 512), np.float32)
    # Crop pixel (48, 32) -> left of centre and near the top: a head.
    sx[0, :, 96] = 1.0
    sy[0, :, 64] = 1.0
    k = refiner_decode(sx, sy, [meta], (256, 192), 352, 288)
    assert k[0, 0, 0] == pytest.approx(cx - bw / 4.0, abs=0.5)
    assert k[0, 0, 1] == pytest.approx(cy - bh * 3.0 / 8.0, abs=0.5)
    assert k[0, 0, 1] < cy                          # the head is above the box centre


# ------------------------------------------------------- real models, end to end


def _sane_upright(d, thr=0.5) -> list[str]:
    """Anatomical checks for a standing person; returns the failed ones."""
    k = d.keypoints
    v = k[:, 2] >= thr
    bw, bh = d.x2 - d.x1, d.y2 - d.y1
    m = 0.15 * max(bw, bh)
    fails = []
    inside = ((k[:, 0] >= d.x1 - m) & (k[:, 0] <= d.x2 + m) & (k[:, 1] >= d.y1 - m) & (k[:, 1] <= d.y2 + m))
    if (v & ~inside).any():
        fails.append("joint outside its box")
    sh = [i for i in (5, 6) if v[i]]
    hp = [i for i in (11, 12) if v[i]]
    if sh and hp and k[sh, 1].mean() >= k[hp, 1].mean():
        fails.append("shoulders not above hips")
    if v[0] and hp and k[0, 1] >= k[hp, 1].mean():
        fails.append("nose not above hips")
    if len(sh) == 2 and len(hp) == 2:
        dx, dy = k[[11, 12], :2].mean(0) - k[[5, 6], :2].mean(0)
        if abs(dx) > abs(dy):
            fails.append("torso axis horizontal")
    return fails


def _detector(name: str, refiner: bool) -> PersonDetector:
    path = MODELS / name
    if not path.exists():
        pytest.skip(f"{name} not present (scripts/fetch_models.py)")
    old = settings.POSE_REFINER
    settings.POSE_REFINER = "on" if refiner else "off"
    try:
        d = PersonDetector(model_path=path, object_model_path="")
        st = d.initialise()
    finally:
        settings.POSE_REFINER = old
    if not st["available"]:
        pytest.skip(f"{name} could not be loaded: {d.last_error}")
    if refiner and not d.refiner_enabled:
        pytest.skip(f"refiner not enabled: {d.refiner_reason}")
    return d


@pytest.fixture(scope="module")
def bus():
    import cv2

    img = cv2.imread(str(FIXTURES / "bus.jpg"))
    assert img is not None
    return img


@pytest.mark.parametrize("model,refiner", [
    ("yolo26n-pose.onnx", False),              # 640x640 network
    ("yolo26n-pose-544x960.onnx", False),      # 960x544 network: 352x288 is pillarboxed
    ("yolo26n-pose-544x960.onnx", True),       # + RTMPose top-down refiner
])
def test_skeletons_upright_on_every_site_frame_shape(bus, model, refiner):
    d = _detector(model, refiner)
    for w, h in FRAME_SIZES:
        frame = _fit(bus, w, h)
        dets = d.detect(frame, conf_threshold=0.4)
        assert len(dets) >= 2, f"{model} {w}x{h}: {len(dets)} people"
        for det in dets:
            assert det.keypoints is not None and det.keypoints.shape == (17, 3)
            assert not _sane_upright(det), f"{model} refiner={refiner} {w}x{h}: {_sane_upright(det)}"


def test_refiner_with_heads_in_the_other_order_gives_the_same_skeleton(bus):
    """An export with simcc_y first must not transpose every refined skeleton."""
    d = _detector("yolo26n-pose-544x960.onnx", True)
    frame = _fit(bus, 704, 576)
    ref = d.detect(frame, conf_threshold=0.4)

    real = d.refiner_session

    class Swapped:
        def get_inputs(self):
            return real.get_inputs()

        def get_outputs(self):
            return list(reversed(real.get_outputs()))

        def run(self, names, feeds):
            return list(reversed(real.run(names, feeds)))

    d.refiner_session = Swapped()
    d.refiner_out_order = simcc_output_order(d.refiner_session.get_outputs(), d.refiner_input_hw)
    assert d.refiner_out_order == (1, 0)
    swapped = d.detect(frame, conf_threshold=0.4)
    assert len(swapped) == len(ref)
    for a, b in zip(sorted(ref, key=lambda x: x.x1), sorted(swapped, key=lambda x: x.x1)):
        assert np.allclose(a.keypoints, b.keypoints, atol=1e-3)


# ------------------------------------------- refiner may not turn a skeleton


def _upright(cx=100.0, top=40.0, h=160.0):
    k = np.zeros((17, 3), np.float32)
    k[:, 2] = 0.9
    k[0] = (cx, top + 0.05 * h, 0.9)
    k[5], k[6] = (cx - 15, top + 0.2 * h, 0.9), (cx + 15, top + 0.2 * h, 0.9)
    k[11], k[12] = (cx - 10, top + 0.55 * h, 0.9), (cx + 10, top + 0.55 * h, 0.9)
    k[13], k[14] = (cx - 10, top + 0.75 * h, 0.9), (cx + 10, top + 0.75 * h, 0.9)
    k[15], k[16] = (cx - 10, top + 0.95 * h, 0.9), (cx + 10, top + 0.95 * h, 0.9)
    for i in (1, 2, 3, 4, 7, 8, 9, 10):
        k[i] = (cx, top + 0.3 * h, 0.9)
    return k


def test_torso_turn_check():
    from app.services.inference_backend import _torso_turned

    a = _upright()
    small = a.copy()
    small[11:13, 0] += 8                                  # a lean, not a turn
    assert not _torso_turned(a, small, 45)
    side = a.copy()
    side[:, 0], side[:, 1] = a[:, 1], a[:, 0]             # transposed: on its side
    assert _torso_turned(a, side, 45)
    hidden = side.copy()
    hidden[11:13, 2] = 0.1                                # no torso seen: nothing to compare
    assert not _torso_turned(a, hidden, 45)
    assert not _torso_turned(a, side, 0)                  # disabled


def test_refined_skeleton_turned_on_its_side_is_discarded(monkeypatch):
    from app.services import inference_backend as ib
    from app.services.inference_backend import Detection

    d = PersonDetector(model_path="/nonexistent/pose.onnx", object_model_path="")
    yolo = _upright()

    class _Ref:
        def get_inputs(self):
            return [_Out("input", ["batch", 3, 256, 192])]

        def run(self, _names, feeds):
            (x,) = feeds.values()
            return [np.zeros((x.shape[0], 17, 384), np.float32), np.zeros((x.shape[0], 17, 512), np.float32)]

    d.refiner_session, d.refiner_input_hw, d.refiner_out_order = _Ref(), (256, 192), (0, 1)
    d.provider = "cpu"
    frame = np.zeros((288, 352, 3), np.uint8)

    turned = yolo.copy()
    turned[:, 0], turned[:, 1] = yolo[:, 1], yolo[:, 0]
    monkeypatch.setattr(ib, "refiner_decode", lambda *a, **k: turned[None].copy())
    det = Detection(80, 40, 120, 200, 0.9, keypoints=yolo.copy())
    d._refine(frame, [det])
    assert np.array_equal(det.keypoints, yolo)            # the pose model's skeleton stands
    assert d.status()["keypoint_refiner"]["skeletons_kept_from_pose_model"] == 1

    adjusted = yolo.copy()
    adjusted[9, :2] += (6, -4)                            # a refined wrist
    monkeypatch.setattr(ib, "refiner_decode", lambda *a, **k: adjusted[None].copy())
    det = Detection(80, 40, 120, 200, 0.9, keypoints=yolo.copy())
    d._refine(frame, [det])
    assert np.allclose(det.keypoints[9, :2], adjusted[9, :2])
