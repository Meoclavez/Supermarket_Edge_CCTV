"""Theft alert tiers: risk score and factors, thresholds, combinations, role floor,
routing per tier (push and escalation mocked), migration m0019, API fields and
filters, /theft/alert-policy and per-tier statistics.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import Base, TheftIncidentModel
from app.services import camera_roles as cr
from app.services import push_alerts as pa_mod
from app.services import site_settings as ss
from app.services import theft_alert_policy as tp
from app.services.alert_dispatcher import alert_dispatcher

AISLE = {"id": "z_aisle", "name": "Biscuits", "category": "SNACKS", "price": 3.0, "high_value": False,
         "value_tier": "STANDARD"}
SPIRITS = {"id": "z_spirits", "name": "Spirits", "category": "SPIRITS", "price": 45.0, "high_value": True,
           "value_tier": "PREMIUM"}
CHEAP = {"id": "z_cheap", "name": "Gum", "category": "CONFECTIONERY", "price": 1.0, "high_value": False,
         "value_tier": "LOW"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _factors(a):
    return {f["factor"]: f for f in a["risk_factors"]}


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Every tier setting at its .env default, whatever another test saved."""
    for spec in ss.SPECS:
        if spec.group == "theft_alerts":
            monkeypatch.setattr(settings, spec.key, ss.ENV_DEFAULTS[spec.key])
    cr.role_cache.set_override(None)
    yield


# --------------------------------------------------------------------------- factor math

def test_defaults_are_the_documented_ones():
    assert tp.thresholds() == {"watch": 0.35, "alert": 0.55, "critical": 0.80}
    assert tp.case_for("CONCEALMENT", "pocket")["weight"] == 1.0
    assert tp.case_for("CONCEALMENT", "chest")["weight"] == 1.0
    assert tp.case_for("CONCEALMENT", "behind_back")["weight"] == 0.85
    assert tp.case_for("SHELF_SWEEPING")["weight"] == 0.9
    assert tp.case_for("EXIT_WITHOUT_CHECKOUT")["weight"] == 0.9
    assert tp.case_for("SUSPICIOUS_LOITERING")["weight"] == 0.55
    assert tp.case_for("BEHAVIOUR_PATTERN")["weight"] == 0.65
    assert tp.case_for("SWEETHEARTING")["setting"] is None          # not wired: neutral weight


def test_risk_is_confidence_times_place_times_case_with_reasons():
    a = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=AISLE)
    assert a["risk_score"] == pytest.approx(0.45) and a["alert_tier"] == "watch" and a["severity"] == "MEDIUM"
    f = _factors(a)
    assert f["confidence"]["value"] == 0.45 and "45%" in f["confidence"]["reason"]
    assert f["case"]["value"] == 1.0 and f["case"]["setting"] == "THEFT_CASE_WEIGHT_CONCEALMENT"
    assert f["place_standard_value"]["value"] == 1.0 and "Biscuits" in f["place_standard_value"]["reason"]
    assert f["level_from_score"]["value"] == "watch"
    assert all({"factor", "value", "effect", "reason"} <= set(x) for x in a["risk_factors"])

    hv = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=SPIRITS)
    assert hv["risk_score"] == pytest.approx(0.45 * 1.25, abs=1e-3) and hv["alert_tier"] == "alert"
    assert "SPIRITS" in _factors(hv)["place_high_value"]["reason"]
    assert hv["severity"] == "HIGH" and hv["event_severity"] == "HIGH"

    low = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=CHEAP)
    assert low["risk_score"] == pytest.approx(0.45 * 0.85, abs=1e-3) and low["alert_tier"] == "watch"

    back = tp.assess("CONCEALMENT", 0.45, target="behind_back", zone=SPIRITS)
    assert back["risk_score"] == pytest.approx(0.45 * 1.25 * 0.85, abs=1e-3) and back["alert_tier"] == "watch"


