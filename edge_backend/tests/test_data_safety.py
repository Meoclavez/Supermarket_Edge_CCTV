"""Audit 2026-10-29 data-loss and event-loop fixes (L4, L6, M1, M2, M4, M8, M14)."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest


def test_shared_mjpeg_encodes_once_for_many_viewers(monkeypatch):
    import app.main as main

    calls = []

    def fake_render(cam, overlay, width):
        calls.append((cam, overlay, width))
        time.sleep(0.05)
        return b"jpeg-" + cam.encode()

    monkeypatch.setattr(main, "_render_mjpeg_frame", fake_render)
    main._MJPEG_LATEST.clear()

    async def run():
        key = ("cam1", 7, None)
        results = await asyncio.gather(*[main.shared_mjpeg_frame("cam1", key, False, 640) for _ in range(6)])
        again = await main.shared_mjpeg_frame("cam1", key, False, 640)
        other = await main.shared_mjpeg_frame("cam1", ("cam1", 8, None), False, 640)
        return results, again, other

    results, again, other = asyncio.run(run())
    assert results == [b"jpeg-cam1"] * 6 and again == b"jpeg-cam1" and other == b"jpeg-cam1"
    assert len(calls) == 2                      # frame 7 once for 7 viewers, frame 8 once


def test_shared_mjpeg_viewer_disconnect_does_not_cancel_others(monkeypatch):
    import app.main as main

    gate = threading.Event()
    monkeypatch.setattr(main, "_render_mjpeg_frame", lambda c, o, w: (gate.wait(2), b"ok")[1])
    main._MJPEG_LATEST.clear()

    async def run():
        key = ("camX", 1, None)
        first = asyncio.ensure_future(main.shared_mjpeg_frame("camX", key, False, 0))
        second = asyncio.ensure_future(main.shared_mjpeg_frame("camX", key, False, 0))
        await asyncio.sleep(0.02)
        first.cancel()
        gate.set()
        return await second

    assert asyncio.run(run()) == b"ok"


def test_failed_pipeline_flush_keeps_rows_for_the_next_try(monkeypatch):
    from app.services import pipeline_supervisor as ps

    sup = ps.PipelineSupervisor()
    visit = SimpleNamespace(phase="open", visit_id="v1", zone_id="z", track_id="t", camera_id="c",
                            entered_at=None, exited_at=None, dwell_seconds=0.0, interacted=False)
    drains = [([visit], []), ([], [])]
    monkeypatch.setattr(ps.live_engine, "drain", lambda: drains.pop(0))

    async def no_crossings(factory):
        return 0

    import app.services.tripwire_engine as te
    monkeypatch.setattr(te, "flush_tripwire_events", no_crossings)

    class Broken:
        async def __aenter__(self):
            raise RuntimeError("database is locked")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ps, "async_session_factory", lambda: Broken())
    assert asyncio.run(sup.flush_once()) == 0
    assert sup._retry_visits == [visit] and sup.flush_failures == 1

    seen = []

    class Recorder:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def add(self, row):
            seen.append(row)

        async def get(self, *a):
            return None

        async def commit(self):
            pass

    monkeypatch.setattr(ps, "async_session_factory", lambda: Recorder())
    assert asyncio.run(sup.flush_once()) == 1   # the kept visit is written now
    assert [r.id for r in seen] == ["v1"] and sup._retry_visits == []


def test_stalled_camera_is_not_reported_online(monkeypatch):
    from app.services.live_analytics_engine import CameraRuntime, stall_seconds

    rt = CameraRuntime(camera_id="c1", name="C1", source="rtsp://x", enabled=True)
    rt.status = "ONLINE"
    rt.last_frame_at = time.time() - stall_seconds() - 5
    d = rt.to_dict()
    assert d["status"] == "STALLED" and "no new picture" in d["stall_note"]
    rt.last_frame_at = time.time()
    assert rt.to_dict()["status"] == "ONLINE"


def test_replaced_worker_does_not_wipe_its_successors_state():
    from app.services.live_analytics_engine import CameraRuntime, CameraWorker, LiveAnalyticsEngine

    eng = LiveAnalyticsEngine()
    old = CameraWorker(CameraRuntime(camera_id="c1", name="C1", source="", enabled=True), eng)
    new = CameraWorker(CameraRuntime(camera_id="c1", name="C1", source="", enabled=True), eng)
    eng.workers["c1"] = new
    assert not old._is_current() and new._is_current()
    eng.workers.pop("c1")
    assert old._is_current()                    # camera removed: the old worker cleans up


def test_offline_watchdog_bundle_is_disabled_outside_the_plan(monkeypatch):
    from app.services import entitlements, offline_watchdog

    monkeypatch.setattr(entitlements, "feature_enabled", lambda key: key != "phone_alerts")

    async def no_subs():
        return []

    import app.services.push_alerts as pa
    monkeypatch.setattr(pa, "recipient_subscriptions", no_subs)
    wd = offline_watchdog.OfflineWatchdog() if hasattr(offline_watchdog, "OfflineWatchdog") else offline_watchdog.offline_watchdog
    bundle = asyncio.run(wd.build_bundle({"watchdog_enabled": True, "watchdog_offline_min": 15}, "store"))
    assert bundle["enabled"] is False and bundle["messages"] == []


def test_insight_narration_releases_the_write_lock(monkeypatch):
    """L4: the session is committed before the (slow) model call."""
    from app.services import insight_topics

    src = insight_topics.TopicService.analyse_topic if hasattr(insight_topics, "TopicService") else None
    import inspect
    code = inspect.getsource(insight_topics)
    i = code.index("if narrate:")
    assert "await db.commit()" in code[i:i + 600].split("asyncio.to_thread(self._narrate")[0]


def test_restricted_outcome_counts_cover_every_alert_not_only_the_newest(tmp_path):
    """M8: counting in SQL over all RESTRICTED_AREA rows (the poll looked at the newest 200)."""
    import json
    import sqlite3
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.migrations.runner import run_migrations
    from app.services import restricted_areas as ra

    db = tmp_path / "ra.db"
    run_migrations(db, backups_dir=tmp_path / "b")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    rows = []
    for i in range(450):
        meta = {"ra_area_id": "vault" if i < 300 else "office", "ra_trigger": "dwell" if i % 2 else "speed",
                "ra_requires_outcome": True}
        if i >= 440:
            meta["ra_outcome"] = "false_alarm"
        rows.append((f"e{i:04d}", "cam", "C", "L", "RESTRICTED_AREA", "HIGH", 0.9,
                     f"2026-10-01 00:{i // 60:02d}:{i % 60:02d}", json.dumps(meta), 0))
    conn.executemany("INSERT INTO security_events (id, camera_id, camera_name, location, event_type, severity, "
                     "confidence, timestamp, metadata_json, acknowledged) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    async def run():
        eng = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with AsyncSession(eng) as s:
            counts = await ra.open_outcome_counts(s)
            ids = await ra.event_ids(s, area_id="vault", trigger="dwell", needs_outcome=True, limit=500)
            done = await ra.event_ids(s, needs_outcome=False)
        await eng.dispose()
        return counts, ids, done

    counts, ids, done = asyncio.run(run())
    assert counts == {"vault": 300, "office": 140}
    assert len(ids) == 150 and ids[0] == "e0299"          # newest first, oldest included
    assert sorted(done) == [f"e{i:04d}" for i in range(440, 450)]


def test_empty_site_timezone_uses_the_hosts_iana_zone_and_follows_dst(monkeypatch):
    """M18: no fixed UTC offset when SITE_TIMEZONE is empty."""
    from datetime import date
    from app.config import settings
    from app.services import timeutil

    monkeypatch.setattr(settings, "SITE_TIMEZONE", "")
    monkeypatch.setenv("TZ", "Australia/Melbourne")
    timeutil._host_zone_cache.clear()
    tz = timeutil.site_tz()
    assert getattr(tz, "key", None) == "Australia/Melbourne"
    start, end = timeutil.local_midnight_utc(date(2026, 10, 4)), timeutil.local_midnight_utc(date(2026, 10, 5))
    assert (end - start).total_seconds() == 23 * 3600       # DST start day
    assert timeutil.site_tz_source()["source"] == "host"
    monkeypatch.setenv("TZ", "UTC0")
    timeutil._host_zone_cache.clear()
    monkeypatch.setattr(timeutil, "host_zone_name", lambda: None)
    assert timeutil.site_tz() is None and "warning" in timeutil.site_tz_source()
    timeutil._host_zone_cache.clear()
