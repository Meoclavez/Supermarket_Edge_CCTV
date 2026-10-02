"""Theft reporting: evidence clips, the clip endpoint, the storage cap and /patterns.

* A camera with the ``theft_clip`` feature saves a short clip (pre-event ring
  + post-roll) with each incident, linked on the incident row; a camera
  without it saves none.
* ``GET /api/v1/theft/incidents/{id}/clip`` serves it (404 without one, 410
  once the evidence storage limit deleted it).
* The clip lives in the theft evidence directory, counts against the
  evidence limit and goes together with the incident's still.
* ``GET /api/v1/theft/patterns`` is built from recorded rows only: empty
  arrays without incidents, and per-rule false-alarm rates from outcomes.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import async_session_factory, engine
from app.main import app
from app.models.db_models import Base, SystemSetupModel, TheftIncidentModel
from app.models.schemas import CameraFeatureConfig
from app.services.auth_service import auth_service
from app.services.clip_recorder import clip_recorder_service
from app.services.evidence_storage import EvidenceStorage
from app.services.feature_manager import feature_manager
from app.services.pose_analytics import PoseAnalytics, pose_analytics
from app.services.theft_detection_service import get_patterns, theft_detection_service
from app.services.timeutil import to_local, utcnow


@pytest.fixture(scope="module", autouse=True)
def init_test_database():
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as session:
            res = await session.execute(select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed"))
            if not res.scalar_one_or_none():
                session.add(SystemSetupModel(key="setup_completed", value="true"))
                await session.commit()
    asyncio.run(_init())


def _client() -> TestClient:
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _row(incident_id: str) -> TheftIncidentModel | None:
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        row = s.get(TheftIncidentModel, incident_id)
        if row is not None:
            s.expunge(row)
    eng.dispose()
    return row


def _incident_payload(camera_id: str) -> dict:
    return {
        "id": f"theft_test_{uuid.uuid4().hex[:10]}",
        "rule": "CONCEALMENT",
        "camera_id": camera_id,
        "track_id": "7",
        "ts": time.time(),
        "confidence": 0.81,
        "severity": "HIGH",
        "zone_id": "pz_report",
        "zone_name": "Razors",
        "evidence": ["Right wrist went from the shelf to the waist band and stayed there"],
        "bbox": (10.0, 10.0, 50.0, 90.0),
        "wrist_trajectory": [],
        "items": [],
        "estimated_loss_value": 0.0,
        "snapshot": None,
    }


def _wait_for_clip_threads(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    for t in [t for t in threading.enumerate() if t.name.startswith("theft-clip-")]:
        t.join(max(0.0, deadline - time.time()))


@pytest.fixture
def clip_camera(monkeypatch):
    """A camera whose clip ring is fed by a stand-in worker thread with real frames."""
    cams = []
    stop = threading.Event()

    def make(camera_id: str, theft_clip: bool):
        feature_manager.set_camera_features(camera_id, CameraFeatureConfig(theft_clip=theft_clip))
        clip_recorder_service.invalidate(camera_id)
        cams.append(camera_id)
        buf = clip_recorder_service.get_or_create_buffer(camera_id, fps=10)
        for i in range(10):                      # ~1 s of pre-event footage
            buf.push_frame(np.full((48, 64, 3), i * 10, np.uint8))

        def feed():
            i = 0
            while not stop.is_set():
                buf.push_frame(np.full((48, 64, 3), (i * 7) % 255, np.uint8))
                i += 1
                time.sleep(0.1)
        threading.Thread(target=feed, daemon=True).start()
        return camera_id

    monkeypatch.setattr(settings, "POST_EVENT_RECORD_SECONDS", 1)
    yield make
    stop.set()
    for c in cams:
        feature_manager.remove_camera(c)
        clip_recorder_service.invalidate(c)
        with clip_recorder_service._service_lock:
            clip_recorder_service.buffers.pop(c, None)


def _flush(engine_: PoseAnalytics, payload: dict) -> None:
    with engine_._pending_lock:
        engine_._pending.append(("incident", payload))
    assert engine_.flush_now() == 1


# ------------------------------------------------------------------ clip saved / absent

def test_clip_saved_and_linked_when_theft_clip_is_on(clip_camera):
    cam = clip_camera("cam_report_clip_on", theft_clip=True)
    assert clip_recorder_service.keeps_ring(cam), "auto mode keeps the ring for a theft_clip camera"
    pa = PoseAnalytics()
    p = _incident_payload(cam)
    _flush(pa, p)
    _wait_for_clip_threads()

    row = _row(p["id"])
    assert row is not None
    assert row.clip_path and Path(row.clip_path).is_file()
    assert Path(row.clip_path).parent.resolve() == pa.evidence_dir().resolve()
    assert Path(row.clip_path).name == f"{p['id']}.mp4"
    assert Path(row.clip_path).stat().st_size > 0
    assert row.evidence_clip_url == f"/api/v1/theft/incidents/{p['id']}/clip"

    res = _client().get(f"/api/v1/theft/incidents/{p['id']}")
    assert res.status_code == 200
    assert res.json()["clip_url"] == f"/api/v1/theft/incidents/{p['id']}/clip"


def test_no_clip_when_theft_clip_is_off(clip_camera):
    cam = clip_camera("cam_report_clip_off", theft_clip=False)
    assert not clip_recorder_service.theft_clip_enabled(cam)
    pa = PoseAnalytics()
    p = _incident_payload(cam)
    _flush(pa, p)
    _wait_for_clip_threads()

    row = _row(p["id"])
    assert row is not None
    assert row.clip_path is None and row.evidence_clip_url is None
    assert not (pa.evidence_dir() / f"{p['id']}.mp4").exists()
    body = _client().get(f"/api/v1/theft/incidents/{p['id']}").json()
    assert body["clip_url"] is None


def test_theft_clip_is_off_by_default_and_needs_theft_detection():
    assert CameraFeatureConfig().theft_clip is False
    feature_manager.set_camera_features("cam_report_td_off",
                                        CameraFeatureConfig(theft_clip=True, theft_detection=False))
    try:
        assert not clip_recorder_service.theft_clip_enabled("cam_report_td_off")
    finally:
        feature_manager.remove_camera("cam_report_td_off")


def test_static_figure_fields_validate():
    c = CameraFeatureConfig()
    assert c.static_figure_filter is True and c.static_figure_seconds is None
    assert CameraFeatureConfig(static_figure_seconds=120).static_figure_seconds == 120
    for bad in (5, 4000):
        with pytest.raises(Exception):
            CameraFeatureConfig(static_figure_seconds=bad)


# ------------------------------------------------------------------ clip endpoint

def _insert(**fields) -> str:
    base = dict(
        id=f"theft_rep_{uuid.uuid4().hex[:10]}", theft_type="CONCEALMENT", rule="CONCEALMENT",
        severity="HIGH", status="ACTIVE", department="GENERAL", camera_id="cam_rep",
        camera_name="Rep cam", timestamp=utcnow(), confidence=0.7, evidence_summary="",
        items_involved=[], estimated_loss_value=0.0,
    )
    base.update(fields)

    async def _w():
        async with async_session_factory() as s:
            s.add(TheftIncidentModel(**base))
            await s.commit()
    asyncio.run(_w())
    return base["id"]


def test_clip_endpoint_serves_the_clip_and_404s_without_one():
    client = _client()
    d = pose_analytics.evidence_dir()
    iid = f"theft_rep_{uuid.uuid4().hex[:10]}"
    clip = d / f"{iid}.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    _insert(id=iid, clip_path=str(clip), evidence_clip_url=f"/api/v1/theft/incidents/{iid}/clip")

    res = client.get(f"/api/v1/theft/incidents/{iid}/clip")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == clip.read_bytes()
    # Seeking: the browser asks for byte ranges.
    part = client.get(f"/api/v1/theft/incidents/{iid}/clip", headers={"Range": "bytes=0-7"})
    assert part.status_code == 206 and len(part.content) == 8

    no_clip = _insert()
    assert client.get(f"/api/v1/theft/incidents/{no_clip}/clip").status_code == 404
    assert client.get("/api/v1/theft/incidents/does_not_exist/clip").status_code == 404

    # A path outside the evidence directory is never served.
    outside = _insert(clip_path="/etc/passwd")
    assert client.get(f"/api/v1/theft/incidents/{outside}/clip").status_code == 404

    # Deleted by the storage limit: 410 and no clip_url on the incident.
    gone = f"theft_rep_{uuid.uuid4().hex[:10]}"
    _insert(id=gone, clip_path=str(d / f"{gone}.mp4"), evidence_expired_at=utcnow())
    assert client.get(f"/api/v1/theft/incidents/{gone}/clip").status_code == 410
    assert client.get(f"/api/v1/theft/incidents/{gone}").json()["clip_url"] is None

    anon = TestClient(app)
    if not getattr(settings, "AUTH_DISABLED", False):
        assert anon.get(f"/api/v1/theft/incidents/{iid}/clip").status_code in (401, 403)


# ------------------------------------------------------------------ storage cap

@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n")
    db = tmp_path / "evidence_test.db"
    for name, value in {
        "STORAGE_DIR": root, "THEFT_EVIDENCE_DIR": "", "ZONE_ALERT_EVIDENCE_DIR": "",
        "NIGHT_WATCH_EVIDENCE_DIR": "", "SNAPSHOTS_DIR": root / "snapshots", "CLIPS_DIR": root / "clips",
        "DATABASE_PATH": db, "STORAGE_RETENTION_DAYS": 0, "STORAGE_MAX_DISK_PERCENT": 0.0,
        "EVIDENCE_MAX_GB": 1.0, "NIGHT_WATCH_EVIDENCE_MAX_MB": 1024.0,
    }.items():
        monkeypatch.setattr(settings, name, value)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE theft_incidents (id TEXT PRIMARY KEY, snapshot_path TEXT, evidence_snapshot_url TEXT,
                                      evidence_clip_url TEXT, evidence_expired_at DATETIME);
        CREATE TABLE security_events (id TEXT PRIMARY KEY, snapshot_url TEXT, clip_url TEXT,
                                      evidence_expired_at DATETIME);
    """)
    conn.close()
    return EvidenceStorage(mounts_file=str(mounts)), root, db