def test_exit_zone_door_camera_and_place_cap():
    at_exit = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=AISLE, floor_category="EXIT")
    assert _factors(at_exit)["place_exit_zone"]["value"] == 1.25 and at_exit["alert_tier"] == "alert"

    # Door camera and exit zone describe the same place: only the larger counts.
    both = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=AISLE, floor_category="ENTRANCE", role="exit")
    f = _factors(both)
    assert "place_exit_zone" in f and "place_door_camera" not in f
    door = tp.assess("CONCEALMENT", 0.45, target="pocket", zone=AISLE, role="entrance_exit")
    assert _factors(door)["place_door_camera"]["value"] == 1.15

    checkout = tp.assess("SHELF_SWEEPING", 0.5, zone=AISLE, role="checkout")
    assert _factors(checkout)["place_checkout_camera"]["value"] == pytest.approx(1.1)
    assert checkout["risk_score"] == pytest.approx(0.5 * 1.1 * 0.9, abs=1e-3)

    capped = tp.assess("CONCEALMENT", 0.5, target="pocket", zone=SPIRITS, floor_category="EXIT")
    assert "place_cap" in _factors(capped)
    assert capped["risk_score"] == pytest.approx(0.5 * tp.PLACE_WEIGHT_CAP, abs=1e-3)

    # The exit rule is about the exit: no exit place weight on top; the
    # product weight comes from what the person picked up.
    ex = tp.assess("EXIT_WITHOUT_CHECKOUT", 0.5, reached_zones=[AISLE, SPIRITS], floor_category="EXIT")
    f = _factors(ex)
    assert "place_exit_zone" not in f and "Picked up from 'Spirits'" in f["place_high_value"]["reason"]


def test_weights_follow_the_site_settings(monkeypatch):
    monkeypatch.setattr(settings, "THEFT_CASE_WEIGHT_LOITERING", 1.0)
    monkeypatch.setattr(settings, "THEFT_PLACE_WEIGHT_HIGH_VALUE", 1.0)
    a = tp.assess("SUSPICIOUS_LOITERING", 0.6, zone=SPIRITS)
    assert a["risk_score"] == pytest.approx(0.6) and a["alert_tier"] == "alert"


# --------------------------------------------------------------------------- thresholds

def test_threshold_boundaries_and_bad_order(monkeypatch):
    assert tp.tier_for_score(0.0) == "review"
    assert tp.tier_for_score(0.349) == "review"
    assert tp.tier_for_score(0.35) == "watch"
    assert tp.tier_for_score(0.55) == "alert"
    assert tp.tier_for_score(0.8) == "critical"
    assert tp.tier_for_score(1.7) == "critical"
    monkeypatch.setattr(settings, "THEFT_TIER_WATCH_MIN", 0.2)
    monkeypatch.setattr(settings, "THEFT_TIER_ALERT_MIN", 0.4)
    monkeypatch.setattr(settings, "THEFT_TIER_CRITICAL_MIN", 0.6)
    assert tp.tier_for_score(0.45) == "alert"
    monkeypatch.setattr(settings, "THEFT_TIER_ALERT_MIN", 0.1)       # out of order: defaults
    assert tp.thresholds() == tp.DEFAULT_THRESHOLDS


def test_site_settings_group_and_rising_levels(tmp_path, monkeypatch):
    for spec in ss.SPECS:
        monkeypatch.setattr(settings, spec.key, getattr(settings, spec.key))
    db = tmp_path / "site.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE system_setup (key VARCHAR PRIMARY KEY, value TEXT, updated_at TEXT)")
    store = ss.SiteSettingsStore(db_path_fn=lambda: db)
    store.load()
    keys = ss.GROUP_KEYS["theft_alerts"]
    assert {"THEFT_TIER_WATCH_MIN", "THEFT_TIER_ALERT_MIN", "THEFT_TIER_CRITICAL_MIN", "THEFT_ROUTE_WATCH_PUSH",
            "THEFT_ROUTE_ALERT_PUSH", "THEFT_CASE_WEIGHT_PATTERN", "THEFT_PLACE_WEIGHT_HIGH_VALUE",
            "THEFT_COMBO_WINDOW_SEC", "THEFT_BURST_MIN_INCIDENTS"} <= set(keys)
    d = store.describe()
    assert d["group_labels"]["theft_alerts"] == "Theft alert levels"
    assert d["settings"]["THEFT_TIER_WATCH_MIN"]["section"] == "levels"
    assert all(d["settings"][k]["label"] and d["settings"][k]["help"] for k in keys)
    with pytest.raises(ss.SiteSettingsError) as exc:
        store.update({"THEFT_TIER_ALERT_MIN": 0.3}, "test")          # below watch (0.35)
    assert "THEFT_TIER_ALERT_MIN" in exc.value.errors and "rise" in exc.value.errors["THEFT_TIER_ALERT_MIN"]
    store.update({"THEFT_TIER_WATCH_MIN": 0.4, "THEFT_ROUTE_WATCH_PUSH": True}, "test")
    assert settings.THEFT_TIER_WATCH_MIN == 0.4 and tp.channels("watch")["push"] is True


