"""GET /api/v1/features/status: the Features tab's one read of every feature's state.

* every card id the dashboard shows (static/js/features.js) has a state, and only
  on / off / setup / plan_off / unknown;
* it follows the real data (cameras, shelves, night watch, store name) and never writes;
* a feature the iKorex plan leaves out is plan_off and nothing else, the route
  itself stays open; while the service is paused it answers 423 like its neighbours;
* it needs a signed-in person; operators get the state of owner/administrator-only
  screens without their detail.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import Base, CameraModel
from app.routes import features as feat
from app.services import entitlements as ent
from app.services import fleet_control as fc
from app.services.auth_service import general_rate_limiter

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
STATES = {"on", "off", "setup", "plan_off", "unknown"}
STORE = "teststore"


def _store_policy(**kw) -> None:
    policy = {"type": "policy", "store_id": STORE, "version": 1, "issued_at": fc.iso(), "state": "active",
              "message": "", "licence": {"valid_until": None, "grace_days": 14},
              "features": {k: True for k in ent.FEATURES}, "limits": {"max_cameras": None}}
    policy.update(kw)
    state = fc.load_state()
    state["policy"] = ent.normalise_policy(policy)
    fc.save_state(state)


def _wipe_policy() -> None:
    import sqlite3

    with sqlite3.connect(str(settings.DATABASE_PATH), timeout=15) as conn:
        conn.execute("DELETE FROM system_setup WHERE key = ?", (ent.STATE_KEY,))
    ent.invalidate()


@pytest.fixture(autouse=True)
def _clean():
    engine = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(engine)
    engine.dispose()
    _wipe_policy()
    general_rate_limiter.history.clear()
    yield
    _wipe_policy()


def _camera(**kw) -> str:
    cid = f"feat_cam_{uuid.uuid4().hex[:6]}"

    async def _add():
        async with async_session_factory() as s:
            s.add(CameraModel(id=cid, name=kw.pop("name", "Aisle 3"), location="test", rtsp_url="rtsp://x/1",
                              is_ai_enabled=True, features=kw.pop("features", {}), **kw))
            await s.commit()

    asyncio.run(_add())
    return cid


def _drop(cid: str) -> None:
    async def _rm():
        async with async_session_factory() as s:
            await s.execute(delete(CameraModel).where(CameraModel.id == cid))
            await s.commit()

    asyncio.run(_rm())


def _status(c=None, headers=None) -> dict:
    r = (c or TestClient(app)).get("/api/v1/features/status", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def test_every_dashboard_card_has_a_known_state():
    body = _status()
    js = (STATIC / "js" / "features.js").read_text(encoding="utf-8")
    card_ids = set(re.findall(r"^\s{4}\{\s*id: '([a-z-]+)', group:", js, flags=re.M))
    assert len(card_ids) >= 25, card_ids
    assert card_ids == set(body["features"]) == set(feat.STATES)
    for fid, st in body["features"].items():
        assert st["state"] in STATES, (fid, st)
        assert isinstance(st["detail"], str)
    assert set(body["plan"]) == set(ent.FEATURES) and all(body["plan"].values())
    assert {"first_camera", "first_without_purpose", "first_to_calibrate", "first_night_watch"} <= set(body["links"])


def test_every_card_has_its_guide_and_a_known_full_settings_target():
    js = (STATIC / "js" / "features.js").read_text(encoding="utf-8")
    help_js = (STATIC / "js" / "help_content.js").read_text(encoding="utf-8")
    go_js = (STATIC / "js" / "help.js").read_text(encoding="utf-8")
    for fid in feat.STATES:
        art = re.search(r"id: 'fs-%s', feature: '%s'.*?steps: \[(.*?)\],\n" % (fid, fid), help_js, re.S)
        assert art, f"help_content.js has no Easy setup guide fs-{fid}"
        steps = re.findall(r"^\s*'(?:[^'\\]|\\.)*',?$", art.group(1), re.M) or art.group(1).count("',") + 1
        assert 2 <= (len(steps) if isinstance(steps, list) else steps) <= 5, fid
    assert "id: 'features-tab'" in help_js and "'features-tab': {" in help_js
    # Every Full settings target uses an action help.js go() knows.
    targets = set(re.findall(r"full: \{ go: '([a-z:-]+)", js))
    prefixes = {t.split(":")[0] + (":" if ":" in t else "") for t in targets}
    for pre in prefixes:
        assert f"'{pre}" in go_js or f"case '{pre}'" in go_js, pre
    for helper in ("studio(", "camLink('night:", "camLink('camera:", "camLink('calibrate:"):
        assert helper in js
    assert "a.startsWith('studio-url:')" in go_js and "a.startsWith('calibrate:')" in go_js


def test_states_follow_the_cameras_night_watch_and_store_settings(monkeypatch):
    # Other tests leave cameras in the shared test database: this camera's name sorts first.
    cid = _camera(name="!Feat front door", features={"theft_detection": True, "people_counting": True,
                                                     "night_watch": {"enabled": True, "start": "21:30", "end": "06:00"}})
    try:
        body = _status()
        nw = body["features"]["night-watch"]
        assert nw["state"] == "on" and "21:30-06:00" in nw["detail"] and "!Feat front door" in nw["detail"]
        purpose = body["features"]["camera-purpose"]
        assert purpose["state"] == "setup" and "!Feat front door" in purpose["detail"]
        assert body["links"]["first_camera"] == cid and body["links"]["first_night_watch"] == cid
        from app.services.shelf_interaction_service import shelf_interaction_service

        monkeypatch.setattr(shelf_interaction_service, "get_zones", lambda camera_id=None: [])
        theft = _status()["features"]["theft-alerts"]
        assert theft["state"] == "setup" and "product shelves" in theft["detail"]
    finally:
        _drop(cid)

    from app.services import site_settings as ss

    monkeypatch.setattr(ss.site_settings, "effective",
                        lambda k: {"STORE_NAME": "Pearcedale IGA", "SITE_TIMEZONE": "Australia/Melbourne"}.get(k))
    assert _status()["features"]["store-settings"] == {"state": "on", "detail": "Pearcedale IGA · Australia/Melbourne"}


def _cam_row(**kw):
    from app.models.schemas import CameraFeatureConfig

    row = {"id": "c1", "name": "Aisle", "on": True, "role": None, "online": True, "calibrated": False,
           "watch_zone_id": None, "placed": True, "features": CameraFeatureConfig()}
    row.update(kw)
    return row


def test_state_rules_on_plain_data():
    from app.models.schemas import CameraFeatureConfig

    base = {"cameras": [], "zone_ids": set(), "structures": 0, "tripwires": [], "restricted": [], "queue_areas": [],
            "privacy_masks": 0, "ignore_areas": 0, "shelves": 3, "accounts": [], "remote": {"enabled": False}}
    assert feat._theft(base)["state"] == "setup"                                   # no cameras yet
    off = dict(base, cameras=[_cam_row(features=CameraFeatureConfig(theft_detection=False))])
    assert feat._theft(off)["state"] == "off"
    on = dict(base, cameras=[_cam_row()])
    assert feat._theft(on)["state"] == "on"
    assert feat._cameras(dict(base, cameras=[_cam_row(online=False)]))["state"] == "setup"
    assert feat._cameras(dict(base, cameras=[_cam_row(on=False, online=False)]))["state"] == "on"   # off is not a fault
    assert feat._footfall(dict(on, tripwires=[{"counts_footfall": True}]))["state"] == "on"
    assert feat._footfall(on)["state"] == "setup"
    assert feat._area_lines(dict(base, tripwires=[{"alert_enabled": True}]))["state"] == "on"
    assert feat._placement(dict(base, cameras=[_cam_row(watch_zone_id="z1")], zone_ids={"z1"}))["state"] == "on"
    assert feat._placement(dict(base, cameras=[_cam_row(placed=False)]))["state"] == "setup"
    assert feat._online(dict(base, remote={"enabled": True, "connected": False}))["state"] == "setup"
    assert feat._backups({"backups": [{"tag": "startup"}]})["state"] == "setup"
    assert feat._backups({"backups": [{"tag": "manual"}]})["state"] == "on"


def test_features_left_out_of_the_plan_are_plan_off_and_nothing_else():
    _store_policy(features={**{k: True for k in ent.FEATURES}, "theft_detection": False, "phone_alerts": False,
                            "heatmaps": False})
    body = _status()
    f = body["features"]
    for fid in ("theft-alerts", "alert-levels", "phone-alerts", "phone-app", "heatmaps"):
        assert f[fid] == {"state": "plan_off", "detail": ""}, fid
    assert f["night-watch"]["state"] != "plan_off" and f["cameras"]["state"] != "plan_off"
    assert body["plan"]["theft_detection"] is False and body["plan"]["night_watch"] is True
    # The route is not itself behind a plan feature.
    assert ent.feature_for_path("/api/v1/features/status") is None


def test_paused_service_answers_423_like_its_neighbours():
    _store_policy(state="suspended", message="Account on hold")
    c = TestClient(app)
    r = c.get("/api/v1/features/status")
    assert r.status_code == 423 and r.json()["code"] == "service_paused"
    assert c.get("/api/v1/cameras").status_code == 423


def test_the_route_is_signed_in_only_and_rate_limited():
    calls = [d.dependency for d in feat.router.dependencies]
    from app.services.auth_service import auth_service

    assert auth_service.verify_api_access in calls and general_rate_limiter in calls
    paths = {(r.path, tuple(sorted(r.methods))) for r in feat.router.routes}
    assert paths == {("/api/v1/features/status", ("GET",))}
    assert "/api/v1/features/status" in app.openapi()["paths"]   # so the "GET never writes" sweep covers it


# ------------------------------------------------------------------- real auth

from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: E402,F401  (fixture)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_needs_sign_in_and_operators_get_no_admin_detail(client):
    assert client.get("/api/v1/features/status").status_code == 401
    owner = _create_admin(client, _issued_code(client), password="correct horse 1").json()["access_token"]
    body = _status(client, _h(owner))
    assert body["admin"] is True
    assert body["features"]["accounts"]["state"] == "setup" and "one account" in body["features"]["accounts"]["detail"]
    r = client.post("/api/v1/users", headers=_h(owner), json={
        "username": "sam", "display_name": "Sam", "role": "operator", "password": "staff password 1"})
    assert r.status_code == 201, r.text
    op = client.post("/api/v1/auth/login", json={"username": "sam", "password": "staff password 1"}).json()["access_token"]
    ob = _status(client, _h(op))
    assert ob["admin"] is False
    assert ob["features"]["accounts"] == {"state": "on", "detail": ""}
    for fid in feat.ADMIN_DETAIL:
        assert ob["features"][fid]["detail"] == "", fid
    assert ob["features"]["store-map"]["detail"]   # everyday status keeps its words
    # Reading it is not a setup change: the operator is not refused.
    assert client.get("/api/v1/features/status", headers=_h(op)).status_code == 200
