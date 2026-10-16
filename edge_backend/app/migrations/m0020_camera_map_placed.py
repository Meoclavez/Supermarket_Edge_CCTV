"""Whether a camera has been put on the store map (``cameras.map_placed``).

Cameras enter the system at a default floor spot (adoption at 1, 1 m, a
recorder import in a grid), so their coordinates alone cannot tell the camera
gallery which ones still need placing. From this version the placement
endpoint sets ``map_placed`` and new cameras start with it off.

Existing rows are backfilled from their coordinates: a camera counts as placed
unless it sits at the origin or shares its spot (within 5 cm) with another
camera, which is what an unplaced pile of recorder channels looks like.
The column list is frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, columns, has_table

NAME = "camera_map_placed"

CAMERAS = {"map_placed": "BOOLEAN"}


def upgrade(conn) -> None:
    if not has_table(conn, "cameras"):
        return
    added = add_columns(conn, "cameras", CAMERAS)
    cols = columns(conn, "cameras")
    if "map_placed" not in added or "floor_x" not in cols or "floor_y" not in cols:
        return
    rows = conn.execute("SELECT id, floor_x, floor_y FROM cameras").fetchall()
    spots: dict[tuple, int] = {}
    for _id, x, y in rows:
        if x is None or y is None:
            continue
        key = (round(float(x) * 20), round(float(y) * 20))
        spots[key] = spots.get(key, 0) + 1
    for cam_id, x, y in rows:
        placed = False
        if x is not None and y is not None:
            key = (round(float(x) * 20), round(float(y) * 20))
            placed = not (abs(float(x)) < 1e-9 and abs(float(y)) < 1e-9) and spots.get(key, 0) < 2
        conn.execute("UPDATE cameras SET map_placed = ? WHERE id = ?", (1 if placed else 0, cam_id))