# --------------------------------------------------------------------------- combinations

def test_concealment_then_exit_is_critical():
    a = tp.assess("EXIT_WITHOUT_CHECKOUT", 0.4, reached_zones=[AISLE],
                  other_rules=[("CONCEALMENT", 40.0)])
    assert a["alert_tier"] == "critical" and a["risk_score"] == pytest.approx(0.36, abs=1e-3)
    assert "hiding an item 40s earlier" in _factors(a)["combo_conceal_then_exit"]["reason"]
    # Outside the combination window it is not combined.
    late = tp.assess("EXIT_WITHOUT_CHECKOUT", 0.4, reached_zones=[AISLE], other_rules=[("CONCEALMENT", 900.0)])
    assert late["alert_tier"] == "watch" and "combo_conceal_then_exit" not in _factors(late)


def test_two_rules_on_one_person_and_zone_burst_raise_one_tier_each():
    two = tp.assess("SHELF_SWEEPING", 0.45, zone=AISLE, other_rules=[("BEHAVIOUR_PATTERN", 30.0)])
    assert two["risk_score"] == pytest.approx(0.405, abs=1e-3)
    assert two["alert_tier"] == "alert" and _factors(two)["combo_two_signs"]["effect"] == "+1 level"
    same_rule = tp.assess("SHELF_SWEEPING", 0.45, zone=AISLE, other_rules=[("SHELF_SWEEPING", 30.0)])
    assert same_rule["alert_tier"] == "watch"

    burst = tp.assess("CONCEALMENT", 0.3, target="pocket", zone=AISLE, zone_burst=3, zone_label="Biscuits")
    assert burst["alert_tier"] == "watch" and "3 incidents at 'Biscuits'" in _factors(burst)["zone_burst"]["reason"]
    assert tp.assess("CONCEALMENT", 0.3, target="pocket", zone=AISLE, zone_burst=2)["alert_tier"] == "review"

    both = tp.assess("CONCEALMENT", 0.6, target="pocket", zone=AISLE, zone_burst=5,
                     other_rules=[("SUSPICIOUS_LOITERING", 10.0)])
    assert both["alert_tier"] == "critical"


# --------------------------------------------------------------------------- role floor

def test_role_floor_is_a_minimum_tier_for_watch_and_above():
    assert cr.min_alert_tier("high_value") == "alert" and cr.min_alert_tier("aisle") is None
    up = tp.assess("CONCEALMENT", 0.3, target="pocket", zone=SPIRITS, role="high_value")
    assert up["risk_score"] == pytest.approx(0.375) and up["alert_tier"] == "alert"
    assert _factors(up)["role_floor"]["value"] == "alert"
    weak = tp.assess("BEHAVIOUR_PATTERN", 0.3, zone=SPIRITS, role="high_value")
    assert weak["alert_tier"] == "review" and "role_floor_not_applied" in _factors(weak)
    assert tp.assess("CONCEALMENT", 0.9, target="pocket", zone=SPIRITS, role="high_value")["alert_tier"] == "critical"


# --------------------------------------------------------------------------- routing per tier

class _Calls:
    def __init__(self):
        self.push, self.web, self.logged = [], [], []


@pytest.fixture
def mocked_channels(monkeypatch):
    calls = _Calls()

    async def fake_push(title, body, data, camera, bypass_cooldown=False):
        calls.push.append({"data": dict(data), "bypass_cooldown": bypass_cooldown})
        return {"provider": "fcm_v1", "sent": 1, "skipped": None}

    async def fake_web(title, body, data, camera, bypass_cooldown=False):
        calls.web.append({"data": dict(data), "bypass_cooldown": bypass_cooldown})
        return {"provider": "web_push", "pushed": True}

    async def fake_log(alert_id, camera, et, sev, title, body, data):
        calls.logged.append({"alert_id": alert_id, "severity": sev, "data": dict(data)})
        return True

    async def fake_camera(cid):
        return SimpleNamespace(id=cid, name="Tier Cam", location="Aisle 1", muted_until=None)

    monkeypatch.setattr(alert_dispatcher, "_push", fake_push)
    monkeypatch.setattr(alert_dispatcher, "_log_alert", fake_log)
    monkeypatch.setattr(alert_dispatcher, "_get_camera", fake_camera)
    monkeypatch.setattr(pa_mod.push_alerts, "on_alert", fake_web)
    return calls


