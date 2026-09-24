"""Drop ``security_events.kinematics`` (fall-detection telemetry).

Fall detection was removed from the product and the column is no longer
mapped. It is dropped with the 12-step table rebuild so it works on every
SQLite version; the runner has already taken a backup of any database with
data. All other rows and columns are preserved (row ids included) and the
table's indexes are restored.
"""

from .rebuild import rebuild_table
from .sqlite_utils import columns, has_table, q

NAME = "drop_security_events_kinematics"

# Frozen shape of security_events after this migration (baseline minus kinematics).
SECURITY_EVENTS_V4 = """
CREATE TABLE security_events (
	id VARCHAR(64) NOT NULL,
	camera_id VARCHAR(64) NOT NULL,
	camera_name VARCHAR(128) NOT NULL,
	location VARCHAR(128) NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	severity VARCHAR(32) NOT NULL,
	confidence FLOAT NOT NULL,
	timestamp DATETIME NOT NULL,
	clip_url VARCHAR(512),
	snapshot_url VARCHAR(512),
	bounding_box JSON,
	keypoints JSON,
	metadata_json JSON,
	acknowledged BOOLEAN NOT NULL,
	acknowledged_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(camera_id) REFERENCES cameras (id) ON DELETE CASCADE
)
"""

# Values for NOT NULL columns an older table may lack or hold NULL in.
_NOT_NULL_FALLBACK = {
    "camera_name": "''",
    "location": "''",
    "event_type": "'UNKNOWN'",
    "severity": "'MEDIUM'",
    "confidence": "0.0",
    "timestamp": "'1970-01-01 00:00:00.000000'",
    "acknowledged": "0",
}


def upgrade(conn) -> None:
    if not has_table(conn, "security_events"):
        return
    old = columns(conn, "security_events")
    if "kinematics" not in old:
        return
    exprs = {
        col: (f"COALESCE({q(col)}, {fallback})" if col in old else fallback)
        for col, fallback in _NOT_NULL_FALLBACK.items()
    }
    rebuild_table(conn, "security_events", SECURITY_EVENTS_V4, column_exprs=exprs)
