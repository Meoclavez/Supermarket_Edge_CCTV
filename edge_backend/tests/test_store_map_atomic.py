"""Store map site changes (extend / fit) are atomic, serialised and idempotent.

Live incident 2026-10-21 (build 3ec190d): the owner clicked "Fit to content"
repeatedly; the requests overlapped and some failed with ``database is
locked``. Afterwards every structure and camera had moved by (+2, +6) m but
every zone by (+43, +2) m: zones and structures were translated by different
totals. These tests pin the guarantees the fix gives:

* a site change reads and writes everything under one write lock, so two
  overlapping changes cannot compute from a stale snapshot (lost update);
* a lock error leaves nothing changed (no partial translate);
* fit is idempotent; any sequence keeps zones, structures and cameras at the
  same offsets relative to each other.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import CameraModel
from app.services.auth_service import auth_service
from app.services.store_layout_service import store_layout_service

CAM_ID = "cam_map_atomic"


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _run(coro):
    return asyncio.run(coro)


async def _insert_camera(cam_id: str, x: float, y: float) -> None:
    async with async_session_factory() as db:
        existing = await db.get(CameraModel, cam_id)
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        db.add(CameraModel(
            id=cam_id, name="Atomic cam", location="test", rtsp_url="rtsp://192.0.2.1/unreachable",
            is_ai_enabled=False, channel_number=92, floor_x=x, floor_y=y, map_placed=True,
        ))
        await db.commit()


@pytest.fixture
def site(client, auth_headers):
    """A 36 x 21 m site whose content touches the origin (like the live store)."""
    assert client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers).status_code == 200
    assert client.put("/api/v1/layout", json={"width_m": 36, "height_m": 21}, headers=auth_headers).status_code == 200
    for i in range(4):
        r = client.post("/api/v1/layout/zones", json={
            "name": f"Aisle {i}", "category": "AISLE",
            "polygon": [{"x": 1 + 8 * i, "y": 1}, {"x": 6 + 8 * i, "y": 1}, {"x": 6 + 8 * i, "y": 20},
                        {"x": 1 + 8 * i, "y": 20}],
        }, headers=auth_headers)
        assert r.status_code == 201, r.text
    for i in range(6):
        r = client.post("/api/v1/layout/structures", json={
            "kind": "SHELF", "name": f"Shelf {i}",
            "polygon": [{"x": 2 + 5 * i, "y": 3}, {"x": 4 + 5 * i, "y": 3}, {"x": 4 + 5 * i, "y": 18},
                        {"x": 2 + 5 * i, "y": 18}],
        }, headers=auth_headers)
        assert r.status_code == 201, r.text
    r = client.post("/api/v1/layout/structures", json={
        "kind": "ROOM", "name": "Building",
        "polygon": [{"x": 0, "y": 0}, {"x": 36, "y": 0}, {"x": 36, "y": 21}, {"x": 0, "y": 21}],
    }, headers=auth_headers)
    assert r.status_code == 201, r.text
    _run(_insert_camera(CAM_ID, 10.0, 0.5))
    yield _get(client, auth_headers)
    client.post("/api/v1/layout/reset", json={"confirm": "RESET"}, headers=auth_headers)


def _get(client, headers):
    res = client.get("/api/v1/layout", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _offsets(before: dict, after: dict) -> set[tuple[str, float, float]]:
    """Every (kind, dx, dy) by which a point moved between two layouts."""
    out: set[tuple[str, float, float]] = set()
    for kind in ("zones", "structures"):
        old = {i["id"]: i["polygon"] for i in before[kind]}
        for item in after[kind]:
            for p, q in zip(old[item["id"]], item["polygon"]):
                out.add((kind, round(q["x"] - p["x"], 3), round(q["y"] - p["y"], 3)))
    old_c = {c["camera_id"]: c for c in before["cameras"]}
    for c in after["cameras"]:
        o = old_c[c["camera_id"]]
        out.add(("cameras", round(c["floor_x"] - o["floor_x"], 3), round(c["floor_y"] - o["floor_y"], 3)))
    return out


def assert_aligned(before: dict, after: dict) -> tuple[float, float]:
    """Zones, structures and cameras all moved by one and the same (dx, dy)."""
    moves = {(dx, dy) for _, dx, dy in _offsets(before, after)}
    assert len(moves) == 1, f"items moved by different amounts: {sorted(_offsets(before, after))}"
    return next(iter(moves))


def assert_fits(layout: dict, margin: float = 2.0) -> None:
    xs = [p["x"] for k in ("zones", "structures") for i in layout[k] for p in i["polygon"]]
    ys = [p["y"] for k in ("zones", "structures") for i in layout[k] for p in i["polygon"]]
    xs += [c["floor_x"] for c in layout["cameras"]]
    ys += [c["floor_y"] for c in layout["cameras"]]
    assert min(xs) == pytest.approx(margin, abs=0.002) and min(ys) == pytest.approx(margin, abs=0.002)
    assert layout["width_m"] == pytest.approx(max(xs) + margin, abs=0.002)
    assert layout["height_m"] == pytest.approx(max(ys) + margin, abs=0.002)


# ------------------------------------------------------------ idempotence


def test_fit_twice_is_unchanged_and_bounds_are_margin_around_content(client, auth_headers, site):
    first = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert first.status_code == 200, first.text
    assert assert_aligned(site, first.json()) == (2, 2)
    assert_fits(first.json())
    for _ in range(5):
        again = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
        assert again.status_code == 200, again.text
        assert again.json()["site_change"].get("unchanged") is True
    assert _offsets(first.json(), _get(client, auth_headers)) == {
        ("zones", 0, 0), ("structures", 0, 0), ("cameras", 0, 0)}


def test_extend_then_fit_sequences_keep_everything_aligned(client, auth_headers, site):
    # The live sequence: extend, then "Fit to content" over and over.
    steps = [
        ("extend", {"left_m": 41, "top_m": 4}),
        ("fit", {"margin_m": 2}),
        ("fit", {"margin_m": 2}),
        ("extend", {"right_m": 5, "bottom_m": 5}),
        ("extend", {"left_m": 5, "top_m": 5, "right_m": 5, "bottom_m": 5}),
        ("fit", {"margin_m": 2}),
        ("extend", {"left_m": -1}),
        ("fit", {"margin_m": 0.5}),
        ("fit", {"margin_m": 2}),
        ("fit", {"margin_m": 2}),
    ]
    for kind, body in steps:
        res = client.post(f"/api/v1/layout/{kind}", json=body, headers=auth_headers)
        assert res.status_code == 200, (kind, body, res.text)
        assert_aligned(site, _get(client, auth_headers))
    final = _get(client, auth_headers)
    assert assert_aligned(site, final) == (2, 2)
    assert_fits(final)


# ------------------------------------------------------------ concurrency


async def _service_fit(margin: float = 2.0) -> dict:
    async with async_session_factory() as db:
        layout = await store_layout_service.get_active_layout(db)
        return await store_layout_service.fit_site(db, layout, margin)


async def _service_extend(**sides) -> dict:
    async with async_session_factory() as db:
        layout = await store_layout_service.get_active_layout(db)
        return await store_layout_service.extend_site(db, layout, **sides)


def test_concurrent_fits_never_split_zones_from_structures(client, auth_headers, site):
    """Overlapping fits (repeated clicks) used to translate zones from a stale
    read and structures from a fresh one: zones moved once, structures twice."""
    extend = client.post("/api/v1/layout/extend", json={"left_m": 41, "top_m": 4}, headers=auth_headers)
    assert extend.status_code == 200, extend.text

    async def burst():
        return await asyncio.gather(*[_service_fit(2.0) for _ in range(6)], return_exceptions=True)

    results = _run(burst())
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    after = _get(client, auth_headers)
    assert assert_aligned(site, after) == (2, 2)
    assert_fits(after)
    # Exactly one of them did the work; the rest found the map already fitted.
    assert sum(1 for r in results if not r.get("unchanged")) == 1


def test_concurrent_mixed_extend_and_fit_over_http(client, auth_headers, site):
    """Many overlapping site changes through the API: every one is applied
    whole, in some order, and the end state is consistent."""
    def call(kind, body, out, i):
        out[i] = client.post(f"/api/v1/layout/{kind}", json=body, headers=auth_headers)

    calls = []
    for i in range(20):
        if i % 3 == 0:
            calls.append(("extend", {"left_m": 3, "top_m": 1}))
        elif i % 3 == 1:
            calls.append(("extend", {"right_m": 2, "bottom_m": 2}))
        else:
            calls.append(("fit", {"margin_m": 2}))
    out: list = [None] * len(calls)
    threads = [threading.Thread(target=call, args=(k, b, out, i)) for i, (k, b) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(r is not None and r.status_code == 200 for r in out), [
        (r.status_code, r.text[:200]) if r is not None else None for r in out]
    after = _get(client, auth_headers)
    dx, dy = assert_aligned(site, after)
    # Every extend that added left/top space moved things by exactly that much,
    # and each fit's reported (dx, dy) is what really happened: the sum of all
    # reported moves equals the observed move.
    total_dx = sum(r.json()["site_change"]["dx"] for r in out)
    total_dy = sum(r.json()["site_change"]["dy"] for r in out)
    assert (round(total_dx, 3), round(total_dy, 3)) == (dx, dy)
    final = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert final.status_code == 200
    assert assert_aligned(site, final.json()) == (2, 2)
    assert_fits(final.json())


# --------------------------------------------------------- lock contention


class _Writer:
    """Another process-like connection holding the SQLite write lock."""

    def __init__(self, hold_s: float):
        self.hold_s = hold_s
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        con = sqlite3.connect(str(settings.DATABASE_PATH), timeout=30, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE store_layouts SET name = name")
            self.started.set()
            time.sleep(self.hold_s)
            con.execute("COMMIT")
        finally:
            con.close()

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(10)
        return self

    def __exit__(self, *exc):
        self.thread.join(30)


def test_fit_under_lock_contention_waits_or_fails_whole(client, auth_headers, site, monkeypatch):
    """A writer holding the lock longer than the busy timeout: the fit fails
    cleanly (503) and nothing moved; once the lock is free it succeeds whole."""
    from app.services import store_layout_service as sls

    extend = client.post("/api/v1/layout/extend", json={"left_m": 10, "top_m": 3}, headers=auth_headers)
    assert extend.status_code == 200, extend.text
    before = _get(client, auth_headers)
    monkeypatch.setattr(sls, "SITE_CHANGE_LOCK_TIMEOUT_S", 0.3, raising=False)
    monkeypatch.setattr(sls, "SITE_CHANGE_ATTEMPTS", 2, raising=False)
    with _Writer(hold_s=2.0):
        res = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert res.status_code == 503, res.text
    assert "busy" in res.text.lower()
    unchanged = _get(client, auth_headers)
    assert _offsets(before, unchanged) == {("zones", 0, 0), ("structures", 0, 0), ("cameras", 0, 0)}
    assert (unchanged["width_m"], unchanged["height_m"]) == (before["width_m"], before["height_m"])

    # A short hold: the operation waits for the lock and is applied whole.
    monkeypatch.setattr(sls, "SITE_CHANGE_LOCK_TIMEOUT_S", 5.0, raising=False)
    with _Writer(hold_s=0.5):
        res = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert assert_aligned(site, res.json()) == (2, 2)
    assert_fits(res.json())


def test_lock_error_midway_leaves_nothing_translated(client, auth_headers, site, monkeypatch):
    """A lock error after zones were translated in memory (the live traceback:
    autoflush inside list_structures) must not leave a partial move behind,
    and the operation is retried whole."""
    from sqlalchemy.exc import OperationalError

    from app.services import store_layout_service as sls

    calls = {"n": 0}
    real = sls.StoreLayoutService.list_structures

    async def flaky(self, db, layout_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE store_zones", {}, sqlite3.OperationalError("database is locked"))
        return await real(self, db, layout_id)

    monkeypatch.setattr(sls.StoreLayoutService, "list_structures", flaky)
    res = client.post("/api/v1/layout/extend", json={"left_m": 7, "top_m": 1}, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert calls["n"] >= 2   # retried
    assert assert_aligned(site, _get(client, auth_headers)) == (7, 1)

    # Every attempt failing: clean 503, nothing moved.
    async def always(self, db, layout_id):
        raise OperationalError("UPDATE store_zones", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(sls.StoreLayoutService, "list_structures", always)
    monkeypatch.setattr(sls, "SITE_CHANGE_RETRY_DELAY_S", 0.01, raising=False)
    before = _get(client, auth_headers.copy()) if False else None
    monkeypatch.setattr(sls.StoreLayoutService, "list_structures", real)
    before = _get(client, auth_headers)
    monkeypatch.setattr(sls.StoreLayoutService, "list_structures", always)
    res = client.post("/api/v1/layout/extend", json={"left_m": 3}, headers=auth_headers)
    assert res.status_code == 503, res.text
    monkeypatch.setattr(sls.StoreLayoutService, "list_structures", real)
    after = _get(client, auth_headers)
    assert _offsets(before, after) == {("zones", 0, 0), ("structures", 0, 0), ("cameras", 0, 0)}
    assert after["width_m"] == before["width_m"]


def test_get_db_does_not_reyield_after_a_lock_error(client, auth_headers):
    """get_db used to catch 'database is locked', sleep and yield a second
    session -- illegal for a FastAPI dependency (RuntimeError 'generator
    didn't stop after athrow') and a retry of nothing."""
    from sqlalchemy.exc import OperationalError

    from app.database import get_db

    async def scenario():
        gen = get_db()
        session = await gen.__anext__()
        err = OperationalError("x", {}, sqlite3.OperationalError("database is locked"))
        with pytest.raises(OperationalError):
            await gen.athrow(err)
        # The generator is finished: no second session.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        return session

    _run(scenario())


# ---------------------------------------------------- shopper path pre-scan


async def _add_track(track_id: str, points: list, start=None) -> None:
    from datetime import datetime, timedelta

    from app.models.db_models import CustomerTrackModel

    async with async_session_factory() as db:
        db.add(CustomerTrackModel(id=track_id, track_id=track_id, camera_id=CAM_ID, trajectory_points=points,
                                  start_time=start or datetime.utcnow() - timedelta(days=30)))
        await db.commit()


async def _track_points(track_id: str) -> list:
    from app.models.db_models import CustomerTrackModel

    async with async_session_factory() as db:
        return list((await db.get(CustomerTrackModel, track_id)).trajectory_points)


def test_prescan_keeps_the_lock_short_and_misses_no_path(client, auth_headers, site, monkeypatch):
    """Shopper paths with floor points are found before the write lock is
    taken; one recorded between that scan and the lock still moves, and an
    image-only path is left alone."""
    from app.services import store_layout_service as sls

    _run(_add_track("ct_old_floor", [{"u": 0.1, "v": 0.2, "x": 3.0, "y": 4.0, "t": 1.0}]))
    _run(_add_track("ct_old_image", [{"u": 0.1, "v": 0.2, "t": 1.0}]))
    real = sls._prescan_tracks
    seen = {}

    async def prescan_then_record(session):
        out = await real(session)
        seen["prescan"] = out
        if not seen.get("recorded"):
            # Recorded after the scan, before the change takes the lock.
            await _add_track("ct_new_floor", [{"x": 5.0, "y": 6.0, "t": 2.0}])
            seen["recorded"] = True
        return out

    monkeypatch.setattr(sls, "_prescan_tracks", prescan_then_record)
    res = client.post("/api/v1/layout/extend", json={"left_m": 2, "top_m": 1}, headers=auth_headers)
    assert res.status_code == 200, res.text
    assert seen["prescan"] is not None and len(__import__("json").loads(seen["prescan"]["rowids"])) == 1
    assert res.json()["site_change"]["translated"]["customer_tracks"] == 2
    assert _run(_track_points("ct_old_floor"))[0] == {"u": 0.1, "v": 0.2, "x": 5.0, "y": 5.0, "t": 1.0}
    assert _run(_track_points("ct_new_floor"))[0] == {"x": 7.0, "y": 7.0, "t": 2.0}
    assert _run(_track_points("ct_old_image"))[0] == {"u": 0.1, "v": 0.2, "t": 1.0}

    # A fit that changes nothing does not scan shopper paths at all.
    first = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert first.status_code == 200
    seen.pop("prescan", None)
    again = client.post("/api/v1/layout/fit", json={"margin_m": 2}, headers=auth_headers)
    assert again.json()["site_change"].get("unchanged") is True
    assert "prescan" not in seen
