"""Turning a camera off and on from the dashboard.

Off is the stored ``cameras.is_ai_enabled`` switch. An off camera has no
worker and opens no connection (counted on a real local RTSP socket), reports
DISABLED everywhere instead of offline, survives a restart, and never makes
the health status degraded. Turning it on starts its worker straight away.
"""

from __future__ import annotations

import socket
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import live_analytics_engine as lae
from app.services.live_analytics_engine import live_engine
from tests.test_camera_auth_and_discovery import GOOD_PW, USER, FakeRtspCamera

API = "/api/v1/cameras"


class CountingRtspCamera(FakeRtspCamera):
    """The fake Dahua camera, also counting every TCP connection it accepts."""

    def __init__(self, *a, **k):
        self.connections = 0
        super().__init__(*a, **k)

    def _handle(self, conn):
        self.connections += 1
        super()._handle(conn)


class _NoStream:
    """OpenCV capture that never opens, so only the worker's RTSP login check connects."""

    def isOpened(self):
        return False

    def release(self):
        pass


@pytest.fixture
def camera():
    cam = CountingRtspCamera()
    yield cam
    cam.close()


@pytest.fixture
def no_stream(monkeypatch):
    monkeypatch.setattr(lae.CameraWorker, "_open", lambda self, src=None: _NoStream())


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _stored_enabled(cam_id: str):
    with sqlite3.connect(settings.DATABASE_PATH) as db:
        row = db.execute("SELECT is_ai_enabled FROM cameras WHERE id=?", (cam_id,)).fetchone()
    return None if row is None else bool(row[0])


