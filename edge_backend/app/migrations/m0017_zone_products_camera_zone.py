"""Products sold in each zone, and the zone an uncalibrated camera watches.

1. ``store_zones.products``: JSON list of the product categories the operator
   says are sold in the zone ("Chips", "Soft drink", ...). Purely descriptive:
   the business analysis names them in findings and in the local model's
   summary so a recommendation says *what* is in a bypassed aisle. NULL or
   ``[]`` = not described.
2. ``cameras.watch_zone_id``: for a camera without a floor calibration, the
   blueprint zone its whole view belongs to. Its counted tracks then open
   visits in that zone, so zone analytics work before every camera is
   calibrated. A calibrated camera ignores it (exact floor positions win).
   NULL = not linked, which behaves exactly as before.

The column lists are frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, has_table

NAME = "zone_products_camera_zone"

STORE_ZONES = {"products": "JSON"}
CAMERAS = {"watch_zone_id": "VARCHAR(64)"}


def upgrade(conn) -> None:
    if has_table(conn, "store_zones"):
        add_columns(conn, "store_zones", STORE_ZONES)
    if has_table(conn, "cameras"):
        add_columns(conn, "cameras", CAMERAS)
