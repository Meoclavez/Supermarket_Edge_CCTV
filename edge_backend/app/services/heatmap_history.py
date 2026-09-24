"""Recorded heatmap history: hourly and daily heatmap snapshots.

The live heatmap (``GET /api/v1/analytics/heatmaps``) answers "where were
people today". This module keeps the answer for every past hour and day in
``heatmap_snapshots`` (migration m0013), so the Analysis page can replay,
aggregate and compare periods and the business analysis can reason about
trends. Every cell value comes from rows the live pipeline wrote
(``customer_tracks.trajectory_points``, ``shelf_interactions``); nothing is
interpolated or simulated.

Spaces
------
* ``floor`` -- blueprint metres, ``camera_id`` NULL. Built from calibrated
  trajectory points (``x``/``y``) and floor-placed shelf interactions.
  Grid: ``HEATMAP_FLOOR_CELL_M`` cells (0.5 m), capped at
  ``HEATMAP_FLOOR_MAX_CELLS`` per axis.
* ``image`` -- one camera's frame, normalised 0..1, so an *uncalibrated*
  camera still has heatmaps. Built from the normalised foot point
  (``u``/``v``) the engine samples for every confirmed track, and from the
  interaction contact point (``shelf_interactions.image_x/image_y``).
  Grid: ``HEATMAP_IMAGE_GRID_W`` x ``HEATMAP_IMAGE_GRID_H`` (64 x 36, 16:9).

Kinds -- how presence, dwell and interaction differ
---------------------------------------------------
* ``presence`` is **track-weighted**: each track adds 1 to every cell it was
  seen in during the bucket (visitors per cell). A person who stands still
  for an hour is one visitor in one or two cells, not thousands of samples,
  so presence shows *where people go* (traffic, reach of the store) and is
  not dominated by one loiterer or a cashier. A daily presence snapshot is
  the sum of its hours (visitor-hours per cell): a shopper whose visit
  crosses an hour boundary counts once in each hour.
* ``dwell`` is **time-weighted**: seconds spent per cell, each trajectory
  sample credited with the time to the track's next sample (gaps over
  ``HEATMAP_DWELL_MAX_GAP_SEC`` are not credited). It shows *where time is
  spent*; staff standing at a till or a person waiting count fully here,
  which is correct for congestion but is why presence exists separately.
  dwell / 3600 over an area = the average number of people standing in it.
* ``interaction`` is **event-weighted**: one count per shelf reach, at the
  shopper's floor position (floor) or the hand's contact point (image).

Staff-only cameras
------------------
Cameras whose role never counts customers (``camera_roles.NON_FOOTFALL_ROLES``,
i.e. ``stockroom``) are excluded: their tracks and reaches never enter the
floor snapshots (nor the live floor heatmap), and they get **no** image-space
snapshots or uptime, because staff movement is not a customer heatmap.
Snapshots recorded before a camera became a stockroom keep what they held.

The binning is ``retail_metrics_service.bin_heatmap_paths`` /
``bin_heatmap_points``, the same code the live endpoint runs, with the
window applied per trajectory point (``t``) so an hour holds exactly the
samples observed in that hour.

Buckets and uptime
------------------
Hourly buckets start on store-local hours (``timeutil``; half-hour zones
and DST are handled by flooring in local time), stored as naive UTC. An
hour is recorded ``HEATMAP_SETTLE_SEC`` after it closes (tracks are written
when they end) and the two hours before it are re-recorded at the same time
to pick up long tracks. After the store-local day ends its hours are summed
into a daily snapshot (``bucket_minutes`` 1440, also on 23/25-hour DST days);
it is re-summed if an hour is re-recorded later.

``uptime_seconds`` is the time this process saw the space being measured
(camera delivering frames, detector loaded, the relevant feature flag on;
floor space: any calibrated camera). An hour with zero samples is stored
only when uptime was measured, so "nobody came" (row, ``total_samples`` 0)
stays distinct from "camera off / not recording" (no row). Rows rebuilt from
observations after a restart carry NULL uptime (unknown).

Encoding and size
-----------------
``cells`` = base64(zlib(uint16 little-endian, row-major)), ``encoding``
``zlib-u16le-v1``; value = uint16 x ``scale``. ``scale`` is the kind's
resolution (1 visitor, 0.1 s of dwell, 1 reach) unless the peak would
overflow uint16, then peak / 65535. Mostly-empty grids compress to tens or
a few hundred bytes (see tests/test_heatmap_history.py for measured sizes).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import numpy as np
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import (
    CameraModel,
    CustomerTrackModel,
    HeatmapSnapshotModel,
    ShelfInteractionModel,
)
from app.services.retail_metrics_service import (
    HEATMAP_KINDS,
    bin_heatmap_paths,
    bin_heatmap_points,
    heatmap_unit,
    staff_only_camera_ids,
)
from app.services.timeutil import local_day_bounds_utc, to_local, utcnow

logger = logging.getLogger(__name__)

ENCODING = "zlib-u16le-v1"
SPACES = ("floor", "image")
KINDS = HEATMAP_KINDS
HOUR_MINUTES = 60
DAY_MINUTES = 1440
_U16_MAX = 65535
# Smallest value step stored per kind.
VALUE_RESOLUTION = {"presence": 1.0, "dwell": 0.1, "interaction": 1.0}
# Presence/dwell need people_counting; interaction needs shelf_interaction.
_KIND_GROUP = {"presence": "track", "dwell": "track", "interaction": "interaction"}
# Tracks are selected by start time; a track row holds at most
# TRAJECTORY_MAX_POINTS x TRAJECTORY_SAMPLE_SEC of path, so look back this far.
_TRACK_LOOKBACK = timedelta(hours=6)


# ------------------------------------------------------------------ encoding

def encode_cells(grid, kind: str) -> tuple[str, float]:
    """Grid (rows of floats) -> (base64 text, scale). Values are >= 0."""
    arr = np.asarray(grid, dtype=np.float64)
    res = VALUE_RESOLUTION.get(kind, 1.0)
    peak = float(arr.max()) if arr.size else 0.0
    scale = max(res, peak / _U16_MAX) if peak > 0 else res
    q = np.clip(np.rint(arr / scale), 0, _U16_MAX).astype("<u2")
    return base64.b64encode(zlib.compress(q.tobytes(), 9)).decode("ascii"), float(scale)


def decode_cells(cells: str, grid_w: int, grid_h: int, scale: float, encoding: str = ENCODING) -> np.ndarray:
    """Inverse of :func:`encode_cells`: a (grid_h, grid_w) float64 array."""
    if encoding != ENCODING:
        raise ValueError(f"unknown heatmap encoding {encoding!r}")
    raw = zlib.decompress(base64.b64decode(cells))
    arr = np.frombuffer(raw, dtype="<u2")
    if arr.size != grid_w * grid_h:
        raise ValueError(f"cells hold {arr.size} values, expected {grid_w}x{grid_h}")
    return arr.reshape(grid_h, grid_w).astype(np.float64) * float(scale)


def row_grid(row: HeatmapSnapshotModel) -> np.ndarray:
    return decode_cells(row.cells, row.grid_w, row.grid_h, row.scale, row.encoding)


# --------------------------------------------------------------- geometry

def floor_grid_dims(width_m: float, height_m: float) -> tuple[int, int]:
    cell = max(float(settings.HEATMAP_FLOOR_CELL_M), 0.05)
    cap = max(int(settings.HEATMAP_FLOOR_MAX_CELLS), 8)

    def n(extent: float) -> int:
        return int(min(max(math.ceil(max(extent, 0.0) / cell), 8), cap))

    return n(width_m), n(height_m)


def image_grid_dims() -> tuple[int, int]:
    return (int(min(max(settings.HEATMAP_IMAGE_GRID_W, 8), 200)),
            int(min(max(settings.HEATMAP_IMAGE_GRID_H, 8), 200)))


# ------------------------------------------------------------------- time

def _epoch(dt_utc_naive: datetime) -> float:
    return dt_utc_naive.replace(tzinfo=timezone.utc).timestamp()


def hour_start(dt_utc_naive: datetime) -> datetime:
    """Naive-UTC start of the store-local hour containing ``dt_utc_naive``."""
    local = to_local(dt_utc_naive).replace(minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def hours_between(start: datetime, end: datetime) -> list[datetime]:
    """Store-local hour starts h with start <= h < end (naive UTC)."""
    out = []
    h = hour_start(start)
    if h < start:
        h += timedelta(hours=1)
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def day_of(bucket_start: datetime) -> date:
    return to_local(bucket_start).date()


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.replace(microsecond=0).isoformat() + "Z"


def iso_local(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else to_local(dt).replace(microsecond=0).isoformat()


# -------------------------------------------------------------- metadata

def snapshot_meta(row: HeatmapSnapshotModel) -> dict:
    status = "observed" if row.total_samples > 0 else "empty"
    return {
        "id": row.id,
        "space": row.space,
        "camera_id": row.camera_id,
        "kind": row.kind,
        "unit": heatmap_unit(row.kind, "tracks"),
        "bucket_start": iso_z(row.bucket_start),
        "bucket_start_local": iso_local(row.bucket_start),
        "bucket_minutes": row.bucket_minutes,
        "grid_width": row.grid_w,
        "grid_height": row.grid_h,
        "width_m": row.width_m,
        "height_m": row.height_m,
        "total_samples": row.total_samples,
        "total_value": round(row.total_value, 2),
        "peak_value": round(row.peak_value, 2),
        "uptime_seconds": None if row.uptime_seconds is None else round(row.uptime_seconds, 1),
        "status": status,
        "complete": bool(row.complete),
        "source": row.source,
        "updated_at": iso_z(row.updated_at),
    }


def grid_payload(grid: np.ndarray, kind: str) -> dict:
    """Raw values plus the live endpoint's normalised ``density_matrix`` (null when empty)."""
    peak = float(grid.max()) if grid.size else 0.0
    values = np.round(grid, 2).tolist()
    return {
        "values": values,
        "density_matrix": None if peak <= 0 else np.round(grid / peak, 4).tolist(),
        "peak_value": round(peak, 2),
        "total_value": round(float(grid.sum()), 2),
        "observed": peak > 0,
    }