def _dispatch(tier, conf=0.6):
    data = {"camera_id": "cam_tier", "event_type": "CONCEALMENT", "confidence": conf, "incident_id": "inc_t",
            "alert_tier": tier}
    return _run(alert_dispatcher.dispatch("CONCEALMENT", tp.TIER_EVENT_SEVERITY[tier], "t", "b", data))


def test_review_is_never_logged_broadcast_or_pushed(mocked_channels):
    r = _dispatch("review")
    assert r["alert_tier"] == "review" and r["logged"] is False and r["websocket_clients"] == 0
    assert mocked_channels.push == [] and mocked_channels.web == [] and mocked_channels.logged == []
    assert "Review level" in r["push"]["skipped"] and "Review level" in r["web_push"]["skipped"]


def test_watch_is_dashboard_only_unless_switched_on(mocked_channels, monkeypatch):
    r = _dispatch("watch")
    assert r["logged"] is True and r["severity"] == "WARNING"
    assert r["channels"] == {"queue": True, "banner": True, "sound": False, "push": False, "escalate": False,
                             "repeat": False}
    assert mocked_channels.push == [] and mocked_channels.web == []
    assert "dashboard only" in r["push"]["skipped"]
    monkeypatch.setattr(settings, "THEFT_ROUTE_WATCH_PUSH", True)
    _dispatch("watch")
    assert len(mocked_channels.push) == 1 and len(mocked_channels.web) == 1


def test_alert_pushes_without_escalation_and_critical_escalates(mocked_channels, monkeypatch):
    r = _dispatch("alert")
    assert r["severity"] == "HIGH" and r["channels"]["push"] and not r["channels"]["escalate"]
    assert len(mocked_channels.push) == 1 and mocked_channels.push[0]["bypass_cooldown"] is False
    assert mocked_channels.web[0]["data"]["channels"]["escalate"] is False

    r = _dispatch("critical")
    assert r["channels"]["escalate"] and r["channels"]["repeat"]
    assert mocked_channels.push[-1]["bypass_cooldown"] is True       # never held back by the camera cooldown
    assert mocked_channels.web[-1]["data"]["alert_tier"] == "critical"

    monkeypatch.setattr(settings, "THEFT_ROUTE_ALERT_PUSH", False)
    n = len(mocked_channels.push)
    _dispatch("alert")
    assert len(mocked_channels.push) == n                            # switched to dashboard only


def test_untiered_alerts_route_as_before(mocked_channels):
    data = {"camera_id": "cam_tier", "event_type": "CONCEALMENT", "confidence": 0.3, "incident_id": "inc_u"}
    r = _run(alert_dispatcher.dispatch("CONCEALMENT", "INFO", "t", "b", data))
    assert "alert_tier" not in r and len(mocked_channels.push) == 1 and len(mocked_channels.web) == 1


@pytest.fixture
def roster(monkeypatch):
    """Web push with a roster of two first-priority people and one backup; sends recorded."""
    sent, recorded = [], []
    monkeypatch.setattr(settings, "CAMERA_ALERT_COOLDOWN_SEC", 0)
    monkeypatch.setattr(pa_mod, "load_roster", lambda: {
        **pa_mod.DEFAULT_ROSTER, "first_priority": ["a", "b"], "backup": ["c"], "configured": True})

    async def accounts():
        return {u: {"id": u, "is_active": True, "role": "staff"} for u in ("a", "b", "c")}

    async def subs(user_ids):
        return [SimpleNamespace(id=f"sub_{u}", user_id=u, label=u, platform="android") for u in user_ids]

    state = {"accept": True}

    async def send_all(self, subs_, base, alert_id, data, tier, ack=True):
        sent.append({"tier": tier, "kind": base["kind"], "subs": [s.id for s in subs_], "base": dict(base)})
        return [{"subscription_id": s.id, "status": "sent" if state["accept"] else "failed"} for s in subs_]

    async def record(self, alert_id, title, body, data, payload, now, escalate_at, sent_to, closed, report):
        recorded.append({"escalate_at": escalate_at, "closed": closed, "payload": payload})

    monkeypatch.setattr(pa_mod, "load_accounts", accounts)
    monkeypatch.setattr(pa_mod, "subscriptions_for", subs)
    monkeypatch.setattr(pa_mod.PushAlertService, "_send_all", send_all)
    monkeypatch.setattr(pa_mod.PushAlertService, "_record_alert", record)
    svc = pa_mod.PushAlertService()
    return SimpleNamespace(svc=svc, sent=sent, recorded=recorded, state=state)


