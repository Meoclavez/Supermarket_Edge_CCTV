"""Automatic set-up (services/automation.py, routes/automation.py).

Learnt store hours (thresholds, a daylight-saving week, manual days winning),
the backup hour, the analysis-speed choice and its precedence under saved
settings, stuck-camera restarts, night watch outside store hours, and the
three confirm-first suggestions: guard after hours, fewer wrong alerts (never
for critical restricted-area checks or tampering) and camera-to-area links
(the 60 % rule), with apply / dismiss / undo audited and refused to operators.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import (AdminUserModel, Base, CameraModel, RestrictedAreaModel, SecurityEventModel,
                                  TheftIncidentModel)
from app.services import auth_service as auth_mod
from app.services import automation as auto
from app.services import entitlements as ent
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.restricted_areas import AreaError, restricted_areas, validate_arming

CAM, CAM2 = "cam_auto_1", "cam_auto_2"
ADMIN, OP = "usr_auto_admin", "usr_auto_op"
REGION = [{"x": 0.3, "y": 0.2}, {"x": 0.7, "y": 0.2}, {"x": 0.7, "y": 0.95}, {"x": 0.3, "y": 0.95}]
WEEK = {wd: (480, 1260) for wd in auto.DAYS}          # 08:00-21:00 every day


def _engine():
    eng = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(eng)
    return eng


def _wipe(eng):
    with eng.begin() as conn:
        for t in ("restricted_areas", "restricted_area_announcements", "restricted_area_audit"):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("DELETE FROM security_events WHERE camera_id IN (:a, :b)"), {"a": CAM, "b": CAM2})
        conn.execute(text("DELETE FROM theft_incidents WHERE camera_id IN (:a, :b)"), {"a": CAM, "b": CAM2})
        conn.execute(text("DELETE FROM customer_tracks WHERE camera_id = 'cam_auto_learn'"))
        conn.execute(text("DELETE FROM cameras WHERE id IN (:a, :b)"), {"a": CAM, "b": CAM2})
        conn.execute(text("DELETE FROM admin_users WHERE id IN (:a, :b)"), {"a": ADMIN, "b": OP})
        conn.execute(text("DELETE FROM system_setup WHERE key IN (:a, :b, :c)"),
                     {"a": auto.HOURS_KEY, "b": auto.STATE_KEY, "c": ent.STATE_KEY})
    ent.invalidate()
    auto.load_cache()


@pytest.fixture
def api(monkeypatch):
    eng = _engine()
    _wipe(eng)
    with Session(eng) as s:
        for cid, name in ((CAM, "Back door"), (CAM2, "Aisle 3")):
            s.add(CameraModel(id=cid, name=name, location="Store", rtsp_url=f"rtsp://127.0.0.1:554/{cid}",
                              status="OFFLINE"))
        s.add(AdminUserModel(id=ADMIN, username="auto_owner", password_hash="x", display_name="Ada Owner",
                             role="owner", is_active=True))
        s.add(AdminUserModel(id=OP, username="auto_op", password_hash="x", display_name="Otto Operator",
                             role="operator", is_active=True))
        s.commit()
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    auth_mod._epoch_cache.update(value=0, at=0.0)
    auth_mod._accounts_cache.update(at=0.0, db=None, rows={})
    app.state.setup_completed = True
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    h = {u: {"Authorization": f"Bearer {auth_service.issue_session_tokens(u, r)['access_token']}"}
         for u, r in ((ADMIN, "owner"), (OP, "operator"))}
    yield TestClient(app), h, eng
    _wipe(eng)
    asyncio.run(restricted_areas.refresh())
    general_rate_limiter.history.clear()
    eng.dispose()


def _audit_actions(eng):
    with eng.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT action FROM restricted_area_audit ORDER BY ts"))]


# =========================================================================== learning (pure)

def _act(open_h, close_h, n=30):
    return [n if open_h <= h < close_h else 0 for h in range(24)]


def test_a_day_needs_enough_evidence_and_sales_win_over_footfall():
    zero = [0] * 24
    assert auto.classify_day(_act(8, 21), zero, 22) == (480, 1260)
    assert auto.classify_day(_act(8, 21), zero, 12) is None              # cameras off half the day
    assert auto.classify_day(_act(8, 21, n=5), zero, 24) == "closed"      # busiest hour below 10
    assert auto.classify_day(zero, zero, None) is None                    # nothing known at all
    staff = _act(8, 21)
    staff[6] = 4                                                          # cleaners at 06:00 (< 15 %)
    assert auto.classify_day(staff, zero, 24) == (480, 1260)
    pos = _act(9, 20, n=12)
    assert auto.classify_day(_act(7, 22), pos, 4) == (540, 1200)          # till sales decide


def test_hours_are_learnt_after_seven_days_with_medians_closed_days_and_estimates():
    start = date(2026, 10, 5)                                             # a Monday
    res = {start + timedelta(days=i): (480, 1260) for i in range(6)}
    out = auto.aggregate(res, {})
    assert out["status"] == "learning" and out["days_measured"] == 6 and out["needed"] == 7
    res[start + timedelta(days=6)] = "closed"                             # Sunday closed
    res[start + timedelta(days=7)] = (510, 1230)                          # Monday 08:30-20:30
    res[start + timedelta(days=8)] = None                                 # Tuesday unusable
    out = auto.aggregate(res, {})
    assert out["status"] == "learned" and out["days_measured"] == 8
    assert out["days"]["mon"] == {"open": "08:00", "close": "21:00"}      # median 08:15 -> down, 20:45 -> up
    assert out["days"]["sun"] == {"closed": True}
    hours = auto.store_hours_minutes({"learned": out})
    assert hours["sun"] is None and hours["tue"] == (480, 1260)
    # A weekday never seen is estimated from the other open days.
    only = {start + timedelta(days=i): (480, 1260) for i in range(5)}
    only.update({start + timedelta(days=7 + i): (480, 1260) for i in range(2)})
    out = auto.aggregate(only, {})
    assert out["status"] == "learned" and set(out["estimated"]) == {"sat", "sun"}


def test_store_hours_view_label_rows_and_manual_days_win():
    doc = {"learned": {"status": "learned", "days": {
        **{wd: {"open": "08:00", "close": "21:00"} for wd in ("mon", "tue", "wed", "thu", "fri")},
        "sat": {"open": "09:00", "close": "18:00"}, "sun": {"open": "09:00", "close": "18:00"}}}}
    view = auto.store_hours_view(doc)
    assert view == {"learnt": True, "label": "Mon–Fri 08:00–21:00, Sat–Sun 09:00–18:00",
                    "rows": [{"days": ["mon", "tue", "wed", "thu", "fri"], "from": "08:00", "to": "21:00"},
                             {"days": ["sat", "sun"], "from": "09:00", "to": "18:00"}]}
    doc["manual"] = {"sun": {"closed": True}, "wed": {"open": "07:00", "close": "22:00"}}
    view = auto.store_hours_view(doc)
    assert view["label"] == "Mon–Tue, Thu–Fri 08:00–21:00, Wed 07:00–22:00, Sat 09:00–18:00, Sun closed"
    assert auto.store_hours_view({"learned": {"status": "learning", "days": {}}}) is None
    # Only some days typed in and nothing learnt yet: not known.
    assert auto.store_hours_view({"manual": {"mon": {"open": "08:00", "close": "21:00"}}}) is None


def test_hours_are_learnt_across_a_daylight_saving_week(monkeypatch):
    """Melbourne clocks go forward on Sunday 4 Oct 2026: local opening hours stay 08:00-21:00."""
    monkeypatch.setattr(settings, "SITE_TIMEZONE", "Australia/Melbourne")
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Melbourne")
    eng = _engine()
    first = date(2026, 9, 28)
    rows = []
    for k in range(14):
        d = first + timedelta(days=k)
        for h in range(8, 21):
            local = datetime(d.year, d.month, d.day, h, 20, tzinfo=tz)
            utc = local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            for i in range(12):
                rows.append({"id": uuid.uuid4().hex, "track_id": f"t{k}{h}{i}", "camera_id": "cam_auto_learn",
                             "start_time": utc, "end_time": utc + timedelta(seconds=40)})
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM customer_tracks WHERE camera_id = 'cam_auto_learn'"))
        conn.execute(text("INSERT INTO customer_tracks (id, track_id, camera_id, start_time, end_time, "
                          "trajectory_points, created_at) VALUES (:id, :track_id, :camera_id, :start_time, "
                          ":end_time, '[]', :start_time)"), rows)

    async def run():
        from app.database import async_session_factory

        async with async_session_factory() as db:
            return await auto.observe_days(db, first + timedelta(days=14), days=14)

    try:
        res, profile = asyncio.run(run())
    finally:
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM customer_tracks WHERE camera_id = 'cam_auto_learn'"))
        eng.dispose()
    assert len(res) == 14
    assert all(r == (480, 1260) for r in res.values()), res
    out = auto.aggregate(res, profile)
    assert out["status"] == "learned" and out["days"]["sun"] == {"open": "08:00", "close": "21:00"}
    assert out["profile"][12] == 12 and out["profile"][3] == 0


# =========================================================================== backup hour, analysis speed

def test_backup_runs_in_the_middle_of_closed_hours_and_never_at_four():
    h, why = auto.quietest_hour(WEEK, None)
    assert h != auto.MAINTENANCE_HOUR and "closed" in why
    assert h in (21, 22, 23, 0, 1, 2, 3) and h == 0                      # middle of 21:00-03:59
    late = {wd: (360, 1440) for wd in auto.DAYS}                          # 06:00-24:00
    assert auto.quietest_hour(late, None)[0] in (0, 1, 2, 3, 5)
    allday = {wd: (0, 1440) for wd in auto.DAYS}
    profile = [5.0] * 24
    profile[2], profile[4] = 1.0, 0.0
    assert auto.quietest_hour(allday, profile) == (2, "the quietest hour seen by the cameras")
    assert auto.quietest_hour(None, None)[0] == 3


def test_analysis_speed_choice_respects_the_restricted_floor_and_reconnect_hysteresis():
    assert auto.choose_rates(None, 10, 8, False, 10) == {}                # not measured yet
    r = auto.choose_rates(20.0, 32, 8, False, 10)
    assert r["ANALYTICS_MAX_DETECT_FPS"] >= settings.RESTRICTED_MIN_ANALYSIS_FPS >= 3
    assert r["ANALYTICS_MAX_DETECT_FPS"] <= r["DECODE_MAX_FPS"] <= 10
    assert 0.3 <= r["ANALYTICS_IDLE_DETECT_FPS"] <= 1.0
    # A small change of frames read is not worth reconnecting every camera.
    assert auto.choose_rates(200.0, 4, 8, False, 9)["DECODE_MAX_FPS"] == 9
    assert auto.choose_rates(200.0, 4, 8, True, 10)["POSE_BUDGET_UTILISATION"] == 0.5
    slow = auto.choose_rates(4.0, 40, 2, True, 10)
    assert slow["ANALYTICS_MAX_DETECT_FPS"] >= 3 and slow["DECODE_MAX_FPS"] >= 3


def test_a_saved_setting_wins_over_the_automatic_value(monkeypatch):
    from app.services.site_settings import ENV_DEFAULTS, site_settings

    key = "ANALYTICS_IDLE_DETECT_FPS"
    saved_over = dict(site_settings.overrides)
    try:
        site_settings.overrides.pop(key, None)
        site_settings.set_auto({key: 0.9}, source="auto_tune")
        assert site_settings.effective(key) == 0.9 and settings.ANALYTICS_IDLE_DETECT_FPS == 0.9
        d = site_settings.describe()["settings"][key]
        assert d["auto"] is True and d["auto_value"] == 0.9
        site_settings.overrides[key] = 0.2
        site_settings.apply()
        assert settings.ANALYTICS_IDLE_DETECT_FPS == 0.2
        assert site_settings.describe()["settings"][key]["auto"] is False
        site_settings.overrides.pop(key)
        site_settings.apply()
        assert settings.ANALYTICS_IDLE_DETECT_FPS == 0.9                  # back to automatic, not .env
        site_settings.set_auto({key: 99}, source="auto_tune")             # refused value: dropped
        assert site_settings.effective(key) == ENV_DEFAULTS[key]
    finally:
        site_settings.set_auto({})
        site_settings.overrides = saved_over
        site_settings.apply()


# =========================================================================== stuck cameras, night watch

def test_stuck_cameras_restart_with_back_off():
    from types import SimpleNamespace

    from app.services.camera_restarts import MAX_PER_HOUR, CameraRestarts
    from app.services.live_analytics_engine import stall_seconds

    clock = [1_000_000.0]
    cr = CameraRestarts(clock=lambda: clock[0])
    rt = SimpleNamespace(status="ONLINE", last_frame_at=clock[0] - stall_seconds() * 1.5)
    assert not cr.stuck_for_restart(rt)                                   # stalled, not long enough yet
    rt.last_frame_at = clock[0] - stall_seconds() * 2.5
    assert cr.stuck_for_restart(rt) and cr.may_restart("c")
    cr.record("c", "stuck")
    assert not cr.may_restart("c")                                        # 5 minutes between restarts
    for _ in range(MAX_PER_HOUR - 1):
        clock[0] += 301
        assert cr.may_restart("c")
        cr.record("c", "stuck")
    clock[0] += 301
    assert not cr.may_restart("c")                                        # at most 6 an hour
    assert not cr.stuck_for_restart(SimpleNamespace(status="RECONNECTING", last_frame_at=0))


def test_night_watch_follows_store_hours_when_known(monkeypatch):
    from app.models.schemas import NightWatchConfig
    from app.services import night_watch as nw

    monkeypatch.setattr(settings, "SITE_TIMEZONE", "Australia/Melbourne")
    cfg = NightWatchConfig(enabled=True, start="23:00", end="05:00", follow_store_hours=True)
    noon = datetime(2026, 10, 7, 12, 0).timestamp()
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Australia/Melbourne")
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=tz).timestamp()   # noqa: E731
    monkeypatch.setattr(nw, "_store_hours", lambda: None)
    assert nw.in_schedule(cfg, at(7, 23, 30)) and not nw.in_schedule(cfg, at(7, 22, 0))   # own times
    hours = dict(WEEK)
    hours["sun"] = None
    monkeypatch.setattr(nw, "_store_hours", lambda: hours)
    assert nw.in_schedule(cfg, at(7, 21, 30)) and nw.in_schedule(cfg, at(8, 7, 59))
    assert not nw.in_schedule(cfg, at(7, 8, 0)) and not nw.in_schedule(cfg, at(7, 20, 59))
    assert nw.in_schedule(cfg, at(11, 13, 0))                            # Sunday: closed all day
    assert nw.window_label(cfg) == "Outside store hours"
    assert noon


# =========================================================================== store hours API, area rewrite

def test_store_hours_api_edits_audits_and_rewrites_store_hours_areas(api):
    c, h, eng = api
    r = c.get("/api/v1/store-hours", headers=h[OP])
    assert r.status_code == 200 and r.json()["view"] is None and r.json()["can_edit"] is False
    week = {wd: {"open": "08:00", "close": "21:00"} for wd in auto.DAYS}
    assert c.put("/api/v1/store-hours", json={"days": week}, headers=h[OP]).status_code == 403
    assert c.put("/api/v1/store-hours", json={"days": {"mon": {"open": "8", "close": "21:00"}}},
                 headers=h[ADMIN]).status_code == 422
    # An area that follows the store hours (the dashboard's "Outside store hours").
    body = {"name": "Stockroom", "camera_regions": [{"camera_id": CAM, "points": REGION}],
            "arming": {"mode": "schedule", "schedule_mode": "armed_outside", "source": "store_hours",
                       "schedule": [{"days": ["mon"], "from": "09:00", "to": "17:00"}]}}
    a = c.post("/api/v1/restricted-areas", json=body, headers=h[ADMIN])
    assert a.status_code == 201, a.text
    assert a.json()["arming"]["source"] == "store_hours"
    r = c.put("/api/v1/store-hours", json={"days": week}, headers=h[ADMIN])
    assert r.status_code == 200, r.text
    assert r.json()["view"]["label"] == "Mon–Sun 08:00–21:00"
    with Session(eng) as s:
        row = s.get(RestrictedAreaModel, a.json()["id"])
        assert row.arming["schedule"] == [{"days": list(auto.DAYS), "from": "08:00", "to": "21:00"}]
        assert row.arming["schedule_mode"] == "armed_outside" and row.arming["source"] == "store_hours"
    acts = _audit_actions(eng)
    assert "store_hours_set" in acts and "hours_rewrite" in acts
    # Sunday closed: one more rewrite.
    r = c.put("/api/v1/store-hours", json={"days": {"sun": {"closed": True}}}, headers=h[ADMIN])
    assert r.json()["view"]["label"] == "Mon–Sat 08:00–21:00, Sun closed"
    assert _audit_actions(eng).count("hours_rewrite") == 2
    st = c.get("/api/v1/automation/status", headers=h[OP]).json()
    assert st["store_hours"]["backup"]["hour"] != 4 and st["can_edit"] is False
    assert "evidence_gb" in st["storage"] and "current" in st["analysis"]


def test_arming_source_is_kept_and_checked():
    out = validate_arming({"mode": "schedule", "schedule_mode": "armed_outside", "source": "store_hours",
                           "schedule": [{"days": ["mon"], "from": "08:00", "to": "21:00"}]})
    assert out["source"] == "store_hours"
    assert "source" not in validate_arming({"mode": "always"})
    with pytest.raises(AreaError):
        validate_arming({"mode": "always", "source": "somewhere"})


# =========================================================================== suggestions

def _manual_week(c, h):
    week = {wd: {"open": "08:00", "close": "21:00"} for wd in auto.DAYS}
    assert c.put("/api/v1/store-hours", json={"days": week}, headers=h[ADMIN]).status_code == 200


def test_guard_after_hours_for_night_watch_is_confirmed_audited_and_undone(api):
    c, h, eng = api
    with Session(eng) as s:
        cam = s.get(CameraModel, CAM)
        cam.features = {"night_watch": {"enabled": True, "start": "22:00", "end": "06:00"}}
        s.commit()
    from app.services.feature_manager import feature_manager

    feature_manager.remove_camera(CAM)
    assert not [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
                if x["kind"] == "guard_hours"]                            # no store hours yet: nothing
    _manual_week(c, h)
    r = c.get("/api/v1/automation/suggestions", headers=h[OP]).json()
    assert r["can_apply"] is False
    sg = next(x for x in r["suggestions"] if x["kind"] == "guard_hours" and x["target"] == "night_watch")
    assert [x["id"] for x in sg["cameras"]] == [CAM]
    assert c.post(f"/api/v1/automation/suggestions/{sg['id']}/apply", headers=h[OP]).status_code == 403
    ap = c.post(f"/api/v1/automation/suggestions/{sg['id']}/apply", headers=h[ADMIN])
    assert ap.status_code == 200, ap.text
    assert feature_manager.get_setting(CAM, "night_watch").follow_store_hours is True
    assert "undo" not in ap.json()
    assert not [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
                if x["id"] == sg["id"]]
    u = c.post(f"/api/v1/automation/applied/{ap.json()['apply_id']}/undo", headers=h[ADMIN])
    assert u.status_code == 200, u.text
    assert feature_manager.get_setting(CAM, "night_watch").follow_store_hours is False
    assert c.post(f"/api/v1/automation/applied/{ap.json()['apply_id']}/undo", headers=h[ADMIN]).status_code == 404
    acts = _audit_actions(eng)
    assert "auto_apply" in acts and "auto_undo" in acts


def _ra_events(eng, area_id, trigger, n, false_alarms, tier="alert"):
    with Session(eng) as s:
        for i in range(n):
            s.add(SecurityEventModel(
                id=f"ev_auto_{area_id}_{trigger}_{i}", camera_id=CAM, camera_name="Back door", location="Store",
                event_type="RESTRICTED_AREA", severity="HIGH", timestamp=datetime.utcnow() - timedelta(hours=i),
                metadata_json={"ra_area_id": area_id, "ra_trigger": trigger, "alert_tier": tier,
                               "ra_outcome": "false_alarm" if i < false_alarms else "authorised"}))
        s.commit()


def test_wrong_alerts_suggest_a_step_but_never_for_critical_checks(api):
    c, h, eng = api
    a = c.post("/api/v1/restricted-areas", json={"name": "Office", "camera_regions": [
        {"camera_id": CAM, "points": REGION}]}, headers=h[ADMIN]).json()
    _ra_events(eng, a["id"], "dwell", 6, 5)                              # 83 % wrong
    _ra_events(eng, a["id"], "unannounced_entry", 8, 8, tier="critical")  # never
    _ra_events(eng, a["id"], "tamper", 8, 8, tier="critical")             # never
    _ra_events(eng, a["id"], "pacing", 6, 3)                             # 50 %: not enough
    sgs = [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
           if x["kind"] == "less_sensitive"]
    assert [x["trigger"] for x in sgs] == ["dwell"]
    sg = sgs[0]
    assert sg["from"] == {"max_seconds": 60.0} and sg["to"] == {"max_seconds": 90.0}
    ap = c.post(f"/api/v1/automation/suggestions/{sg['id']}/apply", headers=h[ADMIN]).json()
    got = c.get(f"/api/v1/restricted-areas/{a['id']}", headers=h[ADMIN]).json()
    assert got["triggers"]["dwell"]["max_seconds"] == 90.0
    assert got["triggers"]["unannounced_entry"]["min_presence_sec"] == 0.5
    c.post(f"/api/v1/automation/applied/{ap['apply_id']}/undo", headers=h[ADMIN])
    got = c.get(f"/api/v1/restricted-areas/{a['id']}", headers=h[ADMIN]).json()
    assert got["triggers"]["dwell"]["max_seconds"] == 60.0
    # A trigger the area set to critical is never offered either.
    c.put(f"/api/v1/restricted-areas/{a['id']}", json={"triggers": {"dwell": {"tier": "critical"}}}, headers=h[ADMIN])
    assert not [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
                if x["kind"] == "less_sensitive"]


def test_wrong_theft_alerts_make_one_rule_on_one_camera_less_sensitive(api):
    c, h, eng = api
    with Session(eng) as s:
        for i in range(6):
            s.add(TheftIncidentModel(
                id=f"ti_auto_{i}", timestamp=datetime.utcnow() - timedelta(hours=i), camera_id=CAM2,
                camera_name="Aisle 3", department="Grocery", theft_type="CONCEALMENT", rule="CONCEALMENT",
                status="FALSE_ALARM" if i < 5 else "RESOLVED", resolution="FALSE_ALARM" if i < 5 else "RECOVERED_GOODS",
                resolved_by="Ada Owner"))
        s.commit()
    sg = next(x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
              if x["target"] == "theft_rule")
    assert sg["camera"]["id"] == CAM2 and sg["rule"] == "CONCEALMENT" and sg["to_factor"] == 0.8
    c.post(f"/api/v1/automation/suggestions/{sg['id']}/apply", headers=h[ADMIN])
    from app.services.feature_manager import feature_manager

    assert feature_manager.get_setting(CAM2, "theft_rule_sensitivity") == {"CONCEALMENT": 0.8}
    with Session(eng) as s:
        assert s.get(CameraModel, CAM2).features["theft_rule_sensitivity"] == {"CONCEALMENT": 0.8}
    # The next step is offered from the new value; dismissing hides it.
    sg2 = next(x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
               if x["target"] == "theft_rule")
    assert sg2["from_factor"] == 0.8 and sg2["to_factor"] == 0.64
    assert c.post(f"/api/v1/automation/suggestions/{sg2['id']}/dismiss", headers=h[OP]).status_code == 403
    assert c.post(f"/api/v1/automation/suggestions/{sg2['id']}/dismiss", headers=h[ADMIN]).status_code == 200
    assert not [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
                if x["target"] == "theft_rule"]
    assert "auto_dismiss" in _audit_actions(eng)


def test_the_rule_factor_raises_the_confidence_needed_on_that_camera_only():
    from app.models.schemas import CameraFeatureConfig

    cfg = CameraFeatureConfig.model_validate({"theft_rule_sensitivity": {"concealment": 0.8, "X": 1.2, "Y": "a"}})
    assert cfg.theft_rule_sensitivity == {"CONCEALMENT": 0.8}             # >= 1 and junk dropped
    import inspect

    from app.services import pose_analytics

    src = inspect.getsource(pose_analytics.PoseAnalytics._raise)
    assert "rule_factors.get(rule)" in src and "min_conf / factor" in src


def test_link_cameras_to_areas_needs_sixty_percent_and_flags_conflicts(api, monkeypatch):
    c, h, eng = api
    from app.services.store_layout_service import store_layout_service

    def cam(cid, name, zones, floor=40.0, **kw):
        return {"camera_id": cid, "name": name, "calibrated": False, "watch_zone_id": None, "counted": True,
                "enabled": True, "floor_seen_m2": floor, "zones": zones, **kw}

    report = {"cameras": [
        cam(CAM, "Back door", [{"zone_id": "z1", "name": "Aisle 1", "category": "AISLE", "seen_m2": 30.0},
                               {"zone_id": "z2", "name": "Aisle 2", "category": "AISLE", "seen_m2": 5.0}]),
        cam(CAM2, "Aisle 3", [{"zone_id": "z1", "name": "Aisle 1", "category": "AISLE", "seen_m2": 25.0}]),
        cam("cam_auto_x", "Half", [{"zone_id": "z3", "name": "Deli", "category": "DELI", "seen_m2": 20.0}]),
        cam("cam_auto_cal", "Calibrated", [{"zone_id": "z4", "name": "Bakery", "category": "BAKERY",
                                            "seen_m2": 40.0}], calibrated=True),
    ]}

    async def fake(db):
        return report

    monkeypatch.setattr(store_layout_service, "coverage_report", fake)
    sg = next(x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
              if x["kind"] == "link_zone")
    links = {x["camera_id"]: x for x in sg["links"]}
    assert set(links) == {CAM, CAM2}                                      # 50 % and calibrated: left out
    assert links[CAM]["ok"] and links[CAM]["share"] == 0.75
    assert not links[CAM2]["ok"] and "sees more" in links[CAM2]["check"]
    ap = c.post(f"/api/v1/automation/suggestions/{sg['id']}/apply", headers=h[ADMIN])
    assert ap.status_code == 200, ap.text
    with Session(eng) as s:
        assert s.get(CameraModel, CAM).watch_zone_id == "z1" and s.get(CameraModel, CAM2).watch_zone_id is None
    c.post(f"/api/v1/automation/applied/{ap.json()['apply_id']}/undo", headers=h[ADMIN])
    with Session(eng) as s:
        assert s.get(CameraModel, CAM).watch_zone_id is None


def test_suggestions_are_hidden_when_the_feature_is_not_in_the_plan(api, monkeypatch):
    c, h, eng = api
    with Session(eng) as s:
        s.get(CameraModel, CAM).features = {"night_watch": {"enabled": True}}
        s.commit()
    _manual_week(c, h)
    monkeypatch.setattr(auto, "_plan", lambda key: key != "night_watch")
    assert not [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
                if x.get("target") == "night_watch"]


def test_an_older_night_watch_save_keeps_outside_store_hours(api):
    c, h, eng = api
    body = {"enabled": True, "start": "22:00", "end": "06:00", "follow_store_hours": True}
    assert c.put(f"/api/v1/cameras/{CAM}/night-watch", json=body, headers=h[ADMIN]).status_code == 200
    old_client = {"enabled": True, "start": "23:00", "end": "06:00"}
    r = c.put(f"/api/v1/cameras/{CAM}/night-watch", json=old_client, headers=h[ADMIN])
    assert r.status_code == 200 and r.json()["config"]["follow_store_hours"] is True
    r = c.put(f"/api/v1/cameras/{CAM}/night-watch", json={**old_client, "follow_store_hours": False}, headers=h[ADMIN])
    assert r.json()["config"]["follow_store_hours"] is False


def test_only_areas_guarded_mostly_after_hours_are_offered_store_hours():
    night = {"mode": "schedule", "schedule_mode": "armed_during",
             "schedule": [{"days": list(auto.DAYS), "from": "21:30", "to": "07:30"}]}
    day = {"mode": "schedule", "schedule_mode": "armed_during",
           "schedule": [{"days": list(auto.DAYS), "from": "09:00", "to": "17:00"}]}
    outside = {"mode": "schedule", "schedule_mode": "armed_outside",
               "schedule": [{"days": list(auto.DAYS), "from": "07:00", "to": "22:00"}]}
    assert auto.after_hours_share(night, WEEK) == 1.0
    assert auto.after_hours_share(day, WEEK) == 0.0
    assert auto.after_hours_share(outside, WEEK) == 1.0


def test_guard_after_hours_switches_a_night_schedule_area_to_store_hours(api):
    c, h, eng = api
    night = {"mode": "schedule", "schedule_mode": "armed_during",
             "schedule": [{"days": list(auto.DAYS), "from": "21:30", "to": "07:30"}]}
    day = {"mode": "schedule", "schedule_mode": "armed_during",
           "schedule": [{"days": list(auto.DAYS), "from": "09:00", "to": "17:00"}]}
    a = c.post("/api/v1/restricted-areas", json={"name": "Cash office", "arming": night, "camera_regions": [
        {"camera_id": CAM, "points": REGION}]}, headers=h[ADMIN]).json()
    c.post("/api/v1/restricted-areas", json={"name": "Day store", "arming": day, "camera_regions": [
        {"camera_id": CAM2, "points": REGION}]}, headers=h[ADMIN])
    c.post("/api/v1/restricted-areas", json={"name": "Always", "camera_regions": [
        {"camera_id": CAM2, "points": REGION}]}, headers=h[ADMIN])
    _manual_week(c, h)
    sgs = [x for x in c.get("/api/v1/automation/suggestions", headers=h[ADMIN]).json()["suggestions"]
           if x.get("target") == "restricted_area"]
    assert [x["area"]["name"] for x in sgs] == ["Cash office"]          # never the day or always-armed ones
    ap = c.post(f"/api/v1/automation/suggestions/{sgs[0]['id']}/apply", headers=h[ADMIN]).json()
    got = c.get(f"/api/v1/restricted-areas/{a['id']}", headers=h[ADMIN]).json()["arming"]
    assert got["source"] == "store_hours" and got["schedule_mode"] == "armed_outside"
    assert got["schedule"] == [{"days": list(auto.DAYS), "from": "08:00", "to": "21:00"}]
    c.post(f"/api/v1/automation/applied/{ap['apply_id']}/undo", headers=h[ADMIN])
    got = c.get(f"/api/v1/restricted-areas/{a['id']}", headers=h[ADMIN]).json()["arming"]
    assert got["schedule_mode"] == "armed_during" and "source" not in got
