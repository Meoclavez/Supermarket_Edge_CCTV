"""Site settings set in the dashboard (services/site_settings.py, routes/site_settings.py).

Covers the store (saved in system_setup, reloaded, reset), validation, and
that a saved value reaches its consumers without a restart: the time zone
(timeutil), the evidence limits (evidence_storage) and the theft repeat-alert
quiet time (pose_analytics), plus the high-value cache bump.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import timedelta

import pytest
from fastapi import Request

from app.config import settings
from app.services import site_settings as ss


def _make_db(path):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE system_setup (key VARCHAR(128) PRIMARY KEY, value VARCHAR(4096) NOT NULL, "
              "updated_at DATETIME)")
    c.commit()
    c.close()
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store on its own database; every site setting restored after the test."""
    for spec in ss.SPECS:
        monkeypatch.setattr(settings, spec.key, getattr(settings, spec.key))
    calls = []
    monkeypatch.setattr(ss, "run_evidence_pass", lambda: calls.append(1))
    db = _make_db(tmp_path / "site.db")
    st = ss.SiteSettingsStore(db_path_fn=lambda: db)
    st.load()
    st.evidence_passes = calls
    st.db = db
    return st


# ------------------------------------------------------------------ validation

def test_timezone_validation():
    assert ss.validate_one("SITE_TIMEZONE", "Australia/Melbourne") == "Australia/Melbourne"
    assert ss.validate_one("SITE_TIMEZONE", "  ") == ""          # host zone
    assert ss.validate_one("SITE_TIMEZONE", "UTC") == "UTC"
    for bad in ("Mars/Olympus", "../etc/passwd", "AEST", 5):
        with pytest.raises(ValueError):
            ss.validate_one("SITE_TIMEZONE", bad)


def test_number_ranges_and_types():
    assert ss.validate_one("STORAGE_RETENTION_DAYS", 0) == 0          # no age limit
    assert ss.validate_one("STORAGE_RETENTION_DAYS", "14") == 14
    for bad in (-1, 731, 2.5, "x", None, True, float("nan")):
        with pytest.raises(ValueError):
            ss.validate_one("STORAGE_RETENTION_DAYS", bad)
    assert ss.validate_one("EVIDENCE_MAX_GB", 0) == 0.0               # automatic
    with pytest.raises(ValueError):
        ss.validate_one("EVIDENCE_MAX_GB", 0.1)                       # below 0.5 GB
    for bad in (49, 96, 0):
        with pytest.raises(ValueError):
            ss.validate_one("STORAGE_MAX_DISK_PERCENT", bad)
    assert ss.validate_one("THEFT_MIN_CONFIDENCE", 0.45) == 0.45
    for bad in (0.05, 0.99, 45):
        with pytest.raises(ValueError):
            ss.validate_one("THEFT_MIN_CONFIDENCE", bad)
    with pytest.raises(ValueError):
        ss.validate_one("TRIPWIRE_ALERT_COOLDOWN_SEC", 1)
    assert ss.validate_one("THEFT_EXIT_RULE_ENABLED", False) is False
    with pytest.raises(ValueError):
        ss.validate_one("THEFT_EXIT_RULE_ENABLED", 1)


def test_categories_are_uppercase_words():
    assert ss.validate_one("THEFT_HIGH_VALUE_CATEGORIES", "spirits, baby-formula  Cosmetics,spirits") == [
        "SPIRITS", "BABY_FORMULA", "COSMETICS"]
    assert ss.validate_one("THEFT_HIGH_VALUE_CATEGORIES", ["wine"]) == ["WINE"]
    assert ss.validate_one("THEFT_HIGH_VALUE_CATEGORIES", "") == []
    with pytest.raises(ValueError):
        ss.validate_one("THEFT_HIGH_VALUE_CATEGORIES", "WINE, R&D")


def test_store_name_is_trimmed_and_bounded():
    assert ss.validate_one("STORE_NAME", "  IGA   Pearcedale ") == "IGA Pearcedale"
    for bad in ("", "   ", "x" * 81, "a\x00b", None):
        with pytest.raises(ValueError):
            ss.validate_one("STORE_NAME", bad)


