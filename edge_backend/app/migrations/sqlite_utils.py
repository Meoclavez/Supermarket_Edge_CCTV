"""Small, idempotent SQLite schema helpers used by migrations and the reconcile step.

All helpers take a plain ``sqlite3.Connection`` opened with
``isolation_level=None`` (the migration runner owns BEGIN/COMMIT).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Iterable

logger = logging.getLogger("edge.migrations")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def q(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """``{name: {type, notnull, default, pk}}`` from PRAGMA table_info (empty if no table)."""
    out: dict[str, dict] = {}
    for cid, name, ctype, notnull, dflt, pk in conn.execute(f"PRAGMA table_info({q(table)})"):
        out[name] = {"type": ctype or "", "notnull": bool(notnull), "default": dflt, "pk": int(pk)}
    return out


def index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({q(table)})")}


def add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE ... ADD COLUMN`` only if the table exists and lacks the column.

    ``ddl`` is everything after the column name, e.g. ``"FLOAT DEFAULT 0.0"``.
    Returns True when the column was added.
    """
    if not has_table(conn, table):
        return False
    if column in columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(column)} {ddl}")
    logger.info(f"added column {table}.{column} {ddl}")
    return True


def add_columns(conn: sqlite3.Connection, table: str, spec: dict[str, str]) -> list[str]:
    return [c for c, ddl in spec.items() if add_column(conn, table, c, ddl)]


def create_index(conn: sqlite3.Connection, name: str, table: str, cols: Iterable[str], unique: bool = False) -> bool:
    """Create an index if the table and every column exist and the name is free."""
    cols = list(cols)
    if not has_table(conn, table):
        return False
    present = columns(conn, table)
    if any(c not in present for c in cols):
        return False
    if name in index_names(conn, table):
        return False
    conn.execute(
        f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {q(name)} "
        f"ON {q(table)} ({', '.join(q(c) for c in cols)})"
    )
    logger.info(f"created index {name} on {table}({', '.join(cols)})")
    return True


def foreign_key_violations(conn: sqlite3.Connection, table: str | None = None) -> set[tuple]:
    """Rows of PRAGMA foreign_key_check as ``(table, rowid, parent)`` tuples."""
    sql = "PRAGMA foreign_key_check" if table is None else f"PRAGMA foreign_key_check({q(table)})"
    return {(r[0], r[1], r[2]) for r in conn.execute(sql)}


def user_tables_have_rows(conn: sqlite3.Connection, ignore: Iterable[str] = ("schema_migrations",)) -> bool:
    ignore = set(ignore)
    for t in table_names(conn):
        if t in ignore:
            continue
        if conn.execute(f"SELECT 1 FROM {q(t)} LIMIT 1").fetchone():
            return True
    return False
