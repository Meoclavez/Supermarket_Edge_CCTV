"""Area & line alerts on the dashboard: GET /api/v1/events filters and acknowledgement.

Restricted-area (RESTRICTED_AREA) and tripwire (TRIPWIRE_ALERT) alerts are
stored as security events. The Loss prevention tab lists them through
``/api/v1/events?types=...&acknowledged=...`` and acknowledges them with
``POST /api/v1/events/{id}/acknowledge``; operators may do that. These tests
run with real authentication against the suite's throwaway database.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import AdminUserModel, Base, CameraModel
from app.services import auth_service as auth_mod
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector

CAM = "cam_za_api"
USER_ID = "usr_za_operator"
TYPES = "RESTRICTED_AREA,TRIPWIRE_ALERT"


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _insert_event(conn, eid, etype, ts, *, ack=False, snapshot=None, expired=None, meta=None, severity="HIGH"):
    import json

    conn.execute(
        "INSERT INTO security_events (id, camera_id, camera_name, location, event_type, severity, confidence,"
        " timestamp, clip_url, snapshot_url, bounding_box, keypoints, metadata_json, acknowledged,"
        " acknowledged_at, evidence_expired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, CAM, "Old name", "Back of store", etype, severity, 0.0,
         ts.strftime("%Y-%m-%d %H:%M:%S.%f"), None, snapshot, None, None, json.dumps(meta or {}),
         1 if ack else 0, ts.strftime("%Y-%m-%d %H:%M:%S.%f") if ack else None,
         expired.strftime("%Y-%m-%d %H:%M:%S.%f") if expired else None),
    )


@pytest.fixture
def seeded(monkeypatch):
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    now = datetime.utcnow()
    ids = {k: f"za_{k}_{uuid.uuid4().hex[:6]}" for k in
           ("area_new", "line", "area_acked", "line_expired", "night", "theft")}
    with _db() as conn:
        conn.execute("DELETE FROM security_events WHERE camera_id = ?", (CAM,))
        conn.execute("DELETE FROM cameras WHERE id = ?", (CAM,))
        conn.execute("DELETE FROM admin_users WHERE id = ?", (USER_ID,))
    with Session(eng) as session:   # the ORM fills the column defaults
        session.add(CameraModel(id=CAM, name="Stockroom door", location="Back of store",
                                rtsp_url="rtsp://127.0.0.1:554/za", status="OFFLINE"))
        session.add(AdminUserModel(id=USER_ID, username="za_sam", password_hash="x",
                                   display_name="Sam Operator", role="operator", is_active=True))
        session.commit()
    eng.dispose()
    with _db() as conn:
        _insert_event(conn, ids["area_new"], "RESTRICTED_AREA", now - timedelta(minutes=1),
                      snapshot=f"/api/zones/alerts/{ids['area_new']}/snapshot",
                      meta={"area_name": "Stockroom", "zone_name": "Stockroom", "dwell_seconds": 12.0})
        _insert_event(conn, ids["line"], "TRIPWIRE_ALERT", now - timedelta(minutes=2), severity="WARNING",
                      meta={"tripwire_name": "Entrance", "zone_name": "Entrance", "direction": "in"})
        _insert_event(conn, ids["area_acked"], "RESTRICTED_AREA", now - timedelta(minutes=3), ack=True,
                      meta={"area_name": "Cash office"})
        _insert_event(conn, ids["line_expired"], "TRIPWIRE_ALERT", now - timedelta(minutes=4),
                      expired=now - timedelta(minutes=1), meta={"tripwire_name": "Fire exit", "direction": "out"})
        _insert_event(conn, ids["night"], "NIGHT_INTRUSION", now - timedelta(seconds=30))
        _insert_event(conn, ids["theft"], "THEFT_SUSPECTED", now - timedelta(seconds=20))

    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)
    app.state.setup_completed = True
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    token = auth_service.issue_session_tokens(USER_ID, "operator")["access_token"]
    client = TestClient(app)
    yield client, {"Authorization": f"Bearer {token}"}, ids

    with _db() as conn:
        conn.execute("DELETE FROM security_events WHERE camera_id = ?", (CAM,))
        conn.execute("DELETE FROM cameras WHERE id = ?", (CAM,))
        conn.execute("DELETE FROM admin_users WHERE id = ?", (USER_ID,))
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()


def _mine(body):
    return [e for e in body["events"] if e["camera_id"] == CAM]


def test_types_filter_returns_only_area_and_line_alerts_newest_first(seeded):
    client, h, ids = seeded
    r = client.get(f"/api/v1/events?types={TYPES}&limit=200", headers=h)
    assert r.status_code == 200, r.text
    got = _mine(r.json())
    assert [e["id"] for e in got] == [ids["area_new"], ids["line"], ids["area_acked"], ids["line_expired"]]
    assert {e["event_type"] for e in got} == {"RESTRICTED_AREA", "TRIPWIRE_ALERT"}
    assert r.json()["matching"] >= 4


def test_acknowledged_filter(seeded):
    client, h, ids = seeded
    need = _mine(client.get(f"/api/v1/events?types={TYPES}&acknowledged=false&limit=200", headers=h).json())
    assert [e["id"] for e in need] == [ids["area_new"], ids["line"], ids["line_expired"]]
    done = _mine(client.get(f"/api/v1/events?types={TYPES}&acknowledged=true&limit=200", headers=h).json())
    assert [e["id"] for e in done] == [ids["area_acked"]]


def test_entry_fields_camera_name_zone_store_time_and_evidence(seeded):
    client, h, ids = seeded
    got = {e["id"]: e for e in _mine(client.get(f"/api/v1/events?types={TYPES}&limit=200", headers=h).json())}
    area = got[ids["area_new"]]
    # The camera's current name, not the one stored with the alert, and never the raw id.
    assert area["camera_name"] == "Stockroom door"
    assert area["zone_name"] == "Stockroom"
    assert area["evidence_url"] == f"/api/zones/alerts/{ids['area_new']}/snapshot"
    assert area["evidence_expired_at"] is None
    assert area["store_time"]["label"] and area["store_time"]["date"]
    assert got[ids["line"]]["zone_name"] == "Entrance"
    assert got[ids["line"]]["evidence_url"] is None          # no still was saved
    expired = got[ids["line_expired"]]
    assert expired["evidence_url"] is None and expired["evidence_expired_at"]


def test_evidence_links_are_paths_and_legacy_absolute_links_are_normalised(seeded):
    # Older builds stored EDGE_BASE_URL links (http://localhost:8000/...), which
    # break on a phone, another address or online access.
    client, h, ids = seeded
    snap = "/api/v1/events/snapshots/legacy.jpg?token=abc"
    clip = "/api/v1/events/clips/legacy.mp4?token=def"
    with _db() as conn:
        conn.execute("UPDATE security_events SET snapshot_url = ?, clip_url = ? WHERE id = ?",
                     (f"http://localhost:8000{snap}", f"http://localhost:8000{clip}", ids["line"]))
    got = {e["id"]: e for e in _mine(client.get(f"/api/v1/events?types={TYPES}&limit=200", headers=h).json())}
    line = got[ids["line"]]
    assert line["evidence_url"] == snap                       # dashboard: same-origin path
    # The phone plays clip_url as given: absolute on the address the caller used.
    assert line["snapshot_url"] == f"http://testserver{snap}"
    assert line["clip_url"] == f"http://testserver{clip}"
    other = client.get(f"/api/v1/events/{ids['line']}", headers={**h, "Host": "192.168.1.50:8000"}).json()
    assert other["clip_url"] == f"http://192.168.1.50:8000{clip}" and other["evidence_url"] == snap
    # Paths stored by this build stay paths for the dashboard too.
    assert got[ids["area_new"]]["snapshot_url"] == f"http://testserver/api/zones/alerts/{ids['area_new']}/snapshot"


def test_evidence_path_helper():
    from app.services.evidence_urls import evidence_path

    assert evidence_path("http://localhost:8000/api/v1/events/clips/a.mp4?token=x") == "/api/v1/events/clips/a.mp4?token=x"
    assert evidence_path("https://shop.example/api/v1/theft/incidents/1/evidence") == "/api/v1/theft/incidents/1/evidence"
    assert evidence_path("/api/v1/night-watch/evidence/a.mp4") == "/api/v1/night-watch/evidence/a.mp4"
    assert evidence_path("https://cdn.example/other.jpg") == "https://cdn.example/other.jpg"   # not ours
    assert evidence_path(None) is None and evidence_path("") == ""


def test_snapshot_and_clip_links_are_stored_as_paths(monkeypatch, tmp_path):
    import numpy as np

    from app.services.clip_recorder import clip_recorder_service
    from app.services.live_analytics_engine import live_engine

    monkeypatch.setattr(settings, "SNAPSHOTS_DIR", tmp_path)
    monkeypatch.setattr(live_engine, "get_frame", lambda cam: np.zeros((8, 8, 3), dtype=np.uint8))
    url = clip_recorder_service.save_snapshot("cam_za_path_nobuf", "evt_path")
    assert url and url.startswith("/api/v1/events/snapshots/evt_path.jpg?token="), url


def test_unknown_type_is_rejected(seeded):
    client, h, _ = seeded
    assert client.get("/api/v1/events?types=RESTRICTED_AREA,FALL_DETECTED", headers=h).status_code == 422


def test_not_signed_in_gets_nothing(seeded):
    client, _, ids = seeded
    assert client.get(f"/api/v1/events?types={TYPES}").status_code == 401
    assert client.get(f"/api/v1/events/{ids['area_new']}").status_code == 401
    assert client.post(f"/api/v1/events/{ids['area_new']}/acknowledge").status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert client.get(f"/api/v1/events?types={TYPES}", headers=bad).status_code == 401


def test_camera_scoped_video_token_does_not_open_the_alert_log(seeded):
    client, _, ids = seeded
    stream = auth_service.create_access_token({"sub": "viewer", "type": "stream_access", "camera_id": CAM})
    h = {"Authorization": f"Bearer {stream}"}
    assert client.get(f"/api/v1/events?types={TYPES}", headers=h).status_code == 403
    assert client.post(f"/api/v1/events/{ids['area_new']}/acknowledge", headers=h).status_code == 403


def test_operator_acknowledges_and_who_is_recorded(seeded):
    client, h, ids = seeded
    r = client.post(f"/api/v1/events/{ids['area_new']}/acknowledge", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["acknowledged_by"] == "Sam Operator"
    assert body["acknowledged_at"]

    one = client.get(f"/api/v1/events/{ids['area_new']}", headers=h).json()
    assert one["acknowledged"] is True
    assert one["acknowledged_by"] == "Sam Operator"
    assert one["camera_name"] == "Stockroom door"
    first_at = one["acknowledged_at"]

    # Moves from "Needs attention" to "Acknowledged".
    need = _mine(client.get(f"/api/v1/events?types={TYPES}&acknowledged=false&limit=200", headers=h).json())
    assert ids["area_new"] not in [e["id"] for e in need]
    done = _mine(client.get(f"/api/v1/events?types={TYPES}&acknowledged=true&limit=200", headers=h).json())
    assert ids["area_new"] in [e["id"] for e in done]

    # A second acknowledgement keeps the first time and person.
    again = client.post(f"/api/v1/events/{ids['area_new']}/acknowledge", headers=h).json()
    assert again["acknowledged_by"] == "Sam Operator"
    assert client.get(f"/api/v1/events/{ids['area_new']}", headers=h).json()["acknowledged_at"] == first_at


def test_acknowledge_unknown_alert_is_404(seeded):
    client, h, _ = seeded
    assert client.post("/api/v1/events/za_nope/acknowledge", headers=h).status_code == 404
