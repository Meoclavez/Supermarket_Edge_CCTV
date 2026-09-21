"""Per-camera helper endpoints, system telemetry and the honest-empty guarantees.

Nothing in these routes may return a plausible figure for something that was
not observed. The tests below pin that down.
"""

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings
from app.database import async_session_factory
from app.models.db_models import CameraModel
from app.services.feature_manager import FeatureManager
from app.services.inference_backend import person_detector
from app.services.live_analytics_engine import CameraRuntime, live_engine
from app.services.shelf_interaction_service import ShelfInteractionService
from app.routes import cameras as cameras_module

CAM_ID = "cam_helper_test_01"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def camera_row(client):
    async def _write():
        async with async_session_factory() as session:
            existing = await session.get(CameraModel, CAM_ID)
            if existing is None:
                session.add(CameraModel(
                    id=CAM_ID, name="Helper Test Cam", location="Test",
                    rtsp_url="", is_ai_enabled=False,
                ))
                await session.commit()

    async def _delete():
        async with async_session_factory() as session:
            existing = await session.get(CameraModel, CAM_ID)
            if existing is not None:
                await session.delete(existing)
                await session.commit()

    asyncio.run(_write())
    yield CAM_ID
    live_engine.runtimes.pop(CAM_ID, None)
    asyncio.run(_delete())


# ---------------- live-status ----------------

def test_live_status_unknown_camera_is_404(client):
    res = client.get("/api/v1/cameras/does_not_exist/live-status")
    assert res.status_code == 404


def test_live_status_without_worker_reports_not_running(client, camera_row):
    res = client.get(f"/api/v1/cameras/{camera_row}/live-status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["camera_id"] == camera_row
    assert data["running"] is False
    assert data["status"] == "OFFLINE"
    assert data["has_frame"] is False
    # Telemetry that was not measured is null, not zero-looking.
    assert data["fps"] is None
    assert data["live_tracks"] is None
    assert data["frame_width"] is None and data["frame_height"] is None
    assert data["calibrated"] is False
    assert isinstance(data["last_error"], str)


def test_live_status_with_worker_mirrors_pipeline_entry(client, camera_row):
    rt = CameraRuntime(camera_id=camera_row, name="Helper Test Cam", source="")
    rt.status = "ONLINE"
    live_engine.runtimes[camera_row] = rt
    try:
        res = client.get(f"/api/v1/cameras/{camera_row}/live-status")
        assert res.status_code == 200, res.text
        data = res.json()
        expected = rt.to_dict()
        for key, value in expected.items():
            assert data[key] == value, key
        assert data["running"] is True
        assert "calibrated" in data and "frame_width" in data and "frame_height" in data
    finally:
        live_engine.runtimes.pop(camera_row, None)


# ---------------- snapshot action ----------------

def test_snapshot_action_409_when_no_frame(client, camera_row):
    res = client.post(f"/api/v1/cameras/{camera_row}/actions/snapshot")
    assert res.status_code == 409, res.text
    assert "no frame" in res.json()["detail"].lower()


def test_snapshot_action_unknown_camera_is_404(client):
    res = client.post("/api/v1/cameras/nope/actions/snapshot")
    assert res.status_code == 404


def test_snapshot_action_saves_the_real_frame(client, camera_row):
    rt = CameraRuntime(camera_id=camera_row, name="Helper Test Cam", source="")
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:, :, 1] = 200
    rt.put_frame(frame)
    live_engine.runtimes[camera_row] = rt
    try:
        res = client.post(f"/api/v1/cameras/{camera_row}/actions/snapshot")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["saved"] is True
        assert data["filename"].startswith(f"{camera_row}_") and data["filename"].endswith(".jpg")
        assert data["url"] == f"/api/v1/events/snapshots/{data['filename']}"
        assert (data["frame_width"], data["frame_height"]) == (64, 48)
        path = settings.SNAPSHOTS_DIR / data["filename"]
        assert path.exists() and path.stat().st_size == data["bytes"] > 0
        path.unlink()
    finally:
        live_engine.runtimes.pop(camera_row, None)


# ---------------- clip action ----------------

def test_clip_action_501_without_real_buffer(client, camera_row):
    res = client.post(f"/api/v1/cameras/{camera_row}/actions/clip")
    assert res.status_code == 501, res.text
    assert res.json()["detail"].startswith("clip export not available:")


# ---------------- legacy fabricated routes are gone ----------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/status"),
    ("POST", "/api/action/snapshot"),
    ("POST", "/api/action/clip"),
    ("POST", "/api/v1/theft/simulate"),
])
def test_legacy_fabricated_routes_removed(client, method, path):
    res = client.request(method, path)
    assert res.status_code in (404, 405), f"{method} {path} -> {res.status_code}"


def test_camera_module_has_no_fallback_fleet():
    assert not hasattr(cameras_module, "DEFAULT_CAMERAS")
    assert not hasattr(cameras_module, "CURRENT_ACTIVE_SOURCE")


# ---------------- system telemetry ----------------

def test_system_stats_have_no_constants(client):
    res = client.get("/api/v1/system/stats")
    assert res.status_code == 200, res.text
    data = res.json()
    # The old constants.
    assert data["gpu_usage_percent"] != 12.5
    assert data["shm_buffer_used_mb"] is None
    assert data["cpu_usage_percent"] != 5.0 or data["cpu_usage_percent"] is None
    # Cameras come from the live engine, not a literal.
    pipeline = live_engine.status()
    assert data["active_cameras"] == pipeline["cameras_online"]
    assert data["cameras_total"] == pipeline["cameras_total"]
    assert data["inference_engine"] == person_detector.status()["backend"]
    assert data["inference_provider"] == person_detector.status()["provider"]
    for key in ("cpu_usage_percent", "ram_used_gb", "ram_total_gb"):
        assert data[key] is None or isinstance(data[key], (int, float))


def test_hardware_profile_reports_live_detector(client):
    res = client.get("/api/v1/system/hardware")
    assert res.status_code == 200, res.text
    data = res.json()
    status = person_detector.status()
    assert data["inference_backend"] == status["backend"]
    assert data["inference_provider"] == status["provider"]
    assert data["inference_available"] == bool(status["available"])
    assert data["decoder_capability"] == data["decoder_type"]
    assert data["decoder_type"] in ("cuda", "vaapi_intel", "vaapi_amd", "cpu")


# ---------------- in-memory services start empty ----------------

def test_feature_manager_starts_empty():
    fm = FeatureManager()
    assert fm.count_active_features() == 0


def test_shelf_service_never_seeds(tmp_path):
    svc = ShelfInteractionService(config_path=tmp_path / "shelf.json")
    assert svc.get_zones() == []
    assert (tmp_path / "shelf.json").exists()
    import json
    assert json.loads((tmp_path / "shelf.json").read_text()) == {"zones": []}


def test_shelf_stats_start_at_zero_with_null_rates(tmp_path):
    from app.services.shelf_interaction_service import ProductShelfZone, PointCoord

    svc = ShelfInteractionService(config_path=tmp_path / "shelf.json")
    svc.save_zone(ProductShelfZone(
        id="z1", camera_id="cam_x", name="Zone", sku_id="SKU", category="Test",
        points=[PointCoord(x=0, y=0), PointCoord(x=1, y=0), PointCoord(x=1, y=1), PointCoord(x=0, y=1)],
    ))
    stats = svc.get_zone_stats("z1")
    assert stats["impressions"] == stats["touches"] == stats["picks"] == 0
    assert stats["attraction_rate"] is None
    assert stats["friction_index"] is None
    assert stats["conversion_rate"] is None
    assert stats["avg_dwell_sec"] is None