def _web(svc, tier, conf=0.6):
    data = {"camera_id": "cam_w", "event_type": "CONCEALMENT", "confidence": conf, "alert_id": uuid.uuid4().hex,
            "incident_id": "inc_w", "alert_tier": tier, "channels": tp.channels(tier),
            "severity": tp.TIER_EVENT_SEVERITY[tier]}
    return _run(svc.on_alert("t", "b", data))


def test_web_push_escalates_only_where_the_tier_says(roster):
    # Alert (60% risk, below the old 75% phone level): pushed, not escalated.
    rep = _web(roster.svc, "alert", conf=0.6)
    assert rep["pushed"] and rep["escalate_at"] is None and rep["escalates"] is False
    assert roster.sent[-1]["subs"] == ["sub_a", "sub_b"] and roster.sent[-1]["base"]["tier"] == "alert"
    assert roster.sent[-1]["base"]["repeat"] is False
    # Critical: escalation scheduled for the backup people, repeats on.
    rep = _web(roster.svc, "critical", conf=0.5)
    assert rep["escalate_at"] and rep["escalates"] is True and roster.sent[-1]["base"]["repeat"] is True
    # Watch is not sent to phones unless its channels say so.
    rep = _web(roster.svc, "watch")
    assert rep["pushed"] is False and "not sent to phones" in rep["skipped"]
    # An alert no first-priority phone accepted still reaches the backup now.
    roster.state["accept"] = False
    rep = _web(roster.svc, "alert")
    assert rep["escalate_at"] is not None


def test_critical_repeat_schedule(monkeypatch):
    monkeypatch.setattr(pa_mod, "load_roster", lambda: {**pa_mod.DEFAULT_ROSTER, "escalate_after_min": 5})
    created = datetime(2026, 10, 8, 10, 0, 0)
    row = SimpleNamespace(report={}, escalated_at=None, created_at=created, payload={"repeat": True})
    svc = pa_mod.PushAlertService()
    assert svc._due_repeat(row, created + timedelta(minutes=1)) == 0       # before escalate_after_min
    assert svc._due_repeat(row, created + timedelta(minutes=6)) == 1
    assert svc._due_repeat(row, created + timedelta(minutes=7)) == 0       # interval from the last repeat
    assert svc._due_repeat(row, created + timedelta(minutes=12)) == 2
    assert svc._due_repeat(row, created + timedelta(minutes=18)) == 3
    assert svc._due_repeat(row, created + timedelta(minutes=60)) == 0       # at most CRITICAL_MAX_REPEATS
    assert row.report["repeats"]["count"] == tp.CRITICAL_MAX_REPEATS


# --------------------------------------------------------------------------- migration

LEGACY_SQL = Path(__file__).parent / "fixtures" / "legacy_schema_v0.sql"