# ------------------------------------------------------------------ store

def test_saved_values_persist_reload_and_reset(store):
    default = ss.ENV_DEFAULTS["STORE_NAME"]
    diff = store.update({"STORE_NAME": "IGA Pearcedale", "QUEUE_CONGESTED_WAIT_SEC": 300}, actor="owner1")
    assert set(diff) == {"STORE_NAME", "QUEUE_CONGESTED_WAIT_SEC"}
    assert settings.STORE_NAME == "IGA Pearcedale" and settings.QUEUE_CONGESTED_WAIT_SEC == 300

    # A new process (a fresh store on the same database) applies them again.
    settings.STORE_NAME = default
    again = ss.SiteSettingsStore(db_path_fn=lambda: store.db)
    again.load()
    assert settings.STORE_NAME == "IGA Pearcedale"
    d = again.describe()["settings"]["STORE_NAME"]
    assert d["overridden"] and d["default"] == default and d["value"] == "IGA Pearcedale"
    assert d["changed_by"] == "owner1" and d["applies"] == "live"
    assert again.describe()["settings"]["THEFT_MIN_CONFIDENCE"]["min"] == 0.1

    again.reset("STORE_NAME", actor="owner1")
    assert settings.STORE_NAME == default
    assert not again.describe()["settings"]["STORE_NAME"]["overridden"]
    third = ss.SiteSettingsStore(db_path_fn=lambda: store.db)
    third.load()
    assert "STORE_NAME" not in third.overrides and third.overrides["QUEUE_CONGESTED_WAIT_SEC"] == 300


def test_a_refused_value_refuses_the_whole_change(store):
    before = settings.STORE_NAME
    with pytest.raises(ss.SiteSettingsError) as exc:
        store.update({"STORE_NAME": "New name", "SITE_TIMEZONE": "Nowhere/Land", "NOPE": 1})
    assert set(exc.value.errors) == {"SITE_TIMEZONE", "NOPE"}
    assert settings.STORE_NAME == before and store.overrides == {}


def test_bad_saved_value_is_ignored_on_load(store):
    c = sqlite3.connect(str(store.db))
    c.execute("INSERT INTO system_setup (key, value) VALUES ('site_settings', ?)",
              ('{"values": {"SITE_TIMEZONE": "Bad/Zone", "STORAGE_RETENTION_DAYS": 3}}',))
    c.commit()
    c.close()
    store.load()
    assert store.overrides == {"STORAGE_RETENTION_DAYS": 3}
    assert settings.SITE_TIMEZONE == ss.ENV_DEFAULTS["SITE_TIMEZONE"]


def test_missing_database_means_env_defaults(tmp_path, monkeypatch):
    for spec in ss.SPECS:
        monkeypatch.setattr(settings, spec.key, getattr(settings, spec.key))
    st = ss.SiteSettingsStore(db_path_fn=lambda: tmp_path / "none.db")
    assert st.load() == {}
    empty = tmp_path / "no_table.db"
    sqlite3.connect(str(empty)).close()
    assert ss.SiteSettingsStore(db_path_fn=lambda: empty).load() == {}


# ------------------------------------------------------------------ live effect

def test_time_zone_applies_without_restart(store):
    from app.services import timeutil

    store.update({"SITE_TIMEZONE": "Asia/Kolkata"})
    assert timeutil.local_now().utcoffset() == timedelta(hours=5, minutes=30)
    assert store.describe()["store_time"]["zone"] == "Asia/Kolkata"
    store.update({"SITE_TIMEZONE": "Australia/Adelaide"})
    assert timeutil.local_now().utcoffset() in (timedelta(hours=9, minutes=30), timedelta(hours=10, minutes=30))
    store.update({"SITE_TIMEZONE": ""})                     # this device's own zone
    assert timeutil.site_tz() is None


