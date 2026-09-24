"""In-house schema migrations for the edge SQLite database.

Adding a schema change:

* A new model column or table needs nothing: the reconcile step adds it on the
  next start (with a default derived from the model).
* Anything else -- data fixes, renames, type changes, NOT NULL tightening,
  dropping columns -- goes in a new ``mNNNN_<name>.py`` with an idempotent
  ``upgrade(conn)``; use :func:`rebuild_table` for what SQLite's ALTER cannot do.

Never edit a migration after it has shipped. Devices migrate automatically on
start (``init_db``); ops can run ``python -m app.migrations status|upgrade|check``.
"""

from .rebuild import RebuildError, rebuild_table
from .runner import (
    MigrationError,
    SchemaTooNewError,
    discover,
    head_version,
    run_migrations,
    status,
)

__all__ = [
    "MigrationError",
    "RebuildError",
    "SchemaTooNewError",
    "discover",
    "head_version",
    "rebuild_table",
    "run_migrations",
    "status",
]