def test_migration_adds_tier_columns_to_an_old_database(tmp_path):
    from app.migrations import run_migrations
    from app.migrations import m0019_theft_alert_tiers as m19

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.executescript(LEGACY_SQL.read_text())
        cols = {r[1] for r in c.execute("PRAGMA table_info(theft_incidents)")}
        assert not {"alert_tier", "risk_score", "risk_factors"} & cols
        c.execute("INSERT INTO theft_incidents (id, timestamp, camera_id, camera_name, department, theft_type, "
                  "severity, confidence, evidence_summary, status, estimated_loss_value, items_involved, "
                  "created_at, updated_at) VALUES ('old1', '2026-09-01 10:00:00', 'cam1', 'Aisle 3', 'GENERAL', "
                  "'CONCEALMENT', 'HIGH', 0.8, '', 'ACTIVE', 0, '[]', '2026-09-01 10:00:00', "
                  "'2026-09-01 10:00:00')")
    report = run_migrations(db, backups_dir=tmp_path / "backups", run_reconcile=False)
    assert "0019_theft_alert_tiers" in report["applied"]
    with sqlite3.connect(db) as c:
        cols = {r[1]: r[2] for r in c.execute("PRAGMA table_info(theft_incidents)")}
        assert cols["alert_tier"] == "VARCHAR(16)" and cols["risk_score"] == "FLOAT" and cols["risk_factors"] == "JSON"
        idx = {r[1] for r in c.execute("PRAGMA index_list(theft_incidents)")}
        assert "ix_theft_incidents_alert_tier" in idx
        assert c.execute("SELECT alert_tier, risk_score FROM theft_incidents WHERE id = 'old1'").fetchone() == (None, None)
        assert c.execute("SELECT name FROM schema_migrations WHERE version = 19").fetchone() == (m19.NAME,)
    # Idempotent on a database that already has the columns.
    c = sqlite3.connect(db, isolation_level=None)
    try:
        m19.upgrade(c)
    finally:
        c.close()


# --------------------------------------------------------------------------- API

def _engine():
    return create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")


@pytest.fixture(scope="module")
def client():
    eng = _engine()
    Base.metadata.create_all(eng)
    eng.dispose()
    with TestClient(app) as c:
        yield c


def _add(tier, *, cam="cam_tier_api", status="ACTIVE", resolution=None, resolved_by=None, when=None):
    iid = f"tier_{tier or 'none'}_{uuid.uuid4().hex[:8]}"
    a = tp.assess("CONCEALMENT", 0.6, target="pocket", zone=SPIRITS) if tier else None
    eng = _engine()
    with Session(eng) as s:
        s.add(TheftIncidentModel(
            id=iid, timestamp=when or datetime.utcnow(), camera_id=cam, camera_name="Tier API", department="GENERAL",
            theft_type="CONCEALMENT", rule="CONCEALMENT", severity=tp.TIER_SEVERITY.get(tier or "", "HIGH"),
            confidence=0.6, status=status, resolution=resolution, resolved_by=resolved_by, zone_id="z_spirits",
            alert_tier=tier, risk_score=a["risk_score"] if a else None, risk_factors=a["risk_factors"] if a else None,
            evidence=["test"]))
        s.commit()
    eng.dispose()
    return iid


def test_incident_api_fields_and_tier_filters(client):
    ids = {t: _add(t) for t in ("review", "watch", "alert", "critical")}
    legacy = _add(None)

    r = client.get("/api/v1/theft/incidents?tier=alert&limit=200")
    assert r.status_code == 200, r.text
    got = {i["id"]: i for i in r.json()["incidents"]}
    assert ids["alert"] in got and ids["watch"] not in got and legacy not in got
    inc = got[ids["alert"]]
    assert inc["alert_tier"] == "alert" and inc["alert_tier_label"] == "Alert"
    assert inc["risk_score"] == pytest.approx(0.75) and inc["severity"] == "HIGH"
    assert inc["alert_channels"]["push"] is True and inc["alert_channels"]["escalate"] is False
    assert any(f["factor"] == "place_high_value" for f in inc["risk_factors"])

    r = client.get("/api/v1/theft/incidents?min_tier=alert&limit=200").json()["incidents"]
    tiers = {i["id"]: i["alert_tier"] for i in r}
    assert ids["alert"] in tiers and ids["critical"] in tiers and ids["watch"] not in tiers
    assert all(t in ("alert", "critical") for t in tiers.values())

    r = client.get("/api/v1/theft/incidents?tier=review,watch&limit=200").json()["incidents"]
    assert {ids["review"], ids["watch"]} <= {i["id"] for i in r}
    r = client.get("/api/v1/theft/incidents?tier=unclassified&limit=200").json()["incidents"]
    found = [i for i in r if i["id"] == legacy]
    assert found and found[0]["alert_tier"] is None and found[0]["risk_factors"] == []
    assert found[0]["alert_channels"] is None
    assert client.get("/api/v1/theft/incidents?tier=loud").status_code == 422
    assert client.get("/api/v1/theft/incidents?min_tier=unclassified").status_code == 422
    assert client.get("/api/v1/theft/incidents?tier=review&min_tier=alert").json()["incidents"] == []

    one = client.get(f"/api/v1/theft/incidents/{ids['critical']}").json()
    assert one["alert_tier"] == "critical" and one["alert_channels"]["escalate"] is True