# ------------------------------------------------------------- recording

async def _active_layout(db: AsyncSession):
    from app.services.store_layout_service import store_layout_service

    return await store_layout_service.get_active_layout(db)


async def compute_hour(db: AsyncSession, start: datetime, end: datetime) -> dict:
    """All grids for the window [start, end) from observations.

    Returns {(space, camera_id, kind): {"grid", "samples", "grid_w", "grid_h",
    "width_m", "height_m"}} for floor space (when a layout exists) and for
    every camera with a track or interaction in the window.
    """
    t0, t1 = _epoch(start), _epoch(end)
    track_rows = (await db.execute(
        select(CustomerTrackModel.camera_id, CustomerTrackModel.start_time,
               CustomerTrackModel.trajectory_points).where(and_(
                   CustomerTrackModel.start_time < end,
                   CustomerTrackModel.start_time >= start - _TRACK_LOOKBACK,
                   or_(CustomerTrackModel.end_time.is_(None), CustomerTrackModel.end_time >= start),
               ))
    )).all()
    inter_rows = (await db.execute(
        select(ShelfInteractionModel.camera_id, ShelfInteractionModel.floor_x, ShelfInteractionModel.floor_y,
               ShelfInteractionModel.image_x, ShelfInteractionModel.image_y).where(and_(
                   ShelfInteractionModel.timestamp >= start, ShelfInteractionModel.timestamp < end))
    )).all()

    def paths(rows):
        # Untimed (legacy) points belong to the hour their track started in.
        return [(pts, start <= st < end) for (_cam, st, pts) in rows]

    # Customer heatmaps only: staff-only (stockroom-role) cameras are left out
    # of the floor and get no image-space snapshots (see module docstring).
    staff = set(await staff_only_camera_ids(db))
    track_rows = [r for r in track_rows if r[0] not in staff]
    inter_rows = [r for r in inter_rows if r[0] not in staff]

    out: dict = {}
    layout = await _active_layout(db)
    if layout is not None and layout.width_m and layout.height_m:
        wm, hm = float(layout.width_m), float(layout.height_m)
        gw, gh = floor_grid_dims(wm, hm)
        for kind in ("presence", "dwell"):
            grid, n = bin_heatmap_paths(paths(track_rows), kind, wm, hm, gw, gh, window=(t0, t1),
                                        presence_weighting="tracks")
            out[("floor", None, kind)] = dict(grid=grid, samples=n, grid_w=gw, grid_h=gh, width_m=wm, height_m=hm)
        grid, n = bin_heatmap_points(((r[1], r[2]) for r in inter_rows), wm, hm, gw, gh)
        out[("floor", None, "interaction")] = dict(grid=grid, samples=n, grid_w=gw, grid_h=gh,
                                                    width_m=wm, height_m=hm)

    iw, ih = image_grid_dims()
    cams = sorted({r[0] for r in track_rows} | {r[0] for r in inter_rows})
    for cam in cams:
        cam_tracks = [r for r in track_rows if r[0] == cam]
        for kind in ("presence", "dwell"):
            grid, n = bin_heatmap_paths(paths(cam_tracks), kind, 1.0, 1.0, iw, ih, xkey="u", ykey="v",
                                        window=(t0, t1), presence_weighting="tracks")
            out[("image", cam, kind)] = dict(grid=grid, samples=n, grid_w=iw, grid_h=ih, width_m=None, height_m=None)
        grid, n = bin_heatmap_points(((r[3], r[4]) for r in inter_rows if r[0] == cam), 1.0, 1.0, iw, ih)
        out[("image", cam, "interaction")] = dict(grid=grid, samples=n, grid_w=iw, grid_h=ih,
                                                   width_m=None, height_m=None)
    return out


