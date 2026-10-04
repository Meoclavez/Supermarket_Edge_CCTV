""""Today" is the store's day, not the host's.

The host runs in Pacific/Pago_Pago (UTC-11) and the store is set to
Pacific/Kiritimati (UTC+14): 25 hours apart, so the two calendar dates always
differ. Every check below fails if the code reads the host clock
(``date.today()``, naive ``datetime.now()``, ``datetime.fromtimestamp``) where
it means the store's day or hour. SITE_TIMEZONE is changed by assigning the
setting, which is what the dashboard's site-settings overlay does; the second
half of each test changes it again to prove the zone is read at call time.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import AIDecisionRecommendationModel, Base

HOST_TZ = "Pacific/Pago_Pago"      # UTC-11, no DST
STORE_TZ = "Pacific/Kiritimati"    # UTC+14, no DST
ZONE = "TZ day test zone"


def _store_dates(tz: str = STORE_TZ) -> set[str]:
    """Today in ``tz``; a set so a test straddling midnight still passes."""
    return {datetime.now(ZoneInfo(tz)).date().isoformat()}


@pytest.fixture
def far_zones(monkeypatch):
    """Host clock in Pago Pago, store in Kiritimati; both restored afterwards."""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = HOST_TZ
    time.tzset()
    monkeypatch.setattr(settings, "SITE_TIMEZONE", STORE_TZ)
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    _cleanup()
    # The premise of every test: the host's calendar day is not the store's.
    assert date.today().isoformat() not in _store_dates()
    try:
        yield
    finally:
        _cleanup()
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def _cleanup():
    async def go():
        async with async_session_factory() as db:
            await db.execute(delete(AIDecisionRecommendationModel).where(
                AIDecisionRecommendationModel.zone == ZONE))
            await db.commit()
    asyncio.run(go())


def _offset(iso: str) -> str:
    return iso[-6:]


# ------------------------------------------------------------------ routes/analytics.py

def test_digest_reports_the_store_day(far_zones):
    api = TestClient(app)
    before = _store_dates()
    rep = api.get("/api/v1/analytics/digest").json()
    assert rep["date"] in before | _store_dates()
    assert rep["date"] != date.today().isoformat()
    # generated_at is the store's clock, with its offset, not naive host time.
    assert _offset(rep["generated_at"]) == "+14:00"

    # A live change of the zone applies to the next request.
    settings.SITE_TIMEZONE = HOST_TZ
    rep = api.get("/api/v1/analytics/report/daily").json()
    assert rep["date"] in _store_dates(HOST_TZ)
    assert _offset(rep["generated_at"]) == "-11:00"


def test_overview_timestamp_is_store_time(far_zones):
    api = TestClient(app)
    r = api.get("/api/v1/analytics/overview")
    assert r.status_code == 200, r.text
    assert _offset(r.json()["timestamp"]) == "+14:00"


def test_telemetry_sync_defaults_to_the_store_day(far_zones):
    api = TestClient(app)
    before = _store_dates()
    r = api.post("/api/v1/analytics/sync", json={})
    assert r.status_code == 200, r.text
    assert r.json()["date"] in before | _store_dates()

    settings.SITE_TIMEZONE = HOST_TZ
    assert api.post("/api/v1/analytics/sync", json={}).json()["date"] in _store_dates(HOST_TZ)


# ------------------------------------------------------------------ analysis + recommendations

def test_findings_persist_attach_and_list_under_the_store_day(far_zones):
    from app.services.business_analysis_service import Finding, business_analysis_service
    from app.services.recommendations_service import recommendations_service

    finding = Finding(category="merchandising_engagement", severity="HIGH", zone=ZONE,
                      finding="f", root_cause="r", action_item="a", evidence={"visits": 9})

    async def go():
        async with async_session_factory() as db:
            await business_analysis_service._persist(db, [finding])
        async with async_session_factory() as db:
            rows = (await db.execute(select(AIDecisionRecommendationModel).where(
                AIDecisionRecommendationModel.zone == ZONE))).scalars().all()
            # _attach_evidence looks the row up by the same store-day key.
            await recommendations_service._attach_evidence(db, [finding.to_dict()])
        async with async_session_factory() as db:
            attached = (await db.execute(select(AIDecisionRecommendationModel).where(
                AIDecisionRecommendationModel.zone == ZONE))).scalar_one()
            listed = await recommendations_service.consolidated(db, days=1)
        return rows, attached, listed

    before = _store_dates()
    rows, attached, listed = asyncio.run(go())
    assert [r.date for r in rows] and rows[0].date in before | _store_dates()
    assert rows[0].date != date.today().isoformat()
    assert attached.evidence == {"visits": 9}
    assert ZONE in {i["zone"] for i in listed["items"]}

    # A finding filed on the host's calendar day is a day or two before the
    # store's today, so a one-day list in the store's zone leaves it out. Under
    # the host clock (the old date.today()) it was "today" and listed.
    old = AIDecisionRecommendationModel(id="rec_tzday_host", date=date.today().isoformat(),
                                        category="merchandising_engagement", severity="LOW",
                                        zone=ZONE, finding="older", root_cause="r", action_item="a",
                                        status="PENDING")

    async def listed_ids():
        async with async_session_factory() as db:
            return {i["id"] for i in (await recommendations_service.consolidated(db, days=1))["items"]}

    async def add_old():
        async with async_session_factory() as db:
            db.add(old)
            await db.commit()

    asyncio.run(add_old())
    assert "rec_tzday_host" not in asyncio.run(listed_ids())

    # Live change: with the store moved to the host's zone that day is today.
    settings.SITE_TIMEZONE = HOST_TZ
    assert "rec_tzday_host" in asyncio.run(listed_ids())

    async def persist_again():
        async with async_session_factory() as db:
            await business_analysis_service._persist(db, [finding])
        async with async_session_factory() as db:
            return {r.date for r in (await db.execute(select(AIDecisionRecommendationModel).where(
                AIDecisionRecommendationModel.zone == ZONE))).scalars().all()}

    # A new run now files under the new zone's day.
    assert asyncio.run(persist_again()) & _store_dates(HOST_TZ)


# ------------------------------------------------------------------ hour-of-day windows

def test_push_quiet_hours_follow_the_store_clock(far_zones):
    from app.services import pairing_service

    h = datetime.now(ZoneInfo(STORE_TZ)).hour
    quiet = {"start": f"{h:02d}:00", "end": f"{(h + 1) % 24:02d}:00"}
    # The host clock is one hour behind the store's wall time here.
    assert pairing_service.in_quiet_hours(quiet)
    settings.SITE_TIMEZONE = HOST_TZ
    assert not pairing_service.in_quiet_hours(quiet)


def test_shadow_trial_window_is_store_time(far_zones, monkeypatch):
    from app.services.shadow_trial import ShadowTrial

    # 2026-01-01 00:30 UTC = 14:30 in Kiritimati = 13:30 the day before in Pago Pago.
    ts = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp()
    t = ShadowTrial(wall=lambda: ts, start_threads=False)
    monkeypatch.setattr(settings, "SHADOW_TRIAL_WINDOW", "14:00-15:00")
    assert t._window_open()
    settings.SITE_TIMEZONE = HOST_TZ
    assert not t._window_open()
