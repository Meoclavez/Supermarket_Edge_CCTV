"""Measured-lighting enhancement (services/low_light.py) and its detector hook.

The state comes from the picture (luminance, dark share, colour saturation),
never the clock; it switches only after LOW_LIGHT_HYSTERESIS_FRAMES agreeing
frames; the enhancement is local (a lit region keeps its contrast while a
shaded one is lifted) and is applied only to the network input of frames the
detector actually infers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.config import settings
from app.services import low_light as ll
from app.services.inference_backend import PersonDetector, build_model_spec


def _scene(h=288, w=352, seed=0) -> np.ndarray:
    """A textured, colourful, normally exposed frame."""
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 220, (h // 8, w // 8, 3), dtype=np.uint8)
    import cv2

    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_NEAREST)
    img[..., 2] = np.clip(img[..., 2].astype(int) + 30, 0, 255)   # some colour
    return img


def _darken(img: np.ndarray, ev: float) -> np.ndarray:
    lin = (img.astype(np.float32) / 255.0) ** 2.2 * (2.0 ** ev)
    return (np.clip(lin, 0, 1) ** (1 / 2.2) * 255.0).astype(np.uint8)


def _half_shadow(img: np.ndarray, ev: float = -4.0) -> np.ndarray:
    out = img.copy()
    out[:, : img.shape[1] // 2] = _darken(img[:, : img.shape[1] // 2], ev)
    return out


def _ir(img: np.ndarray) -> np.ndarray:
    import cv2

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


# ------------------------------------------------------------ classification


def test_states_come_from_the_picture():
    day = _scene()
    assert ll.classify(ll.measure(day)) == "day"
    assert ll.classify(ll.measure(_darken(day, -3.0))) == "low_light"
    assert ll.classify(ll.measure(_half_shadow(day))) == "mixed"
    assert ll.classify(ll.measure(_ir(day))) == "ir"
    assert ll.classify(ll.measure(_ir(_darken(day, -3.0)))) == "ir"


def test_measure_is_a_bounded_sample_of_any_frame_size():
    big = np.full((2048, 3072, 3), 90, np.uint8)
    st = ll.measure(big)
    assert st.mean == pytest.approx(90, abs=1)
    assert st.saturation == pytest.approx(0, abs=1e-6)      # gray = no colour
    assert 0.0 <= st.dark_fraction <= 1.0


def test_hysteresis_needs_consecutive_frames_to_switch(monkeypatch):
    monkeypatch.setattr(settings, "LOW_LIGHT_HYSTERESIS_FRAMES", 3)
    mon = ll.LightingMonitor()
    day, dark = _scene(), _darken(_scene(), -3.0)
    assert mon.observe(day, "cam")[0] == "day"          # first frame decides at once
    # One dark frame (a person in front of the lens) does not switch it.
    assert mon.observe(dark, "cam")[0] == "day"
    assert mon.observe(day, "cam")[0] == "day"
    # Three in a row do.
    assert [mon.observe(dark, "cam")[0] for _ in range(3)] == ["day", "day", "low_light"]
    st = mon.camera_status("cam")
    assert st["lighting"] == "low_light" and st["frames_measured"] == 6
    # And back again only after three bright frames.
    assert [mon.observe(day, "cam")[0] for _ in range(3)] == ["low_light", "low_light", "day"]
    # Without a camera id every frame is judged alone and nothing is recorded.
    assert mon.observe(dark)[0] == "low_light"
    assert set(mon.status()["cameras"]) == {"cam"}


def test_dead_band_keeps_the_current_state_near_a_threshold(monkeypatch):
    monkeypatch.setattr(settings, "LOW_LIGHT_MEAN_LEVEL", 70.0)
    monkeypatch.setattr(settings, "LOW_LIGHT_DEAD_BAND", 0.15)
    st = ll.LightStats(mean=75.0, p10=40, p50=70, p90=120, dark_fraction=0.1, saturation=60)
    assert ll.classify(st) == "day"
    assert ll.classify(st, previous="low_light") == "low_light"


# --------------------------------------------------------------- enhancement


def test_enhancement_lifts_the_shade_and_leaves_the_lit_side():
    img = _half_shadow(_scene(seed=3))
    out = ll.enhance_for_detection(img, "mixed")
    half = img.shape[1] // 2
    dark_before, dark_after = img[:, :half].mean(), out[:, :half].mean()
    lit_before, lit_after = img[:, half:].mean(), out[:, half:].mean()
    assert dark_after > dark_before * 1.25                   # shaded side lifted
    assert abs(lit_after - lit_before) < 0.12 * lit_before   # lit side not washed out
    # Contrast in the shade goes up, not just the brightness.
    assert out[:, :half].std() > img[:, :half].std() * 1.3


def test_day_frames_are_passed_through_untouched():
    img = _scene()
    assert ll.enhance_for_detection(img, "day") is img


def test_ir_frames_stay_grayscale():
    out = ll.enhance_for_detection(_ir(_darken(_scene(), -2.0)), "ir")
    assert np.abs(out[..., 0].astype(int) - out[..., 2].astype(int)).max() <= 2


# ------------------------------------------------------------ detector hook


class _IO:
    def __init__(self, name, shape, type_="tensor(float)"):
        self.name, self.shape, self.type = name, shape, type_


class _SpySession:
    """Records the network input; returns no candidates."""

    def __init__(self):
        self.blobs = []

    def run(self, _outputs, feeds):
        (blob,) = feeds.values()
        self.blobs.append(blob.copy())
        return [np.zeros((1, 56, 8400), np.float32)]


def _spy_detector():
    d = PersonDetector(model_path="/nonexistent/pose.onnx", object_model_path="")
    d.spec = build_model_spec(Path("pose.onnx"), {"kpt_shape": "[17, 3]", "names": "{0: 'person'}"},
                              [_IO("images", [1, 3, 640, 640])], [_IO("output0", [1, 56, 8400])])
    d.session = _SpySession()
    d._initialised = True
    d.provider = "cpu"
    return d


def test_detector_enhances_only_dark_frames_and_reports_it(monkeypatch):
    from app.services.inference_backend import letterbox

    monkeypatch.setattr(settings, "LOW_LIGHT_ENHANCE", "auto")
    monkeypatch.setattr(ll, "lighting_monitor", ll.LightingMonitor())
    d = _spy_detector()
    day, dark = _scene(), _half_shadow(_scene(seed=5))

    d.detect(day, camera_id="cam_day")
    plain, *_ = letterbox(day, (640, 640))
    assert np.array_equal(d.session.blobs[-1], plain)

    d.detect(dark, camera_id="cam_shade")
    plain, *_ = letterbox(dark, (640, 640))
    assert not np.array_equal(d.session.blobs[-1], plain)
    assert d.session.blobs[-1].mean() > plain.mean()

    lighting = d.status()["lighting"]
    assert lighting["cameras"]["cam_day"]["lighting"] == "day"
    assert lighting["cameras"]["cam_day"]["frames_enhanced"] == 0
    assert lighting["cameras"]["cam_shade"]["lighting"] == "mixed"
    assert lighting["cameras"]["cam_shade"]["frames_enhanced"] == 1


def test_enhancement_is_off_by_default_but_lighting_is_still_measured(monkeypatch):
    """Measured on COCO persons, enhancement lowered recall, so it is off by
    default; the per-camera lighting state is reported regardless."""
    from app.services.inference_backend import letterbox

    monkeypatch.setattr(settings, "LOW_LIGHT_ENHANCE", "off")
    monkeypatch.setattr(ll, "lighting_monitor", ll.LightingMonitor())
    d = _spy_detector()
    dark = _darken(_scene(), -3.0)
    d.detect(dark, camera_id="cam")
    plain, *_ = letterbox(dark, (640, 640))
    assert np.array_equal(d.session.blobs[-1], plain)
    cam = d.status()["lighting"]["cameras"]["cam"]
    assert cam["lighting"] == "low_light" and cam["enhancing"] is False and cam["frames_enhanced"] == 0


# ------------------------------------------------------------ size gate


def test_narrow_distant_person_passes_width_floor_but_not_a_sliver(monkeypatch):
    monkeypatch.setattr(settings, "PERSON_MIN_BOX_PIXELS", 24)
    monkeypatch.setattr(settings, "PERSON_MIN_BOX_WIDTH_PIXELS", 16)
    d = PersonDetector(model_path="/nonexistent/pose.onnx", object_model_path="")
    area = 352.0 * 288.0
    assert d._is_plausible_person(100, 100, 118, 160, area)       # 18 x 60: a person far away
    assert not d._is_plausible_person(100, 100, 110, 160, area)   # 10 x 60: a sliver
    assert not d._is_plausible_person(100, 100, 130, 120, area)   # 20 px tall


# ------------------------------------------------ per-box dark threshold


def _det(conf, luma):
    from app.services.inference_backend import Detection

    return Detection(100, 50, 130, 150, confidence=conf, luma=luma)


def test_dark_boxes_need_less_confidence_than_lit_ones(monkeypatch):
    from app.services.inference_backend import person_threshold

    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD_DARK", 0.35)
    monkeypatch.setattr(settings, "PERSON_DARK_LUMA", 60.0)
    assert person_threshold(_det(0.4, 40.0)) == 0.35          # in shade
    assert person_threshold(_det(0.4, 140.0)) == 0.5          # in light
    assert person_threshold(_det(0.4, None)) == 0.5           # not measured
    # Never raises a threshold that is already lower.
    assert person_threshold(_det(0.4, 40.0), base=0.3) == 0.3


def test_detector_keeps_a_dark_person_a_lit_one_of_equal_score_is_dropped(monkeypatch):
    """The per-box rule on a real decode: two people scoring 0.42, one in shade."""
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD_DARK", 0.35)
    monkeypatch.setattr(settings, "PERSON_DARK_LUMA", 60.0)
    monkeypatch.setattr(settings, "LOW_LIGHT_ENHANCE", "off")
    raw = np.zeros((1, 56, 8400), np.float32)
    # 640x640 frame: no letterbox. Person A at x~160 (dark half), B at x~480 (lit half).
    for i, cx in enumerate((160.0, 480.0)):
        raw[0, 0:4, i] = (cx, 320.0, 60.0, 200.0)
        raw[0, 4, i] = 0.42
    d = _spy_detector()
    d.session.run = lambda _o, feeds: [raw]
    frame = np.full((640, 640, 3), 170, np.uint8)
    frame[:, :320] = 25
    dets = d.detect(frame)
    assert len(dets) == 1 and dets[0].x1 < 320 and dets[0].dark
    # An explicit threshold applies to every box, as before.
    assert len(d.detect(frame, conf_threshold=0.4)) == 2
    assert all(x.luma is not None for x in d.detect(frame, conf_threshold=0.4))


def test_tracker_starts_a_track_for_a_dark_person_but_not_a_lit_one(monkeypatch):
    from app.services.tracking_service import ByteTracker

    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "TRACK_NEW_TRACK_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "PERSON_CONF_THRESHOLD_DARK", 0.35)
    monkeypatch.setattr(settings, "PERSON_DARK_LUMA", 60.0)
    t = ByteTracker("cam")
    assert len(t.update([_det(0.42, 30.0)], now=1.0)) == 1
    t2 = ByteTracker("cam2")
    assert len(t2.update([_det(0.42, 150.0)], now=1.0)) == 0
