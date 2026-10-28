"""Camera coverage on the store map: reach and coverage source (``cameras``).

* ``range_m``: how far the camera usefully sees on the floor, in metres. NULL
  means "not set": the map then uses a default derived from the lens angle
  (``coverage_geometry.default_range_m``: 10 m for an 85 degree lens). Before
  this version every camera was drawn with the same fixed reach.
* ``coverage_mode``: which coverage the map and the analytics use for the
  camera: ``auto`` (NULL; the calibrated floor footprint when the camera is
  calibrated, otherwise the manual cone), ``calibrated`` or ``manual``.

Additive and idempotent (columns are added only when missing; existing rows
keep NULL, i.e. the default). The column list is frozen; later changes need a
new migration.
"""

from .sqlite_utils import add_columns

NAME = "camera_coverage"

CAMERAS = {
    "range_m": "FLOAT",
    "coverage_mode": "VARCHAR(16)",
}


def upgrade(conn) -> None:
    add_columns(conn, "cameras", CAMERAS)
