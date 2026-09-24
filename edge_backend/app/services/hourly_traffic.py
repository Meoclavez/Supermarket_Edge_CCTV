"""Visitors by store-local hour for one day, measured, with honest gaps.

Answers "when were we busy today?" from recorded observations only. It
reuses the footfall definitions in ``retail_metrics_service`` rather than
defining footfall again:

* the day's **source** is ``retail_metrics_service.footfall_source`` (entrance
  tripwires, else zone visits, else raw tracks: never summed);
* the same filters: ``_visit_is_real`` / ``_track_is_real`` (fragments are not
  people) and stockroom cameras never count (``_non_footfall_cameras``);
* tripwire in/out come from ``tripwire_engine.tripwire_footfall`` (door
  cameras preferred, stockroom lines excluded), summed over the lines it
  counts.

Per hour:

``visitors``
    entries on the counted lines (tripwire source), or people whose first
    zone visit / track of the day started in that hour, so the hours add up
    to the day's footfall figure.
``footfall_in`` / ``footfall_out``
    crossings on the counted lines; null when the store has no
    footfall-counting line.
``zone_visits``
    zone visits that began in the hour (a shopper visiting three zones is
    three visits); null when no zones are drawn.
``people_present_peak``
    the most people seen at once during the hour (overlapping zone visits,
    else tracks).

**Offline is not zero.** An hour is ``measured`` only when there is evidence
the pipeline was watching: recorded uptime (``heatmap_snapshots.uptime_seconds``
for a non-stockroom camera, or the recorder's live uptime for the current
hour) or at least one observation. ``partial`` = watched for part of the
hour. ``no_data`` = no evidence: every count is null, never 0. ``future`` =
not reached yet.

Hours are store-local (``timeutil``: SITE_TIMEZONE, else the host zone),
stored and queried as naive UTC, so DST days have 23 or 25 hours.
"""

from __future__ import annotations

import bisect
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db_models import CustomerTrackModel, StoreZoneModel, ZoneVisitModel
from app.services.retail_metrics_service import _track_is_real, _visit_is_real, retail_metrics_service
from app.services.timeutil import local_midnight_utc, local_now, to_local, utcnow

logger = logging.getLogger(__name__)

# An hour watched for at least this share of its (elapsed) length is "measured".
MEASURED_MIN_FRACTION = 0.9

SOURCE_LABELS = {
    "tripwire": "Counted at entrance lines",
    "zone_visits": "Estimated from zone visits",
    "tracks": "Estimated from people tracked (no zones drawn)",
    None: "Not measured yet",
}

STATUS_LEGEND = {
    "measured": "Cameras were recording for the whole hour; counts are complete.",
    "partial": "Cameras were recording for part of the hour; counts cover that part only.",
    "no_data": "No evidence the cameras were recording; this is not a zero.",
    "future": "This hour has not happened yet.",
}


def hour_starts(d: date) -> tuple[datetime, datetime, list[datetime]]:
    """(day start, day end, store-local hour starts), all naive UTC."""
    start = local_midnight_utc(d)
    end = local_midnight_utc(d + timedelta(days=1))
    hours = []
    h = start
    while h < end:
        hours.append(h)
        # Stepping in UTC keeps local-hour alignment within a day, across DST
        # changes and in half-hour zones (the offset only jumps on the hour).
        h += timedelta(hours=1)
    return start, end, hours


def _index(hours: list[datetime], end: datetime, t: datetime) -> Optional[int]:
    if t < hours[0] or t >= end:
        return None
    return bisect.bisect_right(hours, t) - 1


def _tz_name() -> str:
    name = (settings.SITE_TIMEZONE or "").strip()
    return name or (local_now().tzname() or "local")


async def _uptime_by_hour(db: AsyncSession, hours: list[datetime], end: datetime,
                          excluded: list[str]) -> dict[datetime, float]:
    """Max measured uptime across non-stockroom cameras per hour start."""
    out: dict[datetime, float] = {}
    try:
        from app.models.db_models import HeatmapSnapshotModel as H

        rows = (await db.execute(
            select(H.bucket_start, H.camera_id, H.uptime_seconds).where(and_(
                H.space == "image", H.kind == "presence", H.bucket_minutes == 60,
                H.bucket_start >= hours[0], H.bucket_start < end, H.uptime_seconds.isnot(None),
            ))
        )).all()
        for b, cam, up in rows:
            if cam in excluded:
                continue
            out[b] = max(out.get(b, 0.0), float(up))
    except Exception as e:  # heatmap history not migrated / not present
        logger.debug(f"hourly traffic: no recorded uptime ({e})")
    # The recorder's in-memory uptime for hours not yet written (current hour).
    try:
        from app.services.heatmap_history import heatmap_recorder

        for h in hours:
            for (space, cam, group), secs in heatmap_recorder.uptime_for(h).items():
                if space == "image" and group == "track" and cam not in excluded:
                    out[h] = max(out.get(h, 0.0), float(secs))
    except Exception:
        pass
    return out


