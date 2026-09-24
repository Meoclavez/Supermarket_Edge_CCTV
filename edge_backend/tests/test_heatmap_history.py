"""Recorded heatmap history (services/heatmap_history.py, migration m0013).

Covers the cell encoding, recorded-vs-live equality, day roll-ups, camera-off
vs empty hours, compare / hour-profile math, backfill idempotency, retention,
the heatmap-trend rules in business_analysis_service (firing, not firing and
"not enough recorded history", with citations), auth, and the migration on
the legacy fixture database.

Observation seeds use an hour nine store-local days ago and camera ids unique
to this module; the rule tests use windows hundreds of days in the future,
so neither collides with other tests' rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import CustomerTrackModel, HeatmapSnapshotModel, ShelfInteractionModel
from app.services import heatmap_history as hh
from app.services.business_analysis_service import heatmap_trends
from app.services.retail_metrics_service import bin_heatmap_paths
from app.services.timeutil import local_midnight_utc, to_local, utcnow

CAM_A = "cam_hmh_a"      # "calibrated": points carry floor x/y and image u/v
CAM_B = "cam_hmh_b"      # uncalibrated: image u/v only
CAM_OFF = "cam_hmh_off"
BASE_DAY = to_local(utcnow()).date() - timedelta(days=9)
H = hh.hour_start(local_midnight_utc(BASE_DAY) + timedelta(hours=10, minutes=5))


def run(coro):
    return asyncio.run(coro)


async def _db(fn):
    async with async_session_factory() as db:
        return await fn(db)


def _z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "HEATMAP_RECORDING_ENABLED", False)   # tests drive the recorder directly
    with TestClient(app) as c:
        yield c
    mp.undo()


@pytest.fixture(scope="module")
def layout_dims(client):
    async def get(db):
        lay = await hh._active_layout(db)
        return float(lay.width_m), float(lay.height_m)
    return run(_db(get))


def _epoch(dt):
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _track(tid, cam, start, pts, floor=True):
    """pts: list of (frac_x, frac_y, seconds after start); floor coords use the layout size later."""
    return tid, cam, start, pts, floor


async def _seed(db, tracks, dims, interactions=()):
    wm, hm = dims
    for tid, cam, start, pts, floor in tracks:
        points = []
        for fx, fy, dt in pts:
            p = {"u": round(fx, 4), "v": round(fy, 4), "t": round(_epoch(start) + dt, 2)}
            if floor:
                p["x"], p["y"] = round(fx * wm, 2), round(fy * hm, 2)
            points.append(p)
        db.add(CustomerTrackModel(id=f"ct_{tid}", track_id=tid, camera_id=cam, start_time=start,
                                  end_time=start + timedelta(seconds=pts[-1][2]), hits=len(pts),
                                  trajectory_points=points))
    for iid, cam, ts, fxy, ixy in interactions:
        db.add(ShelfInteractionModel(
            id=iid, camera_id=cam, shelf_zone_id="pz_hmh", timestamp=ts, person_track_id="t", action_type="REACH",
            floor_x=None if fxy is None else fxy[0] * wm, floor_y=None if fxy is None else fxy[1] * hm,
            image_x=ixy[0], image_y=ixy[1]))
    await db.commit()


def _walk(x0, y0, x1, y1, seconds, step=0.5):
    n = int(seconds / step)
    return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n, i * step) for i in range(n + 1)]


async def _rows(db, bucket_start, minutes=60, **kw):
    q = select(HeatmapSnapshotModel).where(HeatmapSnapshotModel.bucket_start == bucket_start,
                                           HeatmapSnapshotModel.bucket_minutes == minutes)
    for k, v in kw.items():
        q = q.where(getattr(HeatmapSnapshotModel, k) == v)
    return list((await db.execute(q)).scalars().all())


# ------------------------------------------------------------------ encoding

def test_encoding_round_trips_and_is_small():
    rng = np.random.default_rng(7)
    counts = np.zeros((60, 100))
    counts[10:30, 20:60] = rng.integers(0, 40, size=(20, 40))
    cells, scale = hh.encode_cells(counts, "presence")
    assert scale == 1.0
    assert np.array_equal(hh.decode_cells(cells, 100, 60, scale), counts)

    dwell = np.round(rng.random((36, 64)) * 900, 1)
    cells, scale = hh.encode_cells(dwell, "dwell")
    assert scale == pytest.approx(0.1)
    assert np.allclose(hh.decode_cells(cells, 64, 36, scale), dwell, atol=0.05 + 1e-9)

    huge = np.zeros((10, 10))
    huge[3, 4] = 1_000_000.0                          # beyond uint16: scale grows, relative error tiny
    cells, scale = hh.encode_cells(huge, "dwell")
    assert scale > 1.0
    assert abs(hh.decode_cells(cells, 10, 10, scale)[3, 4] - 1_000_000.0) <= scale / 2

    zeros = np.zeros((60, 100))
    cells, scale = hh.encode_cells(zeros, "presence")
    assert not hh.decode_cells(cells, 100, 60, scale).any()
    assert len(cells) < 64                            # an empty hour costs a few dozen bytes

    with pytest.raises(ValueError):
        hh.decode_cells(cells, 99, 60, scale)
    with pytest.raises(ValueError):
        hh.decode_cells(cells, 100, 60, scale, encoding="raw-f32")


def test_measured_snapshot_sizes():
    """Realistic busy hour: 150 walking shoppers on a 50 x 30 m floor, 0.5 s samples."""
    rng = np.random.default_rng(3)
    paths = []
    for i in range(150):
        x0, y0, x1, y1 = rng.random(4)
        paths.append(([{"x": (x0 + (x1 - x0) * k / 120) * 50, "y": (y0 + (y1 - y0) * k / 120) * 30,
                        "u": x0 + (x1 - x0) * k / 120, "v": y0 + (y1 - y0) * k / 120, "t": 1000 + i + k * 0.5}
                       for k in range(121)], True))
    sizes = {}
    gw, gh = hh.floor_grid_dims(50, 30)
    iw, ih = hh.image_grid_dims()
    for kind in ("presence", "dwell"):
        g, _ = bin_heatmap_paths(paths, kind, 50, 30, gw, gh, presence_weighting="tracks")
        sizes[f"floor_{kind}"] = len(hh.encode_cells(g, kind)[0])
        g, _ = bin_heatmap_paths(paths, kind, 1, 1, iw, ih, xkey="u", ykey="v", presence_weighting="tracks")
        sizes[f"image_{kind}"] = len(hh.encode_cells(g, kind)[0])
    print(f"\nencoded cells bytes per snapshot (busy hour): {sizes}")
    assert max(sizes.values()) < 8000        # raw uint16 would be 12,000 (floor) / 4,608 (image)


# ------------------------------------------------ recorded == live, per space

def test_recorded_hour_equals_live_heatmap(client, layout_dims):
    tracks = [
        _track("hmh_walk", CAM_A, H + timedelta(minutes=3), _walk(0.10, 0.20, 0.30, 0.20, 20)),
        # Stands still for a minute: one visitor in presence, ~60 s of dwell.
        _track("hmh_still", CAM_A, H + timedelta(minutes=10), [(0.404, 0.343, i * 0.5) for i in range(121)]),
        # A 10 s gap in the middle is not credited as dwell.
        _track("hmh_gap", CAM_A, H + timedelta(minutes=20),
               _walk(0.6, 0.6, 0.62, 0.6, 5) + [(0.7, 0.7, 15 + i * 0.5) for i in range(5)]),
        _track("hmh_uncal", CAM_B, H + timedelta(minutes=30), _walk(0.2, 0.9, 0.8, 0.9, 30), floor=False),
    ]
    inter = [
        ("si_hmh_1", CAM_A, H + timedelta(minutes=11), (0.404, 0.343), (0.52, 0.41)),
        ("si_hmh_2", CAM_A, H + timedelta(minutes=12), (0.404, 0.343), (0.52, 0.41)),
        ("si_hmh_3", CAM_B, H + timedelta(minutes=31), None, (0.33, 0.44)),     # uncalibrated: image only
    ]
    run(_db(lambda db: _seed(db, tracks, layout_dims, inter)))
    res = run(_db(lambda db: hh.record_hour(db, H)))
    assert res["rows"] >= 9

    gw, gh = hh.floor_grid_dims(*layout_dims)
    for kind in ("presence", "dwell", "interaction"):
        row = run(_db(lambda db: _rows(db, H, space="floor", kind=kind)))[0]
        grid = hh.row_grid(row)
        live = client.get("/api/v1/analytics/heatmaps", params={
            "kind": kind, "resolution_w": gw, "resolution_h": gh, "presence_weighting": "tracks",
            "from": _z(H), "to": _z(H + timedelta(hours=1))}).json()
        assert live["observed"] is True, kind
        assert live["samples"] == row.total_samples, kind
        assert live["peak_value"] == round(grid.max(), 2), kind
        assert live["density_matrix"] == [[round(v / grid.max(), 4) for v in r] for r in grid.tolist()], kind

    pres = hh.row_grid(run(_db(lambda db: _rows(db, H, space="floor", kind="presence")))[0])
    dwell = hh.row_grid(run(_db(lambda db: _rows(db, H, space="floor", kind="dwell")))[0])
    cx, cy = int(0.404 * gw), int(0.343 * gh)
    assert pres[cy, cx] == 1.0                           # 121 samples, one visitor
    assert dwell[cy, cx] == pytest.approx(60.0)          # time-weighted
    assert dwell.sum() == pytest.approx(20 + 60 + 5 + 2, abs=0.2)  # walk + still + gap track (no 10 s gap)

    # Uncalibrated camera: image-space heatmaps, nothing on the floor.
    b = {r.kind: r for r in run(_db(lambda db: _rows(db, H, space="image", camera_id=CAM_B)))}
    assert set(b) == {"presence", "dwell", "interaction"}
    assert b["presence"].total_samples == 61 and b["interaction"].total_samples == 1
    iw, ih = hh.image_grid_dims()
    assert hh.row_grid(b["interaction"])[int(0.44 * ih), int(0.33 * iw)] == 1.0
    a = {r.kind: r for r in run(_db(lambda db: _rows(db, H, space="image", camera_id=CAM_A)))}
    assert a["interaction"].total_samples == 2
    floor_pres = run(_db(lambda db: _rows(db, H, space="floor", kind="presence")))[0]
    assert floor_pres.total_samples == 41 + 121 + 16      # CAM_A points only
    assert floor_pres.uptime_seconds is None              # rebuilt from rows: uptime unknown


def test_rollup_equals_sum_of_its_hours(client, layout_dims):
    h2 = H + timedelta(hours=1)
    # Crosses the hour boundary: each point lands in the hour it was observed in.
    cross = _track("hmh_cross", CAM_A, h2 - timedelta(seconds=10), _walk(0.5, 0.5, 0.55, 0.5, 20))
    later = _track("hmh_later", CAM_A, h2 + timedelta(minutes=5), _walk(0.1, 0.8, 0.2, 0.8, 10))
    run(_db(lambda db: _seed(db, [cross, later], layout_dims)))
    run(_db(lambda db: hh.record_hour(db, H, uptime={("floor", None, "track"): 1800.0})))
    run(_db(lambda db: hh.record_hour(db, h2, uptime={("floor", None, "track"): 3600.0})))
    r1 = run(_db(lambda db: _rows(db, H, space="floor", kind="presence")))[0]
    r2 = run(_db(lambda db: _rows(db, h2, space="floor", kind="presence")))[0]
    assert r1.total_samples == 41 + 121 + 16 + 20 and r2.total_samples == 21 + 21

    res = run(_db(lambda db: hh.rollup_day(db, BASE_DAY)))
    assert res["rows"] >= 3
    day_start = local_midnight_utc(BASE_DAY)

    async def check(db):
        hours = [r for r in (await db.execute(select(HeatmapSnapshotModel).where(
            HeatmapSnapshotModel.bucket_minutes == 60,
            HeatmapSnapshotModel.bucket_start >= day_start,
            HeatmapSnapshotModel.bucket_start < day_start + timedelta(days=1)))).scalars().all()]
        for kind in ("presence", "dwell", "interaction"):
            day = (await _rows(db, day_start, 1440, space="floor", kind=kind))[0]
            hs = [r for r in hours if r.space == "floor" and r.kind == kind]
            assert np.allclose(hh.row_grid(day), sum(hh.row_grid(r) for r in hs), atol=1e-6), kind
            assert day.total_samples == sum(r.total_samples for r in hs)
            assert day.source == "rollup" and day.complete is True
        day_p = (await _rows(db, day_start, 1440, space="floor", kind="presence"))[0]
        assert day_p.uptime_seconds == pytest.approx(5400.0)
        # A re-recorded hour makes the day stale again; nothing else does.
        assert BASE_DAY not in await hh.days_needing_rollup(db, day_start - timedelta(days=1), utcnow())
        await hh.record_hour(db, H)
        assert BASE_DAY in await hh.days_needing_rollup(db, day_start - timedelta(days=1), utcnow())
        await hh.rollup_day(db, BASE_DAY)
    run(_db(check))


def test_camera_off_hour_differs_from_empty_hour(client):
    empty_h = H + timedelta(hours=3)     # measured, nobody came
    off_h = H + timedelta(hours=4)       # camera off: nothing measured
    run(_db(lambda db: hh.record_hour(db, empty_h, uptime={("image", CAM_OFF, "track"): 3600.0})))
    run(_db(lambda db: hh.record_hour(db, off_h, uptime={})))
    empty = run(_db(lambda db: _rows(db, empty_h, space="image", camera_id=CAM_OFF)))
    assert {r.kind for r in empty} == {"presence", "dwell"}   # interaction was not being measured
    assert all(r.total_samples == 0 and r.uptime_seconds == 3600.0 for r in empty)
    assert run(_db(lambda db: _rows(db, off_h, space="image", camera_id=CAM_OFF))) == []

    hist = client.get("/api/v1/analytics/heatmaps/history", params={
        "space": "image", "camera_id": CAM_OFF, "kind": "presence",
        "from": _z(empty_h), "to": _z(off_h + timedelta(hours=1))}).json()
    assert hist["buckets_expected"] == 2
    assert hist["buckets_empty"] == 1 and hist["buckets_recorded"] == 0
    assert hist["buckets_not_recorded"] == 1 and hist["not_recorded"] == [_z(off_h)]
    assert hist["snapshots"][0]["status"] == "empty" and hist["snapshots"][0]["uptime_seconds"] == 3600.0


def test_uptime_credit_splits_at_hour_boundaries():
    rec = hh.HeatmapRecorder()
    h = hh.hour_start(utcnow())
    t0 = _epoch(h) - 20
    rec.credit(t0, t0 + 50, {"c1": {"track": True, "interaction": False, "calibrated": True}})
    before, after = rec.uptime_for(h - timedelta(hours=1)), rec.uptime_for(h)
    assert before[("image", "c1", "track")] == pytest.approx(20.0)
    assert after[("image", "c1", "track")] == pytest.approx(30.0)
    assert after[("floor", None, "track")] == pytest.approx(30.0)
    assert ("image", "c1", "interaction") not in after


# ------------------------------------------------------- compare / profile

def _row(bucket_start, value, minutes=60, rid=None):
    g = np.full((2, 3), float(value))
    cells, scale = hh.encode_cells(g, "presence")
    return HeatmapSnapshotModel(id=rid, space="floor", camera_id=None, kind="presence", bucket_start=bucket_start,
                                bucket_minutes=minutes, grid_w=3, grid_h=2, width_m=3.0, height_m=2.0,
                                encoding=hh.ENCODING, scale=scale, cells=cells, total_samples=int(value) * 6,
                                total_value=float(g.sum()), peak_value=float(value), uptime_seconds=None,
                                complete=True, source="live")


def test_compare_and_hour_profile_math():
    a = np.array([[4.0, 0.0], [2.0, 2.0]])       # 2 recorded hours
    b = np.array([[1.0, 3.0], [1.0, 1.0]])       # 1 recorded hour
    c = hh.compare_grids(a, 2.0, b, 1.0)
    assert np.allclose(c["a_mean"], [[2, 0], [1, 1]])
    assert np.allclose(c["diff"], [[-1, 3], [0, 0]])
    assert c["peak_abs_diff"] == 3.0
    assert hh.weighted_centroid(np.array([[0, 0], [0, 5.0]])) == (1.5, 1.5)

    d0 = local_midnight_utc(BASE_DAY)
    rows = [_row(d0 + timedelta(hours=10), 10, rid=1), _row(d0 + timedelta(days=1, hours=10), 20, rid=2),
            _row(d0 + timedelta(days=1, hours=11), 5, rid=3)]
    prof = {p["hour"]: p for p in hh.hour_profile_from_rows(rows)}
    h10 = to_local(rows[0].bucket_start).hour
    h11 = to_local(rows[2].bucket_start).hour
    # Mean over the days that recorded the hour, not over all days.
    assert prof[h10]["days_recorded"] == 2 and prof[h10]["mean_value"] == pytest.approx(90.0)
    assert prof[h10]["min_value"] == 60.0 and prof[h10]["max_value"] == 120.0
    assert prof[h10]["snapshot_ids"] == [1, 2]
    assert prof[h11]["days_recorded"] == 1 and prof[h11]["mean_value"] == pytest.approx(30.0)
    others = [p for h, p in prof.items() if h not in (h10, h11)]
    assert all(p["mean_value"] is None and p["days_recorded"] == 0 for p in others)


def test_history_api_endpoints(client):
    day_start = local_midnight_utc(BASE_DAY)
    params = {"space": "floor", "kind": "presence", "from": _z(day_start), "to": _z(day_start + timedelta(days=1))}
    hist = client.get("/api/v1/analytics/heatmaps/history", params=params).json()
    ids = [s["id"] for s in hist["snapshots"]]
    assert hist["buckets_expected"] in (23, 24, 25) and len(ids) >= 2
    assert hist["unit"] == "visitors"

    snap = client.get(f"/api/v1/analytics/heatmaps/snapshot/{ids[0]}").json()
    assert snap["kind"] == "presence" and len(snap["values"]) == snap["grid_height"]
    assert max(max(r) for r in snap["density_matrix"]) == 1.0
    assert client.get("/api/v1/analytics/heatmaps/snapshot/999999999").status_code == 404

    agg = client.get("/api/v1/analytics/heatmaps/aggregate", params=params).json()
    assert agg["snapshots_used"] == len(ids) and sorted(agg["snapshot_ids"]) == sorted(ids)
    assert agg["total_value"] == pytest.approx(sum(s["total_value"] for s in hist["snapshots"]), abs=0.05)
    day = client.get("/api/v1/analytics/heatmaps/aggregate", params={**params, "bucket": "day"}).json()
    assert day["total_value"] == pytest.approx(agg["total_value"], abs=0.05)

    cmp_ = client.get("/api/v1/analytics/heatmaps/compare", params={
        "space": "floor", "kind": "dwell", "a_from": _z(H), "a_to": _z(H + timedelta(hours=1)),
        "b_from": _z(H + timedelta(hours=1)), "b_to": _z(H + timedelta(hours=2))}).json()
    assert cmp_["observed"] is True and cmp_["a_summary"]["recorded_hours"] == 1.0
    assert len(cmp_["difference"]) == cmp_["grid_height"]
    none = client.get("/api/v1/analytics/heatmaps/compare", params={
        "a_from": _z(H - timedelta(days=100)), "a_to": _z(H - timedelta(days=99)),
        "b_from": _z(H), "b_to": _z(H + timedelta(hours=1))}).json()
    assert none["observed"] is False and none["difference"] is None

    prof = client.get("/api/v1/analytics/heatmaps/hour-profile",
                      params={"space": "floor", "kind": "presence", "days": 14, "hour": to_local(H).hour}).json()
    hour = [p for p in prof["hours"] if p["hour"] == to_local(H).hour][0]
    assert hour["days_recorded"] >= 1 and prof["hour_grid"]["days"] >= 1

    assert client.get("/api/v1/analytics/heatmaps/history", params={"space": "image"}).status_code == 422
    assert client.get("/api/v1/analytics/heatmaps/history", params={"kind": "bogus"}).status_code == 422
    rec = client.post("/api/v1/analytics/heatmaps/record-now")
    assert rec.status_code == 200 and rec.json()["complete"] is False


def test_backfill_is_idempotent(client, layout_dims):
    h = H + timedelta(hours=6)
    run(_db(lambda db: _seed(db, [_track("hmh_bf", CAM_A, h + timedelta(minutes=1), _walk(0.1, 0.1, 0.2, 0.1, 10))],
                             layout_dims)))
    first = run(_db(lambda db: hh.backfill(db)))
    assert first["hours_recorded"] >= 1

    async def snapshot(db):
        rows = (await db.execute(select(HeatmapSnapshotModel).where(
            HeatmapSnapshotModel.bucket_start < utcnow()))).scalars().all()
        return {(r.space, r.camera_id, r.kind, r.bucket_start, r.bucket_minutes): (r.id, r.cells) for r in rows}

    before = run(_db(snapshot))
    assert any(k[3] == h and k[0] == "floor" for k in before)
    row = run(_db(lambda db: _rows(db, h, space="floor", kind="presence")))[0]
    assert row.source == "backfill" and row.uptime_seconds is None and row.complete is True
    second = run(_db(lambda db: hh.backfill(db)))
    assert second["hours_recorded"] == 0 and second["days_rolled_up"] == []
    assert run(_db(snapshot)) == before


# ------------------------------------------------------------- AI rules

FW, FH = 50.0, 30.0
GW, GH = hh.floor_grid_dims(FW, FH)
IW, IH = hh.image_grid_dims()


def _zone(zid, name, cat, x0, x1, y0, y1):
    return SimpleNamespace(id=zid, name=name, category=cat,
                           polygon=[{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}])


ZONES = [_zone("za1", "Aisle 1", "AISLE", 2, 10, 2, 8), _zone("za2", "Aisle 2", "AISLE", 12, 20, 2, 8),
         _zone("za3", "Aisle 3", "AISLE", 22, 30, 2, 8), _zone("zdead", "Aisle 9", "AISLE", 32, 40, 2, 8),
         _zone("zchk", "Checkout 1", "CHECKOUT", 2, 10, 20, 26)]
CAMS = [SimpleNamespace(id="cam_ai", homography_matrix=[[0.05, 0, 0], [0, 0.05, 0], [0, 0, 1]], resolution="1000x600",
                        calibration_points={"frame_width": 1000, "frame_height": 600,
                                            "image_points": [{"x": 0, "y": 0}, {"x": 1000, "y": 600}]})]


def _pt(x, y):
    return SimpleNamespace(x=x, y=y)


PRODUCTS = [SimpleNamespace(camera_id="cam_ai_img", name="Olive oil", sku_id="SKU-OIL", enabled=True,
                            points=[_pt(0.4, 0.3), _pt(0.6, 0.3), _pt(0.6, 0.5), _pt(0.4, 0.5)]),
            SimpleNamespace(camera_id="cam_ai_img", name="Pasta", sku_id="SKU-PASTA", enabled=True,
                            points=[_pt(0.1, 0.3), _pt(0.2, 0.3), _pt(0.2, 0.5), _pt(0.1, 0.5)])]


def _rect(g, x0, x1, y0, y1, val, w=FW, h=FH):
    gh, gw = g.shape
    for r in range(gh):
        for c in range(gw):
            cx, cy = (c + 0.5) * w / gw, (r + 0.5) * h / gh
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                g[r, c] = val


async def _put(db, space, cam, kind, start, minutes, grid, uptime=None):
    samples = int(grid.sum() > 0) * 100
    hh._upsert(db, None, space=space, camera_id=cam, kind=kind, bucket_start=start, bucket_minutes=minutes,
               grid=grid, grid_w=grid.shape[1], grid_h=grid.shape[0],
               width_m=FW if space == "floor" else None, height_m=FH if space == "floor" else None,
               samples=samples, uptime=uptime, complete=True, source="live")


def _craft(now, *, dead=True, shift=True, congest=True, days=14):
    today = to_local(now).date()

    async def go(db):
        for i in range(days, 0, -1):
            d = today - timedelta(days=i)
            start = local_midnight_utc(d)
            this_week = i <= 7
            g = np.zeros((GH, GW))
            for z in ZONES[:3]:
                _rect(g, z.polygon[0]["x"], z.polygon[1]["x"], z.polygon[0]["y"], z.polygon[2]["y"], 20.0)
            _rect(g, 32, 40, 2, 8, 0.5 if dead else 20.0)
            if shift:
                _rect(g, 22, 30, 2, 8, 220.0) if this_week else _rect(g, 2, 10, 2, 8, 220.0)
            await _put(db, "floor", None, "presence", start, 1440, g)
            if this_week:
                for hr in range(9, 21):
                    dg = np.zeros((GH, GW))
                    people = 6.0 if (congest and hr == 17) else 1.0
                    _rect(dg, 2, 10, 20, 26, people * 3600.0 / 192)       # 192 cells in Checkout 1
                    await _put(db, "floor", None, "dwell", start + timedelta(hours=hr), 60, dg)
                img = np.zeros((IH, IW))
                _rect(img, 0.4, 0.6, 0.3, 0.7, 600.0 / 300, w=1, h=1)    # ~600 s/day before the oil
                _rect(img, 0.1, 0.2, 0.3, 0.7, 600.0 / 150, w=1, h=1)    # ~600 s/day before the pasta
                await _put(db, "image", "cam_ai_img", "dwell", start, 1440, img)
                reach = np.zeros((IH, IW))
                reach[int(0.4 * IH), int(0.15 * IW)] = 10.0              # pasta is picked up
                await _put(db, "image", "cam_ai_img", "interaction", start, 1440, reach, uptime=36000.0)
        await db.commit()
    run(_db(go))


def _trends(now):
    return run(_db(lambda db: heatmap_trends(db, now=now, zones=ZONES, cameras=CAMS, products=PRODUCTS)))


def _cleanup_future():
    async def go(db):
        await db.execute(delete(HeatmapSnapshotModel).where(
            HeatmapSnapshotModel.bucket_start > utcnow() + timedelta(days=300)))
        await db.commit()
    run(_db(go))


def test_ai_rules_fire_with_citations(client):
    now = utcnow() + timedelta(days=500)
    _craft(now)
    try:
        out = _trends(now)
        by_cat = {f.category: f for f in out["findings"]}
        assert set(by_cat) == {"HEATMAP_DEAD_SPACE", "HEATMAP_HOTSPOT_SHIFT", "HEATMAP_CONGESTION",
                               "HEATMAP_BROWSE_NO_TOUCH"}, out["suppressed"]
        assert by_cat["HEATMAP_DEAD_SPACE"].zone == "Aisle 9"
        assert by_cat["HEATMAP_DEAD_SPACE"].evidence["cold_days"] == 7
        assert by_cat["HEATMAP_HOTSPOT_SHIFT"].evidence["distance_m"] == pytest.approx(20.0, abs=0.6)
        assert "Aisle 1" in by_cat["HEATMAP_HOTSPOT_SHIFT"].finding and "Aisle 3" in by_cat["HEATMAP_HOTSPOT_SHIFT"].finding
        cg = by_cat["HEATMAP_CONGESTION"].evidence
        assert cg["avg_people_at_peak"] == pytest.approx(6.0, abs=0.05)
        assert cg["avg_people_at_quietest"] == pytest.approx(1.0, abs=0.05)
        assert cg["peak_hour_local"] == "17:00"
        assert by_cat["HEATMAP_BROWSE_NO_TOUCH"].zone == "Olive oil"          # pasta is reached, oil is not
        assert by_cat["HEATMAP_BROWSE_NO_TOUCH"].evidence["reaches"] == 0

        today = to_local(now).date()

        async def cited(db, ids):
            return list((await db.execute(select(HeatmapSnapshotModel).where(
                HeatmapSnapshotModel.id.in_(ids)))).scalars().all())
        for f in out["findings"]:
            ev = f.evidence
            assert ev["snapshot_ids"] and ev["period"]["days"] >= 1, f.category
            rows = run(_db(lambda db: cited(db, ev["snapshot_ids"])))
            assert len(rows) == len(ev["snapshot_ids"])
            for r in rows:
                d = to_local(r.bucket_start).date()
                assert ev["period"]["from"] <= d.isoformat() <= ev["period"]["to"]
                assert d < today
        assert {r.kind for r in run(_db(lambda db: cited(db, by_cat["HEATMAP_CONGESTION"].evidence["snapshot_ids"])))} == {"dwell"}
        s = out["summary"]
        assert s["status"] == "ok" and s["days_with_floor_history"] == 7
        assert s["busiest_zones_by_dwell"][0]["zone"] == "Checkout 1"
    finally:
        _cleanup_future()


def test_ai_rules_do_not_fire_on_ordinary_history(client):
    now = utcnow() + timedelta(days=700)
    _craft(now, dead=False, shift=False, congest=False)
    try:
        out = _trends(now)
        cats = {f.category for f in out["findings"]}
        assert not cats & {"HEATMAP_DEAD_SPACE", "HEATMAP_HOTSPOT_SHIFT", "HEATMAP_CONGESTION"}
        assert not any("not enough recorded history" in s for s in out["suppressed"] if "browse" not in s)
    finally:
        _cleanup_future()


def test_ai_rules_say_not_enough_history_on_thin_data(client):
    now = utcnow() + timedelta(days=900)
    _craft(now, days=2)
    try:
        out = _trends(now)
        assert out["findings"] == []
        text = " | ".join(out["suppressed"])
        for rule in ("heatmap_dead_space", "heatmap_hotspot_shift", "heatmap_congestion", "heatmap_browse_no_touch"):
            assert f"{rule}" in text and "not enough recorded history" in text, rule
        assert out["summary"]["status"] == "not enough recorded history"
        assert out["summary"]["days_with_floor_history"] == 2
        assert "busiest_zones_by_dwell" not in out["summary"]
    finally:
        _cleanup_future()


def test_business_analysis_includes_heatmap_trends(client):
    res = client.get("/api/v1/analytics/business/analysis").json()
    assert "heatmap_trends" in res
    assert res["heatmap_trends"]["summary"]["days_required"] == settings.HEATMAP_TREND_MIN_DAYS


# ------------------------------------------------------------ camera roles

def test_stockroom_cameras_are_left_out_of_customer_heatmaps(client, layout_dims):
    from app.models.db_models import CameraModel

    stock = "cam_hmh_stock"
    h = H + timedelta(hours=8)

    async def add_cam(db):
        db.add(CameraModel(id=stock, name="Stockroom", location="Back", rtsp_url="rtsp://192.0.2.1/x",
                           is_ai_enabled=False, role="stockroom"))
        await db.commit()

    async def drop_cam(db):
        await db.execute(delete(CameraModel).where(CameraModel.id == stock))
        await db.commit()
    run(_db(add_cam))
    try:
        run(_db(lambda db: _seed(db, [
            _track("hmh_cust", CAM_A, h + timedelta(minutes=2), _walk(0.1, 0.5, 0.2, 0.5, 10)),
            _track("hmh_staff", stock, h + timedelta(minutes=3), _walk(0.6, 0.5, 0.9, 0.5, 30)),
        ], layout_dims, [("si_hmh_staff", stock, h + timedelta(minutes=4), (0.7, 0.5), (0.5, 0.5))])))
        run(_db(lambda db: hh.record_hour(db, h)))
        floor = {r.kind: r for r in run(_db(lambda db: _rows(db, h, space="floor")))}
        assert floor["presence"].total_samples == 21                    # the customer only
        assert "interaction" not in floor                               # the staff reach is not counted
        assert run(_db(lambda db: _rows(db, h, space="image", camera_id=stock))) == []
        assert {r.kind for r in run(_db(lambda db: _rows(db, h, space="image", camera_id=CAM_A)))} >= {"presence"}
        gw, gh = hh.floor_grid_dims(*layout_dims)
        live = client.get("/api/v1/analytics/heatmaps", params={
            "kind": "presence", "resolution_w": gw, "resolution_h": gh, "presence_weighting": "tracks",
            "from": _z(h), "to": _z(h + timedelta(hours=1))}).json()
        assert live["samples"] == 21                                    # live floor heatmap agrees
    finally:
        run(_db(drop_cam))


def test_ai_rules_use_camera_roles(client):
    now = utcnow() + timedelta(days=1100)
    today = to_local(now).date()
    cams = [SimpleNamespace(id="cam_r_chk", name="Lane 1", role="checkout", homography_matrix=None),
            SimpleNamespace(id="cam_r_hv", name="Liquor", role="high_value", homography_matrix=None),
            SimpleNamespace(id="cam_r_door", name="Front door", role="entrance", homography_matrix=None)]
    shelf = [_pt(0.4, 0.3), _pt(0.6, 0.3), _pt(0.6, 0.5), _pt(0.4, 0.5)]
    prods = [SimpleNamespace(camera_id="cam_r_hv", name="Whisky", sku_id="SKU-W", enabled=True, points=shelf),
             SimpleNamespace(camera_id="cam_r_chk", name="Gum rack", sku_id="SKU-G", enabled=True, points=shelf)]

    async def go(db):
        for i in range(7, 0, -1):
            start = local_midnight_utc(today - timedelta(days=i))
            for cam in ("cam_r_hv", "cam_r_chk"):
                img = np.zeros((IH, IW))
                _rect(img, 0.4, 0.6, 0.3, 0.7, 3.0, w=1, h=1)
                await _put(db, "image", cam, "dwell", start, 1440, img)
                await _put(db, "image", cam, "interaction", start, 1440, np.zeros((IH, IW)), uptime=36000.0)
            for hr in range(9, 21):
                for cam, peak in (("cam_r_chk", 7.0), ("cam_r_door", 1.5)):
                    g = np.zeros((IH, IW))
                    people = peak if hr == 17 else 1.0
                    g[10:20, 10:30] = people * 3600.0 / 200
                    await _put(db, "image", cam, "dwell", start + timedelta(hours=hr), 60, g)
        await db.commit()
    run(_db(go))
    try:
        out = run(_db(lambda db: heatmap_trends(db, now=now, zones=[], cameras=cams, products=prods)))
        cong = [f for f in out["findings"] if f.category == "HEATMAP_CONGESTION"]
        assert [f.evidence["camera_id"] for f in cong] == ["cam_r_chk"]     # the door's peak is small
        ev = cong[0].evidence
        assert ev["camera_role"] == "checkout" and ev["peak_hour_local"] == "17:00"
        assert ev["avg_people_at_peak"] == pytest.approx(7.0, abs=0.05) and len(ev["snapshot_ids"]) == 7
        assert "Lane 1 (Checkout / cashier)" == cong[0].zone
        browse = [f for f in out["findings"] if f.category == "HEATMAP_BROWSE_NO_TOUCH"]
        assert [f.zone for f in browse] == ["Whisky"]                     # not the checkout gum rack
        assert browse[0].severity == "HIGH" and browse[0].evidence["camera_role"] == "high_value"
        assert any("heatmap_browse_no_touch[cam_r_chk]: not applied" in s for s in out["suppressed"])
    finally:
        _cleanup_future()


# ------------------------------------------------------------- retention

def test_retention_prunes_old_snapshots(client):
    now = utcnow()
    g = np.ones((2, 3))

    async def go(db):
        for days, minutes in ((40, 60), (10, 60), (500, 1440), (100, 1440)):
            hh._upsert(db, None, space="image", camera_id="cam_hmh_ret", kind="presence",
                       bucket_start=hh.hour_start(now - timedelta(days=days)), bucket_minutes=minutes, grid=g,
                       grid_w=3, grid_h=2, width_m=None, height_m=None, samples=6, uptime=None,
                       complete=True, source="live")
        await db.commit()
        res = await hh.prune(db, now=now)
        left = (await db.execute(select(HeatmapSnapshotModel.bucket_minutes, HeatmapSnapshotModel.bucket_start).where(
            HeatmapSnapshotModel.camera_id == "cam_hmh_ret"))).all()
        return res, left
    res, left = run(_db(go))
    assert res["hourly_deleted"] >= 1 and res["daily_deleted"] >= 1
    ages = sorted((m, round((now - bs).days)) for m, bs in left)
    assert [m for m, _ in ages] == [60, 1440]
    assert all(age < 101 for _, age in ages)


# ------------------------------------------------------------------ auth

def test_heatmap_history_api_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    anon = TestClient(app)
    for method, url in (("get", "/api/v1/analytics/heatmaps/history"),
                        ("get", "/api/v1/analytics/heatmaps/snapshot/1"),
                        ("get", "/api/v1/analytics/heatmaps/aggregate"),
                        ("get", "/api/v1/analytics/heatmaps/compare?a_from=2026-01-01&a_to=2026-01-02"
                                "&b_from=2026-01-02&b_to=2026-01-03"),
                        ("get", "/api/v1/analytics/heatmaps/hour-profile"),
                        ("post", "/api/v1/analytics/heatmaps/record-now")):
        assert getattr(anon, method)(url).status_code == 401, url


# ------------------------------------------------------------- migration

def test_migration_runs_on_legacy_fixture_db(tmp_path):
    from app.migrations import head_version, run_migrations
    from tests.test_migrations import _make_legacy

    db = tmp_path / "legacy.db"
    _make_legacy(db)
    report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert report["to_version"] == head_version()
    assert any("heatmap_snapshots" in str(a) for a in report["applied"])
    c = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(heatmap_snapshots)")}
        assert {"space", "camera_id", "kind", "bucket_start", "bucket_minutes", "grid_w", "grid_h", "width_m",
                "height_m", "cells", "encoding", "scale", "total_samples", "uptime_seconds", "created_at"} <= cols
        assert c.execute("SELECT COUNT(*) FROM customer_tracks").fetchone()[0] == 1   # legacy rows kept
        ins = ("INSERT INTO heatmap_snapshots (space, camera_id, kind, bucket_start, bucket_minutes, grid_w, grid_h,"
               " encoding, scale, cells, total_samples, total_value, peak_value, complete, source, created_at,"
               " updated_at) VALUES (?, ?, 'presence', '2026-09-01 10:00:00', 60, 1, 1, 'zlib-u16le-v1', 1, '',"
               " 0, 0, 0, 1, 'live', '2026-09-01', '2026-09-01')")
        c.execute(ins, ("floor", None))
        c.execute(ins, ("image", "cam1"))
        c.execute(ins, ("image", "cam2"))
        # Floor rows have a NULL camera; the key still refuses a duplicate.
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(ins, ("floor", None))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(ins, ("image", "cam1"))
    finally:
        c.close()
