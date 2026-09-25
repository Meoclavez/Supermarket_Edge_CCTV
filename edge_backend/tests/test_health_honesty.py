"""GET /api/v1/health reports only what was observed.

A fresh install used to answer with hailo, rtsp_cam_0, go2rtc, database and
notification all HEALTHY with one shared timestamp: seeded defaults, not
checks. These tests pin the honest behaviour: absent hardware is NOT_PRESENT,
unconfigured push is NOT_CONFIGURED, unprobed go2rtc is NOT_CHECKED, camera
entries come from the configured cameras only, and the database entry comes
from the query the endpoint actually runs.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.database import async_session_factory, engine
from app.main import app
from app.models.db_models import Base, CameraModel
from app.routes import health as health_route
from app.services.resilience import ServiceHealthTracker

CAM = "cam_health_probe"


def _run(coro):
    return asyncio.run(coro)


async def _create_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _camera_ids() -> set:
    from sqlalchemy import select

    async with async_session_factory() as session:
        return {str(i) for i in (await session.execute(select(CameraModel.id))).scalars().all()}


async def _add_camera():
    async with async_session_factory() as session:
        session.add(CameraModel(id=CAM, name="Health probe", location="Test", rtsp_url="rtsp://127.0.0.1:1/x"))
        await session.commit()


async def _remove_camera():
    async with async_session_factory() as session:
        await session.execute(delete(CameraModel).where(CameraModel.id == CAM))
        await session.commit()


def _pipeline(cameras=None):
    return {"running": bool(cameras), "cameras_online": None, "live_tracks": None,
            "detector": {"available": False, "backend": "unavailable", "hailo": {}}, "cameras": cameras or []}


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A fresh install: no NPU node, no FCM credentials, no reports yet."""
    _run(_create_schema())
    ServiceHealthTracker().reset()
    app.state.setup_completed = True
    monkeypatch.setattr(settings, "HAILO_DEVICE", str(tmp_path / "no_hailo0"))
    from app.services.alert_dispatcher import fcm_provider

    monkeypatch.setattr(fcm_provider, "configured", lambda: False)
    monkeypatch.setattr(health_route, "_pipeline_status", lambda: _pipeline())
    # Nothing has measured the evidence directories on a fresh install either.
    from app.services import evidence_storage as evidence_mod

    monkeypatch.setattr(evidence_mod, "evidence_storage", evidence_mod.EvidenceStorage())
    yield TestClient(app)
    ServiceHealthTracker().reset()
    _run(_remove_camera())


def test_fresh_install_reports_no_fabricated_healthy(fresh):
    r = fresh.get("/api/v1/health")
    assert r.status_code == 200
    data = r.json()
    services = data["services"]

    # The only HEALTHY entry is the database, which the endpoint really queried.
    healthy = {k for k, v in services.items() if v["status"] == "HEALTHY"}
    assert healthy == {"database"}
    assert services["database"]["last_success_time"] is not None

    assert services["hailo"]["status"] == "NOT_PRESENT"
    assert services["notification"]["status"] == "NOT_CONFIGURED"
    assert services["go2rtc"]["status"] == "NOT_CHECKED"
    assert services["evidence_storage"]["status"] == "NOT_CHECKED"
    assert data["storage"]["evidence"]["status"] == "not_run_yet"
    for name in ("hailo", "notification", "go2rtc"):
        assert services[name]["last_success_time"] is None, name
        # Same keys as before, so existing clients keep parsing it.
        assert {"status", "last_error", "last_success_time", "consecutive_failures"} <= set(services[name])

    # Camera entries mirror the configured cameras; no phantom rtsp_cam_0.
    cam_keys = {k for k in services if k.startswith("rtsp_cam_")}
    assert cam_keys == {f"rtsp_cam_{i}" for i in _run(_camera_ids())}
    if "0" not in _run(_camera_ids()):
        assert "rtsp_cam_0" not in services

    assert data["hardware"]["hailo_available"] is False
    assert data["status"] == "healthy"  # from the database probe, not placeholders


def test_configured_camera_without_worker_is_not_checked(fresh):
    _run(_add_camera())
    svc = fresh.get("/api/v1/health").json()["services"][f"rtsp_cam_{CAM}"]
    assert svc["status"] == "NOT_CHECKED"
    assert svc["last_success_time"] is None


def test_camera_status_follows_live_worker(fresh, monkeypatch):
    _run(_add_camera())
    rt = {"camera_id": CAM, "status": "ONLINE", "seconds_since_frame": 0.5, "last_error": None}
    monkeypatch.setattr(health_route, "_pipeline_status", lambda: _pipeline([rt]))
    data = fresh.get("/api/v1/health").json()
    svc = data["services"][f"rtsp_cam_{CAM}"]
    assert svc["status"] == "HEALTHY"
    assert abs(svc["last_success_time"] - (time.time() - 0.5)) < 5

    rt.update(status="OFFLINE", seconds_since_frame=None, last_error="connection refused")
    data = fresh.get("/api/v1/health").json()
    svc = data["services"][f"rtsp_cam_{CAM}"]
    assert svc["status"] == "FAILED"
    assert svc["last_error"] == "connection refused"
    assert svc["last_success_time"] is None
    assert data["status"] == "degraded"


def test_database_failure_is_reported_not_hidden(fresh, monkeypatch):
    class Broken:
        async def __aenter__(self):
            raise RuntimeError("disk I/O error")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(health_route, "async_session_factory", lambda: Broken())
    r = fresh.get("/api/v1/health")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "unhealthy"
    assert data["services"]["database"]["status"] == "FAILED"
    assert "disk I/O error" in data["services"]["database"]["last_error"]


def test_tracker_seeds_nothing_and_counts_only_failures():
    t = ServiceHealthTracker()
    t.reset()
    assert t.get_system_health_report()["services"] == {}

    ServiceHealthTracker.report_status("dvr_recorder", "degraded", "Disk usage critical")
    s = t.get("dvr_recorder")
    assert s["status"] == "DEGRADED" and s["consecutive_failures"] == 1
    assert s["last_success_time"] is None  # never succeeded, so no success time

    ServiceHealthTracker.report_status("dvr_recorder", "healthy", "Clip recorded successfully")
    s = t.get("dvr_recorder")
    assert s["status"] == "HEALTHY" and s["consecutive_failures"] == 0 and s["last_error"] is None
    assert s["last_success_time"] is not None

    ServiceHealthTracker.report_status("go2rtc", ServiceHealthTracker.NOT_CHECKED)
    assert t.get("go2rtc")["consecutive_failures"] == 0
    t.reset()
