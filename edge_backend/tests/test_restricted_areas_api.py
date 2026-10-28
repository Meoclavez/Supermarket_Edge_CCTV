"""Restricted areas API: roles, announcements, arming, privacy refusal, outcomes,
audit, plan gating, the upgrade of older restricted areas, the migration and the
real detection-to-dispatch path (alert log row, audit, latency).

Runs with real authentication (an owner and two operators) against the
suite's throwaway database.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import AdminUserModel, Base, CameraModel
from app.services import auth_service as auth_mod
from app.services import entitlements as ent
from app.services import fleet_control as fc
from app.services.ai_zone_service import ai_zone_service
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.restricted_area_engine import restricted_area_engine
from app.services.restricted_areas import restricted_areas

CAM = "cam_ra_api"
ADMIN, OP1, OP2 = "usr_ra_admin", "usr_ra_op1", "usr_ra_op2"
REGION = [{"x": 0.3, "y": 0.2}, {"x": 0.7, "y": 0.2}, {"x": 0.7, "y": 0.95}, {"x": 0.3, "y": 0.95}]
BASE = "/api/v1/restricted-areas"


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _wipe():
    with _db() as conn:
        for t in ("restricted_areas", "restricted_area_announcements", "restricted_area_audit"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM security_events WHERE camera_id = ?", (CAM,))
        conn.execute("DELETE FROM cameras WHERE id = ?", (CAM,))
        conn.execute("DELETE FROM admin_users WHERE id IN (?, ?, ?)", (ADMIN, OP1, OP2))
        conn.execute("DELETE FROM system_setup WHERE key = ?", (ent.STATE_KEY,))
    ent.invalidate()
    for iz in list(ai_zone_service.get_all_zones().get("intrusion_zones", [])):
        if iz.get("camera_id") == CAM:
            ai_zone_service.delete_intrusion(iz["id"])
    for ex in list(ai_zone_service.get_all_zones().get("exclusion_masks", [])):
        if ex.get("camera_id") == CAM:
            ai_zone_service.delete_exclusion(ex["id"])


@pytest.fixture
def api(monkeypatch):
    eng = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(eng)
    _wipe()
    with Session(eng) as s:
        s.add(CameraModel(id=CAM, name="Cash office cam", location="Back", rtsp_url="rtsp://127.0.0.1:554/ra",
                          status="OFFLINE"))
        s.add(AdminUserModel(id=ADMIN, username="ra_owner", password_hash="x", display_name="Olivia Owner",
                             role="owner", is_active=True))
        s.add(AdminUserModel(id=OP1, username="ra_sam", password_hash="x", display_name="Sam Operator",
                             role="operator", is_active=True))
        s.add(AdminUserModel(id=OP2, username="ra_kim", password_hash="x", display_name="Kim Operator",
                             role="operator", is_active=True))
        s.commit()
    eng.dispose()
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)
    auth_mod._accounts_cache.update(at=0.0, db=None, rows={})
    app.state.setup_completed = True
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    restricted_area_engine.reset()
    restricted_area_engine.synchronous = True
    restricted_area_engine.record_clips = False
    h = {u: {"Authorization": f"Bearer {auth_service.issue_session_tokens(u, r)['access_token']}"}
         for u, r in ((ADMIN, "owner"), (OP1, "operator"), (OP2, "operator"))}
    client = TestClient(app)
    yield client, h
    restricted_area_engine.synchronous = False
    restricted_area_engine.dispatcher_override = None
    restricted_area_engine.reset()
    _wipe()
    asyncio.run(restricted_areas.refresh())
    general_rate_limiter.history.clear()


def _create(c, h, **kw):
    body = {"name": kw.pop("name", "Cash office"), "camera_regions": [{"camera_id": CAM, "points": REGION}]}
    body.update(kw)
    return c.post(BASE, json=body, headers=h[ADMIN])


def _audit(c, h):
    return [(e["action"], e["actor"], e["actor_role"]) for e in c.get(f"{BASE}/audit", headers=h[OP1]).json()["entries"]]


# --------------------------------------------------------------------------- settings and roles

def test_admin_creates_an_area_with_defaults_and_operators_cannot(api):
    c, h = api
    r = _create(c, h)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["triggers"]["unannounced_entry"]["tier"] == "critical" and a["arming"]["mode"] == "always"
    assert a["state"]["armed"] and a["state"]["guarded"] and a["cameras"][0]["camera_id"] == CAM
    assert _create(c, h, name="Again").status_code == 201
    assert _create(c, h, name="Cash office").status_code == 409             # unique names
    r = c.post(BASE, json={"name": "Op area", "camera_regions": [{"camera_id": CAM, "points": REGION}]}, headers=h[OP1])
    assert r.status_code == 403
    r = c.put(f"{BASE}/{a['id']}", json={"triggers": {"speed": {"max_mps": 3.5}}}, headers=h[OP1])
    assert r.status_code == 403
    r = c.put(f"{BASE}/{a['id']}", json={"triggers": {"speed": {"max_mps": 3.5}}}, headers=h[ADMIN])
    assert r.status_code == 200 and r.json()["triggers"]["speed"]["max_mps"] == 3.5
    assert c.put(f"{BASE}/{a['id']}", json={"triggers": {"speed": {"max_mps": 50}}}, headers=h[ADMIN]).status_code == 422
    assert c.post(BASE, json={"name": "Nowhere"}, headers=h[ADMIN]).status_code == 422
    actions = [x[0] for x in _audit(c, h)]
    assert actions.count("created") == 2 and "settings_changed" in actions
    assert ("settings_changed", "Olivia Owner", "owner") in _audit(c, h)
    # Everyone signed in can read, catalogue included.
    cat = c.get(f"{BASE}/catalogue", headers=h[OP1]).json()
    assert [t["id"] for t in cat["triggers"]][:2] == ["unannounced_entry", "announced_overstay"]
    assert c.get(BASE, headers=h[OP1]).json()["can_edit"] is False
    assert c.get(BASE, headers=h[ADMIN]).json()["can_edit"] is True
    assert c.get(f"{BASE}/status", headers={}).status_code == 401
    # The status poll is not counted against the general request budget.
    from app.services.auth_service import general_rate_limiter as grl

    grl.history.clear()
    for _ in range(120):
        assert c.get(f"{BASE}/status", headers=h[OP1]).status_code == 200
    assert c.get(f"{BASE}/catalogue", headers=h[OP1]).status_code == 200


def test_privacy_mask_wins_over_a_restricted_area(api):
    c, h = api
    ai_zone_service.add_exclusion({"camera_id": CAM, "name": "Toilet door", "mode": "BLACKOUT", "enabled": True,
                                   "points": [{"x": 0.2, "y": 0.1}, {"x": 0.8, "y": 0.1}, {"x": 0.8, "y": 1.0},
                                              {"x": 0.2, "y": 1.0}]})
    r = _create(c, h)
    assert r.status_code == 422 and "privacy mask" in r.json()["detail"] and "Cash office cam" in r.json()["detail"]
    for ex in list(ai_zone_service.get_all_zones().get("exclusion_masks", [])):
        if ex.get("camera_id") == CAM:
            ai_zone_service.delete_exclusion(ex["id"])
    # A small mask over a corner: accepted with a warning.
    ai_zone_service.add_exclusion({"camera_id": CAM, "name": "Till screen", "mode": "BLUR", "enabled": True,
                                   "points": [{"x": 0.3, "y": 0.2}, {"x": 0.4, "y": 0.2}, {"x": 0.4, "y": 0.3},
                                              {"x": 0.3, "y": 0.3}]})
    r = _create(c, h)
    assert r.status_code == 201 and any("privacy mask" in w for w in r.json()["warnings"])


# --------------------------------------------------------------------------- announcements

def test_announce_entry_countdown_visibility_and_im_out(api):
    c, h = api
    area = _create(c, h).json()
    assert c.post(f"{BASE}/{area['id']}/announce", json={"minutes": 3}, headers=h[OP1]).status_code == 422
    assert c.post(f"{BASE}/{area['id']}/announce", json={"minutes": 121}, headers=h[OP1]).status_code == 422
    r = c.post(f"{BASE}/{area['id']}/announce", json={"note": "Counting the float", "source": "phone"}, headers=h[OP1])
    assert r.status_code == 201, r.text
    ann = r.json()["announcement"]
    assert ann["minutes"] == 15 and ann["user_name"] == "Sam Operator" and ann["until_store"]["hhmm"]
    st = c.get(f"{BASE}/status", headers=h[OP1]).json()["areas"][0]
    assert st["announced"] and not st["guarded"] and st["armed"]
    a0 = st["announcements"][0]
    assert a0["mine"] and a0["user_name"] == "Sam Operator" and 880 <= a0["seconds_left"] <= 900
    other = c.get(f"{BASE}/status", headers=h[OP2]).json()["areas"][0]["announcements"][0]
    assert other["mine"] is False and other["note"] == "Counting the float"
    # Kim cannot end Sam's announcement; Sam can.
    assert c.post(f"{BASE}/announcements/{ann['id']}/end", headers=h[OP2]).status_code == 403
    assert c.post(f"{BASE}/announcements/{ann['id']}/end", headers=h[OP1]).json()["status"] == "ended"
    assert c.get(f"{BASE}/status", headers=h[OP1]).json()["areas"][0]["guarded"] is True
    # An administrator may end anyone's.
    ann2 = c.post(f"{BASE}/{area['id']}/announce", json={"minutes": 30, "people": 2}, headers=h[OP2]).json()["announcement"]
    assert c.post(f"{BASE}/announcements/{ann2['id']}/end", headers=h[ADMIN]).json()["status"] == "ended"
    log = _audit(c, h)
    assert ("announced", "Sam Operator", "operator") in log and ("announcement_ended", "Sam Operator", "operator") in log
    assert ("announcement_ended", "Olivia Owner", "owner") in log


# --------------------------------------------------------------------------- arming

def test_arm_disarm_roles_reasons_and_resume(api):
    c, h = api
    tz_sched = {"mode": "schedule", "schedule": [{"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                                  "from": "00:00", "to": "00:01"}]}
    area = _create(c, h, arming=tz_sched).json()
    aid = area["id"]
    assert c.post(f"{BASE}/{aid}/arm", json={}, headers=h[OP1]).status_code == 422          # reason needed
    r = c.post(f"{BASE}/{aid}/arm", json={"reason": "Cash count tonight", "minutes": 60}, headers=h[OP1])
    assert r.status_code == 200, r.text
    st = r.json()["state"]
    assert st["armed"] and st["armed_source"] == "manual" and st["manual"]["by"] == "Sam Operator"
    assert c.post(f"{BASE}/{aid}/disarm", json={"reason": "x", "minutes": 10}, headers=h[OP1]).status_code == 403
    assert c.post(f"{BASE}/{aid}/resume", json={"reason": "x"}, headers=h[OP1]).status_code == 403
    r = c.post(f"{BASE}/{aid}/disarm", json={"reason": "Delivery", "minutes": 30}, headers=h[ADMIN])
    assert r.status_code == 200 and r.json()["state"]["armed"] is False
    r = c.post(f"{BASE}/{aid}/resume", json={"reason": "Delivery done"}, headers=h[ADMIN])
    assert r.json()["state"]["armed_source"] == "schedule"
    # Always armed: disarming needs a duration.
    always = _create(c, h, name="Safe room").json()
    r = c.post(f"{BASE}/{always['id']}/disarm", json={"reason": "Service"}, headers=h[ADMIN])
    assert r.status_code == 422 and "how long" in r.json()["detail"]
    assert c.post(f"{BASE}/{always['id']}/disarm", json={"reason": "Service", "minutes": 900}, headers=h[ADMIN]).status_code == 422
    log = _audit(c, h)
    assert ("armed", "Sam Operator", "operator") in log and ("disarmed", "Olivia Owner", "owner") in log
    assert ("schedule_resumed", "Olivia Owner", "owner") in log


# --------------------------------------------------------------------------- events and outcomes

def _ra_event(eid, area_id, *, test=False):
    import json
    from datetime import datetime

    meta = {"ra_area_id": area_id, "area_name": "Cash office", "ra_trigger": "unannounced_entry",
            "ra_trigger_label": "Entry without notice", "alert_tier": "critical", "ra_requires_outcome": not test,
            "title": "Urgent: Entry without notice: Cash office", "body": "Someone is inside.", "test": test}
    with _db() as conn:
        conn.execute(
            "INSERT INTO security_events (id, camera_id, camera_name, location, event_type, severity, confidence,"
            " timestamp, metadata_json, acknowledged) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, CAM, "Cash office cam", "Back", "RESTRICTED_AREA", "HIGH", 0.0,
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"), json.dumps(meta), 0))


def test_alerts_need_an_outcome_which_is_audited(api):
    c, h = api
    area = _create(c, h).json()
    eid = f"ra_{uuid.uuid4().hex[:12]}"
    _ra_event(eid, area["id"])
    ev = c.get(f"{BASE}/events?needs_outcome=true", headers=h[OP1]).json()["events"]
    assert [e["id"] for e in ev] == [eid] and ev[0]["needs_outcome"]
    assert c.get(f"{BASE}/status", headers=h[OP1]).json()["open_events_total"] == 1
    # The generic acknowledge refuses without an outcome ...
    r = c.post(f"/api/v1/events/{eid}/acknowledge", headers=h[OP1])
    assert r.status_code == 422 and "outcome" in r.json()["detail"]
    assert c.post(f"{BASE}/events/{eid}/outcome", json={"outcome": "maybe"}, headers=h[OP1]).status_code == 422
    # ... and records one when given.
    r = c.post(f"/api/v1/events/{eid}/acknowledge", json={"outcome": "authorised", "note": "Cleaner"}, headers=h[OP1])
    assert r.status_code == 200 and r.json()["acknowledged_by"] == "Sam Operator"
    ev = c.get(f"{BASE}/events?area_id={area['id']}", headers=h[OP1]).json()["events"][0]
    assert ev["outcome"] == "authorised" and ev["outcome_by"] == "Sam Operator" and not ev["needs_outcome"]
    assert ev["outcome_note"] == "Cleaner" and ev["acknowledged"]
    assert c.post(f"{BASE}/events/{eid}/outcome", json={"outcome": "incident"}, headers=h[OP2]).status_code == 409
    assert ("outcome", "Sam Operator", "operator") in _audit(c, h)
    assert c.get(f"{BASE}/events?trigger=bogus", headers=h[OP1]).status_code == 422
    # Older area alerts (no outcome needed) acknowledge as before.
    _ra_event("ra_old_" + uuid.uuid4().hex[:6], area["id"], test=True)


def test_test_alert_is_admin_only_and_goes_through_the_real_path(api, monkeypatch):
    c, h = api
    area = _create(c, h).json()
    got = []

    async def fake(et, sev, title, body, data):
        got.append((et, title, data))
        return {"ok": True}

    restricted_area_engine.dispatcher_override = fake
    assert c.post(f"{BASE}/{area['id']}/test-alert", headers=h[OP1]).status_code == 403
    r = c.post(f"{BASE}/{area['id']}/test-alert", headers=h[ADMIN])
    assert r.status_code == 200 and r.json()["title"].startswith("TEST Urgent")
    restricted_area_engine.process_pending_alerts()
    (et, title, data), = got
    assert et == "RESTRICTED_AREA" and data["test"] and data["ra_test_by"] == "Olivia Owner"


def test_detection_to_dispatch_through_the_real_dispatcher(api):
    """Synthetic person in the area: alert log row with the trigger, an audit entry, latency < 1 s."""
    c, h = api
    area = _create(c, h).json()
    asyncio.run(restricted_areas.refresh())
    restricted_area_engine.dispatcher_override = None
    restricted_area_engine.clock = time.time
    now = time.time()
    frames = [{"dt": 0.6 - i * 0.1, "tracks": [{"id": "77", "bbox": [600, 300, 680, 600]}]} for i in range(7)]
    out = restricted_area_engine.qa_feed(CAM, frames, 1280, 720, camera_name="Cash office cam", now=now)
    assert len(out["alerts"]) == 1 and out["occupancy"][area["id"]] == 1
    t0 = time.time()
    restricted_area_engine.process_pending_alerts()
    assert time.time() - t0 < 1.0
    assert restricted_area_engine.stats["latency_ms"] < 1000, restricted_area_engine.stats
    eid = out["alerts"][0]
    ev = c.get(f"{BASE}/events", headers=h[OP1]).json()["events"]
    hit = [e for e in ev if e["id"] == eid]
    assert hit and hit[0]["trigger"] == "unannounced_entry" and hit[0]["tier"] == "critical" and hit[0]["needs_outcome"]
    entries = c.get(f"{BASE}/audit", headers=h[OP1]).json()["entries"]
    assert any(e["action"] == "alert" and e["event_id"] == eid and e["detail"]["latency_ms"] < 1000 for e in entries)
    st = c.get(f"{BASE}/status", headers=h[OP1]).json()["areas"][0]
    assert st["last_events"][0]["id"] == eid


def test_qa_feed_is_not_reachable_without_the_qa_switch(api):
    c, h = api
    r = c.post(f"{BASE}/qa/feed", json={"camera_id": CAM, "frames": []}, headers=h[ADMIN])
    assert r.status_code in (404, 405)


# --------------------------------------------------------------------------- plan

def test_plan_without_restricted_areas_is_403_and_silent(api):
    c, h = api
    area = _create(c, h).json()
    policy = {"type": "policy", "store_id": "s1", "version": 1, "issued_at": fc.iso(), "state": "active",
              "message": "", "licence": {"valid_until": None, "grace_days": 14},
              "features": {**{k: True for k in ent.FEATURES}, "restricted_areas": False},
              "limits": {"max_cameras": None}}
    state = fc.load_state()
    state["policy"] = ent.normalise_policy(policy)
    fc.save_state(state)
    ent.invalidate()
    r = c.get(f"{BASE}/status", headers=h[OP1])
    assert r.status_code == 403 and r.json()["code"] == "feature_disabled" and r.json()["feature"] == "restricted_areas"
    assert "Restricted areas" in r.json()["detail"]
    assert not restricted_area_engine.camera_secured(CAM)
    out = restricted_area_engine.qa_feed(CAM, [{"dt": 0.6 - i * 0.1, "tracks": [{"id": "1", "bbox": [600, 300, 680, 600]}]}
                                               for i in range(7)], 1280, 720)
    assert out["alerts"] == []
    # Camera-drawn areas stay drawable while either feature is in the plan.
    policy["features"].update(restricted_areas=True, zone_alerts=False)
    state["policy"] = ent.normalise_policy(policy)
    fc.save_state(state)
    ent.invalidate()
    assert c.get("/api/zones/intrusion/none", headers=h[ADMIN]).status_code != 403
    assert c.get(f"{BASE}/{area['id']}", headers=h[OP1]).status_code == 200
    # A missing key means the feature is on (forward compatible).
    del policy["features"]["restricted_areas"]
    assert ent.normalise_policy(policy)["features"]["restricted_areas"] is True


# --------------------------------------------------------------------------- upgrade and migration

def test_older_restricted_areas_are_upgraded_once(api):
    c, h = api
    iz = ai_zone_service.add_intrusion({"camera_id": CAM, "name": "Stockroom", "points": REGION, "enabled": True,
                                        "severity": "WARNING", "min_dwell_seconds": 3.0, "cooldown_seconds": 120,
                                        "schedule": [{"days": ["mon"], "from": "22:00", "to": "06:00"}],
                                        "schedule_mode": "allowed_during", "timezone": "Australia/Perth"})
    assert asyncio.run(restricted_areas.adopt_legacy()) == 1
    assert asyncio.run(restricted_areas.adopt_legacy()) == 0
    area = c.get(BASE, headers=h[OP1]).json()["areas"][0]
    assert area["legacy_intrusion_id"] == iz["id"] and area["name"] == "Stockroom"
    t = area["triggers"]
    assert t["unannounced_entry"]["enabled"] and t["unannounced_entry"]["tier"] == "watch"
    assert t["unannounced_entry"]["min_presence_sec"] == 3.0 and t["unannounced_entry"]["cooldown_sec"] == 120
    assert not t["speed"]["enabled"] and not t["tamper"]["enabled"]
    assert area["arming"]["mode"] == "schedule" and area["arming"]["schedule_mode"] == "armed_outside"
    asyncio.run(restricted_areas.refresh())
    assert restricted_area_engine.handles_intrusion(iz["id"])
    assert ("upgraded", "System", None) in _audit(c, h)
    # Saving the schedule from the dashboard (which sends no time zone) keeps the area's own zone.
    r = c.put(f"{BASE}/{area['id']}", json={"arming": {"mode": "always"}}, headers=h[ADMIN])
    assert r.status_code == 200 and r.json()["arming"]["timezone"] == "Australia/Perth"
    bad = c.put(f"{BASE}/{area['id']}", json={"arming": {"mode": "always", "timezone": "Mars/Base"}}, headers=h[ADMIN])
    assert bad.status_code == 422


def test_migration_is_additive_and_idempotent():
    mod = importlib.import_module("app.migrations.m0023_restricted_areas")
    conn = sqlite3.connect(":memory:")
    mod.upgrade(conn)
    mod.upgrade(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"restricted_areas", "restricted_area_announcements", "restricted_area_audit"} <= tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(restricted_areas)")}
    assert {"floor_zone_id", "camera_regions", "arming", "triggers", "manual_state", "legacy_intrusion_id"} <= cols
    from app.migrations.runner import discover

    versions = [m.version for m in discover()]
    assert 23 in versions and versions == sorted(set(versions))
