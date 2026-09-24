"""UX-audit backend items: hourly visitors (P1-5), theft outcomes (P1-6),
read-only GETs + analysis runs + one recommendations list (P1-7).

Runs against the throwaway test database (conftest.py). Rows carry a ``ux_``
prefix and are removed afterwards. The local language model is never called:
``business_analysis_service.narrate`` is stubbed and OLLAMA_BASE_URL points
at a closed port.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import time
import uuid
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import (
    AIDecisionRecommendationModel,
    AnalysisRunModel,
    Base,
    CameraModel,
    CustomerTrackModel,
    HeatmapSnapshotModel,
    StoreZoneModel,
    TheftIncidentModel,
    TripwireEventModel,
    ZoneVisitModel,
)
from app.services import hourly_traffic
from app.services.auth_service import auth_service
from app.services.business_analysis_service import business_analysis_service
from app.services.recommendations_service import recommendations_service
from app.services.retail_metrics_service import retail_metrics_service
from app.services.store_layout_service import store_layout_service
from app.services.timeutil import local_day_bounds_utc, utcnow

TZ = "Australia/Melbourne"          # UTC+10 in June (no DST)
D = date(2025, 6, 11)               # "today" for the hourly tests (a Wednesday)


def _run(coro):
    return asyncio.run(coro)


def _utc(d: date, hour: int, minute: int = 0) -> datetime:
    """Store-local (UTC+10) wall time on day d -> naive UTC."""
    return datetime(d.year, d.month, d.day, hour, minute) - timedelta(hours=10)


async def _add(*rows):
    async with async_session_factory() as db:
        for r in rows:
            db.add(r)
        await db.commit()


def _cleanup():
    async def go():
        async with async_session_factory() as db:
            await db.execute(delete(ZoneVisitModel).where(ZoneVisitModel.camera_id.like("ux_%")))
            await db.execute(delete(CustomerTrackModel).where(CustomerTrackModel.camera_id.like("ux_%")))
            await db.execute(delete(TripwireEventModel).where(TripwireEventModel.camera_id.like("ux_%")))
            await db.execute(delete(HeatmapSnapshotModel).where(HeatmapSnapshotModel.camera_id.like("ux_%")))
            await db.execute(delete(StoreZoneModel).where(StoreZoneModel.id.like("ux_%")))
            await db.execute(delete(TheftIncidentModel).where(TheftIncidentModel.id.like("ux_%")))
            await db.execute(delete(CameraModel).where(CameraModel.id.like("ux_%")))
            await db.execute(delete(AIDecisionRecommendationModel).where(
                AIDecisionRecommendationModel.zone.like("UX %")))
            await db.execute(delete(AnalysisRunModel))
            await db.commit()
    _run(go())


@pytest.fixture
def api(monkeypatch):
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://127.0.0.1:9")

    def fake_narrate(findings, overview, timeout=90.0, heatmap_summary=None):
        return {"summary": f"Stub summary of {len(findings)} finding(s).", "model_used": "stub-model",
                "ollama_active": True, "reason": None}

    monkeypatch.setattr(business_analysis_service, "narrate", fake_narrate)
    recommendations_service.reset()
    _cleanup()
    client = TestClient(app)
    yield client
    _cleanup()
    recommendations_service.reset()


_EMPTY_CELL = base64.b64encode(zlib.compress(b"\x00\x00")).decode()   # one zero uint16 cell


def _uptime_row(cam, d, hour, secs):
    return HeatmapSnapshotModel(space="image", camera_id=cam, kind="presence", bucket_start=_utc(d, hour),
                                bucket_minutes=60, grid_w=1, grid_h=1, encoding="zlib-u16le-v1", scale=1.0,
                                cells=_EMPTY_CELL, total_samples=0, total_value=0.0, peak_value=0.0,
                                uptime_seconds=secs, complete=True, source="live")


def _visit(track, zone, start, minutes, cam="ux_cam"):
    return ZoneVisitModel(id=f"ux_v_{uuid.uuid4().hex[:10]}", zone_id=zone, track_id=track, camera_id=cam,
                          entered_at=start, exited_at=start + timedelta(minutes=minutes),
                          dwell_seconds=minutes * 60.0)


def _crossing(direction, ts):
    return TripwireEventModel(tripwire_id="ux_tw", tripwire_name="UX door", camera_id="ux_door",
                              track_id=f"ux_t_{uuid.uuid4().hex[:6]}", direction=direction, ts=ts,
                              counts_footfall=True)


# =========================================================================== P1-5

def test_hour_starts_follow_store_local_days_and_dst(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TIMEZONE", TZ)
    start, end, hours = hourly_traffic.hour_starts(D)
    assert start == datetime(2025, 6, 10, 14, 0) and end == datetime(2025, 6, 11, 14, 0)
    assert len(hours) == 24
    # Melbourne moves to daylight time on 2025-10-05: a 23-hour store day.
    assert len(hourly_traffic.hour_starts(date(2025, 10, 5))[2]) == 23
    assert len(hourly_traffic.hour_starts(date(2025, 4, 6))[2]) == 25


def test_hourly_visitors_measured_counts_boundaries_and_offline_hours(api, monkeypatch):
    monkeypatch.setattr(settings, "SITE_TIMEZONE", TZ)
    layout = _run(_active_layout())
    y = D - timedelta(days=1)          # yesterday: zone visits only
    _run(_add(
        StoreZoneModel(id="ux_z1", layout_id=layout, name="UX Aisle 1", category="AISLE",
                       polygon=[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]),
        # 23:30 local on the day before yesterday: must not leak into yesterday.
        _visit("ux_trk_E", "ux_z1", _utc(y - timedelta(days=1), 23, 30), 5),
        # 00:15 local yesterday = 14:15 UTC the day before: hour 0 of yesterday.
        _visit("ux_trk_A", "ux_z1", _utc(y, 0, 15), 5),
        # B visits two zones (09:10 and 10:05): one visitor, two zone visits.
        _visit("ux_trk_B", "ux_z1", _utc(y, 9, 10), 30),
        _visit("ux_trk_B", "ux_z2", _utc(y, 10, 5), 5),
        # C overlaps B at 09:20-09:30 -> two people present at once.
        _visit("ux_trk_C", "ux_z2", _utc(y, 9, 20), 10),
        # 12:00 recorded for the whole hour with nobody seen -> measured zero.
        _uptime_row("ux_cam", y, 12, 3600.0),
        # 13:00 recorded for 20 minutes only -> partial.
        _uptime_row("ux_cam", y, 13, 1200.0),
        # Today: entrance line crossings, hour 08 fully recorded.
        _uptime_row("ux_door", D, 8, 3600.0),
        _crossing("in", _utc(D, 8, 5)), _crossing("in", _utc(D, 8, 6)), _crossing("out", _utc(D, 8, 30)),
        _crossing("in", _utc(D, 14, 45)),
    ))

    res = api.get(f"/api/v1/analytics/footfall/hourly?date={D.isoformat()}")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["timezone"] == TZ and data["date"] == "2025-06-11"
    assert data["same_weekday_last_week"]["date"] == "2025-06-04"
    assert data["same_weekday_last_week"]["weekday"] == data["today"]["weekday"] == "Wednesday"

    ys = data["yesterday"]
    assert ys["source"] == "zone_visits" and ys["source_label"] == "Estimated from zone visits"
    h = {x["index"]: x for x in ys["hours"]}
    assert ys["hours"][0]["start"].startswith("2025-06-10T00:00:00+10:00")
    assert h[0]["status"] == "measured" and h[0]["visitors"] == 1 and h[0]["zone_visits"] == 1
    assert h[9]["visitors"] == 2 and h[9]["zone_visits"] == 2 and h[9]["people_present_peak"] == 2
    assert h[10]["visitors"] == 0 and h[10]["zone_visits"] == 1 and h[10]["people_present_peak"] == 1
    # Offline is not zero: no recorded uptime and nothing observed.
    for i in (1, 5, 11, 23):
        assert h[i]["status"] == "no_data", i
        assert h[i]["visitors"] is None and h[i]["zone_visits"] is None and h[i]["people_present_peak"] is None
    assert h[12]["status"] == "measured" and h[12]["visitors"] == 0 and h[12]["people_present_peak"] == 0
    assert h[13]["status"] == "partial" and h[13]["uptime_seconds"] == 1200.0 and h[13]["visitors"] == 0
    # The hours add up to the footfall figure for the day (same definition).
    start, end = local_day_bounds_utc(datetime(y.year, y.month, y.day))
    assert ys["totals"]["visitors"] == 3 == _run(_footfall(start, end))

    td = data["today"]
    assert td["source"] == "tripwire" and td["tripwires_present"] is True
    t = {x["index"]: x for x in td["hours"]}
    assert t[8]["status"] == "measured" and t[8]["uptime_seconds"] == 3600.0
    assert (t[8]["footfall_in"], t[8]["footfall_out"], t[8]["visitors"]) == (2, 1, 2)
    assert t[14]["footfall_in"] == 1 and t[14]["status"] == "measured"
    assert t[15]["status"] == "no_data" and t[15]["footfall_in"] is None
    assert td["totals"]["footfall_in"] == 3 and td["totals"]["footfall_out"] == 1
    assert data["busiest_hour"]["hour"] == 8 and data["busiest_hour"]["visitors"] == 2

    lw = data["same_weekday_last_week"]
    assert lw["source"] is None and lw["totals"]["visitors"] is None
    assert all(x["status"] == "no_data" and x["visitors"] is None for x in lw["hours"])
    assert data["comparison"]["sources_match"] is False and data["comparison"]["note"]

    assert api.get("/api/v1/analytics/footfall/hourly?date=junk").status_code == 422


def test_hourly_visitors_future_hours_today(api):
    data = api.get("/api/v1/analytics/footfall/hourly").json()
    hours = data["today"]["hours"]
    now_ts = time.time()
    for x in hours:
        if datetime.fromisoformat(x["start"]).timestamp() > now_ts + 1 and not x["in_progress"]:
            assert x["status"] == "future" and x["visitors"] is None
    assert sum(1 for x in hours if x["in_progress"]) == 1


async def _active_layout():
    async with async_session_factory() as db:
        return (await store_layout_service.get_active_layout(db)).id


async def _footfall(start, end):
    async with async_session_factory() as db:
        return await retail_metrics_service.footfall(db, start, end)


# =========================================================================== P1-6

def _incident(**kw):
    fields = dict(id=f"ux_inc_{uuid.uuid4().hex[:10]}", theft_type="CONCEALMENT", rule="CONCEALMENT",
                  severity="HIGH", status="ACTIVE", department="UX", camera_id="ux_cam", camera_name="UX cam",
                  timestamp=utcnow(), confidence=0.5, estimated_loss_value=0.0, items_involved=[])
    fields.update(kw)
    _run(_add(TheftIncidentModel(**fields)))
    return fields["id"]


def _headers(sub="ux_operator"):
    token = auth_service.create_access_token({"sub": sub, "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


def test_resolve_requires_an_explicit_outcome(api):
    inc = _incident()
    url = f"/api/v1/theft/incidents/{inc}/resolve"
    assert api.post(url).status_code == 422                                 # no body
    assert api.post(url, json={}).status_code == 422                        # no outcome
    assert api.post(url, json={"notes": "checked"}).status_code == 422
    assert api.post(url, json={"outcome": "BOGUS"}).status_code == 422
    assert api.post(url, json={"outcome": "FALSE_ALARM", "recovered_value": 5}).status_code == 422
    assert api.post(url, json={"outcome": "RECOVERED_GOODS", "recovered_value": -1}).status_code == 422
    assert api.get(f"/api/v1/theft/incidents/{inc}").json()["status"] == "ACTIVE"   # nothing defaulted

    r = api.post(url, json={"outcome": "customer_paid", "notes": "Paid at lane 2", "recovered_value": 12.5},
                 headers=_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "RESOLVED" and body["outcome"] == body["resolution"] == "CUSTOMER_PAID"
    assert body["recovered_value"] == 12.5 and body["resolved_by"] == "ux_operator"
    assert body["outcome_label"] == "Customer paid" and body["notes"] == "Paid at lane 2"

    # The mobile app's field name and old value still work.
    inc2 = _incident()
    r = api.post(f"/api/v1/theft/incidents/{inc2}/resolve", json={"resolution": "POLICE_DISPATCHED"})
    assert r.status_code == 200 and r.json()["resolution"] == "POLICE_REPORTED"

    outcomes = api.get("/api/v1/theft/outcomes").json()["outcomes"]
    ids = {o["id"] for o in outcomes}
    assert {"RECOVERED_GOODS", "FALSE_ALARM", "CUSTOMER_PAID", "NO_ACTION", "POLICE_REPORTED"} <= ids
    fa = next(o for o in outcomes if o["id"] == "FALSE_ALARM")
    assert fa["counts_as_actioned"] is False and fa["is_false_alarm"] is True


def test_statistics_exclude_false_alarms_and_report_rate_per_rule(api):
    before = api.get("/api/v1/theft/statistics").json()

    def resolve(inc, **body):
        r = api.post(f"/api/v1/theft/incidents/{inc}/resolve", json=body, headers=_headers())
        assert r.status_code == 200, r.text

    resolve(_incident(rule="UX_RULE_A", estimated_loss_value=100.0), outcome="FALSE_ALARM")
    resolve(_incident(rule="UX_RULE_A", estimated_loss_value=50.0), outcome="RECOVERED_GOODS", recovered_value=40.0)
    resolve(_incident(rule="UX_RULE_A", estimated_loss_value=30.0), outcome="NO_ACTION")
    resolve(_incident(rule="UX_RULE_B", estimated_loss_value=20.0), outcome="CUSTOMER_PAID")
    # Resolved before outcomes were required (possibly the old default): not trusted.
    _incident(rule="UX_RULE_B", estimated_loss_value=999.0, status="RESOLVED", resolution="RECOVERED_GOODS")
    _incident(rule="UX_RULE_B", estimated_loss_value=10.0)   # still open

    after = api.get("/api/v1/theft/statistics").json()
    assert round(after["value_actioned"] - before["value_actioned"], 2) == 70.0
    assert round(after["prevented_loss_estimate"] - before["prevented_loss_estimate"], 2) == 70.0
    assert round(after["value_pending_outcome"] - before["value_pending_outcome"], 2) == 10.0
    assert after["unverified_legacy_resolutions"] - before["unverified_legacy_resolutions"] == 1
    assert after["value_recovered"] - (before["value_recovered"] or 0.0) == pytest.approx(40.0)
    assert after["false_alarm_count"] - before["false_alarm_count"] == 1
    rules = {r["rule"]: r for r in after["false_alarm_rate_by_rule"]}
    assert rules["UX_RULE_A"]["reviewed"] == 3 and rules["UX_RULE_A"]["false_alarms"] == 1
    assert rules["UX_RULE_A"]["false_alarm_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert rules["UX_RULE_B"]["reviewed"] == 1 and rules["UX_RULE_B"]["false_alarm_rate"] == 0.0
    assert after["reviewed_count"] >= 4 and 0.0 <= after["false_alarm_rate"] <= 1.0


def test_dispatch_records_who_and_when_and_alerts_phones_without_fabrication(api, monkeypatch):
    from app.services.alert_dispatcher import alert_dispatcher

    calls = []

    async def fake_dispatch(event_type, severity, title, body, data=None, **kw):
        calls.append({"event_type": event_type, "severity": severity, "title": title, "body": body,
                      "data": data, **kw})
        return {"alert_id": "evt_ux", "logged": False, "websocket_clients": 0,
                "push": {"devices": 1, "sent": 1, "failed": 0, "skipped": None}}

    monkeypatch.setattr(alert_dispatcher, "dispatch", fake_dispatch)
    inc = _incident(rule="SHELF_SWEEPING", theft_type="SHELF_SWEEPING")
    r = api.post(f"/api/v1/theft/incidents/{inc}/dispatch", headers=_headers("ux_manager"))
    assert r.status_code == 200, r.text
    body = r.json()
    details = body["dispatch_details"]
    assert body["status"] == "DISPATCHED" and body["dispatched_by"] == "ux_manager" and body["dispatched_at"]
    assert details["dispatched_by"] == "ux_manager" and details["dispatched_at"]
    # Nothing invented: no guard unit, no deterrent, no announcement.
    for key in ("guard_unit", "staff_sent", "audio_deterrent_triggered", "announcement_type"):
        assert key not in details
    assert details["alert"] == {"alert_id": "evt_ux", "logged": False, "websocket_clients": 0,
                                "phones_paired": 1, "phones_sent": 1, "phones_failed": 0, "skipped": None}
    assert len(calls) == 1 and calls[0]["bypass_cooldown"] is True
    assert calls[0]["event_type"] == "SHELF_SWEEP" and calls[0]["data"]["incident_id"] == inc
    assert "ux_manager" in calls[0]["body"]

    # What the operator typed is recorded as typed; nothing else.
    inc2 = _incident()
    r = api.post(f"/api/v1/theft/incidents/{inc2}/dispatch",
                 json={"staff_name": "Sam", "note": "Aisle 4", "notify_phones": False})
    d2 = r.json()["dispatch_details"]
    assert d2["staff_sent"] == "Sam" and d2["note"] == "Aisle 4" and "alert" not in d2
    assert len(calls) == 1

    # Acknowledge records the actor, not a made-up guard id.
    inc3 = _incident()
    r = api.post(f"/api/v1/theft/incidents/{inc3}/acknowledge", headers=_headers("ux_night"))
    assert r.json()["guard_id"] == "ux_night"


# =========================================================================== P1-7

def _seed_analysis_fixture():
    """Data that makes the rule engine produce a finding (checkout wait > 4.5 min)."""
    layout = _run(_active_layout())
    start, _ = local_day_bounds_utc()
    rows = [
        StoreZoneModel(id="ux_checkout", layout_id=layout, name="UX Checkout", category="CHECKOUT",
                       polygon=[{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]),
        CameraModel(id="ux_get_cam", name="UX cam", location="UX", rtsp_url="rtsp://127.0.0.1:1/ux",
                    status="OFFLINE", is_ai_enabled=False),
    ]
    for i in range(6):
        rows.append(_visit(f"ux_q{i}", "ux_checkout", start + timedelta(seconds=10 + i), 5))
    rows.append(_crossing("in", start + timedelta(seconds=30)))
    rows.append(_uptime_row("ux_cam", date.today(), 0, 60.0))
    _run(_add(*rows))
    inc = _incident()
    return inc


def _db_fingerprint() -> dict:
    con = sqlite3.connect(str(Path(settings.DATABASE_PATH).resolve()))
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        out = {}
        for n in names:
            rows = con.execute(f'SELECT * FROM "{n}"').fetchall()
            out[n] = (len(rows), hashlib.sha256(repr(sorted(map(repr, rows))).encode()).hexdigest())
        return out
    finally:
        con.close()


# GET paths that stream forever or serve media files; everything else is called.
_SKIP_GET = {"/stream", "/api/v1/system/preflight"}
_PARAMS = {"date_str": "2025-06-11", "filename": "ux.jpg"}


def test_no_get_route_writes_to_the_database(api):
    inc = _seed_analysis_fixture()
    params = dict(_PARAMS, camera_id="ux_get_cam", incident_id=inc, zone_id="ux_checkout",
                  snapshot_id="1", event_id="ux_evt", segment_id="1", archive_id="ux", alert_id="ux")
    paths = [p for p, ops in app.openapi()["paths"].items() if "get" in ops and p not in _SKIP_GET]
    assert len(paths) > 60

    # The fixture really exercises the analysis: a writing GET would persist this.
    live = api.get("/api/v1/analytics/business/analysis").json()
    assert live["findings_count"] >= 1 and live["persisted"] is False

    # What the lifespan creates at startup (the device id; the default layout
    # was created by the fixture above). The lifespan does not run under
    # TestClient without a context manager.
    from app.services import device_identity
    device_identity.ensure_identity()
    sweep = TestClient(app, raise_server_exceptions=False)   # a 500 on a dummy id is not a write
    before = _db_fingerprint()
    statuses = {}
    for p in paths:
        url = p
        for k, v in params.items():
            url = url.replace("{" + k + "}", v)
        assert "{" not in url, url
        statuses[p] = sweep.get(url).status_code
        after = _db_fingerprint()
        changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
        assert not changed, f"GET {p} changed tables {changed}"
    assert statuses["/api/v1/analytics/digest"] == 200
    assert statuses["/api/v1/analytics/recommendations"] == 200


def test_analysis_runs_and_the_consolidated_recommendations_list(api):
    _seed_analysis_fixture()
    empty = api.get("/api/v1/analytics/recommendations").json()
    assert empty["latest_run"] is None and empty["empty_reason"]
    assert api.get("/api/v1/analytics/business/analysis/latest").json()["run"] is None

    r = api.post("/api/v1/analytics/business/analysis/run", headers=_headers("ux_owner"))
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["trigger"] == "manual" and run["requested_by"] == "ux_owner" and run["generated_at"]
    assert run["findings_count"] >= 1 and run["result"]["findings_count"] == run["findings_count"]
    inputs = run["inputs_summary"]
    assert {"window", "footfall", "footfall_source", "zones_assessed", "zones_total",
            "pos_connected", "sufficient_data"} <= set(inputs)

    # Rate-limited: a second run straight away is refused, nothing recorded.
    r2 = api.post("/api/v1/analytics/business/analysis/run")
    assert r2.status_code == 429 and int(r2.headers["Retry-After"]) >= 1
    assert r2.json()["latest_run_id"] == run["id"]

    latest = api.get("/api/v1/analytics/business/analysis/latest").json()
    assert latest["run"]["id"] == run["id"] and latest["generated_at"] == run["generated_at"]
    assert latest["inputs_summary"] == inputs

    rec = api.get("/api/v1/analytics/recommendations").json()
    assert rec["latest_run"]["id"] == run["id"] and rec["generated_at"] == run["generated_at"]
    item = next(i for i in rec["items"] if i["zone"] == "UX Checkout")
    assert item["source"] == "business_rules" and item["category"] == "STAFFING"
    assert item["priority"] == "critical" and item["status"] == "PENDING"
    assert item["evidence"]["avg_wait_seconds"] == 300.0 and item["evidence_available"] is True
    assert item["do"] and item["why"] and item["title"]
    assert item["status_endpoint"] == f"/api/v1/analytics/decisions/{item['id']}/action"
    assert rec["counts"]["by_source"]["business_rules"] >= 1

    # The market tab's POST is also a recorded run; inside the limit it reuses the latest.
    recommendations_service.reset()
    m = api.post("/api/v1/analytics/market/llm-optimize")
    assert m.status_code == 200 and m.json()["reused_run"] is False
    assert m.json()["narrative"]["summary"].startswith("Stub summary")
    again = api.post("/api/v1/analytics/market/llm-optimize").json()
    assert again["reused_run"] is True and again["run"]["id"] == m.json()["run"]["id"]

    rec = api.get("/api/v1/analytics/recommendations").json()
    summary = next(i for i in rec["items"] if i["source"] == "ai_summary")
    assert summary["do"].startswith("Stub summary") and summary["evidence"]["model"] == "stub-model"
    assert summary["evidence"]["based_on_findings"] and summary["status"] == "INFO"

    # Dismissed items leave the default list but stay retrievable.
    r = api.post(f"/api/v1/analytics/decisions/{item['id']}/action", json={"status": "DISMISSED"})
    assert r.status_code == 200
    ids = {i["id"] for i in api.get("/api/v1/analytics/recommendations").json()["items"]}
    assert item["id"] not in ids
    ids = {i["id"] for i in api.get("/api/v1/analytics/recommendations?include_dismissed=true").json()["items"]}
    assert item["id"] in ids

    # Scheduled runs are not subject to the manual rate limit.
    async def scheduled():
        async with async_session_factory() as db:
            return await recommendations_service.run(db, trigger="schedule", requested_by="scheduler")
    assert _run(scheduled())["trigger"] == "schedule"


def test_digest_is_read_only_and_labels_its_scorecard(api):
    _seed_analysis_fixture()
    before = _db_fingerprint()
    rep = api.get("/api/v1/analytics/digest").json()
    assert rep["findings_count"] >= 1
    assert set(rep["kpi_scorecard_labels"]) == set(rep["kpi_scorecard"])
    assert _db_fingerprint() == before


def test_pos_status_explains_how_to_connect(api):
    s = api.get("/api/v1/analytics/pos/status").json()
    assert isinstance(s["connected"], bool)
    assert s["ingest"]["path"] == "/api/v1/analytics/pos/ingest" and s["ingest"]["method"] == "POST"
