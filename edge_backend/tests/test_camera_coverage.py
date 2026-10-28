"""Camera coverage on the store map (2026-10-28).

* reach (``cameras.range_m``, m0022) is per camera, validated and defaulted
  from the lens angle instead of one fixed length;
* the calibrated footprint is the floor the camera picture really shows
  (homography), with near / far limits;
* coverage is cut by walls, room edges and shelves (doors and outdoor areas
  do not block), with areas, store coverage %, gaps and overlap;
* cameras <-> zones / shelf fronts they see, and shelf reaches along the
  camera's line of sight;
* space metrics (per m^2, per metre of shelf, walking distance) are null when
  nothing was observed;
* the low-traffic insight compares visits per m^2 and skips zones the
  calibrated cameras hardly see.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.database import async_session_factory
from app.main import app
from app.models.db_models import CameraModel, CustomerTrackModel, POSTransactionModel, ShelfInteractionModel, ZoneVisitModel
from app.services import coverage_geometry as cg
from app.services.auth_service import auth_service
from app.services.business_analysis_service import _detect
from app.services.retail_metrics_service import ZoneMetrics, path_length_m, retail_metrics_service

CAM = "cam_cov_a"
CAM_B = "cam_cov_b"


def rect(x0, y0, x1, y1):
    return [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ synthetic camera

def pinhole_homography(cx, cy, height, bearing_deg, pitch_deg, f=1000.0, w=1920, h=1080):
    """Image -> floor homography of an ideal camera hanging at (cx, cy, height) m.

    Map frame x right, y down, z up; the camera looks along the map bearing
    (0 = -y), tilted down by ``pitch_deg``.
    """
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    b = math.radians(bearing_deg)
    fwd_h = np.array([math.sin(b), -math.cos(b), 0.0])
    p = math.radians(pitch_deg)
    forward = fwd_h * math.cos(p) + np.array([0, 0, -1.0]) * math.sin(p)
    # Image x to the right of the view, image y downwards.
    right = np.cross(forward, np.array([0, 0, 1.0]))
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    R = np.vstack([right, down, forward])           # world -> camera
    C = np.array([cx, cy, height])
    t = -R @ C
    P = K @ np.column_stack([R[:, 0], R[:, 1], t])  # floor (X, Y, 1) -> image
    Hm = np.linalg.inv(P)
    return (Hm / Hm[2, 2]).tolist(), P


def image_of(P, x, y):
    u, v, s = P @ np.array([x, y, 1.0])
    return {"x": float(u / s), "y": float(v / s)}


# ---------------------------------------------------------------- reach / range

def test_default_range_follows_the_lens_angle():
    assert cg.default_range_m(85) == 10.0
    assert cg.default_range_m(None) == 10.0
    assert cg.default_range_m(60) > 10.0 > cg.default_range_m(110)
    assert cg.default_range_m(5) == 30.0 and cg.default_range_m(170) == 3.0
    assert cg.default_range_m(360) == 6.0


def test_range_validation():
    assert cg.validate_range_m(None) is None
    assert cg.validate_range_m(12.345) == 12.35
    for bad in (0.1, 0, -3, 150, float("nan")):
        with pytest.raises(ValueError):
            cg.validate_range_m(bad)
    assert cg.validate_coverage_mode("auto") is None
    assert cg.validate_coverage_mode("Manual") == "manual"
    with pytest.raises(ValueError):
        cg.validate_coverage_mode("guess")


def test_m0022_adds_the_columns_once_and_keeps_rows(tmp_path):
    from app.migrations import m0022_camera_coverage as m

    db = tmp_path / "old.db"
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute("CREATE TABLE cameras (id VARCHAR PRIMARY KEY, name VARCHAR, fov_deg FLOAT)")
    c.execute("INSERT INTO cameras VALUES ('c1', 'Aisle', 90.0)")
    m.upgrade(c)
    m.upgrade(c)   # idempotent
    cols = {r[1]: r[2] for r in c.execute("PRAGMA table_info(cameras)")}
    assert cols["range_m"] == "FLOAT" and cols["coverage_mode"] == "VARCHAR(16)"
    assert c.execute("SELECT name, fov_deg, range_m, coverage_mode FROM cameras").fetchone() == ("Aisle", 90.0, None, None)
    # A database without a cameras table is left alone.
    e = sqlite3.connect(str(tmp_path / "empty.db"), isolation_level=None)
    m.upgrade(e)
    assert e.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_migration_runner_backs_up_before_m0022(tmp_path):
    from app.migrations import run_migrations

    db = tmp_path / "data.db"
    run_migrations(db, backups_dir=tmp_path / "b0")
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute("DELETE FROM schema_migrations WHERE version = 22")
    c.execute("INSERT INTO system_setup (key, value, updated_at) VALUES ('x', 'y', '2026-10-28')")
    c.close()
    report = run_migrations(db, backups_dir=tmp_path / "b1")
    assert [a for a in report["applied"] if "0022" in str(a)]
    assert report["backup"] and "pre-v0022" in report["backup"]


# ------------------------------------------------------------- polygon basics

def test_polygon_area_and_convex_clip():
    sq = [(0, 0), (4, 0), (4, 4), (0, 4)]
    assert cg.polygon_area(sq) == 16
    other = [(2, 2), (6, 2), (6, 6), (2, 6)]
    assert abs(cg.polygon_area(cg.clip_polygon_convex(sq, other)) - 4.0) < 1e-9
    assert cg.clip_polygon_convex(sq, [(10, 10), (11, 10), (11, 11)]) == []
    # A concave subject (L shape) against a convex clip keeps the exact area.
    L = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)]
    assert abs(cg.polygon_area(cg.clip_polygon_convex(L, [(0, 0), (2, 0), (2, 2), (0, 2)])) - 3.0) < 1e-9
    assert cg.oriented_box([(0, 0), (12, 0), (12, 1.2), (0, 1.2)])[:2] == pytest.approx((12, 1.2))


def test_unblocked_cone_area_is_the_sector():
    v = cg.camera_view({"camera_id": "c", "floor_x": 50, "floor_y": 50, "azimuth_deg": 30, "fov_deg": 90, "range_m": 8}, [])
    assert v.mode == "manual" and v.range_m == 8 and not v.range_is_default
    assert v.area_m2 == pytest.approx(math.pi * 64 / 4, rel=0.01)
    # No reach stored: the lens default.
    d = cg.camera_view({"camera_id": "c", "floor_x": 50, "floor_y": 50, "azimuth_deg": 0, "fov_deg": 85}, [])
    assert d.range_is_default and d.range_m == 10.0
    # Bearing 90 = right: every point of the cone is right of the camera.
    r = cg.camera_view({"camera_id": "c", "floor_x": 50, "floor_y": 50, "azimuth_deg": 90, "fov_deg": 60, "range_m": 5}, [])
    assert all(x >= 50 - 1e-9 for x, _ in r.polygon)


# ----------------------------------------------------------------- occlusion

ROOM = {"id": "room", "kind": "ROOM", "polygon": rect(0, 0, 20, 20)}


def test_wall_blocks_and_a_door_lets_the_camera_see_through():
    cam = {"camera_id": "c", "floor_x": 10, "floor_y": 15, "azimuth_deg": 0, "fov_deg": 60, "range_m": 12}
    open_area = cg.camera_view(cam, cg.blocking_segments([ROOM])).area_m2
    wall = {"id": "w", "kind": "WALL", "polygon": [{"x": 0, "y": 10}, {"x": 20, "y": 10}], "thickness_m": 0.2}
    walled = cg.camera_view(cam, cg.blocking_segments([ROOM, wall]))
    assert walled.area_m2 < open_area * 0.6
    assert all(y >= 10 - 1e-6 for _, y in walled.polygon)            # nothing beyond the wall
    door = {"id": "d", "kind": "DOOR", "polygon": [{"x": 9, "y": 10}, {"x": 11, "y": 10}], "thickness_m": 0.1}
    with_door = cg.camera_view(cam, cg.blocking_segments([ROOM, wall, door]))
    assert walled.area_m2 < with_door.area_m2 < open_area
    assert any(y < 9 for _, y in with_door.polygon)                  # it sees through the doorway


def test_shelves_block_low_counters_outdoor_and_overrides_do_not():
    cam = {"camera_id": "c", "floor_x": 10, "floor_y": 18, "azimuth_deg": 0, "fov_deg": 40, "range_m": 15}
    base = cg.camera_view(cam, cg.blocking_segments([ROOM])).area_m2
    shelf = {"id": "s", "kind": "SHELF", "polygon": rect(8, 10, 12, 11.2)}
    assert cg.camera_view(cam, cg.blocking_segments([ROOM, shelf])).area_m2 < base * 0.9
    low = dict(shelf, properties={"height_m": 0.9})
    assert cg.camera_view(cam, cg.blocking_segments([ROOM, low])).area_m2 == pytest.approx(base, rel=1e-3)
    counter = dict(shelf, id="k", kind="COUNTER")
    assert cg.camera_view(cam, cg.blocking_segments([ROOM, counter])).area_m2 == pytest.approx(base, rel=1e-3)
    tall_counter = dict(counter, properties={"blocks_view": True})
    assert cg.camera_view(cam, cg.blocking_segments([ROOM, tall_counter])).area_m2 < base * 0.9
    outdoor = {"id": "o", "kind": "OUTDOOR", "polygon": rect(5, 5, 15, 12)}
    assert cg.camera_view(cam, cg.blocking_segments([ROOM, outdoor])).area_m2 == pytest.approx(base, rel=1e-3)


def test_a_camera_on_the_room_edge_still_sees_into_the_room():
    cam = {"camera_id": "c", "floor_x": 10, "floor_y": 20.05, "azimuth_deg": 0, "fov_deg": 90, "range_m": 5}
    v = cg.camera_view(cam, cg.blocking_segments([ROOM]))
    assert v.area_m2 == pytest.approx(math.pi * 25 / 4, rel=0.03)


# ------------------------------------------------------- calibrated footprint

def test_homography_footprint_near_far_limits():
    H, P = pinhole_homography(10, 18, 3.0, bearing_deg=0, pitch_deg=30)
    floor_pts = [(6, 14), (14, 14), (13, 10), (7, 10)]
    img_pts = [image_of(P, x, y) for x, y in floor_pts]
    fp = cg.homography_footprint(H, 1920, 1080, img_pts, (10, 18))
    assert fp is not None
    poly = cg._pts(fp["polygon"])
    assert all(y < 18 for _, y in poly)                                 # in front of the camera
    # Near limit: the bottom image row meets the floor at 3 m / tan(pitch + half the vertical view).
    near = 3.0 / math.tan(math.radians(30) + math.atan(540 / 1000))
    assert fp["near_m"] == pytest.approx(near, abs=0.15)
    # Far limit: no point deeper (along the camera's view axis) than three times the
    # farthest calibration point, and within FOOTPRINT_MAX_M.
    assert 8 < fp["far_m"] <= cg.FOOTPRINT_MAX_M
    axis = np.array([0.0, -math.cos(math.radians(30)), -math.sin(math.radians(30))])
    depth = lambda x, y: float(axis @ np.array([x - 10, y - 18, -3.0]))  # noqa: E731
    depth_far = max(depth(x, y) for x, y in floor_pts)
    assert max(depth(x, y) for x, y in poly) <= cg.FOOTPRINT_DEPTH_FACTOR * depth_far + 1e-6
    # The calibration points themselves are inside the footprint.
    assert all(cg.point_in_polygon(x, y, poly) for x, y in floor_pts)


def test_unusable_calibration_gives_no_footprint():
    H, P = pinhole_homography(10, 18, 3.0, 0, 30)
    assert cg.homography_footprint(H, None, None) is None              # frame size unknown
    assert cg.homography_footprint([[1, 0], [0, 1]], 1920, 1080) is None
    assert cg.homography_footprint([[0] * 3] * 3, 1920, 1080) is None


def test_calibrated_coverage_is_clipped_by_a_wall_and_manual_overrides():
    H, P = pinhole_homography(10, 18, 3.0, 0, 30)
    img = [image_of(P, x, y) for x, y in [(6, 14), (14, 14), (13, 10), (7, 10)]]
    cam = {"camera_id": "c", "floor_x": 10, "floor_y": 18, "azimuth_deg": 0, "fov_deg": 85, "map_placed": True,
           "homography_matrix": H, "has_homography": True,
           "calibration_points": {"image_points": img, "frame_width": 1920, "frame_height": 1080}}
    free = cg.camera_view(cam, cg.blocking_segments([ROOM]))
    assert free.mode == "calibrated" and free.footprint_available
    wall = {"id": "w", "kind": "WALL", "polygon": [{"x": 0, "y": 12}, {"x": 20, "y": 12}]}
    walled = cg.camera_view(cam, cg.blocking_segments([ROOM, wall]))
    assert walled.area_m2 < free.area_m2 and all(y >= 12 - 1e-6 for _, y in walled.polygon)
    manual = cg.camera_view(dict(cam, coverage_mode="manual", range_m=4), cg.blocking_segments([ROOM]))
    assert manual.mode == "manual" and manual.area_m2 == pytest.approx(math.pi * 16 * 85 / 360, rel=0.02)


# --------------------------------------------- store numbers and shelf fronts

def _store():
    shelf = {"id": "shelf1", "kind": "SHELF", "name": "Bread", "polygon": rect(4, 8, 16, 9.2)}
    zones = [
        {"id": "z_front", "name": "Front aisle", "category": "AISLE", "polygon": rect(0, 10, 20, 20)},
        {"id": "z_back", "name": "Back aisle", "category": "AISLE", "polygon": rect(0, 0, 20, 8)},
    ]
    return [ROOM, shelf], zones


def test_store_coverage_zones_shelves_gaps_and_overlap():
    structures, zones = _store()
    cams = [
        {"camera_id": "A", "name": "Front", "floor_x": 10, "floor_y": 19.5, "azimuth_deg": 0, "fov_deg": 120, "range_m": 12},
        {"camera_id": "B", "name": "Front 2", "floor_x": 2, "floor_y": 19.5, "azimuth_deg": 30, "fov_deg": 60, "range_m": 8},
        {"camera_id": "OFF", "name": "Off", "floor_x": 10, "floor_y": 1, "azimuth_deg": 180, "fov_deg": 120, "range_m": 10,
         "is_ai_enabled": False},
    ]
    r = cg.analyse({"width_m": 20, "height_m": 20}, structures, zones, cams)
    store = r["store"]
    assert store["floor_source"] == "rooms+zones"
    assert store["floor_m2"] == pytest.approx(400 - 12 * 1.2, abs=1.0)
    assert 0 < store["coverage_pct"] < 100 and store["cameras_counted"] == 2
    assert store["overlap_m2"] > 0
    # The back aisle is behind the shelf and the switched-off camera does not count: a gap.
    assert store["gaps"] and store["gaps"][0]["zone_id"] == "z_back"
    by_zone = {z["zone_id"]: z for z in r["zones"]}
    assert by_zone["z_front"]["covered_fraction"] > 0.5
    assert by_zone["z_back"]["covered_fraction"] < 0.25
    assert [c["camera_id"] for c in by_zone["z_front"]["cameras"]][0] == "A"
    cam_a = next(c for c in r["cameras"] if c["camera_id"] == "A")
    assert cam_a["zones"][0]["zone_id"] == "z_front"
    assert cam_a["overlaps"] and cam_a["overlaps"][0]["camera_id"] == "B"
    # Shelf: 12 m long, two 12 m fronts; camera A sees the front facing it (about 12 m), never the back.
    sh = r["shelves"][0]
    assert sh["length_m"] == pytest.approx(12) and sh["front_m"] == pytest.approx(24)
    a_sees = next(s for s in cam_a["shelves"] if s["structure_id"] == "shelf1")["seen_front_m"]
    assert 8 <= a_sees <= 12.01
    assert sh["seen_front_m"] <= 12.01
    off = next(c for c in r["cameras"] if c["camera_id"] == "OFF")
    assert off["counted"] is False and off["overlaps"] == []
    # Grid area agrees with the exact polygon area.
    assert cam_a["floor_seen_m2"] == pytest.approx(cam_a["area_m2"], rel=0.08)


def test_floor_falls_back_to_zones_then_site():
    zones = [{"id": "z", "name": "Z", "category": "AISLE", "polygon": rect(0, 0, 5, 4)},
             {"id": "s", "name": "Stock", "category": "STOCKROOM", "polygon": rect(5, 0, 10, 4)}]
    r = cg.analyse({"width_m": 10, "height_m": 10}, [], zones, [])
    assert r["store"]["floor_source"] == "zones" and r["store"]["floor_m2"] == pytest.approx(20, abs=0.5)
    assert r["store"]["coverage_pct"] == 0.0
    assert cg.analyse({"width_m": 10, "height_m": 10}, [], [], [])["store"]["floor_source"] == "site"


def test_sales_floor_area_rooms_and_zones_minus_stockroom():
    area, src = cg.sales_floor_area(30, 20, [{"kind": "ROOM", "polygon": rect(0, 0, 20, 10)}],
                                    [{"category": "STOCKROOM", "polygon": rect(15, 0, 20, 10)}])
    assert src == "rooms" and area == pytest.approx(150, abs=1)
    # A store that drew only some rooms (cool room) and zones for the sales floor: both count.
    area, src = cg.sales_floor_area(30, 20, [{"kind": "ROOM", "polygon": rect(0, 0, 5, 5)}],
                                    [{"category": "AISLE", "polygon": rect(10, 0, 20, 10)}])
    assert src == "rooms+zones" and area == pytest.approx(125, abs=1)
    assert cg.sales_floor_area(30, 20, [], []) == (None, None)


def test_shelf_on_sight_line_finds_the_face_the_camera_sees():
    shelves = [("near", cg._pts(rect(4, 8, 16, 9.2))), ("far", cg._pts(rect(4, 3, 16, 4.2)))]
    # Camera at (10, 18) 3 m up; a wrist 1.4 m up at true floor (10, 9.0) projects to
    # C + (P - C) * H / (H - h): beyond both shelves.
    h, Hc = 1.4, 3.0
    px, py = 10, 18 + (9.0 - 18) * Hc / (Hc - h)
    assert cg.shelf_on_sight_line((10, 18), Hc, (px, py), shelves) == "near"
    assert cg.shelf_on_sight_line((10, 18), Hc, (px, py), shelves[1:]) == "far"
    assert cg.shelf_on_sight_line((10, 18), 0.3, (px, py), shelves) is None   # too low a camera
    assert cg.shelf_on_sight_line((10, 18), Hc, (1, 17), shelves) is None    # hand in the open


def test_pose_floor_fallback_uses_the_sight_line(monkeypatch):
    from app.services import tracking_service
    from app.services.pose_analytics import pose_analytics

    zone = {"id": "z_shelf", "name": "Bread shelf", "category": "SHELF", "polygon": rect(4, 8, 16, 9.2)}
    pose_analytics.set_floor_zones([zone])
    cam = pose_analytics._camera("cam_sight")
    cam.frame_size = (1920, 1080)
    cam.zones = []
    projected = (10.0, 18 + (9.0 - 18) * 3.0 / 1.6)
    monkeypatch.setattr(tracking_service.floor_projector, "to_floor", lambda *a, **k: projected)
    try:
        assert pose_analytics._zone_at_points(cam, [("wrist", 900.0, 400.0)], None) is None   # old behaviour: miss
        pose_analytics.set_camera_geometry("cam_sight", 10, 18, 3.0, placed=True)
        hit = pose_analytics._zone_at_points(cam, [("wrist", 900.0, 400.0)], None)
        assert hit is not None and hit[0].id == "z_shelf" and hit[0].space == "floor"
        pose_analytics.set_camera_geometry("cam_sight", 10, 18, 3.0, placed=False)
        assert pose_analytics._zone_at_points(cam, [("wrist", 900.0, 400.0)], None) is None
    finally:
        pose_analytics.set_floor_zones(None)
        pose_analytics.set_camera_geometry("cam_sight", None, None, None)


# ---------------------------------------------------------------- metrics

def test_zone_space_metrics_are_null_until_observed():
    z = ZoneMetrics(zone_id="z", name="Bread", category="SHELF", area_m2=12.0, shelf_length_m=6.0)
    d = z.to_dict()
    for k in ("visits_per_m2", "dwell_seconds_per_m2", "people_per_m2_now", "reaches", "reaches_per_shelf_m"):
        assert d[k] is None, k
    assert d["area_m2"] == 12.0 and d["shelf_length_m"] == 6.0
    z.visits, z.total_dwell_seconds, z.occupancy_now, z.reaches = 24, 600.0, 3, 18
    d = z.to_dict()
    assert d["visits_per_m2"] == 2.0 and d["dwell_seconds_per_m2"] == 50.0
    assert d["people_per_m2_now"] == 0.25 and d["reaches_per_shelf_m"] == 3.0
    assert ZoneMetrics(zone_id="y", name="No area", category="AISLE", visits=5).to_dict()["visits_per_m2"] is None


def test_path_length_skips_jumps_and_image_points():
    pts = [{"u": 0.1, "v": 0.2, "t": 0}, {"x": 0, "y": 0, "t": 1}, {"x": 3, "y": 4, "t": 3},
           {"x": 3, "y": 4.5, "t": 3.5}, {"x": 30, "y": 4.5, "t": 4}, {"x": 30, "y": 5.5, "t": 5}]
    # 5 m in 2 s and 0.5 m are walking; 27 m in 0.5 s is a jump; then 1 m.
    assert path_length_m(pts) == pytest.approx(5 + 0.5 + 1.0)
    # Without timestamps a step longer than 4 m is a jump.
    assert path_length_m([{"x": 0, "y": 0}, {"x": 3, "y": 0}, {"x": 9, "y": 0}]) == 3.0
    assert path_length_m([{"x": 1, "y": 1}]) is None
    assert path_length_m([]) is None


# ------------------------------------------------------------ insights

def _z(zid, area, visits, cat="AISLE"):
    return ZoneMetrics(zone_id=zid, name=zid, category=cat, visits=visits, area_m2=area)


def test_low_traffic_compares_visits_per_square_metre():
    zones = [_z("Big", 100, 100), _z("Mid", 50, 60), _z("Small", 4, 12), _z("Hall", 400, 60)]
    found = {f.zone: f for f in _detect(zones, {}) if f.category == "STORE_LAYOUT"}
    # Small has few visits but many per m^2: not dead. Hall has many visits spread thin: dead.
    assert "Small" not in found and "Hall" in found
    ev = found["Hall"].evidence
    assert ev["basis"] == "per_m2" and ev["visits_per_m2"] == pytest.approx(0.15)
    assert ev["store_avg_visits_per_m2"] > ev["visits_per_m2"] and "per m²" in found["Hall"].finding


def test_low_traffic_without_areas_keeps_the_count_rule():
    zones = [_z(f"Busy {i}", None, 100) for i in range(3)] + [_z("Quiet", None, 5)]
    f = [f for f in _detect(zones, {}) if f.category == "STORE_LAYOUT"]
    assert [x.zone for x in f] == ["Quiet"] and f[0].evidence["basis"] == "visits"


def test_low_traffic_skips_zones_the_cameras_barely_see():
    zones = [_z("Big", 100, 100), _z("Mid", 50, 60), _z("Hall", 400, 60)]
    suppressed: list = []
    f = _detect(zones, {}, {"Hall": 0.3, "Big": 1.0, "Mid": None}, suppressed)
    assert not [x for x in f if x.zone == "Hall"]
    assert suppressed and "Hall" in suppressed[0] and "30%" in suppressed[0]
    f2 = _detect(zones, {}, {"Hall": 0.8})
    hall = next(x for x in f2 if x.zone == "Hall")
    assert hall.evidence["camera_coverage_pct"] == 80.0


# ------------------------------------------------------------ API

@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


async def _camera(cam_id, x, y, **kw):
    async with async_session_factory() as db:
        old = await db.get(CameraModel, cam_id)
        if old is not None:
            await db.delete(old)
            await db.commit()
        db.add(CameraModel(id=cam_id, name=cam_id, location="test", rtsp_url="rtsp://192.0.2.1/x",
                           is_ai_enabled=True, channel_number=97, floor_x=x, floor_y=y, map_placed=True, **kw))
        await db.commit()


@pytest.fixture
def store(client, auth_headers):
    assert client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers).status_code == 200
    assert client.put("/api/v1/layout", json={"width_m": 20, "height_m": 20}, headers=auth_headers).status_code == 200
    client.post("/api/v1/layout/structures", json={"kind": "ROOM", "name": "Shop", "polygon": rect(0, 0, 20, 20)},
                headers=auth_headers)
    shelf = client.post("/api/v1/layout/structures", json={"kind": "SHELF", "name": "Bread", "polygon": rect(4, 8, 16, 9.2)},
                        headers=auth_headers).json()
    front = client.post("/api/v1/layout/zones", json={"name": "Front aisle", "category": "AISLE", "polygon": rect(0, 10, 20, 20)},
                        headers=auth_headers).json()
    back = client.post("/api/v1/layout/zones", json={"name": "Back aisle", "category": "AISLE", "polygon": rect(0, 0, 20, 8)},
                       headers=auth_headers).json()
    H, P = pinhole_homography(10, 19.5, 3.0, 0, 35)
    img = [image_of(P, x, y) for x, y in [(6, 16), (14, 16), (13, 11), (7, 11)]]
    _run(_camera(CAM, 10, 19.5, azimuth_deg=0, fov_deg=85, floor_z=3.0, homography_matrix=H,
                 calibration_points={"image_points": img, "floor_points": [], "frame_width": 1920, "frame_height": 1080}))
    _run(_camera(CAM_B, 18, 1, azimuth_deg=200, fov_deg=60))
    yield {"shelf": shelf, "front": front, "back": back}
    async def _cleanup():
        async with async_session_factory() as db:
            for cid in (CAM, CAM_B):
                c = await db.get(CameraModel, cid)
                if c is not None:
                    await db.delete(c)
            await db.commit()
    _run(_cleanup())
    # Later test modules expect the fresh default site, not this 20 x 20 m store.
    assert client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers).status_code == 200


def test_placement_saves_validates_and_resets_the_reach(client, auth_headers, store):
    url = f"/api/v1/layout/cameras/{CAM_B}/placement"
    r = client.patch(url, json={"floor_x": 18, "floor_y": 1, "range_m": 14}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["range_m"] == 14 and r.json()["range_default_m"] == pytest.approx(cg.default_range_m(60))
    cam = next(c for c in client.get("/api/v1/layout", headers=auth_headers).json()["cameras"] if c["camera_id"] == CAM_B)
    assert cam["range_m"] == 14 and cam["coverage_mode"] == "auto" and cam["calibrated_footprint"] is None
    # Left out: unchanged. null: back to the default.
    assert client.patch(url, json={"floor_x": 18, "floor_y": 1, "azimuth_deg": 210}, headers=auth_headers).json()["range_m"] == 14
    assert client.patch(url, json={"floor_x": 18, "floor_y": 1, "range_m": None}, headers=auth_headers).json()["range_m"] is None
    for bad in ({"range_m": 0.1}, {"range_m": 101}, {"coverage_mode": "guess"}):
        assert client.patch(url, json={"floor_x": 18, "floor_y": 1, **bad}, headers=auth_headers).status_code == 422
    r = client.patch(url, json={"floor_x": 18, "floor_y": 1, "coverage_mode": "manual"}, headers=auth_headers)
    assert r.json()["coverage_mode"] == "manual"
    assert client.patch(url, json={"floor_x": 18, "floor_y": 1, "coverage_mode": "auto"}, headers=auth_headers).json()["coverage_mode"] == "auto"


def test_layout_serves_the_calibrated_footprint(client, auth_headers, store):
    cam = next(c for c in client.get("/api/v1/layout", headers=auth_headers).json()["cameras"] if c["camera_id"] == CAM)
    fp = cam["calibrated_footprint"]
    assert fp and len(fp["polygon"]) >= 3 and 0 < fp["near_m"] < fp["far_m"]
    assert cam["range_default_m"] == 10.0


def test_coverage_endpoint(client, auth_headers, store):
    r = client.get("/api/v1/layout/coverage", headers=auth_headers)
    assert r.status_code == 200, r.text
    rep = r.json()
    a = next(c for c in rep["cameras"] if c["camera_id"] == CAM)
    assert a["mode"] == "calibrated" and a["near_m"] is not None and a["area_m2"] > 0
    assert a["zones"][0]["zone_id"] == store["front"]["id"]
    assert any(s["structure_id"] == store["shelf"]["id"] for s in a["shelves"])
    zones = {z["zone_id"]: z for z in rep["zones"]}
    assert zones[store["front"]["id"]]["calibrated_covered_fraction"] > 0.2
    # The back aisle is mostly behind the shelf for the calibrated camera (it sees past the ends).
    assert zones[store["back"]["id"]]["calibrated_covered_fraction"] < 0.25
    assert rep["store"]["coverage_pct"] is not None and rep["store"]["gaps"]


def test_insight_coverage_map_from_the_store(client, auth_headers, store):
    from app.services.business_analysis_service import business_analysis_service

    async def go():
        async with async_session_factory() as db:
            return await business_analysis_service._zone_coverage(db)
    cov = _run(go())
    assert cov is not None
    assert cov[store["back"]["id"]] < 0.25 < cov[store["front"]["id"]]


def test_space_metrics_are_null_without_observations_and_real_with_them(client, auth_headers, store):
    r = client.get("/api/v1/analytics/space", headers=auth_headers)
    assert r.status_code == 200, r.text
    sp = r.json()
    assert sp["sales_floor_source"] == "rooms+zones" and sp["sales_floor_m2"] == pytest.approx(400, abs=2)
    assert sp["sales_per_m2"] is None and sp["revenue"] is None
    assert sp["walking"]["avg_path_m"] is None and sp["walking"]["paths_measured"] == 0
    zone = next(z for z in sp["zones"] if z["zone_id"] == store["front"]["id"])
    assert zone["area_m2"] == 200 and zone["visits_per_m2"] is None

    now = datetime.utcnow()
    async def seed():
        async with async_session_factory() as db:
            db.add(POSTransactionModel(id="pos_cov_1", transaction_id="txcov1", timestamp=now - timedelta(seconds=20),
                                       register_id="R1", sku_id="SKU1", amount=800.0))
            for i in range(4):
                db.add(ZoneVisitModel(id=f"zv_cov_{i}", zone_id=store["front"]["id"], camera_id=CAM, track_id=f"t{i}",
                                      entered_at=now - timedelta(seconds=100), exited_at=now - timedelta(seconds=40),
                                      dwell_seconds=60.0))
            db.add(CustomerTrackModel(id="ct_cov", track_id="tcov", camera_id=CAM, start_time=now - timedelta(seconds=90),
                                      end_time=now - timedelta(seconds=30), hits=50,
                                      trajectory_points=[{"x": 1, "y": 12, "t": 1.0}, {"x": 4, "y": 16, "t": 4.0}]))
            await db.commit()
    async def unseed():
        async with async_session_factory() as db:
            for model, ids in ((POSTransactionModel, ["pos_cov_1"]), (ZoneVisitModel, [f"zv_cov_{i}" for i in range(4)]),
                               (CustomerTrackModel, ["ct_cov"])):
                for i in ids:
                    row = await db.get(model, i)
                    if row is not None:
                        await db.delete(row)
            await db.commit()
    try:
        _run(seed())
        sp = client.get("/api/v1/analytics/space", headers=auth_headers).json()
        assert sp["revenue"] == 800.0 and sp["sales_per_m2"] == pytest.approx(800 / sp["sales_floor_m2"], rel=1e-3)
        assert sp["walking"]["paths_measured"] == 1 and sp["walking"]["avg_path_m"] == 5.0
        zone = next(z for z in sp["zones"] if z["zone_id"] == store["front"]["id"])
        assert zone["visits"] == 4 and zone["visits_per_m2"] == pytest.approx(4 / 200, abs=1e-3)
        assert zone["dwell_seconds_per_m2"] == pytest.approx(240 / 200, abs=0.05)
    finally:
        _run(unseed())
