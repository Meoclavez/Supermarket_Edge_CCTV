"""Insight topics: one row per recurring issue, grouping the daily findings.

1. ``insight_topics``: a deterministic group of ``ai_decision_recommendations``
   rows that describe the same issue (same subject, issue family and
   direction) across days, with the operator's status, the trend, and the
   unified analysis / action plan (services/insight_topics.py).
2. ``ai_decision_recommendations.topic_id``: the topic a finding belongs to
   (NULL until grouped; the startup backfill and every analysis run group
   new rows).

The DDL and column list are frozen; later changes need a new migration.
"""

from .sqlite_utils import add_columns, create_index, has_table

NAME = "insight_topics"

RECOMMENDATIONS = {"topic_id": "VARCHAR(64)"}

DDL = r"""CREATE TABLE IF NOT EXISTS insight_topics (
	id VARCHAR(64) NOT NULL,
	topic_key VARCHAR(255) NOT NULL,
	alias_keys JSON,
	title VARCHAR(255) NOT NULL,
	family VARCHAR(64) NOT NULL,
	category VARCHAR(64) NOT NULL,
	zone_id VARCHAR(64),
	zone_name VARCHAR(128),
	subject VARCHAR(255),
	direction VARCHAR(16),
	first_seen VARCHAR(32),
	last_seen VARCHAR(32),
	occurrences INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	snooze_until VARCHAR(32),
	trend VARCHAR(16),
	severity VARCHAR(16),
	unified_summary TEXT,
	unified_actions JSON,
	analysis JSON,
	analysed_at DATETIME,
	data_window JSON,
	needs_analysis BOOLEAN NOT NULL,
	action_done_at VARCHAR(32),
	acknowledged_at DATETIME,
	resolved_at DATETIME,
	reopened_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_insight_topics_topic_key ON insight_topics (topic_key);
CREATE INDEX IF NOT EXISTS ix_insight_topics_status ON insight_topics (status);
CREATE INDEX IF NOT EXISTS ix_insight_topics_last_seen ON insight_topics (last_seen);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    if has_table(conn, "ai_decision_recommendations"):
        add_columns(conn, "ai_decision_recommendations", RECOMMENDATIONS)
        create_index(conn, "ix_ai_decision_recommendations_topic_id", "ai_decision_recommendations",
                     ["topic_id"])
