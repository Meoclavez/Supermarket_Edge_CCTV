"""Record the frame size every stored calibration was authored at.

A camera's homography maps image pixels to floor metres, in pixels of the
frames the camera delivered when it was calibrated. From this version big
streams are scaled down on the GPU (DECODE_MAX_WIDTH, default 1920), so the
delivered size can differ from that; ``FloorProjector`` scales points from the
delivered size to ``calibration_points.frame_width/height`` before projecting.

Calibrations saved by the dashboard already carry that size. For one that
does not, it is taken from the stream size the capture recorded
(``STORAGE_DIR/decode_stream_sizes.json``, next to the database): until this
version frames were delivered at native size (DECODE_MAX_WIDTH defaulted to
0), so that is the size the points were clicked in. A camera with no recorded
size, or several different ones, is left alone and logged: it keeps
projecting unscaled, as before, until it is recalibrated.

Only the JSON in ``cameras.calibration_points`` changes; no column is added.
"""

import json
import logging
from pathlib import Path

from .sqlite_utils import columns, has_table

NAME = "calibration_frame_size"

SIZES_FILE = "decode_stream_sizes.json"
logger = logging.getLogger("edge.migrations")


def _stream_sizes(conn) -> dict:
    for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
        if name == "main" and path:
            f = Path(path).parent / SIZES_FILE
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _size(entry):
    try:
        w, h = int(entry[0]), int(entry[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def upgrade(conn) -> None:
    if not has_table(conn, "cameras"):
        return
    cols = columns(conn, "cameras")
    if "homography_matrix" not in cols or "calibration_points" not in cols:
        return
    rows = conn.execute(
        "SELECT id, calibration_points FROM cameras WHERE homography_matrix IS NOT NULL "
        "AND homography_matrix != '' AND homography_matrix != 'null'"
    ).fetchall()
    if not rows:
        return
    sizes = _stream_sizes(conn)
    for cam_id, raw in rows:
        try:
            cp = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        except ValueError:
            cp = {}
        if not isinstance(cp, dict):
            cp = {}
        if _size((cp.get("frame_width"), cp.get("frame_height"))):
            continue
        streams = sizes.get(cam_id) if isinstance(sizes.get(cam_id), dict) else {}
        known = {s for s in (_size(v) for v in streams.values()) if s}
        if len(known) != 1:
            logger.warning(f"camera {cam_id}: calibration has no recorded frame size and the stream size is "
                           f"{'unknown' if not known else 'ambiguous'}; it projects unscaled until recalibrated")
            continue
        w, h = known.pop()
        cp["frame_width"], cp["frame_height"] = w, h
        cp["frame_size_source"] = "native stream size recorded before m0016"
        conn.execute("UPDATE cameras SET calibration_points = ? WHERE id = ?", (json.dumps(cp), cam_id))
        logger.info(f"camera {cam_id}: calibration authored at {w}x{h} recorded")