def _peaks(intervals: list[tuple[datetime, datetime]], hours: list[datetime], end: datetime) -> list[int]:
    """Most intervals overlapping at any instant, per hour."""
    peaks = []
    for i, a in enumerate(hours):
        b = hours[i + 1] if i + 1 < len(hours) else end
        events = []
        for s, e in intervals:
            if s < b and e > a:
                events.append((max(s, a), 1))
                events.append((min(e, b), -1))
        # Ends sort before starts at the same instant (back-to-back is not overlap).
        events.sort(key=lambda ev: (ev[0], ev[1]))
        cur = best = 0
        for _, delta in events:
            cur += delta
            best = max(best, cur)
        peaks.append(best)
    return peaks


def _merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    spans = sorted(spans)
    merged: list[list[datetime]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


async def day_series(db: AsyncSession, d: date, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The hourly series for one store-local day (see module docstring)."""
    now = now or utcnow()
    start, end, hours = hour_starts(d)
    n = len(hours)
    excluded = await retail_metrics_service._non_footfall_cameras(db)
    source = await retail_metrics_service.footfall_source(db, start, end) if start < now else None

    has_obs = [False] * n
    visitors = [0] * n
    zone_visits = [0] * n
    fin = [0] * n
    fout = [0] * n

    # ---- zone visits overlapping the day
    visit_rows = (await db.execute(
        select(ZoneVisitModel.track_id, ZoneVisitModel.entered_at, ZoneVisitModel.exited_at).where(and_(
            ZoneVisitModel.entered_at < end,
            or_(ZoneVisitModel.exited_at.is_(None), ZoneVisitModel.exited_at >= start),
            _visit_is_real(), ZoneVisitModel.camera_id.notin_(excluded),
        ))
    )).all()
    first_visit: dict[str, datetime] = {}
    visit_spans: dict[str, list] = {}
    for track_id, entered, exited in visit_rows:
        stop = min(exited or now, now, end)
        if stop < entered:
            stop = entered
        visit_spans.setdefault(track_id, []).append((entered, stop))
        i = _index(hours, end, entered)
        if i is not None:
            zone_visits[i] += 1
            if track_id not in first_visit or entered < first_visit[track_id]:
                first_visit[track_id] = entered
        for j in range(n):
            a = hours[j]
            b = hours[j + 1] if j + 1 < n else end
            if entered < b and stop >= a:
                has_obs[j] = True

    # ---- tracks overlapping the day (fallback source and presence)
    track_rows = (await db.execute(
        select(CustomerTrackModel.track_id, CustomerTrackModel.start_time, CustomerTrackModel.end_time).where(and_(
            CustomerTrackModel.start_time < end, CustomerTrackModel.end_time >= start,
            _track_is_real(), CustomerTrackModel.camera_id.notin_(excluded),
        ))
    )).all()
    first_track: dict[str, datetime] = {}
    track_spans: dict[str, list] = {}
    for track_id, s, e in track_rows:
        track_spans.setdefault(track_id, []).append((s, e))
        i = _index(hours, end, s)
        if i is not None and (track_id not in first_track or s < first_track[track_id]):
            first_track[track_id] = s
        for j in range(n):
            a = hours[j]
            b = hours[j + 1] if j + 1 < n else end
            if s < b and e >= a:
                has_obs[j] = True

    # ---- tripwires (same counting rules as footfall)
    tripwires_present = False
    try:
        from app.services.tripwire_engine import tripwire_footfall

        tw = await tripwire_footfall(db, start, end, "hour")
        counted = set(tw.get("counted_tripwires") or [])
        lines = tw.get("tripwires") or []
        tripwires_present = bool(counted) or any(
            r.get("counts_footfall") and r.get("configured") and r.get("enabled", True) for r in lines)
        for r in lines:
            for bkt in r.get("buckets") or []:
                t = datetime.fromisoformat(bkt["start"]).astimezone(timezone.utc).replace(tzinfo=None)
                i = _index(hours, end, t)
                if i is None:
                    continue
                if bkt.get("in") or bkt.get("out"):
                    has_obs[i] = True
                if r["tripwire_id"] in counted:
                    fin[i] += int(bkt.get("in") or 0)
                    fout[i] += int(bkt.get("out") or 0)
    except Exception as e:
        logger.debug(f"hourly traffic: tripwire footfall unavailable ({e})")

    if source == "tripwire":
        visitors = list(fin)
    elif source == "zone_visits":
        for t in first_visit.values():
            visitors[_index(hours, end, t)] += 1
    elif source == "tracks":
        for t in first_track.values():
            visitors[_index(hours, end, t)] += 1

    presence_source = "zone_visits" if visit_spans else ("tracks" if track_spans else None)
    spans = visit_spans if visit_spans else track_spans
    intervals = [iv for v in spans.values() for iv in _merge(v)]
    peaks = _peaks(intervals, hours, end)

    zones_drawn = bool(await db.scalar(select(func.count(StoreZoneModel.id))))
    uptime = await _uptime_by_hour(db, hours, end, excluded)

    out_hours = []
    totals = {"visitors": 0, "footfall_in": 0 if tripwires_present else None,
              "footfall_out": 0 if tripwires_present else None,
              "zone_visits": 0 if zones_drawn else None, "people_present_peak": None,
              "hours_measured": 0, "hours_partial": 0, "hours_no_data": 0, "hours_future": 0}
    for i, a in enumerate(hours):
        b = hours[i + 1] if i + 1 < n else end
        in_progress = a <= now < b
        up = uptime.get(a)
        if a >= now:
            status = "future"
        else:
            expected = (min(b, now) - a).total_seconds()
            if up is not None and up > 0:
                status = "measured" if up >= MEASURED_MIN_FRACTION * expected else "partial"
            elif has_obs[i]:
                status = "measured"   # observations prove recording; uptime not recorded
            else:
                status = "no_data"
        known = status in ("measured", "partial")
        local_a = to_local(a)
        rec = {
            "index": i,
            "hour": local_a.hour,
            "label": local_a.strftime("%H:%M"),
            "start": local_a.isoformat(),
            "end": to_local(b).isoformat(),
            "status": status,
            "in_progress": in_progress,
            "uptime_seconds": round(up, 1) if up is not None else None,
            "visitors": visitors[i] if known and source is not None else (0 if known else None),
            "footfall_in": fin[i] if known and tripwires_present else None,
            "footfall_out": fout[i] if known and tripwires_present else None,
            "zone_visits": zone_visits[i] if known and zones_drawn else None,
            "people_present_peak": peaks[i] if known else None,
        }
        out_hours.append(rec)
        totals[f"hours_{status}"] += 1
        if known:
            totals["visitors"] += rec["visitors"] or 0
            if tripwires_present:
                totals["footfall_in"] += fin[i]
                totals["footfall_out"] += fout[i]
            if zones_drawn:
                totals["zone_visits"] += zone_visits[i]
            totals["people_present_peak"] = max(totals["people_present_peak"] or 0, peaks[i])
    if not (totals["hours_measured"] or totals["hours_partial"]):
        totals["visitors"] = None

    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "from": to_local(start).isoformat(),
        "to": to_local(end).isoformat(),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "presence_source": presence_source,
        "tripwires_present": tripwires_present,
        "zones_drawn": zones_drawn,
        "totals": totals,
        "hours": out_hours,
    }


async def hourly_visitors(db: AsyncSession, d: Optional[date] = None) -> dict[str, Any]:
    """The requested day with yesterday and the same weekday last week."""
    now = utcnow()
    d = d or local_now().date()
    today = await day_series(db, d, now=now)
    yesterday = await day_series(db, d - timedelta(days=1), now=now)
    last_week = await day_series(db, d - timedelta(days=7), now=now)

    measured = [h for h in today["hours"] if h["status"] in ("measured", "partial") and h["visitors"]]
    busiest = max(measured, key=lambda h: h["visitors"]) if measured else None
    sources = {s["source"] for s in (today, yesterday, last_week) if s["source"]}
    return {
        "date": d.isoformat(),
        "timezone": _tz_name(),
        "generated_at": to_local(now).isoformat(),
        "today": today,
        "yesterday": yesterday,
        "same_weekday_last_week": last_week,
        "busiest_hour": ({"hour": busiest["hour"], "label": busiest["label"], "start": busiest["start"],
                          "visitors": busiest["visitors"]} if busiest else None),
        "comparison": {
            "sources_match": len(sources) <= 1,
            "note": (None if len(sources) <= 1 else
                     "These days were counted from different sources ("
                     + ", ".join(sorted(SOURCE_LABELS.get(s, s).lower() for s in sources))
                     + "), so compare the shape rather than the exact numbers."),
        },
        "status_legend": STATUS_LEGEND,
    }
