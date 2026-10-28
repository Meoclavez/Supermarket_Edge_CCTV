"""Restricted areas, high security (services/restricted_areas.py).

1. ``restricted_areas``: one row per area (store-map zone and/or per-camera
   regions, arming schedule, triggers, points of interest, sensitivity, a
   manual arm/disarm override, and ``legacy_intrusion_id`` for an area
   upgraded from an older zone-store restricted area).
2. ``restricted_area_announcements``: "I'm going in" announcements.
3. ``restricted_area_audit``: append-only audit trail (settings changes,
   arm/disarm, announcements, alerts, acknowledgement outcomes).

Additive and idempotent (``IF NOT EXISTS``). Older restricted areas in
``zones_config.json`` are upgraded at startup by the service, not here (they
live outside the database). The DDL is frozen; later changes need a new
migration.
"""

NAME = "restricted_areas"

DDL = r"""CREATE TABLE IF NOT EXISTS restricted_areas (
	id VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	enabled BOOLEAN NOT NULL,
	floor_zone_id VARCHAR(64),
	camera_regions JSON,
	arming JSON,
	triggers JSON,
	points_of_interest JSON,
	security JSON,
	manual_state VARCHAR(16),
	manual_until DATETIME,
	manual_reason VARCHAR(256),
	manual_by VARCHAR(128),
	legacy_intrusion_id VARCHAR(64),
	created_at DATETIME NOT NULL,
	updated_at DATETIME,
	updated_by VARCHAR(128),
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_restricted_areas_floor_zone_id ON restricted_areas (floor_zone_id);
CREATE INDEX IF NOT EXISTS ix_restricted_areas_legacy_intrusion_id ON restricted_areas (legacy_intrusion_id);
CREATE TABLE IF NOT EXISTS restricted_area_announcements (
	id VARCHAR(64) NOT NULL,
	area_id VARCHAR(64) NOT NULL,
	user_id VARCHAR(64),
	user_name VARCHAR(128) NOT NULL,
	note VARCHAR(256),
	people INTEGER NOT NULL,
	source VARCHAR(32),
	started_at DATETIME NOT NULL,
	until DATETIME NOT NULL,
	ended_at DATETIME,
	ended_by VARCHAR(128),
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_restricted_area_announcements_area_id ON restricted_area_announcements (area_id);
CREATE INDEX IF NOT EXISTS ix_restricted_area_announcements_ended_at ON restricted_area_announcements (ended_at);
CREATE INDEX IF NOT EXISTS ix_restricted_area_announcements_until ON restricted_area_announcements (until);
CREATE TABLE IF NOT EXISTS restricted_area_audit (
	id VARCHAR(64) NOT NULL,
	ts DATETIME NOT NULL,
	area_id VARCHAR(64),
	area_name VARCHAR(128),
	action VARCHAR(32) NOT NULL,
	actor_id VARCHAR(64),
	actor_name VARCHAR(128),
	actor_role VARCHAR(16),
	event_id VARCHAR(64),
	detail JSON,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_restricted_area_audit_action ON restricted_area_audit (action);
CREATE INDEX IF NOT EXISTS ix_restricted_area_audit_area_id ON restricted_area_audit (area_id);
CREATE INDEX IF NOT EXISTS ix_restricted_area_audit_event_id ON restricted_area_audit (event_id);
CREATE INDEX IF NOT EXISTS ix_restricted_area_audit_ts ON restricted_area_audit (ts);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
