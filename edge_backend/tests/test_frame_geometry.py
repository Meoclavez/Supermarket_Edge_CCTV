"""Pixel geometry stays valid when a camera's delivered frame size changes.

Big streams are now scaled down on the GPU (DECODE_MAX_WIDTH), so the frames
the pipeline sees can be smaller than the frames a calibration, a legacy
pixel mask or the person-size gate were authored for. Each test checks that
the same point of the scene gives the same answer at different delivered
sizes. Also covered: the lazily converted NV12 frame, the evidence frame that
is masked only when copied, and the clip ring that is fed only when needed.
"""

from __future__ import annotations

import json
import sqlite3
import time

import cv2
import numpy as np
import pytest

from app.config import settings
from app.services import capture_backends as cb
from app.services import live_analytics_engine as lae
from app.services.clip_recorder import ClipRecorderService, StreamRingBuffer
from app.services.frame_geometry import FrameSizes, calibration_frame_size, frame_sizes, scale_point
from app.services.inference_backend import PersonDetector, _to_blob, letterbox
from app.services.privacy_mask import (
    DeferredMaskedFrame,
    apply_privacy_masks,
    deferred_privacy_masks,
    ignore_polygons,
    outside_ignore_regions,
    polygon_pixels,
)
from app.services.tracking_service import FloorProjector

CAM = "cam_geom_01"
# A 1280x720-authored calibration (the live NVR camera's size): a floor
# trapezoid in the image -> a 10 x 8 m area.
IMG = [(200, 700), (1080, 700), (900, 300), (380, 300)]
FLOOR = [(0, 0), (10, 0), (10, 8), (0, 8)]


@pytest.fixture(autouse=True)
def _clean_sizes():
    frame_sizes.clear()
    yield
    frame_sizes.clear()


def _projector() -> FloorProjector:
    fp = FloorProjector()
    fp.set_homography(CAM, FloorProjector.estimate_homography(IMG, FLOOR), (1280, 720))
    return fp


# ------------------------------------------------------------ homography


@pytest.mark.parametrize("delivered", [(1280, 720), (1920, 1080), (640, 360), (2560, 1440)])
def test_same_world_point_at_any_delivered_size(delivered):
    fp = _projector()
    ref = fp.to_floor(CAM, 640.0, 500.0)                      # authored pixels
    x, y = scale_point(640.0, 500.0, (1280, 720), delivered)  # the same spot, delivered pixels
    got = fp.to_floor(CAM, x, y, frame_size=delivered)
    assert got == pytest.approx(ref, abs=1e-9)
    # ... and through the size the worker registered, without passing it.
    frame_sizes.set(CAM, delivered, (2560, 1440))
    assert fp.to_floor(CAM, x, y) == pytest.approx(ref, abs=1e-9)


def test_calibration_corners_land_on_their_floor_points_when_downscaled():
    fp = _projector()
    for (u, v), (fx, fy) in zip(IMG, FLOOR):
        got = fp.to_floor(CAM, u * 1.5, v * 1.5, frame_size=(1920, 1080))
        assert got == pytest.approx((fx, fy), abs=1e-6)


def test_unknown_sizes_project_unscaled_as_before():
    fp = FloorProjector()
    H = FloorProjector.estimate_homography(IMG, FLOOR)
    fp.set_homography(CAM, H)                                 # no authored size recorded
    frame_sizes.set(CAM, (1920, 1080), (1920, 1080))
    assert fp.to_floor(CAM, 200, 700) == pytest.approx((0.0, 0.0), abs=1e-6)
    fp.set_homography(CAM, H, (1280, 720))
    frame_sizes.clear()                                      # delivered size unknown
    assert fp.to_floor(CAM, 200, 700) == pytest.approx((0.0, 0.0), abs=1e-6)
    fp.set_homography(CAM, None)
    assert fp.authored_size(CAM) is None and fp.to_floor(CAM, 1, 1) is None


