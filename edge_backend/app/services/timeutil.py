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


_host_zone_cache: dict[str, Optional[str]] = {}


def host_zone_name() -> Optional[str]:
    """The host's IANA zone (TZ, else the /etc/localtime link), or None when unknown."""
    import os

    env = os.environ.get("TZ", "").lstrip(":")
    if env in _host_zone_cache:
        return _host_zone_cache[env]
    name: Optional[str] = None
    if env and "/" in env:
        name = env
    else:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
        except OSError:
            name = None
        if name is None:
            try:
                with open("/etc/timezone", encoding="utf-8") as fh:
                    name = fh.read().strip() or None
            except OSError:
                name = None
    _host_zone_cache[env] = name
    return name


def _zone(name: str) -> Optional[tzinfo]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def configured_tz() -> Optional[tzinfo]:
    """The zone set in Settings (SITE_TIMEZONE), or None when it is left automatic.

    "Configured" and "resolved" stay apart: the dashboard shows "automatic
    (Australia/Melbourne)" from configured_tz() is None plus site_tz_source().
    """
    name = (settings.SITE_TIMEZONE or "").strip()
    return _zone(name) if name else None


def site_tz() -> Optional[tzinfo]:
    """The zone used for store days and hours (resolved): SITE_TIMEZONE, else the
    host's IANA zone; None only when neither is known.

    None makes callers fall back to the host's *current UTC offset*, which does
    not follow daylight saving (day buckets split on DST day; audit M18), so the
    host's real IANA zone is resolved instead of using that fallback.
    """
    name = (settings.SITE_TIMEZONE or "").strip()
    if name:
        tz = _zone(name)
        if tz is not None:
            return tz
        if name not in _warned:
            _warned.add(name)
            logger.warning(f"SITE_TIMEZONE={name!r} is not a known time zone; using the host's time zone")
    host = host_zone_name()
    if host:
        tz = _zone(host)
        if tz is not None:
            return tz
    if "__fixed__" not in _warned:
        _warned.add("__fixed__")
        logger.warning("No store time zone: SITE_TIMEZONE is not set and the host's zone is unknown; "
                       "using a fixed UTC offset, so days will be off by an hour after a daylight-saving "
                       "change. Set the store time zone in Settings.")
    return None


def site_tz_source() -> dict:
    """Where the store's zone comes from (status display): setting | host | fixed_offset."""
    name = (settings.SITE_TIMEZONE or "").strip()
    if name and _zone(name) is not None:
        return {"source": "setting", "name": name}
    host = host_zone_name()
    if host and _zone(host) is not None:
        return {"source": "host", "name": host,
                "note": "From the box's own clock settings. Set the store time zone in Settings to be sure."}
    return {"source": "fixed_offset", "name": None,
            "warning": "The store time zone is unknown: days shift by an hour after a daylight-saving change. "
                       "Set it in Settings."}


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
