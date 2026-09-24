"""Columns that init_db used to add with ad-hoc ALTERs on every start.

Folded in verbatim (same column types and defaults) so devices that never ran
those ALTERs converge exactly as before. Each ALTER only runs when the table
exists and lacks the column. ``ai_decisions`` is a table name used by very old
builds; it is only touched if a device still has it.
"""

from .sqlite_utils import add_columns

NAME = "legacy_adhoc_columns"

AI_DECISIONS = {
    "date": "VARCHAR(32)",
    "severity": "VARCHAR(32) DEFAULT 'MEDIUM'",
    "zone": "VARCHAR(128)",
    "finding": "VARCHAR(1024)",
    "root_cause": "VARCHAR(1024)",
    "action_item": "VARCHAR(1024)",
    "title": "VARCHAR(256)",
    "description": "VARCHAR(1024)",
    "impact": "VARCHAR(32) DEFAULT 'MEDIUM'",
    "confidence": "FLOAT DEFAULT 0.85",
    "action_type": "VARCHAR(64) DEFAULT 'OPEN_REGISTER'",
    "target_zone": "VARCHAR(128)",
    "payload_json": "JSON",
    "updated_at": "DATETIME",
    "applied_at": "DATETIME",
}

POS_TRANSACTIONS = {
    "amount": "FLOAT DEFAULT 0.0",
    "total_amount": "FLOAT DEFAULT 0.0",
}

CAMERAS = {
    "channel_number": "INTEGER DEFAULT 1",
    "department": "VARCHAR(64) DEFAULT 'GENERAL'",
    "floor_x": "FLOAT DEFAULT 100.0",
    "floor_y": "FLOAT DEFAULT 100.0",
    "floor_z": "FLOAT DEFAULT 3.2",
    "azimuth_deg": "FLOAT DEFAULT 0.0",
    "fov_deg": "FLOAT DEFAULT 85.0",
    "homography_matrix": "JSON",
    "calibration_points": "JSON",
    "features": "JSON",
}


def upgrade(conn) -> None:
    add_columns(conn, "ai_decisions", AI_DECISIONS)
    add_columns(conn, "pos_transactions", POS_TRANSACTIONS)
    add_columns(conn, "cameras", CAMERAS)
