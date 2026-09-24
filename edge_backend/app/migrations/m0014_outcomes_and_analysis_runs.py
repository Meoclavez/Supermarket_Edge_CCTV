"""Theft review outcomes, who acted, recommendation evidence, analysis runs.

1. ``theft_incidents``: ``recovered_value`` (what staff actually recovered,
   entered by the reviewer; NULL = not entered), ``resolved_by`` (the
   signed-in operator or paired phone that recorded the outcome; NULL on rows
   resolved before this migration, whose outcome may be the old
   ``RECOVERED_GOODS`` default and is therefore treated as unverified),
   ``dispatched_by`` / ``dispatched_at`` (who marked "staff sent", and when).
   The outcome itself stays in the existing ``resolution`` column.
2. ``ai_decision_recommendations``: ``evidence`` (the metrics the rule cited)
   and ``source`` (which engine produced the row, e.g. ``business_rules``).
3. ``analysis_runs``: one row per business-analysis run (manual, scheduled or
   from the market tab), with the inputs it saw and its result, so the
   dashboard can show the latest recommendations with a plain GET instead of
   triggering a run.

The DDL and column list are frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, has_table

NAME = "outcomes_and_analysis_runs"

THEFT_INCIDENTS = {
    "recovered_value": "FLOAT",
    "resolved_by": "VARCHAR(128)",
    "dispatched_by": "VARCHAR(128)",
    "dispatched_at": "DATETIME",
}

RECOMMENDATIONS = {
    "evidence": "JSON",
    "source": "VARCHAR(32)",
}

DDL = r"""CREATE TABLE IF NOT EXISTS analysis_runs (
	id VARCHAR(64) NOT NULL,
	run_trigger VARCHAR(16) NOT NULL,
	requested_by VARCHAR(128),
	generated_at DATETIME NOT NULL,
	findings_count INTEGER NOT NULL,
	narrated BOOLEAN NOT NULL,
	inputs_summary JSON,
	result JSON,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_analysis_runs_generated_at ON analysis_runs (generated_at);
"""


def upgrade(conn) -> None:
    if has_table(conn, "theft_incidents"):
        add_columns(conn, "theft_incidents", THEFT_INCIDENTS)
    if has_table(conn, "ai_decision_recommendations"):
        add_columns(conn, "ai_decision_recommendations", RECOMMENDATIONS)
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