def _put(path: Path, size: int, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_clip_counts_against_the_cap_and_goes_with_the_still(store, monkeypatch):
    es, root, db = store
    now = 2_000_000_000.0
    te = root / "theft_evidence"
    _put(te / "inc_a.jpg", 100, now - 300)
    _put(te / "inc_b.jpg", 100, now - 200)
    _put(te / "inc_a.mp4", 400, now - 100)      # the clip is written after its still
    _put(te / "inc_b.mp4", 400, now - 50)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO theft_incidents (id, evidence_snapshot_url, evidence_clip_url) VALUES (?,?,?)", [
        (i, f"/api/v1/theft/incidents/{i}/evidence", f"/api/v1/theft/incidents/{i}/clip") for i in ("inc_a", "inc_b")])
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "EVIDENCE_MAX_GB", 10_000 / 1024 ** 3)
    rep = es.enforce(now=now)
    assert rep["by_kind"]["theft_evidence"]["bytes"] == 1000      # clips are counted
    assert rep["by_kind"]["theft_evidence"]["files"] == 4

    # Over the cap by one still: the oldest still goes, and its clip with it.
    monkeypatch.setattr(settings, "EVIDENCE_MAX_GB", 950 / 1024 ** 3)
    rep = es.enforce(now=now)
    assert not (te / "inc_a.jpg").exists() and not (te / "inc_a.mp4").exists()
    assert (te / "inc_b.jpg").exists() and (te / "inc_b.mp4").exists()
    assert rep["deleted"] == 2
    conn = sqlite3.connect(db)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, evidence_snapshot_url, evidence_clip_url, evidence_expired_at IS NOT NULL FROM theft_incidents")}
    conn.close()
    assert rows["inc_a"] == (None, None, 1)
    assert rows["inc_b"][0] and rows["inc_b"][1] and rows["inc_b"][2] == 0