def _upsert(db: AsyncSession, existing: Optional[HeatmapSnapshotModel], *, space: str, camera_id: Optional[str],
            kind: str, bucket_start: datetime, bucket_minutes: int, grid, grid_w: int, grid_h: int,
            width_m, height_m, samples: int, uptime: Optional[float], complete: bool, source: str) -> HeatmapSnapshotModel:
    arr = np.asarray(grid, dtype=np.float64)
    cells, scale = encode_cells(arr, kind)
    now = utcnow()
    row = existing
    if row is None:
        row = HeatmapSnapshotModel(space=space, camera_id=camera_id, kind=kind, bucket_start=bucket_start,
                                   bucket_minutes=bucket_minutes, created_at=now)
        db.add(row)
    row.grid_w, row.grid_h = int(grid_w), int(grid_h)
    row.width_m, row.height_m = width_m, height_m
    row.encoding, row.scale, row.cells = ENCODING, scale, cells
    row.total_samples = int(samples)
    row.total_value = float(arr.sum())
    row.peak_value = float(arr.max()) if arr.size else 0.0
    row.uptime_seconds = uptime
    row.complete = bool(complete)
    row.source = source
    row.updated_at = now
    return row


def _key(space: str, camera_id: Optional[str], kind: str) -> tuple:
    return (space, camera_id or None, kind)


async def _existing_rows(db: AsyncSession, bucket_start: datetime, bucket_minutes: int) -> dict:
    rows = (await db.execute(select(HeatmapSnapshotModel).where(and_(
        HeatmapSnapshotModel.bucket_start == bucket_start,
        HeatmapSnapshotModel.bucket_minutes == bucket_minutes)))).scalars().all()
    return {_key(r.space, r.camera_id, r.kind): r for r in rows}


async def record_hour(db: AsyncSession, bucket_start: datetime, *, uptime: Optional[dict] = None,
                      complete: bool = True, source: str = "live", end: Optional[datetime] = None) -> dict:
    """Build and upsert every snapshot of one store-local hour. Commits.

    ``uptime`` maps (space, camera_id, group) -> measured seconds for this
    hour; a key present there replaces the stored uptime, otherwise the
    stored value is kept (NULL for rows built only from observations).
    A (space, camera, kind) with no samples is written only when uptime was
    measured for it or a row already exists. ``end`` (default hour end)
    lets record-now close a partial hour at the current time.
    """
    uptime = uptime or {}
    stop = end or (bucket_start + timedelta(hours=1))
    grids = await compute_hour(db, bucket_start, stop)
    existing = await _existing_rows(db, bucket_start, HOUR_MINUTES)

    # Spaces measured in this hour but with no observation at all.
    for (space, cam, group), secs in uptime.items():
        if not secs:
            continue
        for kind in KINDS:
            if _KIND_GROUP[kind] != group or _key(space, cam, kind) in grids:
                continue
            if space == "floor":
                ref = next((v for (s, _c, _k), v in grids.items() if s == "floor"), None)
                if ref is None:
                    continue  # no layout
                gw, gh, wm, hm = ref["grid_w"], ref["grid_h"], ref["width_m"], ref["height_m"]
            else:
                (gw, gh), wm, hm = image_grid_dims(), None, None
            grids[_key(space, cam, kind)] = dict(grid=np.zeros((gh, gw)), samples=0, grid_w=gw, grid_h=gh,
                                                width_m=wm, height_m=hm)

    written = empty = 0
    for key, g in grids.items():
        space, cam, kind = key
        ukey = (space, cam, _KIND_GROUP[kind])
        row = existing.get(key)
        up = uptime.get(ukey) if ukey in uptime else (row.uptime_seconds if row is not None else None)
        if g["samples"] == 0 and not up and row is None:
            continue  # nothing seen and not known to be measuring: "not recorded", no row
        _upsert(db, row, space=space, camera_id=cam, kind=kind, bucket_start=bucket_start,
                bucket_minutes=HOUR_MINUTES, grid=g["grid"], grid_w=g["grid_w"], grid_h=g["grid_h"],
                width_m=g["width_m"], height_m=g["height_m"], samples=g["samples"], uptime=up,
                complete=complete, source=source)
        written += 1
        empty += g["samples"] == 0
    await db.commit()
    return {"bucket_start": iso_z(bucket_start), "rows": written, "empty_rows": empty}


