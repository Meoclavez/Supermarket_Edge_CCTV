"""Tripwire crossings, track hit counts, and local-time rows converted to UTC.

1. ``tripwire_events``: one row per person crossing a Camera Studio tripwire,
   written by ``services/tripwire_engine.py`` (direction "in"/"out" relative to
   the line's configured in-side, ``ts`` naive UTC). Hourly/daily aggregates
   are a GROUP BY over ``ts`` folded into store-local buckets, so no aggregate
   table is kept.
2. ``customer_tracks.hits``: detection frames the tracker matched, so footfall
   can ignore flickering ~1 s track fragments (NULL on older rows).
3. Stored observation times become naive UTC (services/timeutil.py). Earlier
   builds wrote ``customer_tracks.start_time/end_time``,
   ``zone_visits.entered_at/exited_at``, ``shelf_interactions.timestamp/
   started_at/ended_at`` and ``theft_incidents.timestamp`` with
   ``datetime.fromtimestamp`` (host local time) while ``created_at`` was UTC.

   Conversion is decided **per row** against that row's own ``created_at``
   (always UTC, written within seconds of the observation): a value is
   treated as local and shifted only when its local->UTC conversion (the host
   zone's offset *at that instant*, so DST is honoured) lands closer to
   ``created_at`` than the stored value does. Rows already in UTC -- written
   by other paths, by tests, or on a UTC host -- are left alone, and a host
   whose offset is zero changes nothing. Assumption: the host time zone now is
   the one the rows were written under. A zone visit of several hours whose
   length is close to the UTC offset could be misjudged; that is the only
   ambiguous case.

The DDL is frozen; later changes need a new migration.
"""

from datetime import datetime, timezone

from .sqlite_utils import add_columns, columns, has_table

NAME = "tripwire_events"

DDL = r"""CREATE TABLE IF NOT EXISTS tripwire_events (
	id INTEGER NOT NULL,
	tripwire_id VARCHAR(64) NOT NULL,
	tripwire_name VARCHAR(128),
	camera_id VARCHAR(64) NOT NULL,
	track_id VARCHAR(64) NOT NULL,
	direction VARCHAR(8) NOT NULL,
	ts DATETIME NOT NULL,
	counts_footfall BOOLEAN NOT NULL DEFAULT 1,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_tripwire_events_tripwire_ts ON tripwire_events (tripwire_id, ts);
CREATE INDEX IF NOT EXISTS ix_tripwire_events_camera_id ON tripwire_events (camera_id);
CREATE INDEX IF NOT EXISTS ix_tripwire_events_ts ON tripwire_events (ts);
"""

# table -> (column compared with created_at, columns to convert)
LOCAL_TIME_COLUMNS = {
    "customer_tracks": ("end_time", ("start_time", "end_time")),
    "zone_visits": ("entered_at", ("entered_at", "exited_at")),
    "shelf_interactions": ("timestamp", ("timestamp", "started_at", "ended_at")),
    "theft_incidents": ("timestamp", ("timestamp",)),
}

_FMT = "%Y-%m-%d %H:%M:%S.%f"   # SQLAlchemy's SQLite DATETIME storage format


def _parse(value):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("T", " ").replace("Z", ""))
    except ValueError:
        return None


def local_to_utc(dt: datetime) -> datetime:
    """Naive host-local -> naive UTC, using the host offset at that instant."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def looks_local(value: datetime, created_at: datetime) -> bool:
    shifted = local_to_utc(value)
    if shifted == value:
        return False
    return abs((created_at - shifted).total_seconds()) < abs((created_at - value).total_seconds())


def convert_local_rows(conn) -> dict:
    report = {}
    for table, (ref, cols) in LOCAL_TIME_COLUMNS.items():
        if not has_table(conn, table):
            continue
        present = columns(conn, table)
        if "created_at" not in present or ref not in present:
            continue
        cols = [c for c in cols if c in present]
        select_cols = ", ".join(['"id"', '"created_at"', f'"{ref}"'] + [f'"{c}"' for c in cols])
        converted = 0
        for row in conn.execute(f'SELECT {select_cols} FROM "{table}"').fetchall():
            row_id, created, ref_val = row[0], _parse(row[1]), _parse(row[2])
            if ref_val is None and table == "customer_tracks":
                ref_val = _parse(row[3])          # start_time when end_time is missing
            if created is None or ref_val is None or not looks_local(ref_val, created):
                continue
            updates = {}
            for c, raw in zip(cols, row[3:]):
                v = _parse(raw)
                if v is not None:
                    updates[c] = local_to_utc(v).strftime(_FMT)
            if updates:
                sets = ", ".join(f'"{c}" = ?' for c in updates)
                conn.execute(f'UPDATE "{table}" SET {sets} WHERE "id" = ?', [*updates.values(), row_id])
                converted += 1
        report[table] = converted
    return report


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    if has_table(conn, "customer_tracks"):
        add_columns(conn, "customer_tracks", {"hits": "INTEGER"})
    report = convert_local_rows(conn)
    if any(report.values()):
        import logging

        logging.getLogger("edge.migrations").warning(
            "Converted local-time observation rows to UTC: "
            + ", ".join(f"{t}={n}" for t, n in report.items() if n)
        )
