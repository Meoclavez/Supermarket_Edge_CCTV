"""Camera roles, checkout-lane POS link and image-space queue visits.

1. ``cameras.role``: what the camera is for (``entrance``, ``exit``,
   ``entrance_exit``, ``checkout``, ``aisle``, ``high_value``, ``stockroom``,
   ``overview``; see ``services/camera_roles.py``). NULL = no role, which
   behaves exactly as before roles existed.
2. ``cameras.pos_register_id``: for a checkout-lane camera, the POS
   ``register_id`` its lane rings sales on, so ``pos_transactions`` rows can
   be attributed to the lane. NULL = not linked.
3. ``queue_visits``: one row per person who stood in a Camera Studio
   checkout / queue area (image coordinates, so uncalibrated cameras work),
   written by ``services/tripwire_engine.py``. Kept apart from
   ``zone_visits`` so store-wide dwell and occupancy figures, which are over
   blueprint zones, are not skewed by lane visits of the same shoppers.

The DDL and column list are frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, has_table

NAME = "camera_roles"

CAMERAS = {
    "role": "VARCHAR(32)",
    "pos_register_id": "VARCHAR(64)",
}

DDL = r"""CREATE TABLE IF NOT EXISTS queue_visits (
	id VARCHAR(64) NOT NULL,
	area_id VARCHAR(64) NOT NULL,
	area_kind VARCHAR(16) NOT NULL,
	area_name VARCHAR(128),
	camera_id VARCHAR(64) NOT NULL,
	track_id VARCHAR(64) NOT NULL,
	entered_at DATETIME NOT NULL,
	exited_at DATETIME NOT NULL,
	dwell_seconds FLOAT NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_queue_visits_area_entered ON queue_visits (area_id, entered_at);
CREATE INDEX IF NOT EXISTS ix_queue_visits_camera_id ON queue_visits (camera_id);
CREATE INDEX IF NOT EXISTS ix_queue_visits_entered_at ON queue_visits (entered_at);
"""


def upgrade(conn) -> None:
    if has_table(conn, "cameras"):
        add_columns(conn, "cameras", CAMERAS)
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
