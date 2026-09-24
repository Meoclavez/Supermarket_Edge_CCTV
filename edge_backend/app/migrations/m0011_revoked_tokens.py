"""Server-side sign-out: ``revoked_tokens``.

One row per revoked session token or login session, kept until the token
would have expired anyway (``expires_at``, unix seconds; expired rows are
pruned by ``services/token_revocation.py``). ``jti`` holds the revocation key:
``jti:<jti>``, ``h:<sha256 prefix>`` for tokens issued before the ``jti``
claim existed, or ``sid:<session id>``. The DDL is frozen; later changes need
a new migration.
"""

NAME = "revoked_tokens"

DDL = r"""CREATE TABLE IF NOT EXISTS revoked_tokens (
	jti VARCHAR(64) NOT NULL,
	token_type VARCHAR(16) NOT NULL,
	expires_at INTEGER NOT NULL,
	revoked_at DATETIME NOT NULL,
	PRIMARY KEY (jti)
);
CREATE INDEX IF NOT EXISTS ix_revoked_tokens_expires_at ON revoked_tokens (expires_at);
"""


def upgrade(conn) -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
