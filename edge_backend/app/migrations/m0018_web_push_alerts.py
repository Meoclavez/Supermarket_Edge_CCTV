"""Installed web app alerts: ``web_push_subscriptions`` and ``push_alerts``.

1. ``web_push_subscriptions``: one row per installed dashboard web app that
   enabled alerts, bound to the signed-in account (``user_id``). The endpoint
   is unique: the same phone signing in as someone else moves the row.
2. ``push_alerts``: one row per alert pushed to web-app phones, kept open
   until it is acknowledged (or after a day) so it can be escalated from the
   first-priority people to the backup people. ``sent_to`` and ``report`` are
   JSON.

The DDL is frozen; later changes need a new migration.
"""

NAME = "web_push_alerts"

DDL = r"""CREATE TABLE IF NOT EXISTS web_push_subscriptions (
	id VARCHAR(64) NOT NULL,
	user_id VARCHAR(64) NOT NULL,
	endpoint VARCHAR(2048) NOT NULL,
	p256dh VARCHAR(128) NOT NULL,
	auth VARCHAR(64) NOT NULL,
	label VARCHAR(128) NOT NULL,
	platform VARCHAR(16) NOT NULL,
	user_agent VARCHAR(256),
	vapid_key_id VARCHAR(32) NOT NULL,
	created_at DATETIME,
	last_seen_at DATETIME,
	last_push_at DATETIME,
	last_push_status VARCHAR(32),
	last_push_error VARCHAR(512),
	PRIMARY KEY (id),
	UNIQUE (endpoint)
);
CREATE INDEX IF NOT EXISTS ix_web_push_subscriptions_user_id ON web_push_subscriptions (user_id);
CREATE TABLE IF NOT EXISTS push_alerts (
	alert_id VARCHAR(64) NOT NULL,
	incident_id VARCHAR(64),
	event_type VARCHAR(64) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	camera_id VARCHAR(64),
	title VARCHAR(256) NOT NULL,
	body VARCHAR(1024) NOT NULL,
	payload JSON,
	created_at DATETIME,
	escalate_at DATETIME,
	escalated_at DATETIME,
	acknowledged_at DATETIME,
	acknowledged_by VARCHAR(128),
	closed_at DATETIME,
	sent_to JSON,
	report JSON,
	PRIMARY KEY (alert_id)
);
CREATE INDEX IF NOT EXISTS ix_push_alerts_incident_id ON push_alerts (incident_id);
CREATE INDEX IF NOT EXISTS ix_push_alerts_created_at ON push_alerts (created_at);
CREATE INDEX IF NOT EXISTS ix_push_alerts_closed_at ON push_alerts (closed_at);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