def test_calibration_frame_size_reads_the_stored_json():
    assert calibration_frame_size({"frame_width": 1280, "frame_height": 720}) == (1280, 720)
    assert calibration_frame_size({"frame_width": None, "frame_height": None}) is None
    assert calibration_frame_size(None) is None
    assert calibration_frame_size({"frame_width": "x", "frame_height": 3}) is None


def test_frame_sizes_downscale_factor():
    fs = FrameSizes()
    assert fs.downscale("c") == 1.0 and fs.downscale(None) == 1.0
    fs.set("c", (1920, 1080), (2560, 1440))
    assert fs.downscale("c") == pytest.approx(0.75)
    fs.set("d", (704, 576))                                   # native defaults to delivered
    assert fs.native("d") == (704, 576) and fs.downscale("d") == 1.0


# ----------------------------------------------------- masks and zones


def _mask(points, **extra):
    return {"id": "m1", "camera_id": CAM, "mask_mode": "AI_IGNORE", "enabled": True, "points": points, **extra}


NORM = [{"x": 0.5, "y": 0.5}, {"x": 1.0, "y": 0.5}, {"x": 1.0, "y": 1.0}, {"x": 0.5, "y": 1.0}]


class _Det:
    def __init__(self, x, y):
        self.foot_point = (x, y)


@pytest.mark.parametrize("size", [(2560, 1440), (1920, 1080), (1280, 720)])
def test_zone_membership_is_the_same_at_any_delivered_size(size):
    """AI_IGNORE regions (normalised) drop the same people whatever the frame size."""
    w, h = size
    inside, outside = _Det(0.75 * w, 0.8 * h), _Det(0.25 * w, 0.8 * h)
    polys = ignore_polygons(CAM, w, h, [_mask(NORM)])
    assert outside_ignore_regions([inside, outside], polys) == [outside]


def test_legacy_pixel_mask_is_scaled_from_the_size_it_was_drawn_on():
    px = [{"x": 1280, "y": 720}, {"x": 2560, "y": 720}, {"x": 2560, "y": 1440}, {"x": 1280, "y": 1440}]
    ref = polygon_pixels(_mask(NORM), 1920, 1080)
    # Recorded drawing size.
    assert np.abs(polygon_pixels(_mask(px, frame_width=2560, frame_height=1440), 1920, 1080) - ref).max() <= 1
    # No recorded size: the camera's native stream size.
    frame_sizes.set(CAM, (1920, 1080), (2560, 1440))
    assert np.abs(polygon_pixels(_mask(px), 1920, 1080) - ref).max() <= 1
    # Delivered at native size: unchanged (clipped to the frame as before).
    frame_sizes.set(CAM, (2560, 1440), (2560, 1440))
    assert polygon_pixels(_mask(px), 2560, 1440).max() == 2559