# ------------------------------------------------------------------ patterns

@pytest.fixture
def fresh_db(tmp_path):
    """A private database so the pattern counts are exact."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'patterns.db'}")
    factory = async_sessionmaker(eng, expire_on_commit=False)

    async def _init():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_init())
    yield factory
    asyncio.run(eng.dispose())


def _seed(factory, rows):
    async def _w():
        async with factory() as s:
            for r in rows:
                base = dict(id=f"inc_{uuid.uuid4().hex[:10]}", theft_type=r.get("rule", "CONCEALMENT"),
                            severity="HIGH", status="ACTIVE", department="GENERAL", confidence=0.7,
                            evidence_summary="", items_involved=[], estimated_loss_value=0.0)
                base.update(r)
                s.add(TheftIncidentModel(**base))
            await s.commit()
    asyncio.run(_w())


def _patterns(factory, **kw):
    async def _r():
        async with factory() as s:
            return await get_patterns(s, **kw)
    return asyncio.run(_r())


def _stats(factory):
    async def _r():
        async with factory() as s:
            return await theft_detection_service.get_statistics(s)
    return asyncio.run(_r())


def test_patterns_empty_when_there_are_no_incidents(fresh_db):
    p = _patterns(fresh_db, days=28)
    assert p["summary"]["incidents"] == 0
    assert p["summary"]["false_alarm_rate"] is None
    assert p["hotspots"] == {"cameras": [], "zones": [], "unzoned_incidents": 0}
    assert p["hour_dow"]["matrix"] == [] and p["hour_dow"]["peak"] is None
    assert p["rules"] == [] and p["weekly"] == [] and p["bursts"] == []
    assert "do not identify a person" in p["note"]


def test_patterns_from_seeded_incidents(fresh_db):
    now = utcnow()
    t0 = now - timedelta(hours=3)
    rows = [
        # Burst on cam_a / zone_1: three incidents, each within 10 minutes.
        dict(camera_id="cam_a", camera_name="Aisle 4", zone_id="zone_1", rule="CONCEALMENT",
             timestamp=t0, status="FALSE_ALARM", resolution="FALSE_ALARM", resolved_by="op"),
        dict(camera_id="cam_a", camera_name="Aisle 4", zone_id="zone_1", rule="CONCEALMENT",
             timestamp=t0 + timedelta(minutes=8), status="RESOLVED", resolution="RECOVERED_GOODS", resolved_by="op"),
        dict(camera_id="cam_a", camera_name="Aisle 4", zone_id="zone_1", rule="SHELF_SWEEPING",
             timestamp=t0 + timedelta(minutes=15), status="ACTIVE"),
        # cam_b: two incidents far apart (no burst), one legacy resolution.
        dict(camera_id="cam_b", camera_name="Exit", zone_id=None, rule="SHELF_SWEEPING",
             timestamp=now - timedelta(days=2), status="FALSE_ALARM", resolution="FALSE_ALARM", resolved_by="op"),
        dict(camera_id="cam_b", camera_name="Exit", zone_id=None, rule="SHELF_SWEEPING",
             timestamp=now - timedelta(days=1), status="RESOLVED", resolution="RECOVERED_GOODS", resolved_by=None),
        # Outside the window: ignored.
        dict(camera_id="cam_c", camera_name="Old", zone_id="zone_9", rule="CONCEALMENT",
             timestamp=now - timedelta(days=60), status="ACTIVE"),
    ]
    _seed(fresh_db, rows)
    p = _patterns(fresh_db, days=7, burst_minutes=10, burst_min=3)

    s = p["summary"]
    assert s["incidents"] == 5
    assert s["open"] == 1
    assert s["reviewed"] == 3 and s["false_alarms"] == 2 and s["confirmed"] == 1
    assert s["unverified_legacy"] == 1

    cams = {c["camera_id"]: c for c in p["hotspots"]["cameras"]}
    assert set(cams) == {"cam_a", "cam_b"}
    assert p["hotspots"]["cameras"][0]["camera_id"] == "cam_a"
    assert cams["cam_a"]["incidents"] == 3 and cams["cam_a"]["camera_name"] == "Aisle 4"
    assert cams["cam_a"]["false_alarm_rate"] == 0.5
    assert cams["cam_a"]["top_rule"] == "CONCEALMENT"
    assert cams["cam_b"]["false_alarm_rate"] == 1.0          # the legacy row is not counted
    zones = p["hotspots"]["zones"]
    assert [(z["camera_id"], z["zone_id"], z["incidents"]) for z in zones] == [("cam_a", "zone_1", 3)]
    assert p["hotspots"]["unzoned_incidents"] == 2

    # Rule mix and false-alarm rate per rule, from outcomes.
    rules = {r["rule"]: r for r in p["rules"]}
    assert rules["CONCEALMENT"]["incidents"] == 2
    assert rules["CONCEALMENT"]["reviewed"] == 2 and rules["CONCEALMENT"]["false_alarms"] == 1
    assert rules["CONCEALMENT"]["false_alarm_rate"] == 0.5
    assert rules["SHELF_SWEEPING"]["incidents"] == 3
    assert rules["SHELF_SWEEPING"]["reviewed"] == 1 and rules["SHELF_SWEEPING"]["false_alarm_rate"] == 1.0
    assert rules["CONCEALMENT"]["label"]
    # The same numbers as the statistics endpoint.
    st = {r.rule: r for r in _stats(fresh_db).false_alarm_rate_by_rule}
    assert st["CONCEALMENT"].false_alarm_rate == 0.5
    assert st["SHELF_SWEEPING"].false_alarm_rate == 1.0

    # Hour-of-day x day-of-week in store time.
    m = p["hour_dow"]["matrix"]
    assert len(m) == 7 and all(len(r) == 24 for r in m)
    assert sum(map(sum, m)) == 5
    for r in rows[:5]:
        loc = to_local(r["timestamp"])
        assert m[loc.weekday()][loc.hour] >= 1
    assert p["hour_dow"]["peak"]["incidents"] == p["hour_dow"]["max"] >= 1

    # Weekly trend covers every incident; weeks are consecutive Mondays.
    assert sum(w["incidents"] for w in p["weekly"]) == 5
    starts = [datetime.fromisoformat(w["week_start"]) for w in p["weekly"]]
    assert all(d.weekday() == 0 for d in starts)
    assert all((b - a).days == 7 for a, b in zip(starts, starts[1:]))

    # One burst: cam_a / zone_1, three incidents, no identity claimed.
    assert len(p["bursts"]) == 1
    b = p["bursts"][0]
    assert (b["camera_id"], b["zone_id"], b["incidents"]) == ("cam_a", "zone_1", 3)
    assert b["duration_minutes"] == 15.0
    assert {r["rule"] for r in b["rules"]} == {"CONCEALMENT", "SHELF_SWEEPING"}
    assert len(b["incident_ids"]) == 3
    assert not any(k in b for k in ("person_id", "person", "identity"))

    # A shorter window splits the run: no burst of three.
    assert _patterns(fresh_db, days=7, burst_minutes=5, burst_min=3)["bursts"] == []


def test_patterns_endpoint_shape():
    _insert(zone_id="zone_shape")
    res = _client().get("/api/v1/theft/patterns?days=7")
    assert res.status_code == 200
    body = res.json()
    for key in ("days", "since", "summary", "hotspots", "hour_dow", "rules", "weekly", "bursts", "note"):
        assert key in body
    assert body["days"] == 7
    assert body["summary"]["incidents"] >= 1
    assert len(body["hour_dow"]["matrix"]) == 7
    assert _client().get("/api/v1/theft/patterns?days=0").status_code == 422


def test_an_older_client_does_not_reset_the_new_switches():
    """The mobile app sends only the three toggles; theft_clip and the static filter keep their values."""
    from app.routes.cameras import _keep_unsent_settings

    stored = CameraFeatureConfig(theft_clip=True, static_figure_filter=False, static_figure_seconds=300).model_dump()
    sent = CameraFeatureConfig.model_validate({"people_counting": True, "shelf_interaction": True,
                                               "theft_detection": True})
    merged = sent.model_dump()
    _keep_unsent_settings(merged, sent, stored)
    cfg = CameraFeatureConfig.model_validate(merged)
    assert cfg.theft_clip is True and cfg.static_figure_filter is False and cfg.static_figure_seconds == 300