async def rollup_day(db: AsyncSession, day: date, *, now: Optional[datetime] = None) -> dict:
    """Sum a store-local day's hourly snapshots into daily ones (1440). Commits.

    Per (space, camera, kind) the hours with the newest grid shape are summed
    (a blueprint resize mid-day changes the floor grid; other hours are
    skipped and counted). Uptime is the sum of known hourly uptimes.
    """
    day_start, day_end = local_day_bounds_utc(datetime.combine(day, datetime.min.time()))
    rows = (await db.execute(select(HeatmapSnapshotModel).where(and_(
        HeatmapSnapshotModel.bucket_minutes == HOUR_MINUTES,
        HeatmapSnapshotModel.bucket_start >= day_start,
        HeatmapSnapshotModel.bucket_start < day_end)).order_by(HeatmapSnapshotModel.bucket_start))).scalars().all()
    groups: dict = {}
    for r in rows:
        groups.setdefault(_key(r.space, r.camera_id, r.kind), []).append(r)
    existing = await _existing_rows(db, day_start, DAY_MINUTES)
    ended = (now or utcnow()) >= day_end
    written = skipped = 0
    for key, hours in groups.items():
        ref = hours[-1]
        shape = (ref.grid_w, ref.grid_h, ref.width_m, ref.height_m)
        use = [h for h in hours if (h.grid_w, h.grid_h, h.width_m, h.height_m) == shape]
        skipped += len(hours) - len(use)
        total = np.zeros((ref.grid_h, ref.grid_w))
        for h in use:
            total += row_grid(h)
        ups = [h.uptime_seconds for h in use if h.uptime_seconds is not None]
        _upsert(db, existing.get(key), space=key[0], camera_id=key[1], kind=key[2], bucket_start=day_start,
                bucket_minutes=DAY_MINUTES, grid=total, grid_w=ref.grid_w, grid_h=ref.grid_h,
                width_m=ref.width_m, height_m=ref.height_m, samples=sum(h.total_samples for h in use),
                uptime=sum(ups) if ups else None, complete=ended and all(h.complete for h in use),
                source="rollup")
        written += 1
    await db.commit()
    return {"day": day.isoformat(), "rows": written, "hours_skipped_shape_change": skipped}


async def days_needing_rollup(db: AsyncSession, since: datetime, now: datetime) -> list[date]:
    """Ended store-local days since ``since`` whose daily snapshots are missing or older than an hour."""
    hourly = (await db.execute(select(HeatmapSnapshotModel.bucket_start, HeatmapSnapshotModel.updated_at).where(and_(
        HeatmapSnapshotModel.bucket_minutes == HOUR_MINUTES, HeatmapSnapshotModel.bucket_start >= since)))).all()
    daily = (await db.execute(select(HeatmapSnapshotModel.bucket_start, HeatmapSnapshotModel.updated_at).where(and_(
        HeatmapSnapshotModel.bucket_minutes == DAY_MINUTES,
        HeatmapSnapshotModel.bucket_start >= since - timedelta(days=1))))).all()
    newest_hour: dict[date, datetime] = {}
    for bs, up in hourly:
        d = day_of(bs)
        newest_hour[d] = max(newest_hour.get(d, up), up)
    oldest_daily: dict[date, datetime] = {}
    for bs, up in daily:
        d = day_of(bs)
        oldest_daily[d] = min(oldest_daily.get(d, up), up)
    out = []
    for d, newest in sorted(newest_hour.items()):
        if local_day_bounds_utc(datetime.combine(d, datetime.min.time()))[1] > now:
            continue  # the day has not ended
        if d not in oldest_daily or oldest_daily[d] < newest:
            out.append(d)
    return out


async def backfill(db: AsyncSession, *, now: Optional[datetime] = None, days: Optional[int] = None) -> dict:
    """Record past hours that have observations but no complete snapshot. Idempotent.

    Bounded to the last ``HEATMAP_BACKFILL_DAYS`` and to closed hours. Rows
    built here have unknown uptime (NULL), so an hour with no observation is
    never invented as "empty". Then rolls up ended days.
    """
    now = now or utcnow()
    days = settings.HEATMAP_BACKFILL_DAYS if days is None else days
    since = hour_start(now - timedelta(days=max(days, 0)))
    last_closed_end = hour_start(now)
    candidates: set[datetime] = set()
    tracks = (await db.execute(select(CustomerTrackModel.start_time, CustomerTrackModel.end_time).where(and_(
        CustomerTrackModel.start_time < last_closed_end,
        CustomerTrackModel.start_time >= since - _TRACK_LOOKBACK,
        or_(CustomerTrackModel.end_time.is_(None), CustomerTrackModel.end_time >= since))))).all()
    for st, en in tracks:
        a = max(st, since)
        b = min(max(en or st, st), last_closed_end - timedelta(microseconds=1))
        h = hour_start(a)
        while h <= b:
            candidates.add(h)
            h += timedelta(hours=1)
    for (ts,) in (await db.execute(select(ShelfInteractionModel.timestamp).where(and_(
            ShelfInteractionModel.timestamp >= since, ShelfInteractionModel.timestamp < last_closed_end)))).all():
        candidates.add(hour_start(ts))
    rows = (await db.execute(select(HeatmapSnapshotModel.bucket_start, HeatmapSnapshotModel.complete).where(and_(
        HeatmapSnapshotModel.bucket_minutes == HOUR_MINUTES, HeatmapSnapshotModel.bucket_start >= since,
        HeatmapSnapshotModel.bucket_start < last_closed_end)))).all()
    complete_hours = {bs for bs, c in rows if c}
    candidates |= {bs for bs, c in rows if not c}   # partial hours left by record-now or a shutdown
    todo = sorted(h for h in candidates if h not in complete_hours and since <= h < last_closed_end)
    recorded = 0
    for h in todo:
        await record_hour(db, h, complete=True, source="backfill")
        recorded += 1
    rolled = []
    for d in await days_needing_rollup(db, since, now):
        await rollup_day(db, d, now=now)
        rolled.append(d.isoformat())
    return {"hours_recorded": recorded, "days_rolled_up": rolled, "since": iso_z(since)}


