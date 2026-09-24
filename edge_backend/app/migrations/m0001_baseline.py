"""Baseline: the schema every device had before versioned migrations existed.

This DDL is frozen. It was captured from a production database (2026-09-23)
and must never be edited to follow the models; later changes belong in new
``mNNNN_*.py`` files. Every statement is ``IF NOT EXISTS``, so running it
against an existing, un-versioned database *adopts* that database: tables and
indexes it already has are left untouched (with their data) and only absent
ones are created. Columns an older table lacks are added by m0002/m0003.
"""

NAME = "baseline"

BASELINE_DDL = r"""CREATE TABLE IF NOT EXISTS admin_users (
	id VARCHAR(64) NOT NULL, 
	username VARCHAR(128) NOT NULL, 
	password_hash VARCHAR(256) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_login DATETIME, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS system_setup (
	"key" VARCHAR(128) NOT NULL, 
	value VARCHAR(4096) NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);
CREATE TABLE IF NOT EXISTS cameras (
	id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	location VARCHAR(128) NOT NULL, 
	rtsp_url VARCHAR(512) NOT NULL, 
	webrtc_url VARCHAR(512), 
	status VARCHAR(32) NOT NULL, 
	fps INTEGER NOT NULL, 
	resolution VARCHAR(32) NOT NULL, 
	is_ai_enabled BOOLEAN NOT NULL, 
	ai_models JSON NOT NULL, 
	dvr_enabled BOOLEAN NOT NULL, 
	dvr_retention_days INTEGER NOT NULL, 
	dvr_quota_gb FLOAT NOT NULL, 
	muted_until DATETIME, 
	last_seen DATETIME, 
	created_at DATETIME NOT NULL, 
	channel_number INTEGER NOT NULL, 
	department VARCHAR(64) NOT NULL, 
	floor_x FLOAT NOT NULL, 
	floor_y FLOAT NOT NULL, 
	floor_z FLOAT NOT NULL, 
	azimuth_deg FLOAT NOT NULL, 
	fov_deg FLOAT NOT NULL, 
	homography_matrix JSON, 
	features JSON, calibration_points JSON, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS device_tokens (
	device_token VARCHAR(256) NOT NULL, 
	platform VARCHAR(32) NOT NULL, 
	device_name VARCHAR(128), 
	app_version VARCHAR(32), 
	last_registered DATETIME NOT NULL, 
	PRIMARY KEY (device_token)
);
CREATE TABLE IF NOT EXISTS planogram_items (
	sku_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	shelf_zone_id VARCHAR(64) NOT NULL, 
	price FLOAT NOT NULL, 
	facing_count INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (sku_id)
);
CREATE TABLE IF NOT EXISTS pos_transactions (
	id VARCHAR(64) NOT NULL, 
	transaction_id VARCHAR(64) NOT NULL, 
	timestamp DATETIME NOT NULL, 
	register_id VARCHAR(64) NOT NULL, 
	sku_id VARCHAR(64) NOT NULL, 
	quantity INTEGER NOT NULL, 
	amount FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, total_amount FLOAT DEFAULT 0.0, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS shelf_interactions (
	id VARCHAR(64) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	shelf_zone_id VARCHAR(64) NOT NULL, 
	timestamp DATETIME NOT NULL, 
	person_track_id VARCHAR(64) NOT NULL, 
	action_type VARCHAR(32) NOT NULL, 
	duration_sec FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS customer_tracks (
	id VARCHAR(64) NOT NULL, 
	track_id VARCHAR(64) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	start_time DATETIME NOT NULL, 
	end_time DATETIME, 
	trajectory_points JSON NOT NULL, 
	age_group VARCHAR(32), 
	gender VARCHAR(32), 
	sentiment VARCHAR(32), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS retail_analytics_summaries (
	id VARCHAR(64) NOT NULL, 
	date VARCHAR(32) NOT NULL, 
	store_id VARCHAR(64) NOT NULL, 
	total_footfall INTEGER NOT NULL, 
	avg_dwell_time FLOAT NOT NULL, 
	zone_metrics JSON NOT NULL, 
	lost_sales_alerts JSON NOT NULL, 
	recommendations JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS ai_decision_recommendations (
	id VARCHAR(64) NOT NULL, 
	date VARCHAR(32) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	severity VARCHAR(32) NOT NULL, 
	zone VARCHAR(64) NOT NULL, 
	finding VARCHAR(512) NOT NULL, 
	root_cause VARCHAR(512) NOT NULL, 
	action_item VARCHAR(512) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS theft_incidents (
	id VARCHAR(64) NOT NULL, 
	timestamp DATETIME NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	camera_name VARCHAR(128) NOT NULL, 
	department VARCHAR(64) NOT NULL, 
	shelf_zone_id VARCHAR(64), 
	theft_type VARCHAR(64) NOT NULL, 
	severity VARCHAR(32) NOT NULL, 
	confidence FLOAT NOT NULL, 
	person_track_id VARCHAR(64), 
	evidence_summary VARCHAR(1024) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	snapshot_path VARCHAR(512), 
	clip_path VARCHAR(512), 
	officer_notes VARCHAR(1024), 
	notes VARCHAR(1024), 
	zone_id VARCHAR(64), 
	estimated_loss_value FLOAT NOT NULL, 
	items_involved JSON NOT NULL, 
	evidence_snapshot_url VARCHAR(512), 
	evidence_clip_url VARCHAR(512), 
	bounding_box JSON, 
	wrist_trajectory JSON, 
	guard_id VARCHAR(64), 
	dispatch_details JSON, 
	resolution VARCHAR(64), 
	resolved_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS security_events (
	id VARCHAR(64) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	camera_name VARCHAR(128) NOT NULL, 
	location VARCHAR(128) NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	severity VARCHAR(32) NOT NULL, 
	confidence FLOAT NOT NULL, 
	timestamp DATETIME NOT NULL, 
	clip_url VARCHAR(512), 
	snapshot_url VARCHAR(512), 
	bounding_box JSON, 
	keypoints JSON, 
	kinematics JSON, 
	metadata_json JSON, 
	acknowledged BOOLEAN NOT NULL, 
	acknowledged_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(camera_id) REFERENCES cameras (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS dvr_segments (
	id VARCHAR(128) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	start_time DATETIME NOT NULL, 
	end_time DATETIME NOT NULL, 
	duration_seconds FLOAT NOT NULL, 
	file_path VARCHAR(512) NOT NULL, 
	file_size_bytes BIGINT NOT NULL, 
	is_corrupted BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(camera_id) REFERENCES cameras (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS incident_archives (
	id VARCHAR(64) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	camera_name VARCHAR(128) NOT NULL, 
	title VARCHAR(256) NOT NULL, 
	description VARCHAR(512), 
	start_time DATETIME NOT NULL, 
	end_time DATETIME NOT NULL, 
	file_path VARCHAR(512) NOT NULL, 
	file_size_bytes BIGINT NOT NULL, 
	duration_seconds FLOAT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	download_url VARCHAR(512), 
	is_protected BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(camera_id) REFERENCES cameras (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS store_layouts (
	id VARCHAR(64) NOT NULL, 
	store_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	width_m FLOAT NOT NULL, 
	height_m FLOAT NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS zone_visits (
	id VARCHAR(64) NOT NULL, 
	zone_id VARCHAR(64) NOT NULL, 
	track_id VARCHAR(64) NOT NULL, 
	camera_id VARCHAR(64) NOT NULL, 
	entered_at DATETIME NOT NULL, 
	exited_at DATETIME, 
	dwell_seconds FLOAT NOT NULL, 
	interacted BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS discovered_devices (
	id VARCHAR(128) NOT NULL, 
	transport VARCHAR(16) NOT NULL, 
	driver VARCHAR(32) NOT NULL, 
	host VARCHAR(128), 
	port INTEGER, 
	device_path VARCHAR(128), 
	model_name VARCHAR(128), 
	manufacturer VARCHAR(128), 
	stream_urls JSON NOT NULL, 
	channels INTEGER NOT NULL, 
	requires_credentials BOOLEAN NOT NULL, 
	reachable BOOLEAN NOT NULL, 
	adopted_camera_id VARCHAR(64), 
	first_seen DATETIME NOT NULL, 
	last_seen DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS store_zones (
	id VARCHAR(64) NOT NULL, 
	layout_id VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	polygon JSON NOT NULL, 
	color VARCHAR(16) NOT NULL, 
	sku_id VARCHAR(64), 
	sort_order INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(layout_id) REFERENCES store_layouts (id)
);
CREATE TABLE IF NOT EXISTS store_structures (
	id VARCHAR(64) NOT NULL, 
	layout_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	polygon JSON NOT NULL, 
	thickness_m FLOAT NOT NULL, 
	color VARCHAR(16) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	properties JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(layout_id) REFERENCES store_layouts (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_username ON admin_users (username);
CREATE INDEX IF NOT EXISTS ix_planogram_items_category ON planogram_items (category);
CREATE INDEX IF NOT EXISTS ix_planogram_items_shelf_zone_id ON planogram_items (shelf_zone_id);
CREATE INDEX IF NOT EXISTS ix_pos_transactions_timestamp ON pos_transactions (timestamp);
CREATE INDEX IF NOT EXISTS ix_pos_transactions_register_id ON pos_transactions (register_id);
CREATE INDEX IF NOT EXISTS ix_pos_transactions_transaction_id ON pos_transactions (transaction_id);
CREATE INDEX IF NOT EXISTS ix_pos_transactions_sku_id ON pos_transactions (sku_id);
CREATE INDEX IF NOT EXISTS ix_shelf_interactions_person_track_id ON shelf_interactions (person_track_id);
CREATE INDEX IF NOT EXISTS ix_shelf_interactions_shelf_zone_id ON shelf_interactions (shelf_zone_id);
CREATE INDEX IF NOT EXISTS ix_shelf_interactions_timestamp ON shelf_interactions (timestamp);
CREATE INDEX IF NOT EXISTS ix_shelf_interactions_camera_id ON shelf_interactions (camera_id);
CREATE INDEX IF NOT EXISTS ix_customer_tracks_start_time ON customer_tracks (start_time);
CREATE INDEX IF NOT EXISTS ix_customer_tracks_camera_id ON customer_tracks (camera_id);
CREATE INDEX IF NOT EXISTS ix_customer_tracks_track_id ON customer_tracks (track_id);
CREATE INDEX IF NOT EXISTS ix_retail_analytics_summaries_date ON retail_analytics_summaries (date);
CREATE INDEX IF NOT EXISTS ix_ai_decision_recommendations_date ON ai_decision_recommendations (date);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_camera_id ON theft_incidents (camera_id);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_severity ON theft_incidents (severity);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_status ON theft_incidents (status);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_department ON theft_incidents (department);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_theft_type ON theft_incidents (theft_type);
CREATE INDEX IF NOT EXISTS ix_theft_incidents_timestamp ON theft_incidents (timestamp);
CREATE INDEX IF NOT EXISTS ix_dvr_segments_camera_id ON dvr_segments (camera_id);
CREATE INDEX IF NOT EXISTS ix_dvr_segments_end_time ON dvr_segments (end_time);
CREATE INDEX IF NOT EXISTS ix_dvr_camera_time ON dvr_segments (camera_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS ix_dvr_segments_start_time ON dvr_segments (start_time);
CREATE INDEX IF NOT EXISTS ix_incident_archives_camera_id ON incident_archives (camera_id);
CREATE INDEX IF NOT EXISTS ix_store_layouts_store_id ON store_layouts (store_id);
CREATE INDEX IF NOT EXISTS ix_zone_visits_camera_id ON zone_visits (camera_id);
CREATE INDEX IF NOT EXISTS ix_zone_visits_entered_at ON zone_visits (entered_at);
CREATE INDEX IF NOT EXISTS ix_zone_visits_zone_id ON zone_visits (zone_id);
CREATE INDEX IF NOT EXISTS ix_zone_visits_track_id ON zone_visits (track_id);
CREATE INDEX IF NOT EXISTS ix_zone_visits_zone_entered ON zone_visits (zone_id, entered_at);
CREATE INDEX IF NOT EXISTS ix_store_zones_layout_id ON store_zones (layout_id);
CREATE INDEX IF NOT EXISTS ix_store_zones_sku_id ON store_zones (sku_id);
CREATE INDEX IF NOT EXISTS ix_store_zones_category ON store_zones (category);
CREATE INDEX IF NOT EXISTS ix_store_structures_kind ON store_structures (kind);
CREATE INDEX IF NOT EXISTS ix_store_structures_layout_id ON store_structures (layout_id);
"""


def _statements(sql: str):
    buf = []
    for line in sql.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            buf = []
            if stmt:
                yield stmt
    tail = "\n".join(buf).strip()
    if tail:
        yield tail


def upgrade(conn) -> None:
    import logging
    import sqlite3

    log = logging.getLogger("edge.migrations")
    for stmt in _statements(BASELINE_DDL):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # An index over a column an old table does not have yet: the
            # column is added by a later migration and the reconcile step
            # then creates the index. Anything else is a real failure.
            if "INDEX" in stmt.split("(")[0] and "no such column" in str(exc):
                log.info(f"baseline: deferring index until its column exists ({exc}): {stmt.split(' ON ')[0]}")
                continue
            raise