def test_evidence_limits_apply_without_restart(store):
    from app.services.evidence_storage import GB, evidence_storage

    store.update({"EVIDENCE_MAX_GB": 1.5, "STORAGE_RETENTION_DAYS": 3, "STORAGE_MAX_DISK_PERCENT": 80})
    assert evidence_storage.cap() == (int(1.5 * GB), "EVIDENCE_MAX_GB=1.5")
    base = evidence_storage._base_report()
    assert base["retention_days"] == 3 and base["max_disk_percent"] == 80
    assert store.evidence_passes, "a limit change runs an evidence pass at once"

    n = len(store.evidence_passes)
    store.update({"EVIDENCE_MAX_GB": 0})                    # automatic
    assert evidence_storage.cap()[1].startswith("automatic")
    assert len(store.evidence_passes) == n + 1
    store.update({"STORE_NAME": "Shop"})
    assert len(store.evidence_passes) == n + 1              # not an evidence setting


def test_evidence_limit_cannot_exceed_the_disk(store, monkeypatch):
    monkeypatch.setattr(ss, "disk_info", lambda: {"total_bytes": 100 * ss.GB, "free_bytes": 20 * ss.GB,
                                                  "used_bytes": 80 * ss.GB, "evidence_bytes": 2 * ss.GB})
    with pytest.raises(ss.SiteSettingsError) as exc:
        store.update({"EVIDENCE_MAX_GB": 30})
    assert "22 GB" in exc.value.errors["EVIDENCE_MAX_GB"]
    store.update({"EVIDENCE_MAX_GB": 20})
    # The night watch sub-cap must fit inside the total.
    with pytest.raises(ss.SiteSettingsError) as exc:
        store.update({"NIGHT_WATCH_EVIDENCE_MAX_MB": 30 * 1024})
    assert "NIGHT_WATCH_EVIDENCE_MAX_MB" in exc.value.errors
    assert store.describe()["disk"]["max_evidence_gb"] == 22.0


def test_describe_reports_what_automatic_means_on_this_disk(store, monkeypatch):
    # The dashboard compares a typed / reset value with this to decide whether
    # "Automatic (10% of disk)" lowers the evidence limit (inline confirm).
    monkeypatch.setattr(ss, "disk_info", lambda: {"total_bytes": 100 * ss.GB, "free_bytes": 20 * ss.GB,
                                                  "used_bytes": 80 * ss.GB, "evidence_bytes": 2 * ss.GB})
    disk = store.describe()["disk"]
    assert disk["auto_evidence_cap_bytes"] == 10 * ss.GB
    assert disk["auto_evidence_gb"] == 10.0
    monkeypatch.setattr(ss, "disk_info", lambda: {"total_bytes": None, "free_bytes": None,
                                                  "used_bytes": None, "evidence_bytes": None})
    disk = store.describe()["disk"]
    assert disk["auto_evidence_cap_bytes"] is None and disk["auto_evidence_gb"] is None


def test_theft_cooldown_and_confidence_apply_without_restart(store):
    from app.services import camera_roles as cr
    from app.services.pose_analytics import CameraState, ObserveResult, PoseAnalytics, TrackState

    pa = PoseAnalytics()
    cam = CameraState(camera_id="cam_site_settings")
    cam.theft_on = True
    st = TrackState(track_id=7, first_seen=0.0, last_seen=0.0, hands={}, skeletons=deque())

    def fire(ts):
        return pa._raise(cam, st, "CONCEALMENT", {"confidence": 0.9}, ts, ObserveResult(), snapshot=None)

    store.update({"THEFT_INCIDENT_COOLDOWN_SEC": 600})
    assert fire(1000.0)
    assert not fire(1300.0)                                 # inside 600 s
    store.update({"THEFT_INCIDENT_COOLDOWN_SEC": 120})
    assert fire(1300.0)                                     # 300 s > the new 120 s

    store.update({"THEFT_MIN_CONFIDENCE": 0.6})
    assert cr.scaled_theft_thresholds(1.0)["min_confidence"] == pytest.approx(0.6)
    pa._refresh_role(cam)                                   # what every analysed frame does
    st.fired.clear()
    assert not pa._raise(cam, st, "CONCEALMENT", {"confidence": 0.5}, 9000.0, ObserveResult(), snapshot=None)
    assert pa._raise(cam, st, "CONCEALMENT", {"confidence": 0.7}, 9000.0, ObserveResult(), snapshot=None)