def _wait(cond, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _add(client, cam_id, url, **extra):
    body = {"id": cam_id, "name": cam_id, "location": "", "source_type": "rtsp", "rtsp_url": url, **extra}
    res = client.post(API, json=body)
    assert res.status_code == 200, res.text


def test_off_camera_has_no_worker_and_makes_no_connection_across_a_restart(camera, no_stream):
    cid = "cam_onoff_rtsp"
    try:
        with TestClient(app) as client:
            _add(client, cid, camera.url(user=None), username=USER, password=GOOD_PW)
            assert cid in live_engine.workers
            assert _wait(lambda: camera.connections >= 1), "the enabled camera never connected"

            res = client.put(f"{API}/{cid}/enabled", json={"enabled": False})
            assert res.status_code == 200, res.text
            assert res.json() == {"camera_id": cid, "name": cid, "enabled": False, "status": "DISABLED"}
            assert _stored_enabled(cid) is False
            assert cid not in live_engine.workers
            rt = live_engine.runtimes[cid]
            assert rt.status == "DISABLED" and rt.enabled is False and rt.source == ""

            time.sleep(0.5)                  # a login check already on the wire may finish
            before = camera.connections
            time.sleep(3.0)                  # an enabled worker retries after 1 s, then 2 s
            assert camera.connections == before, "a camera turned off still connected"

            listed = {c["id"]: c for c in client.get(API).json()["cameras"]}
            assert listed[cid]["status"] == "DISABLED" and listed[cid]["is_ai_enabled"] is False
            live = client.get(f"{API}/{cid}/live-status").json()
            assert live["status"] == "DISABLED" and live["running"] is False and live["last_error"] is None
            snap = client.get(f"{API}/{cid}/snapshot?annotate=false&overlay=1")
            assert snap.headers["X-Frame-Source"] == "no-signal"
            pipe = client.get("/api/v1/layout/pipeline/status").json()
            assert pipe["cameras_off"] >= 1
            assert pipe["cameras_enabled"] + pipe["cameras_off"] == pipe["cameras_total"]

        # Simulated restart: the lifespan stops every worker, a new one reads the DB.
        before = camera.connections
        with TestClient(app) as client:
            assert live_engine.runtimes[cid].status == "DISABLED"
            assert cid not in live_engine.workers
            time.sleep(2.5)
            assert camera.connections == before, "an off camera connected after a restart"

            res = client.put(f"{API}/{cid}/enabled", json={"enabled": True})
            assert res.status_code == 200, res.text
            assert res.json()["enabled"] is True and res.json()["status"] != "DISABLED"
            assert cid in live_engine.workers, "turning on did not start the worker immediately"
            assert _wait(lambda: camera.connections > before), "the camera turned on never connected"
            assert _stored_enabled(cid) is True
    finally:
        with TestClient(app) as client:
            client.delete(f"/api/v1/layout/cameras/{cid}")


def test_bulk_switch_is_all_or_nothing_and_not_taken_for_a_camera_id(no_stream):
    ids = ["cam_onoff_b1", "cam_onoff_b2", "cam_onoff_b3"]
    with TestClient(app) as client:
        try:
            for cid in ids:
                _add(client, cid, f"rtsp://127.0.0.1:{_closed_port()}/x")

            # An unknown id changes nothing.
            res = client.put(f"{API}/enabled", json={"camera_ids": ids + ["cam_onoff_missing"], "enabled": False})
            assert res.status_code == 404 and "cam_onoff_missing" in res.json()["detail"]
            assert all(_stored_enabled(c) is True for c in ids)

            res = client.put(f"{API}/enabled", json={"camera_ids": ids[:2] + ids[:1], "enabled": False})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["updated"] == 2 and [c["camera_id"] for c in body["cameras"]] == ids[:2]
            assert all(c["status"] == "DISABLED" and c["enabled"] is False for c in body["cameras"])
            assert [_stored_enabled(c) for c in ids] == [False, False, True]
            assert not (set(ids[:2]) & set(live_engine.workers)) and ids[2] in live_engine.workers
            # "enabled" is the bulk route, never an upserted camera.
            assert _stored_enabled("enabled") is None

            assert client.put(f"{API}/enabled", json={"camera_ids": [], "enabled": True}).status_code == 422

            # The Settings dialog echoes is_ai_enabled=true: a save must not switch the camera back on.
            feed = client.get(f"{API}/{ids[0]}").json()
            feed.update(name="renamed", is_ai_enabled=True)
            assert client.put(f"{API}/{ids[0]}", json=feed).status_code == 200
            assert _stored_enabled(ids[0]) is False and ids[0] not in live_engine.workers

            res = client.put(f"{API}/enabled", json={"camera_ids": ids, "enabled": True})
            assert res.status_code == 200 and all(c["enabled"] for c in res.json()["cameras"])
            assert set(ids) <= set(live_engine.workers)
        finally:
            for cid in ids:
                client.delete(f"/api/v1/layout/cameras/{cid}")


def test_switch_unknown_camera_is_404():
    with TestClient(app) as client:
        assert client.put(f"{API}/cam_onoff_nope/enabled", json={"enabled": False}).status_code == 404
        assert client.put(f"{API}/cam_onoff_nope/enabled", json={}).status_code == 422


# ------------------------------------------------------------------- health

def test_health_reports_an_off_camera_as_disabled_without_degrading():
    from app.routes.health import _camera_services, _overall
    from app.services.resilience import ServiceHealthTracker as T

    pipeline = {"cameras": [
        {"camera_id": "on1", "status": "ONLINE", "enabled": True, "seconds_since_frame": 0.2},
        {"camera_id": "off1", "status": "DISABLED", "enabled": False, "seconds_since_frame": None},
    ]}
    # off2 was switched off before the pipeline picked it up (no runtime yet).
    services = _camera_services([("on1", "Door", True), ("off1", "Aisle", False), ("off2", "Back", False)], pipeline)
    assert services["rtsp_cam_on1"]["status"] == T.HEALTHY
    assert services["rtsp_cam_off1"]["status"] == T.DISABLED
    assert services["rtsp_cam_off2"]["status"] == T.DISABLED
    assert _overall({"database": T.entry(T.HEALTHY), **services}) == "healthy"

    # The same camera failing (turned on but offline) does degrade it.
    pipeline["cameras"][1].update(status="OFFLINE", enabled=True, last_error="no route")
    services = _camera_services([("on1", "Door", True), ("off1", "Aisle", True)], pipeline)
    assert _overall({"database": T.entry(T.HEALTHY), **services}) == "degraded"
    # Callers that still pass (id, name) keep working.
    assert _camera_services([("on1", "Door")], pipeline)["rtsp_cam_on1"]["status"] == T.HEALTHY


def test_health_endpoint_lists_an_off_camera_as_disabled(no_stream):
    cid = "cam_onoff_health"
    with TestClient(app) as client:
        try:
            _add(client, cid, f"rtsp://127.0.0.1:{_closed_port()}/x")
            assert client.put(f"{API}/{cid}/enabled", json={"enabled": False}).status_code == 200
            body = client.get("/api/v1/health").json()
            entry = body["services"][f"rtsp_cam_{cid}"]
            assert entry["status"] == "DISABLED" and entry["name"] == cid
            assert body["telemetry"]["cameras_off"] >= 1
        finally:
            client.delete(f"/api/v1/layout/cameras/{cid}")
