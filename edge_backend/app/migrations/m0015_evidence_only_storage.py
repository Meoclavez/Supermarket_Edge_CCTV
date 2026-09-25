"""Evidence-only storage: no continuous recording, and expired-evidence markers.

1. ``cameras.dvr_enabled`` is turned off on every row. This device never
   records continuously (the store's NAS does); the flag defaulted to on but
   nothing ever started a recorder from it, so it only misreported the camera.
   No files are touched: segments an older build may have left under
   ``STORAGE_DIR/dvr`` stay where they are for the owner to decide.
2. ``theft_incidents.evidence_expired_at`` and
   ``security_events.evidence_expired_at``: set by
   ``services/evidence_storage.py`` when the evidence storage limit deletes
   the still/clip a row referenced, so it reads "evidence expired" instead of
   a broken link.

The column list is frozen; later changes need a new migration.
"""

import logging

from .sqlite_utils import add_columns, columns, has_table

NAME = "evidence_only_storage"

EXPIRED = {"evidence_expired_at": "DATETIME"}


def upgrade(conn) -> None:
    for table in ("theft_incidents", "security_events"):
        if has_table(conn, table):
            add_columns(conn, table, EXPIRED)
    if has_table(conn, "cameras") and "dvr_enabled" in columns(conn, "cameras"):
        cur = conn.execute("UPDATE cameras SET dvr_enabled = 0 WHERE dvr_enabled != 0")
        if cur.rowcount:
            logging.getLogger("edge.migrations").info(
                f"dvr_enabled turned off on {cur.rowcount} camera(s): this device does not record "
                "continuously (the store NAS does); no recording files were touched"
            )
