"""Product attribution on ``shelf_interactions``.

Each reach row now carries the product it was attributed to at the moment it
happened -- SKU, category, shelf level (TOP / MIDDLE / BOTTOM) and value tier --
plus which point of the hand entered the zone (``hand_tip`` extrapolated past
the wrist along the forearm, or ``wrist``). Copying them onto the row keeps
history correct after an operator edits, re-levels or deletes the zone.

Older rows keep NULL in these columns; analytics fall back to the zone's
current configuration for them. The column list is frozen; later changes need
a new migration.
"""

from .sqlite_utils import add_columns, create_index

NAME = "shelf_interaction_attribution"

SHELF_INTERACTIONS = {
    "sku_id": "VARCHAR(64)",
    "product_category": "VARCHAR(64)",
    "shelf_level": "VARCHAR(8)",
    "value_tier": "VARCHAR(16)",
    "contact_point": "VARCHAR(12)",
}


def upgrade(conn) -> None:
    add_columns(conn, "shelf_interactions", SHELF_INTERACTIONS)
    create_index(conn, "ix_shelf_interactions_zone_ts", "shelf_interactions", ["shelf_zone_id", "timestamp"])
