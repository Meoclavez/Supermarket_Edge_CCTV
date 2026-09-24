"""Camera credentials live in the encrypted store, never in ``cameras.rtsp_url``.

Covers migration m0009 (existing rows) and every flow that creates a camera
from a network device: Dahua NVR adoption, scan adoption and first-run
add-cameras.
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.migrations.runner import discover, run_migrations
from app.services import camera_source

PW = "Nvr:p@ss/w0rd%x"                     # reserved characters on purpose
ENC_PW = "Nvr%3Ap%40ss%2Fw0rd%25x"         # as camera_drivers._auth_prefix writes it
m0009 = importlib.import_module("app.migrations.m0009_camera_credentials_out_of_url")


def _db_rows(sql, args=()):
    with sqlite3.connect(settings.DATABASE_PATH) as db:
        return db.execute(sql, args).fetchall()


def _stored_url(cam_id):
    return _db_rows("SELECT rtsp_url FROM cameras WHERE id=?", (cam_id,))[0][0]


# ------------------------------------------------------------------ migration

def _seed_pre_m0009(db_path: Path) -> None:
    """A database at the schema just before m0009, with legacy URL credentials."""
    run_migrations(db_path, migrations=[m for m in discover() if m.version != 9],
                   backups_dir=db_path.parent / "b0", run_reconcile=True)
    c = sqlite3.connect(str(db_path))
    cols = "id, name, location, rtsp_url, status, fps, resolution, is_ai_enabled, ai_models, dvr_enabled, " \
           "dvr_retention_days, dvr_quota_gb, channel_number, department, created_at, " \
           "floor_x, floor_y, floor_z, azimuth_deg, fov_deg"
    rows = [
        ("cam_dahua", f"rtsp://admin:{ENC_PW}@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1"),
        ("cam_useronly", "rtsp://viewer@10.0.0.7/live"),
        ("cam_clean", "rtsp://10.0.0.5/1"),
        ("cam_usb", "/dev/video0"),
        ("cam_at_in_query", "http://10.0.0.9/stream?owner=a@b"),
    ]
    for i, (cid, url) in enumerate(rows):
        c.execute(f"INSERT INTO cameras ({cols}) VALUES (?,?,?,?,'OFFLINE',0,'unknown',1,'[]',1,7,100.0,?,'GENERAL','2026-09-01 10:00:00',1,1,3,90,85)",
                  (cid, cid, "", url, i + 1))
    c.execute("INSERT INTO discovered_devices (id, transport, driver, host, stream_urls, channels, "
              "requires_credentials, reachable, first_seen, last_seen) "
              "VALUES (?,?,?,?,?,1,1,1,'2026-09-01 10:00:00','2026-09-01 10:00:00')",
              ("rtsp:192.168.1.108:554", "rtsp", "dahua", "192.168.1.108",
               json.dumps([{"url": f"rtsp://admin:{ENC_PW}@192.168.1.108:554/x", "quality": "main"}])))
    c.commit()
    c.close()


def test_m0009_moves_url_credentials_and_is_idempotent(tmp_path, caplog):
    db = tmp_path / "legacy.db"
    _seed_pre_m0009(db)

    with caplog.at_level(logging.INFO, logger="edge.migrations"):
        report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert "0009_camera_credentials_out_of_url" in report["applied"]
    assert report["backup"] and Path(report["backup"]).exists()   # runner backed up first

    c = sqlite3.connect(str(db))
    urls = dict(c.execute("SELECT id, rtsp_url FROM cameras").fetchall())
    assert urls["cam_dahua"] == "rtsp://192.168.1.108:554/cam/realmonitor?channel=1&subtype=1"
    assert urls["cam_useronly"] == "rtsp://10.0.0.7/live"
    assert urls["cam_clean"] == "rtsp://10.0.0.5/1"
    assert urls["cam_usb"] == "/dev/video0"
    assert urls["cam_at_in_query"] == "http://10.0.0.9/stream?owner=a@b"
    dev_urls = json.loads(c.execute("SELECT stream_urls FROM discovered_devices").fetchone()[0])
    assert dev_urls[0]["url"] == "rtsp://192.168.1.108:554/x"
    assert ENC_PW not in json.dumps(dev_urls)

    # Encrypted per camera, and injected again at open time.
    assert camera_source.load_credentials("cam_dahua") == ("admin", PW)
    assert camera_source.load_credentials("cam_useronly") == ("viewer", "")
    assert camera_source.load_credentials("cam_clean") is None
    opened = camera_source.stream_source_for("cam_dahua", urls["cam_dahua"])
    assert camera_source.split_url_credentials(opened) == (urls["cam_dahua"], "admin", PW)

    # Logs name cameras, never credentials.
    text = caplog.text
    assert "cam_dahua" in text and "cam_useronly" in text
    assert PW not in text and ENC_PW not in text and "admin:" not in text

    # Idempotent: a second pass changes nothing; the runner has nothing pending.
    before = c.execute("SELECT id, rtsp_url FROM cameras ORDER BY id").fetchall()
    m0009.upgrade(c)
    c.commit()
    assert c.execute("SELECT id, rtsp_url FROM cameras ORDER BY id").fetchall() == before
    c.close()
    assert run_migrations(db, backups_dir=tmp_path / "backups")["applied"] == []

    for cid in ("cam_dahua", "cam_useronly"):
        camera_source.delete_credentials(cid)


def test_m0009_leaves_the_row_when_the_store_cannot_be_written(tmp_path, caplog):
    db = tmp_path / "legacy.db"
    _seed_pre_m0009(db)
    c = sqlite3.connect(str(db))
    with caplog.at_level(logging.WARNING, logger="edge.migrations"):
        m0009.upgrade(c, storage_dir=tmp_path / "store", machine_key="")  # no key -> cannot encrypt
    url = c.execute("SELECT rtsp_url FROM cameras WHERE id='cam_dahua'").fetchone()[0]
    assert ENC_PW in url                       # not lost
    assert "cam_dahua" in caplog.text and ENC_PW not in caplog.text and PW not in caplog.text
    # With a store it moves them, to exactly that store.
    m0009.upgrade(c, storage_dir=tmp_path / "store", machine_key="k" * 43)
    assert ENC_PW not in c.execute("SELECT rtsp_url FROM cameras WHERE id='cam_dahua'").fetchone()[0]
    assert camera_source.load_credentials("cam_dahua", storage_dir=tmp_path / "store",
                                          machine_key="k" * 43) == ("admin", PW)
    c.close()


# ---------------------------------------------------------------- adopt flows

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_dahua_adopt_stores_credentials_out_of_the_url(client):
    res = client.post("/api/v1/dahua/adopt", json={
        "host": "127.0.0.1", "port": 9, "username": "admin", "password": PW,
        "channels": [{"channel": 1, "name": "Dahua cred 1", "quality": "sub"},
                     {"channel": 2, "name": "Dahua cred 2", "quality": "main"}],
    })
    assert res.status_code == 201, res.text
    ids = [c["camera_id"] for c in res.json()["cameras"]]
    try:
        assert PW not in res.text and ENC_PW not in res.text
        for ch, (cid, sub) in enumerate(zip(ids, (1, 0)), start=1):
            assert _stored_url(cid) == f"rtsp://127.0.0.1:9/cam/realmonitor?channel={ch}&subtype={sub}"
            assert camera_source.load_credentials(cid) == ("admin", PW)
            listed = client.get(f"/api/v1/cameras/{cid}")
            assert PW not in listed.text and ENC_PW not in listed.text
    finally:
        for cid in ids:
            assert client.delete(f"/api/v1/layout/cameras/{cid}").status_code == 200
    assert not any(camera_source.has_credentials(cid) for cid in ids)   # removed with the camera


def test_scan_adopt_stores_credentials_out_of_the_url(client, monkeypatch):
    # Nothing listens on port 9; adoption now checks the RTSP handshake, so
    # stand in a camera that accepts the login (covered for real in
    # tests/test_camera_auth_and_discovery.py).
    from app.services import rtsp_probe

    monkeypatch.setattr(rtsp_probe, "probe_rtsp",
                        lambda url, timeout_s=5.0, method="DESCRIBE": rtsp_probe.RtspProbeResult(rtsp_probe.OK, 200))
    dev_id = "rtsp:127.0.0.1:9"
    with sqlite3.connect(settings.DATABASE_PATH) as db:
        db.execute("DELETE FROM discovered_devices WHERE id=?", (dev_id,))
        db.execute("INSERT INTO discovered_devices (id, transport, driver, host, port, stream_urls, channels, "
                   "requires_credentials, reachable, first_seen, last_seen) VALUES "
                   "(?, 'rtsp', 'hikvision', '127.0.0.1', 9, '[]', 1, 1, 1, '2026-09-01 10:00:00', '2026-09-01 10:00:00')",
                   (dev_id,))
    res = client.post("/api/v1/layout/devices/adopt", json={
        "device_id": dev_id, "name": "Scan cred", "username": "op", "password": PW, "channel": 1, "quality": "main",
    })
    assert res.status_code == 201, res.text
    cid = res.json()["camera_id"]
    try:
        assert PW not in res.text and ENC_PW not in res.text
        assert _stored_url(cid) == "rtsp://127.0.0.1:9/Streaming/Channels/101"
        assert camera_source.load_credentials(cid) == ("op", PW)
    finally:
        assert client.delete(f"/api/v1/layout/cameras/{cid}").status_code == 200
    assert not camera_source.has_credentials(cid)

    # A stream URL typed with inline credentials is split the same way.
    with sqlite3.connect(settings.DATABASE_PATH) as db:
        db.execute("UPDATE discovered_devices SET adopted_camera_id=NULL WHERE id=?", (dev_id,))
    res = client.post("/api/v1/layout/devices/adopt", json={
        "device_id": dev_id, "name": "Scan inline", "stream_url": f"rtsp://op2:{ENC_PW}@127.0.0.1:9/live",
    })
    assert res.status_code == 201, res.text
    cid = res.json()["camera_id"]
    try:
        assert _stored_url(cid) == "rtsp://127.0.0.1:9/live"
        assert camera_source.load_credentials(cid) == ("op2", PW)
    finally:
        client.delete(f"/api/v1/layout/cameras/{cid}")
        with sqlite3.connect(settings.DATABASE_PATH) as db:
            db.execute("DELETE FROM discovered_devices WHERE id=?", (dev_id,))


def test_setup_add_cameras_stores_credentials_out_of_the_url(client):
    res = client.post("/api/v1/setup/add-cameras", json={"cameras": [
        {"name": "Setup cred", "location": "Door", "rtsp_url": f"rtsp://admin:{ENC_PW}@127.0.0.1:9/ch1"},
    ]})
    assert res.status_code == 200, res.text
    cid = res.json()["added_cameras"][0]
    try:
        assert _stored_url(cid) == "rtsp://127.0.0.1:9/ch1"
        assert camera_source.load_credentials(cid) == ("admin", PW)
    finally:
        client.delete(f"/api/v1/cameras/{cid}")
    assert not camera_source.has_credentials(cid)