def test_alert_policy_endpoint(client, monkeypatch):
    r = client.get("/api/v1/theft/alert-policy")
    assert r.status_code == 200, r.text
    p = r.json()
    assert [t["id"] for t in p["tiers"]] == ["review", "watch", "alert", "critical"]
    assert p["thresholds"] == {"watch": 0.35, "alert": 0.55, "critical": 0.8}
    review, watch, alert, critical = p["tiers"]
    assert review["channels"]["push"] is False and review["min_risk"] == 0.0 and review["max_risk"] == 0.35
    assert watch["editable_settings"] == ["THEFT_ROUTE_WATCH_SOUND", "THEFT_ROUTE_WATCH_PUSH"]
    assert critical["channels"] == {"queue": True, "banner": True, "sound": True, "push": True,
                                    "escalate": True, "repeat": True}
    assert {c["setting"] for c in p["case_weights"]} >= {"THEFT_CASE_WEIGHT_CONCEALMENT", "THEFT_CASE_WEIGHT_PATTERN"}
    assert p["place_weight_cap"] == 1.5 and p["settings_group"] == "theft_alerts"
    assert "THEFT_TIER_CRITICAL_MIN" in p["settings_keys"]
    hv = next(r for r in p["roles"] if r["role"] == "high_value")
    assert hv["min_tier"] == "alert"
    door = next(r for r in p["roles"] if r["role"] == "exit")
    assert door["place_weight_setting"] == "THEFT_PLACE_WEIGHT_DOOR_CAMERA" and door["place_weight"] == 1.15
    ex = {e["label"]: e for e in p["examples"]}
    assert ex["Pocket concealment, 45% confidence, normal aisle"]["alert_tier"] == "watch"
    assert ex["Pocket concealment, 45% confidence, spirits"]["alert_tier"] == "alert"
    assert ex["Concealment then exit without checkout (same person, 40 s apart)"]["alert_tier"] == "critical"
    # The policy follows the settings live.
    monkeypatch.setattr(settings, "THEFT_ROUTE_WATCH_SOUND", True)
    p = client.get("/api/v1/theft/alert-policy").json()
    assert p["tiers"][1]["channels"]["sound"] is True


def test_statistics_and_patterns_count_per_tier(client):
    before = client.get("/api/v1/theft/statistics").json()
    pat_before = client.get("/api/v1/theft/patterns?days=7").json()["summary"]["tiers"]
    _add("critical", cam="cam_tier_stats")
    _add("watch", cam="cam_tier_stats", status="FALSE_ALARM", resolution="FALSE_ALARM", resolved_by="tester")
    _add(None, cam="cam_tier_stats")
    after = client.get("/api/v1/theft/statistics").json()
    for key in ("by_tier", "today_by_tier"):
        assert after[key]["critical"] == before[key]["critical"] + 1
        assert after[key]["watch"] == before[key]["watch"] + 1
        assert after[key]["unclassified"] == before[key]["unclassified"] + 1
    assert after["active_by_tier"]["critical"] == before["active_by_tier"]["critical"] + 1
    assert after["active_by_tier"]["watch"] == before["active_by_tier"]["watch"]   # closed as a false alarm
    assert after["false_alarm_rate_by_tier"]["watch"] is not None and after["false_alarm_rate_by_tier"]["watch"] > 0
    assert set(after["by_tier"]) == {"review", "watch", "alert", "critical", "unclassified"}

    pat = client.get("/api/v1/theft/patterns?days=7").json()
    assert pat["summary"]["tiers"]["critical"] == pat_before["critical"] + 1
    cam = next(c for c in pat["hotspots"]["cameras"] if c["camera_id"] == "cam_tier_stats")
    assert cam["tiers"]["critical"] == 1 and cam["tiers"]["watch"] == 1 and cam["tiers"]["unclassified"] == 1
    assert all("tiers" in r for r in pat["rules"]) and all("tiers" in w for w in pat["weekly"])
