"""Phone pairing: ``paired_devices`` and ``pairing_sessions``.

A paired device is a staff phone bound to *this* edge device. Tokens issued to
it carry the device id and the paired-device id, so revoking the row cuts the
phone off, and a phone paired with a different edge box on the same network
can never use its token here. Pairing codes and refresh tokens are stored only
as hashes. The DDL is frozen; later changes need a new migration.
"""

NAME = "paired_devices"

DDL = r"""CREATE TABLE IF NOT EXISTS paired_devices (
	id VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	platform VARCHAR(16) NOT NULL,
	app_instance_id VARCHAR(128) NOT NULL,
	user_id VARCHAR(64),
	paired_via VARCHAR(16) NOT NULL,
	push_provider VARCHAR(16),
	push_token VARCHAR(4096),
	push_token_updated_at DATETIME,
	refresh_token_hash VARCHAR(128),
	alert_prefs JSON,
	created_at DATETIME NOT NULL,
	last_seen_at DATETIME,
	revoked_at DATETIME,
	last_push_status VARCHAR(32),
	last_push_at DATETIME,
	last_push_error VARCHAR(512),
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_paired_devices_app_instance_id ON paired_devices (app_instance_id);
CREATE TABLE IF NOT EXISTS pairing_sessions (
	id VARCHAR(64) NOT NULL,
	code_hash VARCHAR(128) NOT NULL,
	created_by VARCHAR(64),
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	used_at DATETIME,
	paired_device_id VARCHAR(64),
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_pairing_sessions_code_hash ON pairing_sessions (code_hash);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
