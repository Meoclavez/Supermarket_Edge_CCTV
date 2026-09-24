"""Recorded heatmap history: ``heatmap_snapshots``.

One row per (space, camera, kind, bucket): the heatmap grid of one store-local
hour (``bucket_minutes`` 60) or one store-local day (1440, the sum of that
day's hours), written by ``services/heatmap_history.py``.

* ``space`` -- ``floor`` (metres on the blueprint, ``camera_id`` NULL, from
  calibrated trajectories) or ``image`` (one camera's normalised frame, so
  uncalibrated cameras still get heatmaps).
* ``kind`` -- ``presence`` (visitors per cell), ``dwell`` (seconds per cell) or
  ``interaction`` (shelf reaches per cell).
* ``cells`` -- ``encoding`` ``zlib-u16le-v1``: base64 of zlib-compressed
  little-endian uint16, row-major (``grid_h`` rows of ``grid_w``); value =
  uint16 x ``scale``.
* ``uptime_seconds`` -- seconds the pipeline was measuring this space during
  the bucket (NULL = unknown, e.g. rebuilt from rows after a restart). An
  empty bucket (``total_samples`` 0) is only stored when uptime was measured,
  so "nobody came" is distinct from "the camera was off" (no row).

Unique per (space, COALESCE(camera_id, ''), kind, bucket_start,
bucket_minutes): a plain UNIQUE would let floor rows (NULL camera) repeat.
The DDL is frozen; later changes need a new migration.
"""

NAME = "heatmap_snapshots"

DDL = r"""CREATE TABLE IF NOT EXISTS heatmap_snapshots (
	id INTEGER NOT NULL,
	space VARCHAR(8) NOT NULL,
	camera_id VARCHAR(64),
	kind VARCHAR(16) NOT NULL,
	bucket_start DATETIME NOT NULL,
	bucket_minutes INTEGER NOT NULL,
	grid_w INTEGER NOT NULL,
	grid_h INTEGER NOT NULL,
	width_m FLOAT,
	height_m FLOAT,
	encoding VARCHAR(24) NOT NULL,
	scale FLOAT NOT NULL,
	cells TEXT NOT NULL,
	total_samples INTEGER NOT NULL,
	total_value FLOAT NOT NULL,
	peak_value FLOAT NOT NULL,
	uptime_seconds FLOAT,
	complete BOOLEAN NOT NULL,
	source VARCHAR(16) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_heatmap_snapshots_key
	ON heatmap_snapshots (space, COALESCE(camera_id, ''), kind, bucket_start, bucket_minutes);
CREATE INDEX IF NOT EXISTS ix_heatmap_snapshots_bucket ON heatmap_snapshots (bucket_minutes, bucket_start);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