async def prune(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    """Delete snapshots past retention (hourly / daily separately). Commits."""
    now = now or utcnow()
    hourly_cut = now - timedelta(days=max(settings.HEATMAP_HOURLY_RETENTION_DAYS, 1))
    daily_cut = now - timedelta(days=max(settings.HEATMAP_DAILY_RETENTION_DAYS, 1))
    r1 = await db.execute(delete(HeatmapSnapshotModel).where(and_(
        HeatmapSnapshotModel.bucket_minutes == HOUR_MINUTES, HeatmapSnapshotModel.bucket_start < hourly_cut)))
    r2 = await db.execute(delete(HeatmapSnapshotModel).where(and_(
        HeatmapSnapshotModel.bucket_minutes == DAY_MINUTES, HeatmapSnapshotModel.bucket_start < daily_cut)))
    await db.commit()
    return {"hourly_deleted": r1.rowcount or 0, "daily_deleted": r2.rowcount or 0}


# ---------------------------------------------------------------- queries

def _filter(space: str, camera_id: Optional[str], kind: str, bucket_minutes: int):
    conds = [HeatmapSnapshotModel.space == space, HeatmapSnapshotModel.kind == kind,
             HeatmapSnapshotModel.bucket_minutes == bucket_minutes]
    conds.append(HeatmapSnapshotModel.camera_id.is_(None) if space == "floor"
                 else HeatmapSnapshotModel.camera_id == camera_id)
    return and_(*conds)


async def load_rows(db: AsyncSession, space: str, camera_id: Optional[str], kind: str,
                    start: datetime, end: datetime, bucket_minutes: int = HOUR_MINUTES) -> list[HeatmapSnapshotModel]:
    return list((await db.execute(select(HeatmapSnapshotModel).where(and_(
        _filter(space, camera_id, kind, bucket_minutes),
        HeatmapSnapshotModel.bucket_start >= start, HeatmapSnapshotModel.bucket_start < end,
    )).order_by(HeatmapSnapshotModel.bucket_start))).scalars().all())


def _bucket_starts(start: datetime, end: datetime, bucket_minutes: int, now: datetime) -> list[datetime]:
    """Expected bucket starts in [start, end) that have begun by ``now``."""
    stop = min(end, now)
    if bucket_minutes == HOUR_MINUTES:
        return hours_between(start, stop)
    out = []
    d = day_of(start)
    while True:
        s, e = local_day_bounds_utc(datetime.combine(d, datetime.min.time()))
        if s >= stop:
            break
        if s >= start and e <= now:
            out.append(s)
        d += timedelta(days=1)
    return out


async def history(db: AsyncSession, space: str, camera_id: Optional[str], kind: str,
                  start: datetime, end: datetime, bucket_minutes: int) -> dict:
    rows = await load_rows(db, space, camera_id, kind, start, end, bucket_minutes)
    expected = _bucket_starts(start, end, bucket_minutes, utcnow())
    have = {r.bucket_start for r in rows}
    missing = [b for b in expected if b not in have]
    return {
        "space": space, "camera_id": camera_id, "kind": kind,
        "unit": heatmap_unit(kind, "tracks"),
        "bucket": "hour" if bucket_minutes == HOUR_MINUTES else "day",
        "from": iso_z(start), "to": iso_z(end),
        "snapshots": [snapshot_meta(r) for r in rows],
        "buckets_expected": len(expected),
        "buckets_recorded": sum(1 for r in rows if r.total_samples > 0),
        "buckets_empty": sum(1 for r in rows if r.total_samples == 0),
        # Not recorded = camera/pipeline off or before recording began: no data, not zero.
        "buckets_not_recorded": len(missing),
        "not_recorded": [iso_z(b) for b in missing[:500]],
    }


def sum_rows(rows: list[HeatmapSnapshotModel]) -> dict:
    """Sum grids of the newest shape; returns grid, used rows and skipped count."""
    if not rows:
        return {"grid": None, "used": [], "skipped": 0}
    ref = max(rows, key=lambda r: r.bucket_start)
    shape = (ref.grid_w, ref.grid_h, ref.width_m, ref.height_m)
    use = [r for r in rows if (r.grid_w, r.grid_h, r.width_m, r.height_m) == shape]
    total = np.zeros((ref.grid_h, ref.grid_w))
    for r in use:
        total += row_grid(r)
    return {"grid": total, "used": use, "skipped": len(rows) - len(use), "ref": ref}


def recorded_hours(rows: Iterable[HeatmapSnapshotModel]) -> float:
    return sum(r.bucket_minutes for r in rows) / 60.0


async def aggregate(db: AsyncSession, space: str, camera_id: Optional[str], kind: str,
                    start: datetime, end: datetime, bucket_minutes: int) -> dict:
    rows = await load_rows(db, space, camera_id, kind, start, end, bucket_minutes)
    s = sum_rows(rows)
    base = {"space": space, "camera_id": camera_id, "kind": kind, "unit": heatmap_unit(kind, "tracks"),
            "from": iso_z(start), "to": iso_z(end),
            "bucket": "hour" if bucket_minutes == HOUR_MINUTES else "day",
            "snapshot_ids": [r.id for r in s["used"]], "snapshots_used": len(s["used"]),
            "snapshots_skipped_shape_change": s["skipped"],
            "recorded_hours": round(recorded_hours(s["used"]), 2),
            "total_samples": sum(r.total_samples for r in s["used"]),
            "uptime_seconds": (sum(r.uptime_seconds for r in s["used"] if r.uptime_seconds is not None)
                               if any(r.uptime_seconds is not None for r in s["used"]) else None)}
    if s["grid"] is None:
        return {**base, "grid_width": None, "grid_height": None, "values": None, "density_matrix": None,
                "observed": False, "message": "No recorded heatmap snapshots in this range."}
    ref = s["ref"]
    return {**base, "grid_width": ref.grid_w, "grid_height": ref.grid_h, "width_m": ref.width_m,
            "height_m": ref.height_m, **grid_payload(s["grid"], kind)}


def weighted_centroid(grid: np.ndarray, top_fraction: float = 0.05) -> Optional[tuple[float, float]]:
    """Value-weighted centroid (col, row in cell units) of the busiest ``top_fraction`` of non-zero cells.

    Ties with the cut-off value are included, so the centroid never depends on sort order.
    """
    flat = grid.ravel()
    nz = np.flatnonzero(flat > 0)
    if nz.size == 0:
        return None
    k = max(1, int(math.ceil(nz.size * top_fraction)))
    # Every cell at least as busy as the k-th busiest (ties included, so the result is deterministic).
    threshold = np.sort(flat[nz])[-k]
    top = nz[flat[nz] >= threshold]
    rows, cols = np.divmod(top, grid.shape[1])
    w = flat[top]
    return float((cols + 0.5) @ w / w.sum()), float((rows + 0.5) @ w / w.sum())


def compare_grids(a: np.ndarray, a_hours: float, b: np.ndarray, b_hours: float) -> dict:
    """Per-recorded-hour means of two periods and their difference (b - a)."""
    am = a / a_hours if a_hours > 0 else np.zeros_like(a)
    bm = b / b_hours if b_hours > 0 else np.zeros_like(b)
    diff = bm - am
    peak = float(np.abs(diff).max()) if diff.size else 0.0
    return {"a_mean": am, "b_mean": bm, "diff": diff, "peak_abs_diff": peak}


async def compare(db: AsyncSession, space: str, camera_id: Optional[str], kind: str, a: tuple, b: tuple,
                  bucket_minutes: int) -> dict:
    ra = await load_rows(db, space, camera_id, kind, a[0], a[1], bucket_minutes)
    rb = await load_rows(db, space, camera_id, kind, b[0], b[1], bucket_minutes)
    base = {"space": space, "camera_id": camera_id, "kind": kind, "unit": heatmap_unit(kind, "tracks"),
            "a": {"from": iso_z(a[0]), "to": iso_z(a[1])}, "b": {"from": iso_z(b[0]), "to": iso_z(b[1])}}
    if not ra or not rb:
        return {**base, "observed": False, "difference": None,
                "message": "Both periods need recorded heatmap snapshots to compare "
                           f"(A: {len(ra)}, B: {len(rb)})."}
    ref = max(ra + rb, key=lambda r: r.bucket_start)
    shape = (ref.grid_w, ref.grid_h, ref.width_m, ref.height_m)
    sa = sum_rows([r for r in ra if (r.grid_w, r.grid_h, r.width_m, r.height_m) == shape])
    sb = sum_rows([r for r in rb if (r.grid_w, r.grid_h, r.width_m, r.height_m) == shape])
    if sa["grid"] is None or sb["grid"] is None:
        return {**base, "observed": False, "difference": None,
                "message": "The two periods were recorded with different grid shapes (blueprint resized)."}
    ha, hb = recorded_hours(sa["used"]), recorded_hours(sb["used"])
    c = compare_grids(sa["grid"], ha, sb["grid"], hb)
    ta, tb = float(sa["grid"].sum()), float(sb["grid"].sum())
    mean_a, mean_b = ta / ha, tb / hb
    ca, cb = weighted_centroid(c["a_mean"]), weighted_centroid(c["b_mean"])
    shift = None
    if ca and cb:
        if space == "floor" and ref.width_m and ref.height_m:
            sx, sy = ref.width_m / ref.grid_w, ref.height_m / ref.grid_h
            unit = "m"
        else:
            sx, sy = 1.0 / ref.grid_w, 1.0 / ref.grid_h
            unit = "frame"
        shift = {"a": [round(ca[0] * sx, 2), round(ca[1] * sy, 2)], "b": [round(cb[0] * sx, 2), round(cb[1] * sy, 2)],
                 "distance": round(math.hypot((cb[0] - ca[0]) * sx, (cb[1] - ca[1]) * sy), 2), "unit": unit}
    diff = c["diff"]
    peak = c["peak_abs_diff"]
    return {
        **base, "observed": True,
        "grid_width": ref.grid_w, "grid_height": ref.grid_h, "width_m": ref.width_m, "height_m": ref.height_m,
        "a_summary": {"snapshot_ids": [r.id for r in sa["used"]], "recorded_hours": round(ha, 2),
                      "total_value": round(ta, 2), "mean_per_hour": round(mean_a, 3)},
        "b_summary": {"snapshot_ids": [r.id for r in sb["used"]], "recorded_hours": round(hb, 2),
                      "total_value": round(tb, 2), "mean_per_hour": round(mean_b, 3)},
        "change_pct": None if mean_a <= 0 else round((mean_b - mean_a) / mean_a * 100.0, 1),
        # Per recorded hour, so periods of different length compare fairly.
        "difference": np.round(diff, 4).tolist(),
        "difference_normalised": None if peak <= 0 else np.round(diff / peak, 4).tolist(),
        "peak_abs_difference": round(peak, 4),
        "hotspot_shift": shift,
    }


def hour_profile_from_rows(rows: list[HeatmapSnapshotModel]) -> list[dict]:
    """Store-local hour-of-day pattern: mean over the days that recorded that hour (not all days)."""
    by_hour: dict[int, list[HeatmapSnapshotModel]] = {}
    for r in rows:
        by_hour.setdefault(to_local(r.bucket_start).hour, []).append(r)
    out = []
    for h in range(24):
        rs = by_hour.get(h, [])
        vals = [r.total_value for r in rs]
        out.append({
            "hour": h, "label": f"{h:02d}:00",
            "days_recorded": len(rs),
            "mean_value": round(sum(vals) / len(vals), 3) if vals else None,
            "min_value": round(min(vals), 3) if vals else None,
            "max_value": round(max(vals), 3) if vals else None,
            "snapshot_ids": [r.id for r in rs],
        })
    return out


async def hour_profile(db: AsyncSession, space: str, camera_id: Optional[str], kind: str, days: int,
                       hour: Optional[int] = None) -> dict:
    end = utcnow()
    start = local_day_bounds_utc(to_local(end - timedelta(days=max(days - 1, 0))))[0]
    rows = await load_rows(db, space, camera_id, kind, start, end, HOUR_MINUTES)
    prof = hour_profile_from_rows(rows)
    out = {"space": space, "camera_id": camera_id, "kind": kind, "unit": heatmap_unit(kind, "tracks"),
           "days": days, "from": iso_z(start), "to": iso_z(end), "hours": prof,
           "observed": any(p["days_recorded"] for p in prof)}
    known = [p for p in prof if p["mean_value"] is not None]
    out["peak_hour"] = max(known, key=lambda p: p["mean_value"])["label"] if known else None
    if hour is not None:
        sel = [r for r in rows if to_local(r.bucket_start).hour == hour]
        s = sum_rows(sel)
        if s["grid"] is not None and s["used"]:
            ref = s["ref"]
            mean = s["grid"] / len(s["used"])
            out["hour_grid"] = {"hour": hour, "days": len(s["used"]), "grid_width": ref.grid_w,
                                "grid_height": ref.grid_h, **grid_payload(mean, kind)}
        else:
            out["hour_grid"] = None
    return out


# ---------------------------------------------------------------- recorder

class HeatmapRecorder:
    """Background task: uptime sampling, hourly recording, daily roll-ups, retention."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        # (hour_start, space, camera_id, group) -> measured seconds
        self._uptime: dict[tuple, float] = {}
        self._last_uptime_ts: Optional[float] = None
        self._last_recorded_hour: Optional[datetime] = None
        self._last_prune_day: Optional[date] = None
        self._started_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    # ---- lifecycle
    async def start(self) -> None:
        if self._running or not settings.HEATMAP_RECORDING_ENABLED:
            return
        self._running = True
        self._started_at = utcnow()
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="heatmap-recorder")
        logger.info("Heatmap history recorder started")

    async def stop(self, timeout: float = 5.0) -> None:
        was_running = self._running
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if not was_running:
            return
        # Keep the current hour's measured uptime across a restart.
        now = utcnow()
        self._sample_uptime(time.time())
        if any(k[0] == hour_start(now) for k in self._uptime):
            try:
                await asyncio.wait_for(self.record_now(flush=False), timeout)
            except Exception as e:  # never block shutdown
                logger.warning(f"Heatmap recorder: final partial-hour record failed: {e}")
        logger.info("Heatmap history recorder stopped")

    async def _run(self) -> None:
        from app.database import async_session_factory

        try:
            # Let cameras connect and the API come up before the backfill.
            await asyncio.sleep(20.0)
            await self._seed_uptime()
            async with self._lock:
                async with async_session_factory() as db:
                    res = await backfill(db)
            if res["hours_recorded"] or res["days_rolled_up"]:
                logger.info(f"Heatmap backfill: {res['hours_recorded']} hour(s), days {res['days_rolled_up']}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = f"backfill: {e}"
            logger.error(f"Heatmap backfill failed: {e}")
        while self._running:
            try:
                await self.tick()
                await asyncio.sleep(max(float(settings.HEATMAP_TICK_SEC), 1.0))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Heatmap recorder error: {e}")
                await asyncio.sleep(max(float(settings.HEATMAP_TICK_SEC), 1.0))

    async def _seed_uptime(self) -> None:
        """Continue the current hour's uptime from a row written before a restart."""
        from app.database import async_session_factory

        h = hour_start(utcnow())
        async with async_session_factory() as db:
            for (space, cam, kind), row in (await _existing_rows(db, h, HOUR_MINUTES)).items():
                if row.uptime_seconds:
                    k = (h, space, cam, _KIND_GROUP[kind])
                    self._uptime[k] = max(self._uptime.get(k, 0.0), float(row.uptime_seconds))

    # ---- uptime
    @staticmethod
    def measuring_status() -> dict[str, dict]:
        """Per running camera: whether people tracking / interactions are being measured now."""
        try:
            from app.services.inference_backend import person_detector
            from app.services.live_analytics_engine import camera_flag, live_engine
            from app.services.tracking_service import floor_projector
            from app.services.camera_roles import NON_FOOTFALL_ROLES, role_cache
        except Exception:
            return {}
        if not getattr(person_detector, "available", False):
            return {}
        out = {}
        now = time.time()
        for cam, rt in list(live_engine.runtimes.items()):
            up = (rt.enabled and rt.status == "ONLINE" and rt.last_frame_at
                  and now - rt.last_frame_at <= max(15.0, 2 * settings.HEATMAP_TICK_SEC))
            if not up:
                continue
            if role_cache.role_of(cam) in NON_FOOTFALL_ROLES:
                continue  # staff-only camera: not part of the customer heatmaps
            flags = dict(rt.analysis_flags or {})

            def flag(name: str) -> bool:
                return bool(flags[name]) if name in flags else camera_flag(cam, name)

            out[cam] = {"track": flag("people_counting"), "interaction": flag("shelf_interaction"),
                        "calibrated": floor_projector.has_homography(cam)}
        return out

    def credit(self, t_from: float, t_to: float, status: dict[str, dict]) -> None:
        """Credit [t_from, t_to) epoch seconds of measuring time, split at local hour boundaries."""
        keys = []
        for cam, st in status.items():
            for group in ("track", "interaction"):
                if st.get(group):
                    keys.append(("image", cam, group))
                    if st.get("calibrated"):
                        keys.append(("floor", None, group))
        keys = list(dict.fromkeys(keys))
        if not keys or t_to <= t_from:
            return
        a = t_from
        while a < t_to:
            h = hour_start(datetime.fromtimestamp(a, timezone.utc).replace(tzinfo=None))
            b = min(t_to, _epoch(h + timedelta(hours=1)))
            for (space, cam, group) in keys:
                k = (h, space, cam, group)
                self._uptime[k] = self._uptime.get(k, 0.0) + (b - a)
            a = b

    def _sample_uptime(self, now_ts: float) -> None:
        last, self._last_uptime_ts = self._last_uptime_ts, now_ts
        if last is None or now_ts <= last:
            return
        # A stalled loop does not credit time it did not observe.
        span = min(now_ts - last, 3 * max(float(settings.HEATMAP_TICK_SEC), 1.0))
        self.credit(now_ts - span, now_ts, self.measuring_status())

    def uptime_for(self, bucket: datetime) -> dict:
        return {(s, c, g): v for (h, s, c, g), v in self._uptime.items() if h == bucket}

    # ---- periodic work
    async def tick(self, now: Optional[datetime] = None) -> dict:
        from app.database import async_session_factory

        self._sample_uptime(time.time())
        now = now or utcnow()
        done: dict[str, Any] = {}
        settled = hour_start(now - timedelta(seconds=float(settings.HEATMAP_SETTLE_SEC)))
        last_closed = settled - timedelta(hours=1)
        async with self._lock:
            async with async_session_factory() as db:
                if self._last_recorded_hour != last_closed:
                    # The just-settled hour plus two refreshes (long tracks land late).
                    for h in (last_closed - timedelta(hours=2), last_closed - timedelta(hours=1), last_closed):
                        await record_hour(db, h, uptime=self.uptime_for(h), complete=True, source="live")
                    self._last_recorded_hour = last_closed
                    done["recorded_through"] = iso_z(last_closed)
                    # Forget uptime of hours that can no longer be re-recorded.
                    cutoff = last_closed - timedelta(hours=3)
                    self._uptime = {k: v for k, v in self._uptime.items() if k[0] > cutoff}
                since = hour_start(now - timedelta(days=max(settings.HEATMAP_BACKFILL_DAYS, 1)))
                rolled = []
                for d in await days_needing_rollup(db, since, now):
                    await rollup_day(db, d, now=now)
                    rolled.append(d.isoformat())
                if rolled:
                    done["rolled_up"] = rolled
                today = to_local(now).date()
                if self._last_prune_day != today:
                    done["pruned"] = await prune(db, now=now)
                    self._last_prune_day = today
        return done

    async def record_now(self, *, flush: bool = True) -> dict:
        """Record the current (partial) hour now, marked ``complete: false``."""
        from app.database import async_session_factory

        if flush:
            try:
                from app.services.pipeline_supervisor import pipeline_supervisor

                await pipeline_supervisor.flush_once()
            except Exception as e:
                logger.debug(f"flush before record-now failed: {e}")
        self._sample_uptime(time.time())
        now = utcnow()
        h = hour_start(now)
        async with self._lock:
            async with async_session_factory() as db:
                res = await record_hour(db, h, uptime=self.uptime_for(h), complete=False, source="manual", end=now)
        res["complete"] = False
        res["note"] = ("Tracks still in progress are written when they end; the hour is re-recorded "
                       "automatically after it closes.")
        return res

    def status(self) -> dict:
        return {"running": self._running, "last_recorded_hour": iso_z(self._last_recorded_hour),
                "last_error": self.last_error, "enabled": settings.HEATMAP_RECORDING_ENABLED,
                # For the UI's empty state: when this recorder began and how long after an
                # hour closes that hour is written.
                "started_at": iso_z(self._started_at),
                "settle_seconds": float(settings.HEATMAP_SETTLE_SEC)}


heatmap_recorder = HeatmapRecorder()


# ------------------------------------------------------- coverage (for rules)

def _frame_size(cam: CameraModel) -> Optional[tuple[float, float]]:
    cp = cam.calibration_points or {}
    try:
        if cp.get("frame_width") and cp.get("frame_height"):
            return float(cp["frame_width"]), float(cp["frame_height"])
    except AttributeError:
        pass
    try:
        w, h = str(cam.resolution or "").lower().split("x")
        return float(w), float(h)
    except (ValueError, AttributeError):
        return None


def floor_coverage_mask(cameras: list[CameraModel], width_m: float, height_m: float,
                        grid_w: int, grid_h: int) -> np.ndarray:
    """Approximate floor cells in view of a calibrated camera (boolean grid).

    Projects a 48 x 27 lattice of image points through each homography (only
    points on the same side of the horizon as the calibration points), marks
    the cells they land in and dilates by one cell. Used so a zone no camera
    sees is reported as "not covered", never as "dead".
    """
    mask = np.zeros((grid_h, grid_w), dtype=bool)
    for cam in cameras:
        if not cam.homography_matrix:
            continue
        size = _frame_size(cam)
        if size is None:
            continue
        try:
            H = np.asarray(cam.homography_matrix, dtype=np.float64).reshape(3, 3)
        except (ValueError, TypeError):
            continue
        fw, fh = size
        us, vs = np.meshgrid(np.linspace(0, fw, 48), np.linspace(0, fh, 27))
        pts = np.stack([us.ravel(), vs.ravel(), np.ones(us.size)])
        proj = H @ pts
        sign = 1.0
        ref = (cam.calibration_points or {}).get("image_points") if isinstance(cam.calibration_points, dict) else None
        if ref:
            try:
                ws = [float((H @ np.array([p["x"], p["y"], 1.0]))[2]) for p in ref]
                sign = 1.0 if sum(1 for w in ws if w > 0) >= len(ws) / 2 else -1.0
            except (KeyError, TypeError, ValueError):
                pass
        ok = proj[2] * sign > 1e-9
        x = proj[0][ok] / proj[2][ok]
        y = proj[1][ok] / proj[2][ok]
        inb = (x >= 0) & (x <= width_m) & (y >= 0) & (y <= height_m)
        gx = np.minimum((x[inb] / width_m * grid_w).astype(int), grid_w - 1)
        gy = np.minimum((y[inb] / height_m * grid_h).astype(int), grid_h - 1)
        mask[gy, gx] = True
    if mask.any():
        dil = mask.copy()
        dil[1:, :] |= mask[:-1, :]
        dil[:-1, :] |= mask[1:, :]
        dil[:, 1:] |= mask[:, :-1]
        dil[:, :-1] |= mask[:, 1:]
        mask = dil
    return mask


def polygon_cells(polygon: list[dict], width: float, height: float, grid_w: int, grid_h: int) -> np.ndarray:
    """Boolean grid of cells whose centre lies inside ``polygon`` ({x, y} in the grid's units)."""
    from app.services.store_layout_service import point_in_polygon

    mask = np.zeros((grid_h, grid_w), dtype=bool)
    if len(polygon or []) < 3:
        return mask
    xs = [p["x"] for p in polygon]
    ys = [p["y"] for p in polygon]
    cw, ch = width / grid_w, height / grid_h
    c0, c1 = max(int(min(xs) / cw), 0), min(int(max(xs) / cw) + 1, grid_w)
    r0, r1 = max(int(min(ys) / ch), 0), min(int(max(ys) / ch) + 1, grid_h)
    for r in range(r0, r1):
        for c in range(c0, c1):
            if point_in_polygon((c + 0.5) * cw, (r + 0.5) * ch, polygon):
                mask[r, c] = True
    return mask