def test_privacy_mask_covers_the_same_region_at_any_size():
    for w, h in ((1280, 720), (1920, 1080)):
        frame = np.full((h, w, 3), 200, np.uint8)
        out = apply_privacy_masks(frame, CAM, [{**_mask(NORM), "mask_mode": "BLACKOUT"}])
        assert not out[h // 2 + 2:, w // 2 + 2:].any()
        assert (out[: h // 2 - 2, :] == 200).all()


def test_person_size_gate_is_in_native_pixels():
    d = PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")
    area = 1920 * 1080
    # 13x22 px at 1920 = 17x29 px at native 2560: kept when scaled, dropped when not.
    box = (100, 100, 113, 122)
    assert not d._is_plausible_person(*box, area)
    assert d._is_plausible_person(*box, area, min_box_scale=0.75)


# ------------------------------------------------------------ frames


def _nv12(w=64, h=48):
    rng = np.random.default_rng(3)
    y = rng.integers(16, 236, (h, w), dtype=np.uint8)
    uv = rng.integers(16, 241, (h // 2, w), dtype=np.uint8)
    return np.vstack([y, uv])


def test_nv12_frame_converts_once_and_copies_are_independent():
    raw = _nv12()
    f = cb.Frame(nv12=raw, width=64, height=48)
    assert f.shape == (48, 64, 3) and not f.converted
    ref = cv2.cvtColor(raw, cv2.COLOR_YUV2BGR_NV12)
    copy = f.bgr_copy()
    assert np.array_equal(copy, ref) and not f.converted       # a snapshot does not convert for the worker
    shared = f.bgr()
    assert f.converted and f.bgr() is shared and np.array_equal(shared, ref)
    other = f.bgr_copy()
    other[:] = 0
    assert np.array_equal(f.bgr(), ref)
    assert np.array_equal(f.gray(), raw[:48])


def test_runtime_keeps_a_lazy_frame_and_serves_bgr():
    rt = lae.CameraRuntime(camera_id=CAM, name="c", source="/dev/null")
    rt.put_frame(cb.Frame(nv12=_nv12(), width=64, height=48))
    assert (rt.frame_width, rt.frame_height) == (64, 48)
    got = rt.get_frame()
    assert got.shape == (48, 64, 3) and got.dtype == np.uint8
    rt.put_frame(np.zeros((10, 20, 3), np.uint8))               # plain BGR still accepted
    assert rt.get_frame().shape == (10, 20, 3)


def test_read_frame_wraps_an_opencv_capture():
    class Cap:
        def read(self):
            return True, np.ones((4, 6, 3), np.uint8)

    ok, f = cb.read_frame(Cap())
    assert ok and isinstance(f, cb.Frame) and f.bgr().shape == (4, 6, 3)


def test_deferred_mask_is_applied_only_on_copy():
    frame = np.full((40, 60, 3), 200, np.uint8)
    masks = [{**_mask(NORM), "mask_mode": "BLACKOUT"}]
    ev = deferred_privacy_masks(frame, CAM, masks)
    assert isinstance(ev, DeferredMaskedFrame) and ev.shape == frame.shape and ev.ndim == 3
    out = ev.copy()
    assert not out[22:, 32:].any() and (frame == 200).all()     # masked copy, source untouched
    assert deferred_privacy_masks(frame, CAM, [_mask(NORM)]) is frame   # AI_IGNORE only: nothing to hide


def test_letterbox_blob_matches_the_numpy_formula():
    img = np.random.default_rng(1).integers(0, 255, (300, 500, 3), dtype=np.uint8)
    blob, *_ = letterbox(img, (320, 192))
    canvas = np.random.default_rng(2).integers(0, 255, (192, 320, 3), dtype=np.uint8)
    ref = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) * np.float32(1 / 255)
    assert np.array_equal(_to_blob(canvas), ref)
    assert blob.shape == (1, 3, 192, 320) and blob.flags["C_CONTIGUOUS"]
    assert _to_blob(canvas, np.float16).dtype == np.float16


# ------------------------------------------------------ capture colour


@pytest.mark.parametrize("line,need", [
    ("Stream #0:0: Video: hevc (Main), yuv420p(tv, progressive), 2560x1440", False),
    ("Stream #0:0: Video: h264 (High), yuv420p(progressive), 1280x720", False),
    ("Stream #0:0: Video: h264 (Main), yuv420p(tv, bt470bg/bt470bg/smpte170m, progressive), 704x576", False),
    ("Stream #0:0: Video: h264 (High), yuvj420p(pc, progressive), 1280x720", True),
    ("Stream #0:0: Video: hevc (Main), yuv420p(tv, bt709, progressive), 2560x1440", True),
    ("Stream #0:0: Video: h264, yuv420p(tv, bt709/unknown/unknown), 352x288", True),
])
def test_colour_normalisation_is_asked_only_for_non_bt601_streams(line, need):
    assert cb.colour_needs_normalise(line) is need


def test_nv12_chain_downscales_on_the_gpu_and_normalises_on_request():
    vf = cb.filter_chain(cb.VAAPI, cb.NV12_CHAIN, 5, 1920)
    assert "scale_vaapi=w=min(iw\\,1920):h=-2:format=nv12,hwdownload,format=nv12" in vf
    assert "out_color_matrix" not in vf and "bgr24" not in vf
    assert cb.filter_chain(cb.VAAPI, cb.NV12_CHAIN, 5, 0, normalise=True).endswith(
        "format=nv12,scale=out_color_matrix=bt601:out_range=tv,format=nv12")


def test_stream_sizes_remember_the_colour_need(tmp_path):
    s = cb.StreamSizes(tmp_path / "sizes.json")
    s.put(CAM, "rtsp://h/a", 2560, 1440, normalise=True)
    s.put(CAM, "rtsp://h/a", 2560, 1440)                     # unknown (software open): kept
    assert s.needs_normalise(CAM, "rtsp://h/a") and s.get(CAM, "rtsp://h/a") == (2560, 1440)
    fresh = cb.StreamSizes(tmp_path / "sizes.json")
    assert fresh.needs_normalise(CAM, "rtsp://h/a")
    fresh.put(CAM, "rtsp://h/a", 2560, 1440, normalise=False)
    assert not fresh.needs_normalise(CAM, "rtsp://h/a")


def test_max_width_auto_follows_the_pose_model(monkeypatch):
    monkeypatch.setattr(settings, "DECODE_MAX_WIDTH", "auto")
    assert cb.max_width_setting() is None
    assert cb.auto_max_width(960) == 1920 and cb.auto_max_width(640) == 0 and cb.auto_max_width(None) == 0
    monkeypatch.setattr(settings, "DECODE_MAX_WIDTH", "1280")
    assert cb.max_width_setting() == 1280
    monkeypatch.setattr(settings, "DECODE_MAX_WIDTH", "0")
    assert cb.max_width_setting() == 0


# ------------------------------------------------------------ worker


class _Spec:
    def __init__(self, w, h):
        self.input_size = (w, h)


class _Hw(cb.FfmpegHwCapture):
    def __init__(self, max_width, source_size=(2560, 1440), source_fps=25.0, output_fps=5.0):  # noqa: D107
        self.max_width, self.source_size, self.source_fps = max_width, source_size, source_fps
        self._out = output_fps

    @property
    def output_fps(self):
        return self._out


def _worker():
    rt = lae.CameraRuntime(camera_id=CAM, name="c", source="rtsp://h/a")
    return lae.CameraWorker(rt, lae.LiveAnalyticsEngine())


def test_worker_width_follows_model_and_setting(monkeypatch):
    w = _worker()
    monkeypatch.setattr(settings, "DECODE_MAX_WIDTH", "auto")
    monkeypatch.setattr(lae, "camera_setting", lambda cam, name: None)
    monkeypatch.setattr(lae.person_detector, "spec", _Spec(960, 544), raising=False)
    assert w._max_width() == 1920
    assert not w._width_changed(_Hw(1920)) and w._width_changed(_Hw(0))
    monkeypatch.setattr(lae.person_detector, "spec", _Spec(640, 640), raising=False)
    assert w._max_width() == 0 and w._width_changed(_Hw(1920))
    assert not w._width_changed(_Hw(1920, source_size=(1280, 720)))    # never wider than 1920 anyway
    monkeypatch.setattr(lae, "camera_setting", lambda cam, name: 1280 if name == "decode_max_width" else None)
    assert w._max_width() == 1280


@pytest.mark.parametrize("native,delivered,clip_n,detect_n", [
    (25.0, 10.0, 2, 2), (25.0, 5.0, 1, 1), (15.0, 5.0, 2, 1), (15.0, 10.0, 3, 3), (6.0, 5.0, 4, 4),
])
def test_thinning_never_lowers_the_analysis_ceiling(native, delivered, clip_n, detect_n, monkeypatch):
    monkeypatch.setattr(settings, "ANALYTICS_DETECT_EVERY_N_FRAMES", 5)
    w = _worker()
    assert w._clip_every(_Hw(0, source_fps=native, output_fps=delivered)) == clip_n
    assert w._detect_n == detect_n
    assert delivered / w._detect_n >= min(delivered, native / 5) - 1e-9


# ------------------------------------------------------------ clip ring


def test_clip_ring_is_fed_only_when_a_clip_can_need_it(monkeypatch):
    svc = ClipRecorderService()
    monkeypatch.setattr(settings, "NIGHT_WATCH_CLIP", False)
    monkeypatch.setattr(settings, "CLIP_PRE_EVENT_BUFFER", "auto")
    assert not svc.wants(CAM)
    svc.request(CAM, 5)
    assert svc.wants(CAM) and not svc.wants("other")
    monkeypatch.setattr(settings, "CLIP_PRE_EVENT_BUFFER", "on")
    assert svc.keeps_ring("other")
    monkeypatch.setattr(settings, "CLIP_PRE_EVENT_BUFFER", "off")
    assert not svc.keeps_ring("other") and svc.wants(CAM)      # a clip in progress still gets frames

    from app.services import night_watch as nw

    monkeypatch.setattr(settings, "CLIP_PRE_EVENT_BUFFER", "auto")
    monkeypatch.setattr(settings, "NIGHT_WATCH_CLIP", True)
    monkeypatch.setattr(nw.night_watch, "is_armed", lambda cam: cam == "armed")
    assert svc.keeps_ring("armed") and not svc.keeps_ring("idle")


def test_old_ring_frames_never_open_a_new_clip():
    buf = StreamRingBuffer(CAM, max_seconds=5, fps=5)
    buf.push_frame(np.zeros((8, 8, 3), np.uint8))
    buf.buffer[0] = (time.time() - 3600, buf.buffer[0][1])     # fed on demand an hour ago
    buf.push_frame(np.zeros((8, 8, 3), np.uint8))
    assert len(buf.get_pre_event_frames()) == 1


# ------------------------------------------------------------ migration


def test_m0016_records_the_native_size_for_old_calibrations(tmp_path):
    from app.migrations import m0016_calibration_frame_size as m

    db = tmp_path / "cctv_core.db"
    (tmp_path / "decode_stream_sizes.json").write_text(json.dumps({
        "cam_old": {"k1": [1280, 720]},
        "cam_two": {"k1": [704, 576], "k2": [2560, 1440]},
    }))
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cameras (id TEXT PRIMARY KEY, homography_matrix JSON, calibration_points JSON)")
    H = json.dumps([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    rows = [
        ("cam_old", H, json.dumps({"image_points": [{"x": 1, "y": 2}]})),
        ("cam_new", H, json.dumps({"frame_width": 1920, "frame_height": 1080})),
        ("cam_two", H, None),
        ("cam_uncal", None, None),
    ]
    conn.executemany("INSERT INTO cameras VALUES (?, ?, ?)", rows)
    m.upgrade(conn)
    got = {r[0]: json.loads(r[1]) if r[1] else None for r in conn.execute("SELECT id, calibration_points FROM cameras")}
    assert (got["cam_old"]["frame_width"], got["cam_old"]["frame_height"]) == (1280, 720)
    assert got["cam_old"]["image_points"] == [{"x": 1, "y": 2}]
    assert got["cam_new"] == {"frame_width": 1920, "frame_height": 1080}
    assert got["cam_two"] is None and got["cam_uncal"] is None     # ambiguous / uncalibrated: untouched
    m.upgrade(conn)                                                # idempotent
    conn.close()


def test_event_snapshot_comes_from_the_live_frame_without_a_ring(monkeypatch, tmp_path):
    svc = ClipRecorderService()
    rt = lae.CameraRuntime(camera_id=CAM, name="c", source="/dev/null")
    rt.put_frame(np.full((24, 32, 3), 90, np.uint8))
    monkeypatch.setitem(lae.live_engine.runtimes, CAM, rt)
    monkeypatch.setattr(settings, "SNAPSHOTS_DIR", tmp_path)
    url = svc.save_snapshot(CAM, "evt_geom")
    assert url and (tmp_path / "evt_geom.jpg").exists()
    assert svc.save_snapshot("cam_none", "evt_none") is None
