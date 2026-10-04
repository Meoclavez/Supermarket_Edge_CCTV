"""Ignore areas (AI_IGNORE coverage rule, from-box, clear kinds) and the hardened static filter.

Round 2 of the poster fix: a human-sized poster must not be counted as a
person. Covers

* privacy_mask: a detection is dropped when its foot point is inside an
  AI_IGNORE area OR >= ``ignore_box_fraction`` of its box lies inside;
* routes/zones.py: ``ignore_box_fraction`` on exclusions, ``POST
  /api/zones/exclusion/from-box``, ``kinds`` privacy / ignore / all on clear;
* tracking_service: "pending static" tracks, wall-clock persisted memory,
  jitter floor on box motion, occlusion by a passer-by, gaps on a remembered box;
* live engine: never-moved tracks are never stored, telemetry, overlay.

All time is a fake clock passed as ``now``; nothing sleeps.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services import live_analytics_engine as lae
from app.services.ai_zone_service import ai_zone_service
from app.services.inference_backend import Detection
from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine
from app.services.privacy_mask import (
    IgnoreRegion,
    box_coverage,
    ignore_polygons,
    outside_ignore_regions,
)
from app.services.tracking_service import ByteTracker, floor_projector

CAM = "ia_cam_01"
FPS = 5.0
DT = 1.0 / FPS
BOX = (100.0, 50.0, 180.0, 300.0)
_POSE = {
    0: (140, 70), 1: (135, 65), 2: (145, 65), 3: (130, 68), 4: (150, 68),
    5: (120, 110), 6: (160, 110), 7: (112, 150), 8: (168, 150),
    9: (108, 190), 10: (172, 190), 11: (125, 190), 12: (155, 190),
    13: (125, 240), 14: (155, 240), 15: (125, 290), 16: (155, 290),
}


def skeleton(dx: float = 0.0, dy: float = 0.0, moves: dict | None = None) -> np.ndarray:
    k = np.zeros((17, 3), dtype=np.float32)
    for i, (x, y) in _POSE.items():
        mx, my = (moves or {}).get(i, (0.0, 0.0))
        k[i] = (x + dx + mx, y + dy + my, 0.95)
    return k


def det(rng=None, jitter: float = 0.0, dx: float = 0.0, dy: float = 0.0, moves=None,
        box=BOX, conf: float = 0.91) -> Detection:
    k = skeleton(dx + box[0] - BOX[0], dy + box[1] - BOX[1], moves)
    b = np.array([box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy], dtype=np.float64)
    if jitter and rng is not None:
        k[:, :2] += rng.uniform(-jitter, jitter, size=(17, 2)).astype(np.float32)
        b += rng.uniform(-jitter, jitter, size=4)
    return Detection(x1=float(b[0]), y1=float(b[1]), x2=float(b[2]), y2=float(b[3]), confidence=conf, keypoints=k)


def run(tr: ByteTracker, t0: float, seconds: float, make) -> float:
    n = int(round(seconds * FPS))
    for i in range(n):
        tr.update(make(i), now=t0 + i * DT)
    return t0 + n * DT


def only_track(tr: ByteTracker):
    (t,) = tr.tracks.values()
    return t


def retire(tr: ByteTracker, now: float) -> float:
    for _ in range(settings.TRACK_MAX_AGE_FRAMES + 2):
        tr.update([], now=now)
        now += DT
    tr.drain_finished()
    return now


@pytest.fixture
def rng():
    return np.random.default_rng(4321)


def _poster_tracker(rng, t0: float = 1000.0) -> tuple[ByteTracker, float]:
    """A tracker that has learned the poster at BOX (static after 61 s), its track retired."""
    tr = ByteTracker(CAM)
    tr.set_frame_size(640, 480)
    now = run(tr, t0, 61.0, lambda i: [det(rng, 2.0)])
    assert only_track(tr).is_static
    return tr, retire(tr, now)


# ----------------------------------------------------------- coverage rule


def _sq(x1, y1, x2, y2):
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)


def test_box_coverage_rectangles_and_concave_polygons():
    assert box_coverage(_sq(0, 0, 100, 100), (0, 0, 100, 100)) == pytest.approx(1.0)
    assert box_coverage(_sq(0, 0, 50, 100), (0, 0, 100, 100)) == pytest.approx(0.5)
    assert box_coverage(_sq(200, 200, 300, 300), (0, 0, 100, 100)) == 0.0
    # An L shape (concave): 100x100 square minus its top-right 50x50 quarter.
    ell = np.array([[0, 0], [50, 0], [50, 50], [100, 50], [100, 100], [0, 100]], dtype=np.float32)
    assert box_coverage(ell, (0, 0, 100, 100)) == pytest.approx(0.75)
    assert box_coverage(ell, (50, 0, 100, 50)) == pytest.approx(0.0, abs=1e-6)
    assert box_coverage(ell, (25, 25, 75, 75)) == pytest.approx(0.75)


def test_box_mostly_inside_is_dropped_even_with_the_foot_outside():
    area = IgnoreRegion(_sq(90, 40, 190, 280), 0.6)        # the poster's feet (y 300) are below it
    poster = det(box=BOX)                                    # 230 of 250 px of height inside
    passer = det(box=(160.0, 50.0, 240.0, 300.0))            # 30 of 80 px of width inside
    assert box_coverage(area, poster.bbox) > 0.9
    assert outside_ignore_regions([poster, passer], [area]) == [passer]
    # A stricter area keeps a box that is only partly inside.
    strict = IgnoreRegion(_sq(90, 40, 190, 280), 1.0)
    assert outside_ignore_regions([poster], [strict]) == [poster]
    # A plain polygon (older caller) uses settings.IGNORE_BOX_FRACTION.
    assert outside_ignore_regions([poster, passer], [_sq(90, 40, 190, 280)]) == [passer]
    # The foot-point rule is unchanged.
    feet = IgnoreRegion(_sq(120, 290, 160, 310), 1.0)
    assert outside_ignore_regions([poster], [feet]) == []
    # foot only (box=None): the area rule is off.
    assert outside_ignore_regions([poster], [area], box=None) == [poster]


def test_ignore_polygons_carry_the_mask_fraction():
    masks = [{"id": "m1", "camera_id": CAM, "mask_mode": "AI_IGNORE", "points": [
        {"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.1}, {"x": 0.3, "y": 0.6}, {"x": 0.1, "y": 0.6}],
        "ignore_box_fraction": 0.3},
        {"id": "m2", "camera_id": CAM, "mask_mode": "AI_IGNORE", "points": [
            {"x": 0.5, "y": 0.1}, {"x": 0.7, "y": 0.1}, {"x": 0.7, "y": 0.6}]},
        {"id": "m3", "camera_id": CAM, "mask_mode": "BLUR", "points": [
            {"x": 0.5, "y": 0.1}, {"x": 0.7, "y": 0.1}, {"x": 0.7, "y": 0.6}]}]
    polys = ignore_polygons(CAM, 640, 480, masks)
    assert [p.mask_id for p in polys] == ["m1", "m2"]
    assert polys[0].box_fraction == pytest.approx(0.3)
    assert polys[1].box_fraction == pytest.approx(settings.IGNORE_BOX_FRACTION) == pytest.approx(0.6)
    assert polys[0].dtype == np.float32 and polys[0].shape == (4, 2)


def test_per_frame_cost_of_the_coverage_rule():
    """20 detections against 4 ignore areas: well under a millisecond or two per frame."""
    rng = np.random.default_rng(0)
    polys = [IgnoreRegion(_sq(x, 50, x + 120, 400), 0.6) for x in (50, 300, 600, 900)]
    polys.append(IgnoreRegion(np.array([[1000, 100], [1200, 120], [1150, 500], [1050, 450],
                                        [1010, 300], [990, 200]], dtype=np.float32), 0.6))
    dets = []
    for _ in range(20):
        x, y = rng.uniform(0, 1700), rng.uniform(0, 700)
        dets.append(Detection(x1=x, y1=y, x2=x + 80, y2=y + 250, confidence=0.9))
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        outside_ignore_regions(dets, polys)
    ms = (time.perf_counter() - t0) * 1000.0 / n
    print(f"coverage rule: {ms:.3f} ms per frame (20 detections x 5 areas)")
    assert ms < 5.0


# ------------------------------------------------------------------ API


@pytest.fixture
def api():
    client = TestClient(lae_app())

    def wipe():
        for zid, z in list(ai_zone_service.exclusion_masks.items()):
            if str(z.get("camera_id", "")).startswith("ia_"):
                ai_zone_service.exclusion_masks.pop(zid, None)
        ai_zone_service._save_persistent_zones()
    wipe()
    yield client
    wipe()


def lae_app():
    from app.main import app
    return app


_RECT = [{"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.1}, {"x": 0.3, "y": 0.6}, {"x": 0.1, "y": 0.6}]


def test_exclusion_ignore_box_fraction_is_stored_validated_and_returned(api):
    r = api.post("/api/zones/exclusion", json={"camera_id": "ia_a", "points": _RECT, "mask_mode": "AI_IGNORE",
                                               "ignore_box_fraction": 0.4})
    assert r.status_code == 200, r.text
    ex = r.json()["exclusion_mask"]
    assert ex["ignore_box_fraction"] == 0.4 and ex["mask_mode"] == "AI_IGNORE"
    # Default when not given; null for a privacy mask.
    d = api.post("/api/zones/exclusion", json={"camera_id": "ia_a", "points": _RECT,
                                               "mask_mode": "AI_IGNORE"}).json()["exclusion_mask"]
    assert d["ignore_box_fraction"] == 0.6
    b = api.post("/api/zones/exclusion", json={"camera_id": "ia_a", "points": _RECT,
                                               "mask_mode": "BLUR"}).json()["exclusion_mask"]
    assert b["ignore_box_fraction"] is None
    for bad in (0.05, 1.5, "lots"):
        r = api.post("/api/zones/exclusion", json={"camera_id": "ia_a", "points": _RECT,
                                                   "mask_mode": "AI_IGNORE", "ignore_box_fraction": bad})
        assert r.status_code == 422, (bad, r.text)
    # PATCH changes it; out of range is refused.
    r = api.patch(f"/api/zones/exclusion/{d['id']}", json={"ignore_box_fraction": 0.9})
    assert r.status_code == 200 and r.json()["exclusion_mask"]["ignore_box_fraction"] == 0.9
    assert api.patch(f"/api/zones/exclusion/{d['id']}", json={"ignore_box_fraction": 0.0}).status_code == 422
    # Every listing returns the field.
    listed = {m["id"]: m for m in api.get("/api/zones").json()["exclusion_masks"]}
    assert listed[ex["id"]]["ignore_box_fraction"] == 0.4 and listed[d["id"]]["ignore_box_fraction"] == 0.9
    assert listed[b["id"]]["ignore_box_fraction"] is None
    per_cam = [z for z in api.get("/api/v1/cameras/ia_a/zones").json() if z["zone_type"] == "EXCLUSION"]
    assert {z["id"]: z["ignore_box_fraction"] for z in per_cam}[d["id"]] == 0.9


def test_exclusion_from_box_pads_and_clamps(api):
    r = api.post("/api/zones/exclusion/from-box", json={"camera_id": "ia_b", "box": [0.2, 0.1, 0.4, 0.9]})
    assert r.status_code == 200, r.text
    ex = r.json()["exclusion_mask"]
    assert r.json()["status"] == "success"
    assert ex["mask_mode"] == "AI_IGNORE" and ex["camera_id"] == "ia_b" and ex["enabled"] is True
    assert ex["ignore_box_fraction"] == 0.6 and ex["name"] == "Ignore area"
    xs, ys = [p["x"] for p in ex["points"]], [p["y"] for p in ex["points"]]
    assert (min(xs), max(xs)) == pytest.approx((0.18, 0.42))
    assert (min(ys), max(ys)) == pytest.approx((0.02, 0.98))
    assert ex["id"] in ai_zone_service.exclusion_masks
    # Clamped to the frame, own name and pad.
    r = api.post("/api/zones/exclusion/from-box", json={"camera_id": "ia_b", "box": [0.0, 0.0, 0.5, 1.0],
                                                        "pad": 0.5, "name": " Poster by the door "})
    ex = r.json()["exclusion_mask"]
    xs, ys = [p["x"] for p in ex["points"]], [p["y"] for p in ex["points"]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (0.0, 0.75, 0.0, 1.0) and ex["name"] == "Poster by the door"
    for body in ({"camera_id": "ia_b", "box": [0.4, 0.1, 0.2, 0.9]},
                 {"camera_id": "ia_b", "box": [0.1, 0.1, 0.2]},
                 {"camera_id": "ia_b", "box": [0.1, 0.1, 0.2, 0.9], "pad": 0.8},
                 {"camera_id": "", "box": [0.1, 0.1, 0.2, 0.9]},
                 {"camera_id": "ia_b", "box": [1.2, 0.1, 1.4, 0.9]}):
        assert api.post("/api/zones/exclusion/from-box", json=body).status_code == 422, body


def test_from_box_is_an_admin_only_setup_write():
    from app.services.auth_service import write_allowed

    op = {"type": "user_session", "sub": "sam", "role": "operator"}
    assert not write_allowed("POST", "/api/zones/exclusion/from-box", "operator", op)
    assert not write_allowed("POST", "/api/zones/clear", "operator", op)
    adm = {"type": "user_session", "sub": "ada", "role": "admin"}
    assert write_allowed("POST", "/api/zones/exclusion/from-box", "admin", adm)


def test_clear_kinds_privacy_ignore_all(api):
    def add(cam, mode):
        return api.post("/api/zones/exclusion", json={"camera_id": cam, "points": _RECT,
                                                      "mask_mode": mode}).json()["exclusion_mask"]["id"]

    def mine():
        return {m["id"] for m in ai_zone_service.exclusion_masks.values()
                if str(m.get("camera_id", "")).startswith("ia_")}

    blur, ign, blur2, ign2 = add("ia_c", "BLUR"), add("ia_c", "AI_IGNORE"), add("ia_d", "MOSAIC"), add("ia_d", "AI_IGNORE")
    assert api.post("/api/zones/clear?kinds=bogus").status_code == 422
    # Only this camera's ignore areas (query form).
    r = api.post("/api/zones/clear?kinds=ignore&camera_id=ia_c")
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert mine() == {blur, blur2, ign2}
    # Privacy masks only (JSON body form), every camera matching the scope.
    r = api.post("/api/zones/clear", json={"kinds": "privacy", "camera_id": "ia_d"})
    assert r.status_code == 200 and r.json()["removed"] == 1 and mine() == {blur, ign2}
    r = api.post("/api/zones/clear", json={"kinds": ["all"], "camera_id": "ia_c"})
    assert r.json()["removed"] == 1 and mine() == {ign2}
    # Legacy kind names still work.
    r = api.post("/api/zones/clear?kinds=exclusion_masks&camera_id=ia_d")
    assert r.json()["removed"] == 1 and mine() == set()


# ------------------------------------------------------------- tracker


def test_new_track_on_a_remembered_figure_is_not_human_during_the_grace(rng):
    tr, now = _poster_tracker(rng)
    now = run(tr, now, 3.0, lambda i: [det(rng, 2.0)])
    t = only_track(tr)
    assert t.confirmed and t.motion_state == "pending" and t.static_suspect == "memory"
    assert t.is_pending_static and not t.is_human and not t.established
    now = run(tr, now, 3.0, lambda i: [det(rng, 2.0)])
    assert only_track(tr).is_static


def test_pending_static_that_moves_is_a_person_at_once(rng):
    tr, now = _poster_tracker(rng)
    now = run(tr, now, 2.0, lambda i: [det(rng, 1.0)])
    t = only_track(tr)
    assert t.is_pending_static
    run(tr, now, 1.0, lambda i: [det(rng, 1.0, dx=15.0 * (i + 1))])
    assert t.motion_state == "moving" and t.is_human and t.established and t.static_suspect is None


def test_track_mostly_inside_an_ignore_area_is_pending_static(rng):
    tr = ByteTracker(CAM)
    tr.set_frame_size(640, 480)
    # Half of the box inside: kept by the 0.6 drop rule, but suspected.
    tr.set_ignore_regions([IgnoreRegion(_sq(100, 50, 180, 180), 0.6)])
    now = run(tr, 2000.0, 2.0, lambda i: [det(rng, 1.0)])
    t = only_track(tr)
    assert t.static_suspect == "ignore_area" and not t.is_human
    run(tr, now, 4.0, lambda i: [det(rng, 1.0)])
    assert t.is_static
    # Outside every area: a plain pending track, human (fails open).
    tr2 = ByteTracker(CAM)
    tr2.set_ignore_regions([IgnoreRegion(_sq(400, 50, 500, 180), 0.6)])
    run(tr2, 3000.0, 2.0, lambda i: [det(rng, 1.0)])
    assert only_track(tr2).is_human and only_track(tr2).static_suspect is None


def test_memory_is_saved_normalised_and_survives_a_restart(rng, tmp_path):
    path = tmp_path / "static_memory" / f"{CAM}.json"
    tr = ByteTracker(CAM)
    t0 = time.time() - 120.0                         # wall clock, as the live engine uses
    assert tr.attach_static_store(path, now=t0) == 0
    tr.set_frame_size(640, 480)
    run(tr, t0, 61.0, lambda i: [det(rng, 2.0)])
    assert only_track(tr).is_static
    data = json.loads(path.read_text())
    (entry,) = data["entries"]
    assert all(0.0 <= v <= 1.0 for v in entry["box"])
    assert entry["box"][0] == pytest.approx(100.0 / 640, abs=0.01)
    assert len(entry["keypoints"]) == 17

    # "Restart" at another frame size: the box is placed on the new frame.
    tr2 = ByteTracker(CAM)
    assert tr2.attach_static_store(path) == 1
    assert tr2.static_memory == []                   # no frame size yet
    tr2.set_frame_size(1280, 960)
    (e,) = tr2.static_memory
    assert e.bbox[0] == pytest.approx(200.0, abs=8) and e.bbox[3] == pytest.approx(600.0, abs=8)
    big = Detection(x1=200.0, y1=100.0, x2=360.0, y2=600.0, confidence=0.9, keypoints=_scaled(2.0))
    assert tr2.matches_static_memory(big.bbox, big.keypoints, time.time())
    # A reopen at another size rescales what is in memory.
    tr2.set_frame_size(640, 480)
    assert tr2.static_memory[0].bbox[0] == pytest.approx(100.0, abs=4)


def _scaled(f: float) -> np.ndarray:
    k = skeleton()
    k[:, :2] *= f
    return k


def test_persisted_memory_expires_by_wall_clock(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    old = time.time() - settings.STATIC_MEMORY_TTL_SEC - 5.0
    path.write_text(json.dumps({"version": 1, "entries": [
        {"box": [0.1, 0.1, 0.3, 0.6], "keypoints": None, "scale": 0.1, "first_seen": old, "last_seen": old},
        {"box": [0.5, 0.1, 0.7, 0.6], "keypoints": None, "scale": 0.1, "first_seen": old,
         "last_seen": time.time() - 60.0}]}))
    tr = ByteTracker(CAM)
    assert tr.attach_static_store(path) == 1
    tr.set_frame_size(100, 100)
    assert [tuple(round(v) for v in e.bbox) for e in tr.static_memory] == [(50, 10, 70, 60)]
    # A broken file never stops the camera.
    path.write_text("{not json")
    assert ByteTracker(CAM).attach_static_store(path) == 0


def test_load_logs_one_info_line_with_the_count(tmp_path, caplog):
    import logging

    path = tmp_path / "m.json"
    now = time.time()
    path.write_text(json.dumps({"entries": [
        {"box": [0.1, 0.1, 0.3, 0.6], "keypoints": None, "scale": 0.1, "first_seen": now, "last_seen": now},
        {"box": [0.5, 0.1, 0.7, 0.6], "keypoints": None, "scale": 0.1, "first_seen": now, "last_seen": now}]}))
    with caplog.at_level(logging.INFO, logger="edge.pipeline"):
        ByteTracker(CAM).attach_static_store(path)
    lines = [r.getMessage() for r in caplog.records if "remembered static figure" in r.getMessage()]
    assert lines == [f"Camera {CAM}: loaded 2 remembered static figure(s) (posters/mannequins); "
                     "a track on one stays uncounted until it moves"]


def test_passer_by_occluding_a_static_poster_does_not_unflag_it(rng):
    tr = ByteTracker(CAM)
    tr.set_frame_size(640, 480)
    now = run(tr, 4000.0, 61.0, lambda i: [det(rng, 2.0)])
    poster = only_track(tr)
    assert poster.is_static
    # 3 s: someone walks in front; the poster's box stays, its joints are disturbed.
    disturbed = {j: (25.0, 10.0) for j in (0, 1, 2, 5, 7, 9)}
    now = run(tr, now, 3.0, lambda i: [det(rng, 2.0, moves=disturbed),
                                       det(rng, 1.0, box=(140.0 + 2 * i, 60.0, 220.0 + 2 * i, 310.0))])
    assert poster.is_static and poster.track_id in tr.tracks

    # The same disturbance with nobody in front is the figure's own motion.
    tr2 = ByteTracker(CAM)
    now = run(tr2, 5000.0, 61.0, lambda i: [det(rng, 2.0)])
    t2 = only_track(tr2)
    assert t2.is_static
    run(tr2, now, 1.0, lambda i: [det(rng, 2.0, moves=disturbed)])
    assert t2.motion_state == "moving"


def test_occlusion_is_bounded_so_a_real_person_is_a_person_again(rng, monkeypatch):
    monkeypatch.setattr(settings, "STATIC_OCCLUSION_MAX_SEC", 2.0)
    tr = ByteTracker(CAM)
    now = run(tr, 4500.0, 61.0, lambda i: [det(rng, 2.0)])
    t = only_track(tr)

    def frame(i):
        arm = {9: (0.0, -40.0), 10: (0.0, -40.0)}      # arms raised and held, someone beside it
        return [det(rng, 1.0, moves=arm), det(rng, 1.0, box=(150.0, 60.0, 230.0, 310.0))]

    run(tr, now, 4.0, frame)
    assert t.motion_state == "moving"


def test_detection_gap_on_a_remembered_box_keeps_the_stillness_clock(rng):
    tr, now = _poster_tracker(rng)
    now = run(tr, now, 2.0, lambda i: [det(rng, 2.0)])
    t = only_track(tr)
    still_since = t.still_since
    # 30 s with no detection (> MOTION_MAX_OBS_GAP_SEC, <= STATIC_MEMORY_MAX_GAP_SEC); the track coasts.
    monkey_age = settings.TRACK_MAX_AGE_FRAMES
    tr.update([det(rng, 2.0)], now=now + 30.0)
    assert t.still_since == still_since and t.is_static
    assert monkey_age == settings.TRACK_MAX_AGE_FRAMES
    # Without a memory, the same gap restarts the clock (unchanged behaviour).
    tr2 = ByteTracker(CAM)
    now2 = run(tr2, 9000.0, 20.0, lambda i: [det(rng, 2.0)])
    t2 = only_track(tr2)
    tr2.update([det(rng, 2.0)], now=now2 + 30.0)
    assert t2.still_since == pytest.approx(now2 + 30.0)


def test_box_jitter_floor_lets_a_jittery_small_poster_go_static(monkeypatch):
    """Box edges jittering by up to ~0.2 torso would restart the clock without the floor."""
    from app.services import tracking_service

    def jittery(seed):
        r = np.random.default_rng(seed)

        def make(i):
            d = det(r, 1.0)
            dy = float(r.normal(0.0, 10.0))
            return [Detection(x1=d.x1, y1=d.y1 + dy, x2=d.x2, y2=d.y2 + dy * 0.2, confidence=d.confidence,
                              keypoints=d.keypoints)]
        return make

    tr = ByteTracker(CAM)
    run(tr, 100.0, 75.0, jittery(7))
    assert only_track(tr).is_static
    monkeypatch.setattr(tracking_service, "MOTION_JITTER_K", 0.0)
    tr2 = ByteTracker(CAM)
    run(tr2, 100.0, 75.0, jittery(7))
    assert not only_track(tr2).is_static


# -------------------------------------------------------------- engine


class _FakePose:
    def __init__(self):
        self.seen = []

    def observe(self, camera_id, ts, frame_bgr, tracks):
        self.seen.append([t.track_id for t in tracks])

        class R:
            interactions = []
        return R()

    def reset_camera(self, camera_id):
        pass


@pytest.fixture
def fake_pose(monkeypatch):
    fake = _FakePose()
    monkeypatch.setitem(lae._pose_state, "module", fake)
    monkeypatch.setitem(lae._pose_state, "status", "ok")
    monkeypatch.setitem(lae._pose_state, "error", None)
    return fake


@pytest.fixture
def tripwires(monkeypatch):
    from app.services.tripwire_engine import tripwire_engine

    seen = []
    monkeypatch.setattr(tripwire_engine, "evaluate",
                        lambda camera_id, tracks, w, h, now, frame=None, counting=True, camera_name=None:
                        seen.append([t.track_id for t in tracks]))
    return seen


@pytest.fixture
def worker():
    from app.models.schemas import CameraFeatureConfig
    from app.services.feature_manager import feature_manager

    feature_manager.set_camera_features(CAM, CameraFeatureConfig())
    engine = LiveAnalyticsEngine()
    rt = CameraRuntime(camera_id=CAM, name="Ignore Cam", source="/dev/null")
    engine.runtimes[CAM] = rt
    w = CameraWorker(rt, engine)
    yield w, rt, engine
    floor_projector.set_homography(CAM, None)
    feature_manager.remove_camera(CAM)


@pytest.fixture
def scripted(monkeypatch):
    """detect() returns script[i] (a list of Detections) on the i-th call (last one repeats)."""
    state = {"script": [[]], "i": 0}

    def detect(frame, conf_threshold=None, iou_threshold=None, camera_id=None, **_kw):
        s = state["script"]
        out = s[min(state["i"], len(s) - 1)]
        state["i"] += 1
        return out(state["i"]) if callable(out) else list(out)

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    return state


def _zone(engine):
    floor_projector.set_homography(CAM, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    engine.set_zones([{"id": "zone_a", "category": "AISLE",
                       "polygon": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}]}])


def _analyse(w, t0, seconds):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    n = int(round(seconds * FPS))
    for i in range(n):
        w._analyse(frame, now=t0 + i * DT)
    return t0 + n * DT


def test_a_figure_that_never_moved_is_never_stored(worker, scripted, fake_pose, tripwires, monkeypatch):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 0.0)
    _zone(engine)
    rng = np.random.default_rng(3)
    scripted["script"] = [lambda i: [det(rng, 1.0)]]
    _analyse(w, 30000.0, 20.0)
    t = only_track(w.tracker)
    assert t.motion_state == "pending" and t.is_human and not t.established
    assert t.current_zone_id == "zone_a" and t.open_visit_id is None
    for ft in w.tracker.flush_all():
        engine.close_track(ft, reason="track_lost", persist=True)
    visits, tracks = engine.drain()
    assert visits == [] and tracks == []


def test_shopper_standing_still_first_is_counted_once_they_move(worker, scripted, fake_pose, tripwires,
                                                                monkeypatch):
    """Waiting at the till for 8 s, then walking off: one visit with the whole dwell, one track row."""
    w, rt, engine = worker
    monkeypatch.setattr(settings, "ZONE_DWELL_MIN_SECONDS", 3.0)
    _zone(engine)
    rng = np.random.default_rng(4)
    scripted["script"] = [lambda i: [det(rng, 1.0)] if i <= 40 else [det(rng, 1.0, dx=6.0 * (i - 40))]]
    now = _analyse(w, 31000.0, 8.0)                  # 40 still frames
    t = only_track(w.tracker)
    assert t.is_human and not t.established and t.open_visit_id is None
    entered = t.zone_entered_at
    assert engine.drain() == ([], [])
    _analyse(w, now, 2.0)                             # walks
    assert t.established and t.motion_state == "moving"
    visits, _ = engine.drain()
    opened = [v for v in visits if v.phase == "open"]
    assert len(opened) == 1 and opened[0].entered_at == lae.utc_from_ts(entered)
    for ft in w.tracker.flush_all():
        engine.close_track(ft, reason="track_lost", persist=True)
    assert [x.track_id for x in engine.drain()[1]] == [t.track_id]


def test_remembered_figure_is_kept_out_of_rules_pose_and_counts(worker, scripted, fake_pose, tripwires,
                                                               monkeypatch):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "STATIC_FIGURE_SECONDS", 10.0)
    rng = np.random.default_rng(5)
    rt.put_frame(np.zeros((480, 640, 3), dtype=np.uint8))
    scripted["script"] = [lambda i: [det(rng, 2.0)] if i <= 60 else []]
    now = _analyse(w, 32000.0, 12.0)
    assert len(w.tracker.static_memory) == 1
    now = _analyse(w, now, (settings.TRACK_MAX_AGE_FRAMES + 3) * DT)   # the poster's track dies
    assert not w.tracker.tracks
    scripted["script"] = [lambda i: [det(rng, 2.0)]]
    scripted["i"] = 0
    _analyse(w, now, 2.0)                              # a new id on the poster
    t = only_track(w.tracker)
    assert t.confirmed and t.is_pending_static
    assert rt.live_track_count == 0 and tripwires[-1] == [] and fake_pose.seen[-1] == []
    (entry,) = rt.get_tracks()
    assert entry["motion_state"] == "pending" and entry["pending_static"] is True
    s = rt.to_dict()
    assert s["pending_tracks"] == 1 and s["static_tracks"] == 0 and s["static_memory_count"] == 1
    (b,) = s["boxes"]
    assert b["motion_state"] == "pending" and b["pending_static"] is True
    assert b["box"] == pytest.approx([entry["x1"] / 640, entry["y1"] / 480, entry["x2"] / 640, entry["y2"] / 480],
                                     abs=1e-3)
    assert engine.live_snapshot()["uncalibrated_track_count"] == 0


def test_ignored_detections_are_counted_and_drawn(worker, scripted, fake_pose, tripwires, monkeypatch):
    w, rt, engine = worker
    area = {"id": "ia_mask", "camera_id": CAM, "mask_mode": "AI_IGNORE", "enabled": True,
            "points": [{"x": 0.14, "y": 0.08}, {"x": 0.30, "y": 0.08}, {"x": 0.30, "y": 0.58}, {"x": 0.14, "y": 0.58}]}
    monkeypatch.setattr(lae, "camera_masks", lambda cam: [area])
    monkeypatch.setattr("app.services.privacy_mask.camera_masks", lambda cam: [area])
    rng = np.random.default_rng(6)
    rt.put_frame(np.zeros((480, 640, 3), dtype=np.uint8))
    walker = (400.0, 60.0, 480.0, 310.0)
    scripted["script"] = [lambda i: [det(rng, 1.0), det(rng, 1.0, box=walker, dx=4.0 * i)]]
    _analyse(w, 33000.0, 2.0)
    assert rt.ignored_detections_last == 1 and rt.detections_last == 1
    s = rt.to_dict()
    assert s["ignored_detections_last"] == 1 and len(s["boxes"]) == 1
    out = lae.render_overlay(np.zeros((480, 640, 3), dtype=np.uint8), rt)
    # A faint violet tint inside the area, its outline at the edge.
    inside = out[200, 150]
    assert inside[0] > 0 and inside[2] > 0 and inside[1] < inside[0]
    assert tuple(int(v) for v in out[150, int(0.30 * 640)]) == lae.IGNORE_AREA_COLOUR


def test_overlay_labels_pending_static_distinctly(worker, scripted, fake_pose, tripwires, monkeypatch):
    w, rt, engine = worker
    rt.frame_width, rt.frame_height = 640, 480
    rt.set_tracks([{"track_id": "trk_aaaa", "x1": 100.0, "y1": 50.0, "x2": 180.0, "y2": 300.0, "confidence": 0.9,
                    "hits": 9, "confirmed": True, "motion_state": "pending", "pending_static": True,
                    "age_seconds": 2.0, "keypoints": None, "x_m": None, "y_m": None, "zone_id": None}])
    out = lae.render_overlay(np.zeros((480, 640, 3), dtype=np.uint8), rt)
    assert tuple(int(v) for v in out[175, 100]) == lae.PENDING_STATIC_COLOUR


def test_worker_start_loads_and_saves_the_memory(worker, monkeypatch, tmp_path):
    w, rt, engine = worker
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path)
    path = lae.static_memory_path(CAM)
    assert path == Path(tmp_path) / "static_memory" / f"{CAM}.json"
    path.parent.mkdir(parents=True)
    now = time.time()
    path.write_text(json.dumps({"entries": [{"box": [0.15, 0.1, 0.28, 0.62], "keypoints": None, "scale": 0.16,
                                             "first_seen": now, "last_seen": now}]}))
    w._attach_static_store()
    assert rt.static_memory_count == 1 and w.tracker.static_memory == []
    w.tracker.set_frame_size(640, 480)
    assert len(w.tracker.static_memory) == 1
    monkeypatch.setattr(settings, "STATIC_MEMORY_PERSIST", False)
    w2 = CameraWorker(CameraRuntime(camera_id=CAM, name="x", source="/dev/null"), engine)
    w2._attach_static_store()
    assert w2.tracker._store_path is None


def test_live_status_route_reports_the_static_telemetry(monkeypatch):
    from app.main import app
    from app.routes import cameras as cam_routes

    class _Cam:
        id, name, is_ai_enabled, homography_matrix = "ia_live", "Live", True, None

    async def fake_get(camera_id, db):
        return _Cam()

    monkeypatch.setattr(cam_routes, "_get_camera_or_404", fake_get)
    client = TestClient(app)
    off = client.get("/api/v1/cameras/ia_live/live-status").json()
    assert off["running"] is False and off["boxes"] == []
    assert off["static_tracks"] is None and off["ignored_detections_last"] is None

    rt = CameraRuntime(camera_id="ia_live", name="Live", source="/dev/null")
    rt.frame_width, rt.frame_height = 640, 480
    rt.ignored_detections_last, rt.static_memory_count = 2, 3
    rt.set_tracks([{"track_id": "trk_s", "x1": 64.0, "y1": 48.0, "x2": 128.0, "y2": 240.0, "confidence": 0.8,
                    "hits": 9, "confirmed": True, "motion_state": "static", "pending_static": False,
                    "age_seconds": 70.0, "keypoints": None, "x_m": None, "y_m": None, "zone_id": None}])
    monkeypatch.setitem(lae.live_engine.runtimes, "ia_live", rt)
    on = client.get("/api/v1/cameras/ia_live/live-status").json()
    assert on["running"] is True
    assert (on["static_tracks"], on["pending_tracks"], on["ignored_detections_last"], on["static_memory_count"]) \
        == (1, 0, 2, 3)
    assert on["boxes"] == [{"track_id": "trk_s", "box": [0.1, 0.1, 0.2, 0.5], "motion_state": "static",
                            "confidence": 0.8, "confirmed": True, "pending_static": False}]


def test_annotated_snapshot_labels_a_static_figure_grey():
    from app.routes.cameras import snapshot_label

    d = det(box=BOX)
    static = [{"track_id": "trk_1", "x1": 100.0, "y1": 50.0, "x2": 180.0, "y2": 300.0, "motion_state": "static",
               "confirmed": True}]
    assert snapshot_label(d, static, None, (640, 480))[0] == "static figure, not counted"
    assert snapshot_label(d, [], None, (640, 480))[0].startswith("person ")
    tr = ByteTracker(CAM)
    tr.set_frame_size(640, 480)
    rng = np.random.default_rng(8)
    run(tr, 100.0, 61.0, lambda i: [det(rng, 2.0)])
    label, colour = snapshot_label(d, [], tr, (640, 480))
    assert label == "remembered static figure, not counted" and colour == lae.STATIC_COLOUR
    assert snapshot_label(d, [], tr, (1280, 960))[0].startswith("person ")   # memory is at another size


def test_tracking_logs_reach_the_edge_handler():
    # main.py attaches the journal handler to "edge" only; module-named loggers
    # would drop the "treating it as static" INFO line under uvicorn.
    from app.services import tracking_service

    assert tracking_service.logger.name.startswith("edge.")
