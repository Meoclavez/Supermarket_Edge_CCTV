"""Alert tiers on ``theft_incidents``.

``alert_tier`` (review | watch | alert | critical), ``risk_score`` (0..1) and
``risk_factors`` (JSON list of ``{factor, value, effect, reason}``) as scored
by ``services/theft_alert_policy.py`` when the incident was raised, plus an
index on ``alert_tier`` for the ``?tier=`` filter.

Rows recorded before this migration keep NULL: their tier is not inferred
from the old confidence-based severity (the API reports them as
unclassified). The column list is frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, create_index, has_table

NAME = "theft_alert_tiers"

THEFT_INCIDENTS = {
    "alert_tier": "VARCHAR(16)",
    "risk_score": "FLOAT",
    "risk_factors": "JSON",
}


def upgrade(conn) -> None:
    if not has_table(conn, "theft_incidents"):
        return
    add_columns(conn, "theft_incidents", THEFT_INCIDENTS)
    create_index(conn, "ix_theft_incidents_alert_tier", "theft_incidents", ["alert_tier"])