def test_tripwire_cooldown_reads_the_live_value(store):
    store.update({"TRIPWIRE_ALERT_COOLDOWN_SEC": 15, "RESTRICTED_AREA_COOLDOWN_SEC": 45})
    assert settings.TRIPWIRE_ALERT_COOLDOWN_SEC == 15 and settings.RESTRICTED_AREA_COOLDOWN_SEC == 45


def test_high_value_change_invalidates_zone_caches(store):
    from app.services.shelf_interaction_service import shelf_interaction_service as sis

    v = sis.version
    store.update({"THEFT_HIGH_VALUE_CATEGORIES": ["SPIRITS", "WINE"]})
    assert settings.THEFT_HIGH_VALUE_CATEGORIES == "SPIRITS,WINE"   # the form consumers parse
    assert sis.version == v + 1
    assert store.describe()["settings"]["THEFT_HIGH_VALUE_CATEGORIES"]["value"] == ["SPIRITS", "WINE"]


# ------------------------------------------------------------------ API

@pytest.fixture
def client(store, monkeypatch):
    """The site-settings router on its own app; ``client.as_role(r)`` signs in as role r."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.services import auth_service as auth_mod

    if not hasattr(auth_mod, "require_admin"):
        pytest.skip("require_admin (stream B) not present")
    import app.routes.site_settings as routes
    monkeypatch.setattr(routes, "site_settings", store)
    app = FastAPI()
    app.include_router(routes.router)
    who = {"user": None}            # None = AUTH_DISABLED / internal key: acts as owner

    def fake_verify(request: Request):
        request.state.user = who["user"]
        return True

    app.dependency_overrides[auth_mod.auth_service.verify_api_access] = fake_verify
    c = TestClient(app)
    c.as_role = lambda role: who.update(user={"type": "user_session", "sub": f"test-{role}", "role": role})
    return c


def test_api_get_put_reset(client, store):
    r = client.get("/api/v1/site-settings")
    assert r.status_code == 200
    body = r.json()
    assert set(body["settings"]) == {s.key for s in ss.SPECS}
    tz = body["settings"]["SITE_TIMEZONE"]
    assert {"value", "default", "overridden", "min", "max", "label", "help", "applies"} <= set(tz)
    assert "Australia/Melbourne" in client.get("/api/v1/site-settings/timezones").json()["timezones"]

    r = client.put("/api/v1/site-settings", json={"values": {"SITE_TIMEZONE": "Australia/Melbourne",
                                                             "THEFT_EXIT_RULE_ENABLED": False}})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["SITE_TIMEZONE", "THEFT_EXIT_RULE_ENABLED"]
    assert settings.SITE_TIMEZONE == "Australia/Melbourne" and settings.THEFT_EXIT_RULE_ENABLED is False

    r = client.put("/api/v1/site-settings", json={"values": {"QUEUE_CONGESTED_WAIT_SEC": 5}})
    assert r.status_code == 422
    assert "QUEUE_CONGESTED_WAIT_SEC" in r.json()["detail"]["errors"]

    r = client.delete("/api/v1/site-settings/SITE_TIMEZONE")
    assert r.status_code == 200 and not r.json()["settings"]["SITE_TIMEZONE"]["overridden"]
    assert client.delete("/api/v1/site-settings/NOT_A_KEY").status_code == 404


def test_api_write_needs_owner_or_admin(client, store):
    client.as_role("operator")
    assert client.get("/api/v1/site-settings").status_code == 200
    assert client.put("/api/v1/site-settings", json={"values": {"STORE_NAME": "X"}}).status_code == 403
    assert client.delete("/api/v1/site-settings/STORE_NAME").status_code == 403
    assert "STORE_NAME" not in store.overrides
    client.as_role("admin")
    r = client.put("/api/v1/site-settings", json={"values": {"STORE_NAME": "Admin set"}})
    assert r.status_code == 200, r.text
    assert settings.STORE_NAME == "Admin set"
