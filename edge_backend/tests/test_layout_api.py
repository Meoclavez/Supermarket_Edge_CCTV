"""API tests for the blueprint editor additions: structures, live map, calibration, reset.

The invariant under test throughout is that nothing is invented. A camera
without a homography has no floor position, an empty store has an empty
plan, and a reset returns the install to exactly that empty plan.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import CameraModel
from app.services.auth_service import auth_service

SQUARE = [{"x": 1, "y": 1}, {"x": 6, "y": 1}, {"x": 6, "y": 5}, {"x": 1, "y": 5}]


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token(
        {"sub": "test_admin", "role": "admin", "type": "user_session"}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _run(coro):
    return asyncio.run(coro)


async def _insert_camera(cam_id: str) -> None:
    async with async_session_factory() as db:
        existing = await db.get(CameraModel, cam_id)
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        db.add(
            CameraModel(
                id=cam_id,
                name="Calibration Test Cam",
                location="test",
                rtsp_url="rtsp://192.0.2.1/unreachable",
                # Disabled so no worker thread is started for it.
                is_ai_enabled=False,
                channel_number=77,
                floor_x=2.0,
                floor_y=2.0,
            )
        )
        await db.commit()


async def _delete_camera(cam_id: str) -> None:
    async with async_session_factory() as db:
        cam = await db.get(CameraModel, cam_id)
        if cam is not None:
            await db.delete(cam)
            await db.commit()


# ------------------------------------------------------------- GET layout


def test_layout_exposes_structures_and_setup_block(client, auth_headers):
    res = client.get("/api/v1/layout", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["units"] == "metres"
    assert isinstance(data["structures"], list)
    assert data["structure_kinds"] == ["ROOM", "WALL", "SHELF", "COUNTER", "DOOR", "OBSTACLE"]

    setup = data["setup"]
    for key in ("configured", "zones", "structures", "cameras", "cameras_calibrated"):
        assert key in setup
    assert setup["zones"] == len(data["zones"])
    assert setup["structures"] == len(data["structures"])
    assert setup["cameras"] == len(data["cameras"])
    assert setup["configured"] == bool(setup["zones"] or setup["structures"] or setup["cameras"])

    for cam in data["cameras"]:
        # New per-camera fields must exist even when unknown (null, never 0).
        assert "calibration_points" in cam
        assert "frame_width" in cam
        assert "frame_height" in cam
        assert "has_homography" in cam


# -------------------------------------------------------------- structures


def test_structure_crud_and_validation(client, auth_headers):
    before = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]["structures"]

    # A room: a closed polygon with an area.
    res = client.post(
        "/api/v1/layout/structures",
        json={"kind": "room", "name": "Bakery", "polygon": SQUARE, "properties": {"height_m": 2.4}},
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    room = res.json()
    assert room["id"].startswith("st_")
    assert room["kind"] == "ROOM"
    assert room["name"] == "Bakery"
    assert room["area_m2"] == 20.0
    assert room["length_m"] is None
    assert room["thickness_m"] == 0.2
    assert room["properties"] == {"height_m": 2.4}
    assert room["color"].startswith("#")

    # A wall: a polyline with a length, no area.
    res = client.post(
        "/api/v1/layout/structures",
        json={
            "kind": "WALL",
            "name": "North wall",
            "polygon": [{"x": 0, "y": 0}, {"x": 30, "y": 0}, {"x": 30, "y": 4}],
            "thickness_m": 0.3,
            "color": "#ffffff",
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    wall = res.json()
    assert wall["kind"] == "WALL"
    assert wall["area_m2"] == 0.0
    assert wall["length_m"] == 34.0
    assert wall["thickness_m"] == 0.3
    assert wall["color"] == "#ffffff"

    try:
        # Both appear in the layout and the setup counter moved.
        layout = client.get("/api/v1/layout", headers=auth_headers).json()
        ids = {s["id"] for s in layout["structures"]}
        assert {room["id"], wall["id"]} <= ids
        assert layout["setup"]["structures"] == before + 2
        assert layout["setup"]["configured"] is True

        # Dedicated list endpoint agrees.
        listed = client.get("/api/v1/layout/structures", headers=auth_headers).json()
        assert listed["total"] == len(listed["structures"]) >= 2

        # Partial update: rename and redraw, out-of-bounds vertices are clamped.
        res = client.put(
            f"/api/v1/layout/structures/{room['id']}",
            json={"name": "Bakery & Deli", "polygon": [{"x": -3, "y": 0}, {"x": 9999, "y": 0}, {"x": 5, "y": 5}]},
            headers=auth_headers,
        )
        assert res.status_code == 200, res.text
        updated = res.json()
        assert updated["name"] == "Bakery & Deli"
        assert updated["polygon"][0]["x"] == 0.0
        assert updated["polygon"][1]["x"] == layout["width_m"]

        # Validation errors are 400 with a reason.
        bad_kind = client.post(
            "/api/v1/layout/structures",
            json={"kind": "FOUNTAIN", "name": "x", "polygon": SQUARE},
            headers=auth_headers,
        )
        assert bad_kind.status_code == 400
        assert "kind" in bad_kind.json()["detail"]

        short_wall = client.post(
            "/api/v1/layout/structures",
            json={"kind": "WALL", "name": "x", "polygon": [{"x": 0, "y": 0}]},
            headers=auth_headers,
        )
        assert short_wall.status_code == 400

        thin_room = client.post(
            "/api/v1/layout/structures",
            json={"kind": "ROOM", "name": "x", "polygon": [{"x": 0, "y": 0}, {"x": 5, "y": 5}]},
            headers=auth_headers,
        )
        assert thin_room.status_code == 400
        assert "3 points" in thin_room.json()["detail"]

        bad_thickness = client.put(
            f"/api/v1/layout/structures/{wall['id']}",
            json={"thickness_m": 99},
            headers=auth_headers,
        )
        assert bad_thickness.status_code == 400

        # Changing kind re-validates the existing geometry against it: a
        # 3-point wall may become a room, but a 2-point line may not.
        res = client.put(
            f"/api/v1/layout/structures/{wall['id']}",
            json={"kind": "OBSTACLE"},
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["kind"] == "OBSTACLE"
        assert res.json()["area_m2"] > 0

        res = client.put(
            f"/api/v1/layout/structures/{wall['id']}",
            json={"kind": "DOOR", "polygon": [{"x": 1, "y": 1}, {"x": 2, "y": 1}]},
            headers=auth_headers,
        )
        assert res.status_code == 200
        res = client.put(
            f"/api/v1/layout/structures/{wall['id']}",
            json={"kind": "SHELF"},
            headers=auth_headers,
        )
        assert res.status_code == 400

        missing = client.put(
            "/api/v1/layout/structures/st_doesnotexist",
            json={"name": "ghost"},
            headers=auth_headers,
        )
        assert missing.status_code == 404
    finally:
        for sid in (room["id"], wall["id"]):
            res = client.delete(f"/api/v1/layout/structures/{sid}", headers=auth_headers)
            assert res.status_code == 200
            assert res.json()["deleted"] is True

    gone = client.delete(f"/api/v1/layout/structures/{room['id']}", headers=auth_headers)
    assert gone.status_code == 404
    after = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]["structures"]
    assert after == before


# ---------------------------------------------------------------- live map


def test_live_endpoint_shape(client, auth_headers):
    res = client.get("/api/v1/layout/live", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    for key in ("timestamp", "running", "persons", "detections", "persons_total", "uncalibrated_track_count"):
        assert key in data
    assert isinstance(data["running"], bool)
    assert isinstance(data["persons"], list)
    assert isinstance(data["detections"], list)
    assert data["persons_total"] == len(data["persons"])
    # No test camera can deliver frames, let alone confirmed floor tracks.
    assert data["persons"] == []
    for cam in data["detections"]:
        for key in ("camera_id", "camera_name", "status", "calibrated", "live_tracks", "boxes"):
            assert key in cam
        assert isinstance(cam["boxes"], list)


def test_pipeline_status_reports_calibrated_flag(client, auth_headers):
    res = client.get("/api/v1/layout/pipeline/status", headers=auth_headers)
    assert res.status_code == 200
    for cam in res.json()["cameras"]:
        assert isinstance(cam["calibrated"], bool)
        assert "frame_width" in cam and "frame_height" in cam


# ------------------------------------------------------------- calibration


def test_calibration_round_trip(client, auth_headers):
    cam_id = "cam_calib_test_01"
    _run(_insert_camera(cam_id))
    try:
        # Before: nothing known.
        res = client.get(f"/api/v1/layout/cameras/{cam_id}/calibration", headers=auth_headers)
        assert res.status_code == 200, res.text
        assert res.json() == {
            "camera_id": cam_id,
            "has_homography": False,
            "homography_matrix": None,
            "calibration_points": None,
            "frame_width": None,
            "frame_height": None,
        }
        res = client.post(
            f"/api/v1/layout/cameras/{cam_id}/calibration/test",
            json={"image_points": [{"x": 10, "y": 10}]},
            headers=auth_headers,
        )
        assert res.status_code == 200
        assert res.json()["calibrated"] is False
        assert res.json()["floor_points"] == [None]

        # Too few pairs is rejected.
        res = client.post(
            f"/api/v1/layout/cameras/{cam_id}/calibrate",
            json={"image_points": [{"x": 0, "y": 0}] * 3, "floor_points": [{"x": 0, "y": 0}] * 3},
            headers=auth_headers,
        )
        assert res.status_code == 400

        # A 100px square in the image maps to a 10m square on the floor.
        image_pts = [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}]
        floor_pts = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10}]
        res = client.post(
            f"/api/v1/layout/cameras/{cam_id}/calibrate",
            json={
                "image_points": image_pts,
                "floor_points": floor_pts,
                "frame_width": 640,
                "frame_height": 480,
            },
            headers=auth_headers,
        )
        assert res.status_code == 200, res.text
        saved = res.json()
        assert saved["calibrated"] is True
        assert len(saved["homography_matrix"]) == 3
        assert saved["calibration_points"]["frame_width"] == 640
        assert saved["calibration_points"]["frame_height"] == 480
        assert saved["calibration_points"]["image_points"] == image_pts
        assert saved["calibration_points"]["saved_at"]

        # GET returns what was stored.
        res = client.get(f"/api/v1/layout/cameras/{cam_id}/calibration", headers=auth_headers)
        got = res.json()
        assert got["has_homography"] is True
        assert got["calibration_points"]["floor_points"] == floor_pts
        assert got["frame_width"] == 640 and got["frame_height"] == 480

        # The layout's camera entry carries it too.
        layout = client.get("/api/v1/layout", headers=auth_headers).json()
        entry = next(c for c in layout["cameras"] if c["camera_id"] == cam_id)
        assert entry["has_homography"] is True
        assert entry["calibration_points"]["frame_width"] == 640
        assert entry["frame_width"] == 640
        assert layout["setup"]["cameras_calibrated"] >= 1

        # Projection through the stored homography.
        res = client.post(
            f"/api/v1/layout/cameras/{cam_id}/calibration/test",
            json={"image_points": [{"x": 50, "y": 50}, {"x": 100, "y": 0}]},
            headers=auth_headers,
        )
        assert res.status_code == 200
        pts = res.json()["floor_points"]
        assert res.json()["calibrated"] is True
        assert abs(pts[0]["x"] - 5.0) < 1e-6 and abs(pts[0]["y"] - 5.0) < 1e-6
        assert abs(pts[1]["x"] - 10.0) < 1e-6 and abs(pts[1]["y"]) < 1e-6

        # Clearing removes the matrix and the points.
        res = client.delete(f"/api/v1/layout/cameras/{cam_id}/calibrate", headers=auth_headers)
        assert res.status_code == 200
        got = client.get(f"/api/v1/layout/cameras/{cam_id}/calibration", headers=auth_headers).json()
        assert got["has_homography"] is False
        assert got["calibration_points"] is None
        res = client.post(
            f"/api/v1/layout/cameras/{cam_id}/calibration/test",
            json={"image_points": [{"x": 50, "y": 50}]},
            headers=auth_headers,
        )
        assert res.json()["floor_points"] == [None]

        missing = client.get("/api/v1/layout/cameras/cam_nope/calibration", headers=auth_headers)
        assert missing.status_code == 404
    finally:
        _run(_delete_camera(cam_id))


# ------------------------------------------------------------------- reset


def test_reset_requires_confirmation_and_returns_fresh_state(client, auth_headers):
    cam_id = "cam_reset_test_01"
    _run(_insert_camera(cam_id))
    zone = client.post(
        "/api/v1/layout/zones",
        json={"name": "Reset zone", "category": "AISLE", "polygon": SQUARE},
        headers=auth_headers,
    )
    assert zone.status_code == 201
    structure = client.post(
        "/api/v1/layout/structures",
        json={"kind": "ROOM", "name": "Reset room", "polygon": SQUARE},
        headers=auth_headers,
    )
    assert structure.status_code == 201
    renamed = client.put("/api/v1/layout", json={"name": "Temporary Name", "width_m": 12}, headers=auth_headers)
    assert renamed.status_code == 200

    configured = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]
    assert configured["configured"] is True

    # Wrong / missing acknowledgement never deletes anything.
    for body in ({}, {"confirm": "yes"}, {"confirm": "reset"}):
        res = client.post("/api/v1/layout/reset", json=body, headers=auth_headers)
        assert res.status_code == 400
    still = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]
    assert still == configured

    res = client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["reset"] is True
    assert data["removed"]["store_zones"] >= 1
    assert data["removed"]["store_structures"] >= 1
    assert data["removed"]["cameras"] >= 1
    assert data["total_rows"] == sum(data["removed"].values())
    assert data["layout"]["zones"] == [] and data["layout"]["structures"] == []

    fresh = client.get("/api/v1/layout", headers=auth_headers).json()
    assert fresh["name"] == "Store Floor"
    assert fresh["width_m"] == settings.DEFAULT_STORE_WIDTH_M
    assert fresh["height_m"] == settings.DEFAULT_STORE_HEIGHT_M
    assert fresh["zones"] == [] and fresh["structures"] == [] and fresh["cameras"] == []
    assert fresh["setup"] == {
        "configured": False,
        "zones": 0,
        "structures": 0,
        "cameras": 0,
        "cameras_calibrated": 0,
    }
    assert client.get("/api/v1/layout/pipeline/status", headers=auth_headers).json()["cameras_total"] == 0
    live = client.get("/api/v1/layout/live", headers=auth_headers).json()
    assert live["persons"] == [] and live["detections"] == []

    # Resetting an already-empty store must not fail.
    again = client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["total_rows"] == 0

    # The legacy purge route is an alias for the same operation.
    legacy = client.post("/api/v1/layout/purge-seed-data", json={"confirm": True}, headers=auth_headers)
    assert legacy.status_code == 200
    assert legacy.json()["total_rows"] == 0
    assert client.post("/api/v1/layout/purge-seed-data", json={}, headers=auth_headers).status_code == 400
