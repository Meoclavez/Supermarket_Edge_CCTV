"""Insight topics (services/insight_topics.py): grouping, unified analysis, actions, API.

Every test starts from empty ``ai_decision_recommendations`` / ``insight_topics``
tables in the suite's throwaway database. Dates are injected (``today=``), so
nothing depends on the day the suite runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app.config import settings
from app.database import async_session_factory
from app.models.db_models import AIDecisionRecommendationModel as Rec
from app.models.db_models import Base, InsightTopicModel as Topic
from app.services import insight_topics as it
from app.services.insight_topics import (
    Context,
    before_after,
    classify,
    classify_trend,
    insight_topic_service as svc,
    signature,
    subject_tokens,
)

TODAY = date(2026, 10, 21)
DWELL = "Shoppers dwell {s}s in {z} but only {p}% reach for product."
DWELL_WHY = "High attention with low pick-up usually indicates unclear pricing, an out-of-stock facing."
DWELL_DO = "Audit {z} facings and shelf-edge pricing; verify stock against the planogram."


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    engine = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(engine)
    engine.dispose()

    def wipe():
        with sqlite3.connect(str(settings.DATABASE_PATH), timeout=15) as c:
            c.execute("DELETE FROM ai_decision_recommendations")
            c.execute("DELETE FROM insight_topics")
            c.execute("DELETE FROM analysis_runs")

    wipe()
    # No blueprint / Studio zones unless a test supplies them.
    monkeypatch.setattr(it, "load_context", _ctx())
    yield
    wipe()


def _ctx(zones=None, products=None):
    async def load(_db):
        c = Context()
        for z in zones or []:
            c.zones[z["name"].lower()] = z
        for p in products or []:
            c.products[p["name"].lower()] = p
        return c
    return load


def _add(day, zone, finding, *, category="MERCHANDISING", evidence=None, severity="HIGH",
         why=DWELL_WHY, do=None, status="PENDING", updated_at=None) -> str:
    rid = f"rec_{uuid.uuid4().hex[:12]}"

    async def w():
        async with async_session_factory() as s:
            s.add(Rec(id=rid, date=day.isoformat() if isinstance(day, date) else day, category=category,
                      severity=severity, zone=zone, finding=finding, root_cause=why,
                      action_item=do or DWELL_DO.format(z=zone), status=status, evidence=evidence,
                      created_at=datetime(2026, 10, 1, 12), updated_at=updated_at))
            await s.commit()
    _run(w())
    return rid


def _sync(today=TODAY, **kw):
    async def go():
        async with async_session_factory() as s:
            return await svc.sync(s, today=today, **kw)
    return _run(go())


def _topics():
    async def go():
        async with async_session_factory() as s:
            return list((await s.execute(select(Topic))).scalars().all())
    return _run(go())


def _rows():
    async def go():
        async with async_session_factory() as s:
            return list((await s.execute(select(Rec))).scalars().all())
    return _run(go())


def _days(n, end=TODAY):
    return [end - timedelta(days=i) for i in range(n - 1, -1, -1)]


def _pet_food_week():
    d = _days(5, TODAY - timedelta(days=1))
    _add(d[0], "Pet food", DWELL.format(s=52, z="Pet food", p="6.0"), evidence={"engagement_rate_pct": 6.0})
    _add(d[1], "Pet Food", DWELL.format(s=61, z="Pet Food", p="4.2"), evidence={"engagement_rate_pct": 4.2})
    _add(d[2], "Pet foods", "Shoppers spent 40 minutes in front of Pet foods over 7 recorded days but reached "
                            "for it 1 time(s) (0.025 reaches per shopper-minute).",
         category="HEATMAP_BROWSE_NO_TOUCH", severity="MEDIUM",
         why="Browsed but not touched: the product is seen but not picked up.",
         do="Check price label, facings and stock condition of Pet foods (SKU 991).")
    _add(d[3], "Pet food aisle", "No shopper reached for Pet food aisle (SKU 991) today, while the same camera "
                                 "recorded 40 reaches at other products and 30 shoppers.",
         severity="MEDIUM", why="A shelf nobody touches is usually out of stock, hidden or mispriced.",
         do="Check stock, facing and shelf-edge label for Pet food aisle.")
    _add(d[4], "Aisle 7", DWELL.format(s=58, z="Aisle 7", p="3.1"), evidence={"engagement_rate_pct": 3.1})
    return d


# =========================================================================== grouping

def test_normalisation_and_rule_families():
    assert subject_tokens("Pet Foods") == subject_tokens("pet food") == ["pet", "food"]
    assert subject_tokens("Pet food aisle") == ["pet", "food"]
    assert subject_tokens("Aisle 3") == ["aisle", "3"]
    assert subject_tokens("Top-shelf products") == ["top"]
    assert classify("MERCHANDISING", DWELL.format(s=50, z="x", p=3), None)[:2] == ("dwell_low_engage", "low_pickup")
    assert classify("MERCHANDISING", "No shopper reached for X (SKU 1) today", None)[1] == "low_pickup"
    assert classify("HEATMAP_BROWSE_NO_TOUCH", "", None)[1] == "low_pickup"
    assert classify("STAFFING", "Average wait", None)[1:] == ("congestion", "high")
    assert classify("FOOTFALL", "Visits fell below the usual level", None) == ("footfall", "footfall", "low")


def test_pet_food_on_five_days_with_different_wording_is_one_topic(monkeypatch):
    monkeypatch.setattr(it, "load_context", _ctx(zones=[
        {"id": "z7", "name": "Aisle 7", "products": ["Pet food", "Cat litter"]}]))
    _pet_food_week()
    res = _sync()
    assert res["assigned"] == 5 and res["created"] == 1, res
    (t,) = _topics()
    assert t.occurrences == 5 and t.family == "low_pickup"
    assert "pet food" in t.title.lower()
    assert t.zone_id == "z7"                         # the blueprint zone that sells pet food
    assert {r.topic_id for r in _rows()} == {t.id}
    assert t.unified_summary and "Found on all 5 analysed days" in t.unified_summary
    assert [a["priority"] for a in t.unified_actions] == list(range(1, len(t.unified_actions) + 1))
    a = t.unified_actions[0]
    assert {"action", "why", "expected_effect", "effort", "owner_role", "check_after", "done"} <= set(a)


def test_different_zones_and_directions_stay_separate():
    d = _days(3)
    for day in d:
        _add(day, "Aisle 3", DWELL.format(s=50, z="Aisle 3", p="5.0"))
        _add(day, "Aisle 5", DWELL.format(s=50, z="Aisle 5", p="5.0"))
        _add(day, "Bakery", "Bakery saw 3 visits against a store average of 40.", category="STORE_LAYOUT",
             severity="MEDIUM", do="Reposition a destination category into Bakery.")
        _add(day, "Bakery", "Average wait at Bakery is 5.1 minutes.", category="STAFFING", severity="CRITICAL",
             do="Open an additional lane covering Bakery.")
    _add(d[0], "Deli", "Visits rose above the usual level at Deli.", category="FOOTFALL")
    _add(d[1], "Deli", "Visits fell below the usual level at Deli.", category="FOOTFALL")
    _sync()
    keys = sorted(t.topic_key for t in _topics())
    assert len(keys) == 6, keys
    assert sum(1 for t in _topics() if t.occurrences == 3) == 4


def test_real_store_low_traffic_case_groups_by_area_and_reads_well():
    """Anonymised from a store's 12 days: one low-traffic finding a day for two areas
    (one every day, one on 9 days). Two topics; the metric sentence is a real sentence
    ("The share of the store-average visits went from ...", not "Of the store-average ...")."""
    d = _days(12)
    gaps = {d[3], d[6], d[9]}
    for i, day in enumerate(d):
        avg = 1900 + 40 * i
        for zone, visits, skip in (("Chilled room", 60 + 10 * i, set()), ("Aisle 9 · Health", 420 + 5 * i, gaps)):
            if day in skip:
                continue
            _add(day, zone, f"{zone} saw {visits} visits against a store average of {avg}.",
                 category="STORE_LAYOUT", severity="MEDIUM",
                 why="Traffic well below the store average suggests the aisle sits off the dominant circulation path.",
                 do=f"Reposition a destination category into {zone} or add an aisle-end signpost.",
                 evidence={"visits": visits, "store_avg_visits": float(avg)})
    _sync()
    topics = sorted(_topics(), key=lambda t: -t.occurrences)
    assert [(t.subject, t.occurrences, t.family) for t in topics] == [
        ("Chilled room", 12, "low_traffic"), ("Aisle 9 · Health", 9, "low_traffic")]
    for t in topics:
        assert "Of the store-average" not in t.unified_summary, t.unified_summary
        if "went from" in t.unified_summary:
            assert "the share of the store-average visits went from" in t.unified_summary.lower(), t.unified_summary
    ba = before_after(d[5], d, d, [(x, 6.0) for x in d], False, today=d[-1], unit="%",
                      label="of the store-average visits")
    assert "; the share of the store-average visits went from 6.0% to 6.0%" in ba["text"], ba["text"]


def test_similarity_fallback_threshold():
    ctx = Context()
    a = signature("MERCHANDISING", "Pet food", DWELL.format(s=1, z="Pet food", p=1), None, ctx)
    b = signature("MERCHANDISING", "Pet food aisle 2", DWELL.format(s=1, z="Pet food aisle 2", p=1), None, ctx)
    c = signature("MERCHANDISING", "Frozen food", DWELL.format(s=1, z="Frozen food", p=1), None, ctx)
    assert a.key != b.key
    assert it.subject_similarity(a.aliases, b.aliases) >= it.SUBJECT_SIM_MIN
    assert it.subject_similarity(a.aliases, c.aliases) < it.SUBJECT_SIM_MIN      # {pet,food} vs {frozen,food}
    assert it.subject_similarity([["aisle", "3"]], [["aisle", "5"]]) == 0.0


def test_new_finding_joins_existing_topic_incrementally():
    d = _days(4)
    for day in d[:3]:
        _add(day, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync(today=d[2])
    (t,) = _topics()
    first_analysed = t.analysed_at
    assert t.occurrences == 3 and not t.needs_analysis
    _add(d[3], "Pet Foods", DWELL.format(s=70, z="Pet Foods", p="2.0"))
    res = _sync(today=d[3])
    assert res["assigned"] == 1 and res["created"] == 0
    (t,) = _topics()
    assert t.occurrences == 4 and t.last_seen == d[3].isoformat()
    assert t.analysed_at >= first_analysed and not t.needs_analysis


def test_backfill_is_batched_and_idempotent(monkeypatch):
    monkeypatch.setattr(it, "BACKFILL_BATCH", 50)
    zones = [f"Aisle {i}" for i in range(1, 6)]
    days = _days(30)

    async def bulk():
        async with async_session_factory() as s:
            for day in days:
                for z in zones:
                    s.add(Rec(id=f"rec_{uuid.uuid4().hex[:12]}", date=day.isoformat(), category="MERCHANDISING",
                              severity="HIGH", zone=z, finding=DWELL.format(s=50, z=z, p="5.0"),
                              root_cause=DWELL_WHY, action_item=DWELL_DO.format(z=z), status="PENDING"))
            await s.commit()
    _run(bulk())

    async def backfill():
        async with async_session_factory() as s:
            return await svc.backfill(s, today=TODAY)
    r1 = _run(backfill())
    assert r1["assigned"] == 150 and r1["created"] == 5 and r1["unassigned_remaining"] == 0, r1
    before = {(t.id, t.occurrences, t.topic_key) for t in _topics()}
    assert len(before) == 5 and {o for (_i, o, _k) in before} == {30}
    r2 = _run(backfill())
    assert r2["assigned"] == 0 and r2["created"] == 0
    assert {(t.id, t.occurrences, t.topic_key) for t in _topics()} == before


def test_orphan_topics_are_removed_after_their_findings_go():
    _add(TODAY, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync()
    assert len(_topics()) == 1
    with sqlite3.connect(str(settings.DATABASE_PATH)) as c:   # e.g. a store reset (routes/layout.py)
        c.execute("DELETE FROM ai_decision_recommendations")
    assert _sync()["removed_empty"] == 1 and _topics() == []


# ============================================================================ trend

def test_trend_classification():
    d = _days(12)
    # worsening: the headline metric (pick-up share, lower is bad) fell by more than 15%
    series = list(zip(d[:6], [10, 10, 9, 6, 5, 5]))
    assert classify_trend(d[:6], d, today=d[5], series=series, higher_is_bad=False)["value"] == "worsening"
    # improving: metric moved the right way
    series = list(zip(d[:6], [5, 5, 6, 9, 10, 10]))
    assert classify_trend(d[:6], d, today=d[5], series=series, higher_is_bad=False)["value"] == "improving"
    # improving: not found on the last 3+ analysed days
    t = classify_trend(d[:5], d, today=d[-1])
    assert t["value"] == "improving" and "last 7" in t["reason"]
    # recurring: comes and goes
    occ = [d[0], d[2], d[4], d[6], d[8], d[10]]
    assert classify_trend(occ, d[:11], today=d[10])["value"] == "recurring"
    # stable: every analysed day, flat metric
    series = list(zip(d[:5], [5, 5, 5, 5, 5]))
    assert classify_trend(d[:5], d[:5], today=d[4], series=series, higher_is_bad=False)["value"] == "stable"
    # new
    assert classify_trend([d[-1]], d, today=d[-1])["value"] == "new"
    # severity escalation
    sev = [(d[0], "MEDIUM"), (d[1], "HIGH")]
    assert classify_trend(d[:2], d[:2], today=d[1], severities=sev)["value"] == "worsening"
    # back after resolve
    assert classify_trend(d[:3], d[:3], today=d[2], reopened=True)["value"] == "recurring"


def test_weekday_pattern():
    sats = [date(2026, 9, 5) + timedelta(weeks=i) for i in range(4)]
    analysed = [date(2026, 9, 1) + timedelta(days=i) for i in range(28)]
    assert it.weekday_pattern(sats, analysed) == "It shows up mostly at weekends."
    tues = [date(2026, 9, 1) + timedelta(weeks=i) for i in range(4)] + [date(2026, 9, 3)]
    assert it.weekday_pattern(tues, analysed) == "It shows up mostly on Tuesdays."


def test_before_after_effect_of_a_done_action():
    d = _days(20)
    action = d[9]
    occ = d[:9] + [d[12]]                     # every day before, once in the ten days after
    series = [(x, 4.0) for x in d[:9]] + [(d[12], 9.0)]
    ba = before_after(action, occ, d, series, False, today=d[-1], unit="%", label="pick-up share")
    assert ba["verdict"] == "helped"
    assert ba["before"]["days_found"] == 9 and ba["after"]["days_found"] == 1
    assert ba["metric"]["before"] == 4.0 and ba["metric"]["after"] == 9.0
    assert "helped" in ba["text"]
    # unchanged afterwards -> no clear change; too few days -> too early
    ba = before_after(action, d, d, None, None, today=d[-1])
    assert ba["verdict"] == "no_clear_change"
    assert before_after(d[-1], d, d, None, None, today=d[-1])["verdict"] == "too_early"


def test_marking_an_action_done_drives_before_after_and_keeps_done_state():
    d = _days(16, TODAY)
    for day in d[:8]:
        _add(day, "Pet food", DWELL.format(s=50, z="Pet food", p="4.0"), evidence={"engagement_rate_pct": 4.0})
    # analysis kept running afterwards, the finding stopped
    with sqlite3.connect(str(settings.DATABASE_PATH)) as c:
        for day in d[8:]:
            c.execute("INSERT INTO analysis_runs (id, run_trigger, generated_at, findings_count, narrated) "
                      "VALUES (?, 'schedule', ?, 0, 0)", (f"run_{day}", f"{day.isoformat()} 03:00:00"))
    _sync(today=d[7])
    (t,) = _topics()

    async def done():
        async with async_session_factory() as s:
            tt = await s.get(Topic, t.id)
            first = tt.unified_actions[0]["id"]
            await svc.mark_action_done(s, tt, first, on=d[7], today=TODAY)
            return first, tt.unified_actions, (tt.analysis or {}).get("before_after"), tt.trend
    first, actions, ba, trend = _run(done())
    assert ba["verdict"] == "helped" and ba["after"]["days_found"] == 0
    assert trend == "improving"
    by_id = {a["id"]: a for a in actions}
    assert by_id[first]["done"] is True and by_id[first]["done_at"] == d[7].isoformat()
    assert actions[-1]["id"] == first                       # done items sink to the end
    # re-analysis keeps the done state
    async def again():
        async with async_session_factory() as s:
            tt = await s.get(Topic, t.id)
            await svc.reanalyse(s, tt, today=TODAY)
            return {a["id"]: a["done"] for a in tt.unified_actions}
    assert _run(again())[first] is True


# ===================================================================== split / merge

def test_split_and_merge():
    d = _days(3)
    ids = [_add(day, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0")) for day in d]
    _add(d[0], "Dairy", DWELL.format(s=50, z="Dairy", p="5.0"))
    _sync()
    pet = next(t for t in _topics() if "pet" in t.topic_key)
    dairy = next(t for t in _topics() if "dairy" in t.topic_key)

    async def split():
        async with async_session_factory() as s:
            t = await s.get(Topic, pet.id)
            new = await svc.split(s, t, ids[0], today=TODAY)
            return new.id, t.occurrences, new.occurrences
    new_id, left, alone = _run(split())
    assert (left, alone) == (2, 1)
    # a later sync does not pull the split finding back in
    _sync()
    assert {r.id: r.topic_id for r in _rows()}[ids[0]] == new_id

    async def merge():
        async with async_session_factory() as s:
            t = await s.get(Topic, pet.id)
            await svc.merge(s, t, dairy.id, today=TODAY)
            return t.occurrences, list(t.alias_keys or [])
    occ, aliases = _run(merge())
    assert occ == 3 and dairy.topic_key in aliases
    assert {t.id for t in _topics()} == {pet.id, new_id}
    # a new Dairy finding now joins the merged topic
    _add(TODAY + timedelta(days=1), "Dairy", DWELL.format(s=50, z="Dairy", p="5.0"))
    _sync(today=TODAY + timedelta(days=1))
    assert {t.id for t in _topics()} == {pet.id, new_id}


def test_resolved_topic_reopens_as_recurring():
    d = _days(4)
    for day in d[:2]:
        _add(day, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync(today=d[1])
    (t,) = _topics()

    async def resolve():
        async with async_session_factory() as s:
            tt = await s.get(Topic, t.id)
            tt.resolved_at = datetime(2026, 10, 19, 1)
            tt.status = "resolved"
            await s.commit()
    _run(resolve())
    _add(d[3], "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync(today=d[3])
    (t,) = _topics()
    assert t.status == "open" and t.reopened_at is not None and t.trend == "recurring"


# ======================================================================= LLM path

def test_template_without_llm_and_llm_numbers_are_checked(monkeypatch):
    from app.services import business_analysis_service as bas

    for day in _days(3):
        _add(day, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync()
    (t,) = _topics()

    def reanalyse():
        async def go():
            async with async_session_factory() as s:
                tt = await s.get(Topic, t.id)
                await svc.reanalyse(s, tt, narrate=True, today=TODAY)
                return tt.unified_summary, tt.analysis
        return _run(go())

    monkeypatch.setattr(bas.business_analysis_service, "select_model", lambda: None)
    summary, meta = reanalyse()
    assert meta["summary_source"] == "template" and summary.startswith("Shoppers dwell 50s")
    assert "Found on all 3 analysed days" in summary

    class Resp:
        def __init__(self, text):
            self.body = json.dumps({"response": text}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(bas.business_analysis_service, "select_model", lambda: "fake-model")
    monkeypatch.setattr(it.urllib.request, "urlopen", lambda *a, **k: Resp("Pick-ups fell 37% over 9 weeks."))
    summary, meta = reanalyse()
    assert meta["summary_source"] == "template" and "37" not in summary     # invented figures are refused
    monkeypatch.setattr(it.urllib.request, "urlopen",
                        lambda *a, **k: Resp("<think>x</think>Pet food shoppers look but rarely pick up; "
                                             "found on 3 days. Check stock and labels first."))
    summary, meta = reanalyse()
    assert meta["summary_source"] == "local_model" and meta["model"] == "fake-model"
    assert summary.startswith("Pet food shoppers")


# ====================================================================== migration

def test_migration_adds_topics_to_an_existing_database(tmp_path):
    from app.migrations import run_migrations, status
    from app.migrations.runner import discover

    db = tmp_path / "v20.db"
    run_migrations(db, backups_dir=tmp_path / "b", run_reconcile=False,
                   migrations=[m for m in discover() if m.version <= 20])
    with sqlite3.connect(db) as c:
        assert "topic_id" not in {r[1] for r in c.execute("PRAGMA table_info(ai_decision_recommendations)")}
        c.execute("INSERT INTO ai_decision_recommendations (id, date, category, severity, zone, finding, "
                  "root_cause, action_item, status, created_at) VALUES ('r1', '2026-10-01', 'MERCHANDISING', "
                  "'HIGH', 'Pet food', 'f', 'w', 'a', 'PENDING', '2026-10-01 01:00:00')")
    report = run_migrations(db, backups_dir=tmp_path / "b")
    assert report["to_version"] >= 21 and any("insight_topics" in a for a in report["applied"]), report
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT id, topic_id FROM ai_decision_recommendations").fetchall() == [("r1", None)]
        cols = {r[1] for r in c.execute("PRAGMA table_info(insight_topics)")}
        assert {"topic_key", "status", "trend", "unified_summary", "unified_actions", "data_window"} <= cols
        assert "ix_ai_decision_recommendations_topic_id" in {
            r[1] for r in c.execute("PRAGMA index_list(ai_decision_recommendations)")}
    st = status(db)
    assert st["pending"] == [] and st["drift"]["missing"] == [] and st["drift"]["type_mismatches"] == []


# ============================================================================ API

def test_api_list_detail_and_actions(monkeypatch):
    monkeypatch.setattr(it, "load_context", _ctx(zones=[
        {"id": "z7", "name": "Aisle 7", "products": ["Pet food"]}]))
    _pet_food_week()
    _sync(today=date.today())
    c = TestClient(app_())
    body = c.get("/api/v1/analytics/recommendations/topics").json()
    assert body["counts"]["badge"] == 1 and len(body["topics"]) == 1
    tid = body["topics"][0]["id"]
    assert body["topics"][0]["seen_text"].startswith("Seen on 5 days since")
    assert len(body["topics"][0]["timeline"]) == it.TIMELINE_DAYS
    det = c.get(f"/api/v1/analytics/recommendations/topics/{tid}").json()
    assert len(det["insights"]) == 5 and "metrics" in det and det["insights"][0]["date"] >= det["insights"][-1]["date"]
    assert c.get("/api/v1/analytics/recommendations/topics?trend=bogus").status_code == 422
    assert c.get("/api/v1/analytics/recommendations/topics/nope").status_code == 404

    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/acknowledge")
    assert r.status_code == 200 and r.json()["topic"]["status"] == "acknowledged"
    assert c.get("/api/v1/analytics/recommendations/topics").json()["counts"]["badge"] == 0
    assert all(x.status == "REVIEWED" for x in _rows())
    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/snooze", json={"days": 7})
    assert r.json()["topic"]["status"] == "snoozed"
    assert r.json()["topic"]["snooze_until"] == (date.today() + timedelta(days=7)).isoformat()
    action = r.json()["topic"]["actions"][0]["id"]
    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/action-done", json={"action_id": action})
    assert r.status_code == 200 and r.json()["topic"]["action_done_at"] == date.today().isoformat()
    assert c.post(f"/api/v1/analytics/recommendations/topics/{tid}/action-done",
                  json={"action_id": "nope"}).status_code == 404
    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/resolve")
    assert r.json()["topic"]["status"] == "resolved"
    assert c.get("/api/v1/analytics/recommendations/topics").json()["topics"] == []
    assert len(c.get("/api/v1/analytics/recommendations/topics?include_resolved=true").json()["topics"]) == 1
    one = det["insights"][0]["id"]
    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/split", json={"insight_id": one})
    assert r.status_code == 200 and r.json()["new_topic"]["occurrences"] == 1
    new = r.json()["new_topic"]["id"]
    r = c.post(f"/api/v1/analytics/recommendations/topics/{tid}/merge", json={"other_topic_id": new})
    assert r.status_code == 200 and r.json()["topic"]["occurrences"] == 5
    assert c.post(f"/api/v1/analytics/recommendations/topics/{tid}/merge",
                  json={"other_topic_id": tid}).status_code == 409
    assert c.post(f"/api/v1/analytics/recommendations/topics/{tid}/reanalyse", json={}).status_code == 200
    assert c.post("/api/v1/analytics/recommendations/topics/backfill").json()["assigned"] == 0
    # the flat list carries each finding's topic
    flat = c.get("/api/v1/analytics/recommendations?days=30&include_dismissed=true").json()
    assert {i.get("topic_id") for i in flat["items"] if i["source"] != "ai_summary"} == {tid}


def app_():
    from app.main import app

    return app


def test_topic_routes_sit_behind_the_business_insights_gate(monkeypatch):
    from app.services import entitlements as ent

    for p in ("/api/v1/analytics/recommendations/topics", "/api/v1/analytics/recommendations/topics/x/merge",
              "/api/v1/analytics/recommendations/topics/backfill"):
        assert ent.feature_for_path(p) == "business_insights"
    monkeypatch.setattr(ent, "stored_policy", lambda: {"state": "active"})
    monkeypatch.setattr(ent, "effective_state", lambda: {"paused": False, "paused_reason": None, "message": ""})
    monkeypatch.setattr(ent, "feature_enabled", lambda f: f != "business_insights")
    r = TestClient(app_()).get("/api/v1/analytics/recommendations/topics")
    assert r.status_code == 403 and r.json()["code"] == "feature_disabled"


def test_operator_handles_topics_but_backfill_is_admin_only():
    from app.services.auth_service import write_allowed

    op = {"type": "user_session", "sub": "nobody", "role": "operator"}
    from app.routes import insights as routes

    assert write_allowed("POST", "/api/v1/analytics/recommendations/topics/t1/acknowledge", "operator", op,
                         routes.acknowledge_insight_topic)
    for fn in (routes.snooze_insight_topic, routes.resolve_insight_topic, routes.insight_topic_action_done,
               routes.split_insight_topic, routes.merge_insight_topics, routes.reanalyse_insight_topic):
        assert write_allowed("POST", "/x", "operator", op, fn)
        assert not write_allowed("POST", "/x", "viewer", {"type": "stream"}, fn)
    assert not write_allowed("POST", "/api/v1/analytics/recommendations/topics/backfill", "operator", op,
                             routes.backfill_insight_topics)


def test_operator_and_owner_over_real_auth(client):  # noqa: F811
    from tests.test_users_roles import _add as add_user, _h, _login, _owner

    _add(TODAY, "Pet food", DWELL.format(s=50, z="Pet food", p="5.0"))
    _sync()
    (t,) = _topics()
    owner = _owner(client)
    assert add_user(client, owner, "sam").status_code == 201
    sam = _login(client, "sam")
    base = "/api/v1/analytics/recommendations/topics"
    assert client.get(base, headers=_h(sam)).status_code == 200
    assert client.post(f"{base}/{t.id}/acknowledge", headers=_h(sam)).status_code == 200
    r = client.post(f"{base}/backfill", headers=_h(sam))
    assert r.status_code == 403
    assert client.post(f"{base}/backfill", headers=_h(owner)).status_code == 200
    assert client.get(base).status_code in (401, 403)


from tests.test_auth_setup import client  # noqa: E402,F401  (real-auth fixture)
