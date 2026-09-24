"""Data fix: ``setup_completed='true'`` with no operator account is stale.

A database was found with the flag set weeks before any account existed,
which would let the dashboard skip the protected first-run step. Setup is only
complete once an active admin exists (same rule as
``SystemSetupService.is_setup_completed``), so reset the flag in that case.
"""

import logging

from .sqlite_utils import has_table

NAME = "setup_flag_requires_admin"


def upgrade(conn) -> None:
    if not has_table(conn, "system_setup"):
        return
    admin_exists = has_table(conn, "admin_users") and conn.execute(
        "SELECT 1 FROM admin_users WHERE is_active = 1 OR is_active IS NULL LIMIT 1"
    ).fetchone()
    if admin_exists:
        return
    cur = conn.execute(
        "UPDATE system_setup SET value = 'false' WHERE key = 'setup_completed' AND value = 'true'"
    )
    if cur.rowcount:
        logging.getLogger("edge.migrations").warning(
            "setup_completed was 'true' but no active admin account exists; reset to 'false' "
            "so first-run setup is required"
        )
