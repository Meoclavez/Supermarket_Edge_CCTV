"""Theft-incident evidence and pose-based shelf-interaction columns.

Replaces the ad-hoc ALTERs added to init_db for the pose pipeline
(app/services/pose_analytics.py), with the same types and defaults, plus the
index the model declares on ``theft_incidents.rule``.
"""

from .sqlite_utils import add_columns, create_index

NAME = "pose_theft_interaction_columns"

THEFT_INCIDENTS = {
    "camera_name": "VARCHAR(128) DEFAULT 'Camera'",
    "shelf_zone_id": "VARCHAR(64)",
    "evidence_summary": "VARCHAR(1024) DEFAULT ''",
    "snapshot_path": "VARCHAR(512)",
    "clip_path": "VARCHAR(512)",
    "officer_notes": "VARCHAR(1024)",
    "updated_at": "DATETIME",
    "rule": "VARCHAR(64)",
    "evidence": "JSON",
}

SHELF_INTERACTIONS = {
    "hand": "VARCHAR(8)",
    "started_at": "DATETIME",
    "ended_at": "DATETIME",
    "confidence": "FLOAT",
    "zone_name": "VARCHAR(128)",
    "zone_space": "VARCHAR(16)",
    "image_x": "FLOAT",
    "image_y": "FLOAT",
    "floor_x": "FLOAT",
    "floor_y": "FLOAT",
}


def upgrade(conn) -> None:
    add_columns(conn, "theft_incidents", THEFT_INCIDENTS)
    add_columns(conn, "shelf_interactions", SHELF_INTERACTIONS)
    create_index(conn, "ix_theft_incidents_rule", "theft_incidents", ["rule"])
