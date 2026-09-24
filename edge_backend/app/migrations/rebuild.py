"""SQLite table rebuild: the documented 12-step procedure for changes ALTER cannot do.

SQLite's ``ALTER TABLE`` can add and rename columns but cannot change a
column's type, tighten ``NULL`` to ``NOT NULL``, add/remove constraints or (on
older SQLite) drop a column. The supported way is
https://www.sqlite.org/lang_altertable.html#otheralter :

 1. disable foreign keys           7. rename the new table to the old name
 2. BEGIN                          8. recreate indexes and triggers
 3. remember indexes/triggers/views 9. recreate views
 4. CREATE the new table            10. PRAGMA foreign_key_check
 5. copy the rows across           11. COMMIT
 6. DROP the old table              12. re-enable foreign keys

When called inside a transaction (as versioned migrations are: the runner has
already done steps 1-2 and does 11-12) this performs steps 3-10. When called
outside a transaction it performs all twelve itself.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Iterable

from .sqlite_utils import columns, foreign_key_violations, has_table, q

logger = logging.getLogger("edge.migrations")


class RebuildError(RuntimeError):
    pass


def _retarget_create(create_sql: str, table: str, tmp: str) -> str:
    pattern = re.compile(
        r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"%s\"|`%s`|\[%s\]|%s)(?=[\s(])"
        % ((re.escape(table),) * 4),
        re.IGNORECASE,
    )
    if not pattern.search(create_sql):
        raise RebuildError(f"create_sql must be a CREATE TABLE statement for {table!r}")
    return pattern.sub(f"CREATE TABLE {q(tmp)}", create_sql, count=1)


def rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    *,
    column_exprs: dict[str, str] | None = None,
    extra_sql: Iterable[str] = (),
    skip_indexes_on_dropped_columns: bool = True,
) -> dict:
    """Rebuild ``table`` into the shape described by ``create_sql``.

    ``create_sql``: ``CREATE TABLE <table> (...)`` for the new shape.
    ``column_exprs``: optional ``{new_column: SQL expression over the old row}``
        for transforms, e.g. ``{"fps": "CAST(fps AS INTEGER)"}`` or
        ``{"name": "COALESCE(name, '')"}`` when tightening to NOT NULL.
        Other new columns are copied by name when the old table has them, and
        otherwise take the new table's default.
    ``extra_sql``: statements run after the indexes are restored (new indexes).

    Row ids are preserved, so rows keep their identity. Indexes and triggers of
    the old table are recreated; an index over a column that no longer exists
    is skipped (and reported) rather than failing the rebuild.
    """
    if not has_table(conn, table):
        raise RebuildError(f"table {table!r} does not exist")

    own_txn = not conn.in_transaction
    fk_was_on = False
    if own_txn:
        fk_was_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        conn.execute("PRAGMA foreign_keys=OFF")          # step 1
        conn.execute("BEGIN IMMEDIATE")                   # step 2
    try:
        result = _rebuild_steps(conn, table, create_sql, column_exprs or {}, list(extra_sql),
                                skip_indexes_on_dropped_columns)
        if own_txn:
            conn.execute("COMMIT")                        # step 11
        return result
    except Exception:
        if own_txn and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if own_txn and fk_was_on:
            conn.execute("PRAGMA foreign_keys=ON")        # step 12


def _rebuild_steps(conn, table, create_sql, column_exprs, extra_sql, skip_dropped) -> dict:
    fk_before = foreign_key_violations(conn)

    # step 3: remember what hangs off the table
    old_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0] or ""
    without_rowid = "WITHOUT ROWID" in old_sql.upper()
    attached = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    views = [
        (name, sql)
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view'")
        if re.search(r"\b%s\b" % re.escape(table), sql or "", re.IGNORECASE)
    ]
    for name, _ in views:
        conn.execute(f"DROP VIEW {q(name)}")

    old_cols = columns(conn, table)
    old_count = conn.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0]

    # step 4: new table under a temporary name
    tmp = f"_rebuild_new_{table}"
    conn.execute(f"DROP TABLE IF EXISTS {q(tmp)}")
    conn.execute(_retarget_create(create_sql, table, tmp))
    new_cols = columns(conn, tmp)

    # step 5: copy rows
    targets, sources = [], []
    for col in new_cols:
        if col in column_exprs:
            targets.append(q(col))
            sources.append(column_exprs[col])
        elif col in old_cols:
            targets.append(q(col))
            sources.append(q(col))
    dropped = [c for c in old_cols if c not in new_cols]
    if not without_rowid and not any(new_cols[c]["pk"] and new_cols[c]["type"].upper() == "INTEGER"
                                     for c in new_cols):
        targets.insert(0, "rowid")
        sources.insert(0, "rowid")
    conn.execute(
        f"INSERT INTO {q(tmp)} ({', '.join(targets)}) SELECT {', '.join(sources)} FROM {q(table)}"
    )
    new_count = conn.execute(f"SELECT COUNT(*) FROM {q(tmp)}").fetchone()[0]
    if new_count != old_count:
        raise RebuildError(f"{table}: copied {new_count} of {old_count} rows")

    # steps 6-7
    conn.execute(f"DROP TABLE {q(table)}")
    conn.execute(f"ALTER TABLE {q(tmp)} RENAME TO {q(table)}")

    # step 8: indexes and triggers
    restored, skipped = [], []
    for kind, name, sql in attached:
        try:
            conn.execute(sql)
            restored.append(name)
        except sqlite3.OperationalError as exc:
            if skip_dropped and kind == "index" and "no such column" in str(exc):
                skipped.append(name)
                logger.warning(f"rebuild {table}: index {name} not restored ({exc})")
                continue
            raise
    for sql in extra_sql:
        conn.execute(sql)

    # step 9: views
    for name, sql in views:
        conn.execute(sql)

    # step 10: the rebuild must not introduce foreign-key violations.
    # Pre-existing orphans (foreign keys were not enforced by older builds) are
    # left alone; only new ones abort the rebuild.
    introduced = foreign_key_violations(conn) - fk_before
    if introduced:
        raise RebuildError(f"{table}: rebuild would violate foreign keys: {sorted(introduced)[:5]}")

    logger.info(
        f"rebuilt table {table}: {new_count} rows kept, dropped columns={dropped or '-'}, "
        f"indexes restored={len(restored)}, skipped={skipped or '-'}"
    )
    return {"table": table, "rows": new_count, "dropped_columns": dropped,
            "indexes_restored": restored, "indexes_skipped": skipped}
