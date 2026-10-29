"""Indexes for the hot dashboard and alert queries (audit 2026-10-29, M7).

* ``security_events`` had no index at all: the restricted-area status poll,
  the alert list, night watch and health scanned the whole table.
* ``zone_visits`` open visits (``exited_at IS NULL``) are read on every
  occupancy/live poll: a partial index keeps that small.
* ``theft_incidents(status, timestamp)``: the incident list filters on status
  and sorts by time.

``ADDITIVE = True``: only ``CREATE INDEX IF NOT EXISTS``, so the migration
runner may apply it without a pre-migration backup copy when the disk is too
full for one (app/migrations/runner.py, backup safety). The DDL is frozen;
later changes need a new migration. The same indexes are declared on the
models so the reconcile step agrees.
"""

NAME = "hot_query_indexes"
ADDITIVE = True

DDL = (
    "CREATE INDEX IF NOT EXISTS ix_security_events_type_ts ON security_events (event_type, timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_security_events_camera_ts ON security_events (camera_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_zone_visits_open ON zone_visits (zone_id) WHERE exited_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_theft_incidents_status_ts ON theft_incidents (status, timestamp)",
)


def upgrade(conn) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for statement in DDL:
        table = statement.split(" ON ")[1].split(" ")[0]
        if table in tables:
            conn.execute(statement)
