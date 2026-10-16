"""Store map v2: growing/shrinking the site, fit to content, outdoor areas.

The site (``store_layouts.width_m/height_m``) is the whole drawable map. Adding
space on the left or top moves the origin, so everything placed must move
with it and stay on the same physical spot -- including a calibrated
camera, whose homography must still project a pixel onto the same floor spot.
Removing space that holds an item is refused instead of silently clamping.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.database import async_session_factory
from app.main import app
from app.models.db_models import CameraModel, CustomerTrackModel, ShelfInteractionModel
from app.services.auth_service import auth_service
from app.services.store_layout_service import translate_homography

CAM_ID = "cam_store_map_v2"
IMAGE_POINTS = [{"x": 100, "y": 900}, {"x": 1800, "y": 900}, {"x": 1500, "y": 300}, {"x": 400, "y": 300}]
FLOOR_POINTS = [{"x": 2, "y": 10}, {"x": 8, "y": 10}, {"x": 8, "y": 4}, {"x": 2, "y": 4}]
PROBE = [{"x": 960, "y": 600}, {"x": 300, "y": 800}]


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _run(coro):
    return asyncio.run(coro)


async def _insert_camera(cam_id: str, x: float, y: float, placed: bool = True) -> None:
    async with async_session_factory() as db:
        existing = await db.get(CameraModel, cam_id)
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        db.add(CameraModel(
            id=cam_id, name="Map v2 cam", location="test", rtsp_url="rtsp://192.0.2.1/unreachable",
            is_ai_enabled=False, channel_number=91, floor_x=x, floor_y=y, map_placed=placed,
        ))
        await db.commit()


async def _insert_history(cam_id: str) -> None:
    async with async_session_factory() as db:
        db.add(CustomerTrackModel(id="ct_map_v2", track_id="t1", camera_id=cam_id, trajectory_points=[
            {"u": 0.5, "v": 0.5, "t": 1.0},
            {"u": 0.4, "v": 0.6, "x": 3.0, "y": 5.0, "t": 2.0},
            {"x": 3.5, "y": 5.5, "t": 3.0},
        ]))
        db.add(ShelfInteractionModel(id="si_map_v2", camera_id=cam_id, shelf_zone_id="z", person_track_id="t1",
                                     floor_x=4.0, floor_y=6.0))
        db.add(ShelfInteractionModel(id="si_map_v2_img", camera_id=cam_id, shelf_zone_id="z", person_track_id="t2",
                                     image_x=0.3, image_y=0.4))
        await db.commit()


async def _history(cam_id: str):
    async with async_session_factory() as db:
        track = await db.get(CustomerTrackModel, "ct_map_v2")
        si = await db.get(ShelfInteractionModel, "si_map_v2")
        si_img = await db.get(ShelfInteractionModel, "si_map_v2_img")
        return list(track.trajectory_points), (si.floor_x, si.floor_y), (si_img.floor_x, si_img.floor_y)


@pytest.fixture
def fresh(client, auth_headers):
    """An empty 30 x 20 m site with one zone, one room and one camera."""
    assert client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers).status_code == 200
    res = client.put("/api/v1/layout", json={"width_m": 30, "height_m": 20}, headers=auth_headers)
    assert res.status_code == 200, res.text
    zone = client.post("/api/v1/layout/zones", json={
        "name": "Aisle 1", "category": "AISLE",
        "polygon": [{"x": 1, "y": 1}, {"x": 5, "y": 1}, {"x": 5, "y": 4}, {"x": 1, "y": 4}],
    }, headers=auth_headers).json()
    room = client.post("/api/v1/layout/structures", json={
        "kind": "ROOM", "name": "Sales floor",
        "polygon": [{"x": 0, "y": 0}, {"x": 20, "y": 0}, {"x": 20, "y": 15}, {"x": 0, "y": 15}],
        "properties": {"rotation_deg": 0},
    }, headers=auth_headers).json()
    _run(_insert_camera(CAM_ID, 6.0, 7.0))
    yield {"zone": zone, "room": room}
    client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers)


def _get(client, headers):
    res = client.get("/api/v1/layout", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _by_id(items, key, value):
    return next(i for i in items if i[key] == value)


# --------------------------------------------------------------- extend


def test_extend_left_top_translates_everything_and_keeps_calibration(client, auth_headers, fresh):
    cal = client.post(f"/api/v1/layout/cameras/{CAM_ID}/calibrate", json={
        "image_points": IMAGE_POINTS, "floor_points": FLOOR_POINTS, "frame_width": 1920, "frame_height": 1080,
    }, headers=auth_headers)
    assert cal.status_code == 200, cal.text
    probe = {"image_points": PROBE, "frame_width": 1920, "frame_height": 1080}
    before = client.post(f"/api/v1/layout/cameras/{CAM_ID}/calibration/test", json=probe, headers=auth_headers).json()
    assert before["calibrated"] is True
    H_before = np.asarray(cal.json()["homography_matrix"], dtype=float)
    _run(_insert_history(CAM_ID))

    res = client.post("/api/v1/layout/extend", json={"left_m": 3, "top_m": 2, "right_m": 5, "bottom_m": 0},
                      headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    # Same shape as GET /layout, plus what changed.
    for key in ("zones", "structures", "cameras", "setup", "width_m", "height_m"):
        assert key in data
    assert data["width_m"] == 38 and data["height_m"] == 22
    assert data["site_change"]["dx"] == 3 and data["site_change"]["dy"] == 2
    assert data["site_change"]["translated"]["calibrations"] == 1

    zone = _by_id(data["zones"], "id", fresh["zone"]["id"])
    assert zone["polygon"][0] == {"x": 4, "y": 3} and zone["polygon"][2] == {"x": 8, "y": 6}
    room = _by_id(data["structures"], "id", fresh["room"]["id"])
    assert room["polygon"][2] == {"x": 23, "y": 17}
    assert room["properties"] == {"rotation_deg": 0}
    cam = _by_id(data["cameras"], "camera_id", CAM_ID)
    assert (cam["floor_x"], cam["floor_y"]) == (9, 9)
    assert cam["calibration_points"]["floor_points"][0] == {"x": 5, "y": 12}
    assert cam["calibration_points"]["image_points"] == IMAGE_POINTS   # pixels never move

    # The stored homography is T(3, 2) @ H and the running projector uses it:
    # every probed pixel lands on the same physical spot, now 3 m right, 2 m down.
    calib = client.get(f"/api/v1/layout/cameras/{CAM_ID}/calibration", headers=auth_headers).json()
    H_after = np.asarray(calib["homography_matrix"], dtype=float)
    T = np.array([[1, 0, 3], [0, 1, 2], [0, 0, 1]], dtype=float)
    assert np.allclose(H_after, T @ H_before)
    after = client.post(f"/api/v1/layout/cameras/{CAM_ID}/calibration/test", json=probe, headers=auth_headers).json()
    for b, a in zip(before["floor_points"], after["floor_points"]):
        assert a["x"] == pytest.approx(b["x"] + 3, abs=0.002)
        assert a["y"] == pytest.approx(b["y"] + 2, abs=0.002)
    # A calibration floor point still maps from its own image point.
    for img, flo in zip(IMAGE_POINTS, cam["calibration_points"]["floor_points"]):
        v = H_after @ np.array([img["x"], img["y"], 1.0])
        assert v[0] / v[2] == pytest.approx(flo["x"], abs=1e-6)
        assert v[1] / v[2] == pytest.approx(flo["y"], abs=1e-6)

    # Recorded floor positions in the same database moved too; image-space ones did not.
    traj, reach, reach_img = _run(_history(CAM_ID))
    assert traj[0] == {"u": 0.5, "v": 0.5, "t": 1.0}
    assert traj[1] == {"u": 0.4, "v": 0.6, "x": 6.0, "y": 7.0, "t": 2.0}
    assert traj[2] == {"x": 6.5, "y": 7.5, "t": 3.0}
    assert reach == (7.0, 8.0)
    assert reach_img == (None, None)


def test_extend_right_bottom_moves_nothing(client, auth_headers, fresh):
    res = client.post("/api/v1/layout/extend", json={"right_m": 10, "bottom_m": 5}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["width_m"], data["height_m"]) == (40, 25)
    assert _by_id(data["zones"], "id", fresh["zone"]["id"])["polygon"] == fresh["zone"]["polygon"]
    cam = _by_id(data["cameras"], "camera_id", CAM_ID)
    assert (cam["floor_x"], cam["floor_y"]) == (6, 7)


def test_extend_validation(client, auth_headers, fresh):
    assert client.post("/api/v1/layout/extend", json={}, headers=auth_headers).status_code == 400
    assert client.post("/api/v1/layout/extend", json={"left_m": 501}, headers=auth_headers).status_code == 422
    # Removing more than the site has.
    assert client.post("/api/v1/layout/extend", json={"right_m": -40}, headers=auth_headers).status_code == 400


def test_extend_negative_side_refuses_when_items_would_fall_outside(client, auth_headers, fresh):
    res = client.post("/api/v1/layout/extend", json={"left_m": -2}, headers=auth_headers)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "items_outside"
    ids = {i["id"] for i in detail["items"]}
    assert fresh["zone"]["id"] in ids and fresh["room"]["id"] in ids   # zone starts at x=1, room at x=0
    assert CAM_ID not in ids                                           # camera at x=6 survives
    assert _get(client, auth_headers)["width_m"] == 30                  # nothing changed

    # Empty space on the right can be removed: the room ends at x=20.
    ok = client.post("/api/v1/layout/extend", json={"right_m": -10}, headers=auth_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["width_m"] == 20


# ------------------------------------------------------------ PUT shrink


def test_put_shrink_rejects_items_outside_unless_clamped(client, auth_headers, fresh):
    res = client.put("/api/v1/layout", json={"width_m": 10}, headers=auth_headers)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "items_outside"
    assert [i["id"] for i in detail["items"]] == [fresh["room"]["id"]]
    assert _get(client, auth_headers)["width_m"] == 30

    # Growing never conflicts, and the name alone can change.
    assert client.put("/api/v1/layout", json={"width_m": 35, "name": "Main St"}, headers=auth_headers).status_code == 200

    res = client.put("/api/v1/layout", json={"width_m": 10, "clamp": True}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["width_m"] == 10 and data["name"] == "Main St"
    assert "cameras" in data and "setup" in data       # the full layout, like GET
    room = _by_id(data["structures"], "id", fresh["room"]["id"])
    assert max(p["x"] for p in room["polygon"]) == 10


# ------------------------------------------------------------------- fit


def test_fit_shrink_wraps_to_content_plus_margin(client, auth_headers, fresh):
    # Everything sits in x 0..20, y 0..15; the room touches the origin, so a
    # 2 m margin adds space on the left/top and moves everything by (2, 2).
    res = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["width_m"], data["height_m"]) == (24, 19)
    assert data["site_change"]["dx"] == 2 and data["site_change"]["dy"] == 2
    room = _by_id(data["structures"], "id", fresh["room"]["id"])
    assert room["polygon"][0] == {"x": 2, "y": 2}
    cam = _by_id(data["cameras"], "camera_id", CAM_ID)
    assert (cam["floor_x"], cam["floor_y"]) == (8, 9)

    # Fitting again changes nothing; the default margin is 2 m and the body is optional.
    again = client.post("/api/v1/layout/fit", headers=auth_headers)
    assert again.status_code == 200, again.text
    assert (again.json()["width_m"], again.json()["height_m"]) == (24, 19)
    assert again.json()["site_change"].get("unchanged") is True


def test_unplaced_camera_does_not_pin_fit_or_block_shrink(client, auth_headers, fresh):
    # A camera never put on the map sits at the default 0, 0. It must not keep
    # "Fit to content" at the origin nor be listed as cut off by a shrink; it
    # is kept inside the site instead.
    room = fresh["room"]
    client.put(f"/api/v1/layout/structures/{room['id']}", json={
        "polygon": [{"x": 10, "y": 5}, {"x": 20, "y": 5}, {"x": 20, "y": 15}, {"x": 10, "y": 15}]},
        headers=auth_headers)
    client.delete(f"/api/v1/layout/zones/{fresh['zone']['id']}", headers=auth_headers)
    _run(_insert_camera(CAM_ID, 0.0, 0.0, placed=False))
    res = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["width_m"], data["height_m"]) == (14, 14)
    assert data["site_change"]["dx"] == -8 and data["site_change"]["dy"] == -3
    cam = _by_id(data["cameras"], "camera_id", CAM_ID)
    assert cam["map_placed"] is False
    assert 0 <= cam["floor_x"] <= 14 and 0 <= cam["floor_y"] <= 14
    # Removing empty space on the left is not blocked by the unplaced camera.
    ok = client.post("/api/v1/layout/extend", json={"left_m": -1}, headers=auth_headers)
    assert ok.status_code == 200, ok.text
    cam = _by_id(ok.json()["cameras"], "camera_id", CAM_ID)
    assert cam["floor_x"] == 0


def test_fit_on_an_empty_map_is_refused(client, auth_headers):
    assert client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers).status_code == 200
    res = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert res.status_code == 400


# --------------------------------------------------------- outdoor areas


def test_outdoor_kind_and_area_type(client, auth_headers, fresh):
    data = _get(client, auth_headers)
    assert "OUTDOOR" in data["structure_kinds"]
    res = client.post("/api/v1/layout/structures", json={
        "kind": "outdoor", "name": "Car park",
        "polygon": [{"x": 22, "y": 0}, {"x": 30, "y": 0}, {"x": 30, "y": 20}, {"x": 22, "y": 20}],
        "properties": {"area_type": "Car park"},
    }, headers=auth_headers)
    assert res.status_code == 201, res.text
    st = res.json()
    assert st["kind"] == "OUTDOOR" and st["color"] == "#6b8f71"
    assert st["area_m2"] == 160 and st["length_m"] is None
    assert st["properties"]["area_type"] == "Car park"
    # A camera on the car park is outside the building but inside the site.
    placed = client.patch(f"/api/v1/layout/cameras/{CAM_ID}/placement", json={"floor_x": 26, "floor_y": 10},
                          headers=auth_headers)
    assert placed.status_code == 200 and placed.json()["floor_x"] == 26
    cam = _by_id(_get(client, auth_headers)["cameras"], "camera_id", CAM_ID)
    assert cam["map_placed"] is True


def test_rotated_polygon_round_trip_keeps_rotation_hint(client, auth_headers, fresh):
    # Rotation is stored as rotated vertices plus a properties.rotation_deg hint.
    pts = [{"x": 10.0, "y": 5.0}, {"x": 12.598, "y": 6.5}, {"x": 11.598, "y": 8.232}, {"x": 9.0, "y": 6.732}]
    res = client.put(f"/api/v1/layout/structures/{fresh['room']['id']}",
                     json={"polygon": pts, "properties": {"rotation_deg": 30}}, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert res.json()["polygon"] == pts and res.json()["properties"]["rotation_deg"] == 30


def test_translate_homography_helper():
    H = [[0.01, 0.002, -1.0], [0.0005, 0.02, -3.0], [0.00001, 0.0003, 1.0]]
    moved = translate_homography(H, 4.0, -1.5)
    T = np.array([[1, 0, 4.0], [0, 1, -1.5], [0, 0, 1]])
    assert np.allclose(np.asarray(moved), T @ np.asarray(H))
    assert translate_homography([[1, 2], [3, 4]], 1, 1) is None
    assert translate_homography("nope", 1, 1) is None
