"""One time convention for stored observations.

**Storage is naive UTC.** Every pipeline timestamp (zone visits, customer
tracks, shelf interactions, theft incidents, tripwire crossings) is written as
a naive ``datetime`` in UTC, like the ``created_at`` defaults
(``datetime.utcnow``). Earlier builds wrote some of them with
``datetime.fromtimestamp`` (host local time), so those rows were hours away
from ``created_at``; migration m0008 converted them.

**Days and hours are the store's.** "Today", hourly curves and schedules are
local to the store: ``SITE_TIMEZONE`` (IANA name) when set, else the host's
own time zone. Window bounds are computed in that zone and converted to UTC
before they reach a query; grouped results are converted back for display.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)
_warned: set[str] = set()


def site_tz() -> Optional[tzinfo]:
    """ZoneInfo for SITE_TIMEZONE, or None meaning "the host's local zone"."""
    name = (settings.SITE_TIMEZONE or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        if name not in _warned:
            _warned.add(name)
            logger.warning(f"SITE_TIMEZONE={name!r} is not a known time zone; using the host's local time")
        return None


def utc_from_ts(ts: float) -> datetime:
    """Epoch seconds -> naive UTC datetime (the storage form)."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc_naive: datetime) -> datetime:
    """Stored naive-UTC value -> aware datetime in the store's zone."""
    aware = dt_utc_naive.replace(tzinfo=timezone.utc)
    tz = site_tz()
    return aware.astimezone(tz) if tz is not None else aware.astimezone()


def local_now() -> datetime:
    tz = site_tz()
    return datetime.now(tz) if tz is not None else datetime.now().astimezone()


def to_utc(dt: datetime) -> datetime:
    """Any datetime -> naive UTC. A naive input is read as store-local time."""
    if dt.tzinfo is None:
        tz = site_tz()
        dt = dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def local_midnight_utc(d: date) -> datetime:
    return to_utc(datetime.combine(d, dtime.min))


def local_day_bounds_utc(day: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """[start, end) of the store-local day containing ``day``, as naive UTC.

    ``day`` may be aware, or naive store-local; None means now. The end is the
    next local midnight, so DST days are 23 or 25 hours long.
    """
    if day is None:
        d = local_now().date()
    elif day.tzinfo is not None:
        tz = site_tz()
        d = (day.astimezone(tz) if tz is not None else day.astimezone()).date()
    else:
        d = day.date()
    return local_midnight_utc(d), local_midnight_utc(d + timedelta(days=1))
