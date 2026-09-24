"""Additive safety net that runs after the versioned migrations.

For every table in the SQLAlchemy metadata it:

* creates the table (and its indexes) when missing;
* adds each missing column with ``ALTER TABLE ... ADD COLUMN``, using the
  column's SQLAlchemy type and a constant default derived from its Python
  default (NOT NULL without a usable default gets a type-appropriate one);
* creates each missing declared index.

It never drops, renames or retypes anything. Extra columns the models do not
know, type differences and nullability differences are *reported*: changing
those needs a versioned migration using :func:`app.migrations.rebuild.rebuild_table`.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import enum
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from sqlalchemy import MetaData
from sqlalchemy import types as sat
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from .sqlite_utils import columns, has_table, index_names, q

logger = logging.getLogger("edge.migrations")
_DIALECT = sqlite_dialect.dialect()
_EPOCH = "1970-01-01 00:00:00.000000"
# SQLite forbids these as the default of an added column.
_NON_CONSTANT = {"CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"}


@dataclass
class Op:
    kind: str            # create_table | add_column | create_index
    table: str
    name: str
    sql: list[str]
    detail: str = ""


@dataclass
class ReconcilePlan:
    ops: list[Op] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)      # table.column not in models
    type_mismatches: list[str] = field(default_factory=list)    # need a rebuild migration
    nullability: list[str] = field(default_factory=list)        # informational
    unsupported: list[str] = field(default_factory=list)        # cannot be added by ALTER

    @property
    def missing(self) -> list[str]:
        return [f"{o.kind} {o.table}{'.' + o.name if o.kind != 'create_table' else ''}" for o in self.ops]

    @property
    def has_drift(self) -> bool:
        """Drift that ``check`` should fail on (extra columns are informational)."""
        return bool(self.ops or self.type_mismatches or self.unsupported)

    def as_dict(self) -> dict:
        return {
            "missing": self.missing,
            "extra_columns": self.extra_columns,
            "type_mismatches": self.type_mismatches,
            "nullability": self.nullability,
            "unsupported": self.unsupported,
        }


def _type_sql(col) -> str:
    return col.type.compile(dialect=_DIALECT)


def _norm_type(t: str) -> str:
    return "".join(t.upper().split())


def _literal(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    if isinstance(value, (list, dict)):
        value = json.dumps(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        value = value.isoformat(sep=" ") if isinstance(value, _dt.datetime) else value.isoformat()
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _python_default(col):
    """(literal_sql | None, reason). Callables are evaluated only for container defaults."""
    d = col.default
    if d is None:
        return None, ""
    if getattr(d, "is_scalar", False):
        return _literal(d.arg), "model default"
    if getattr(d, "is_callable", False):
        try:
            produced = d.arg(None)
        except Exception:
            return None, ""
        # list/dict factories are stable; timestamps, uuids etc. are per-row and
        # must not be frozen into a constant for existing rows.
        if isinstance(produced, (list, dict)) and not produced:
            return _literal(produced), "model default factory"
    return None, ""


def _server_default(col):
    sd = col.server_default
    if sd is None or not hasattr(sd, "arg"):
        return None
    arg = sd.arg
    text_value = arg if isinstance(arg, str) else str(getattr(arg, "text", arg))
    if text_value.strip().upper() in _NON_CONSTANT or text_value.strip().startswith("("):
        return None
    return _literal(arg) if isinstance(arg, str) else text_value


def _fallback_default(col) -> str:
    t = col.type
    if isinstance(t, sat.Boolean):
        return "0"
    if isinstance(t, sat.Integer):
        return "0"
    if isinstance(t, (sat.Float, sat.Numeric)):
        return "0.0"
    if isinstance(t, sat.DateTime):
        return f"'{_EPOCH}'"
    if isinstance(t, sat.Date):
        return "'1970-01-01'"
    if isinstance(t, sat.Time):
        return "'00:00:00'"
    if isinstance(t, sat.JSON):
        return "'null'"
    if isinstance(t, (sat.LargeBinary,)):
        return "X''"
    return "''"


def column_add_sql(table: str, col) -> tuple[list[str], str]:
    """SQL to add ``col`` to an existing table, and a human description."""
    type_sql = _type_sql(col)
    default = _server_default(col)
    why = "server default" if default is not None else ""
    if default is None:
        default, why = _python_default(col)
    notnull = not col.nullable and not col.primary_key
    if notnull and default is None:
        default, why = _fallback_default(col), "type fallback for NOT NULL"

    parts = [q(col.name), type_sql]
    if col.unique:
        # ADD COLUMN cannot carry UNIQUE; a unique index enforces it instead,
        # and the column stays nullable so existing rows do not collide.
        notnull, default = False, None
    if notnull:
        parts.append("NOT NULL")
    if default is not None:
        parts.append(f"DEFAULT {default}")
    fks = list(col.foreign_keys)
    if fks and default is None:
        fk = fks[0]
        ref = f"REFERENCES {q(fk.column.table.name)} ({q(fk.column.name)})"
        if fk.ondelete:
            ref += f" ON DELETE {fk.ondelete}"
        parts.append(ref)
    stmts = [f"ALTER TABLE {q(table)} ADD COLUMN {' '.join(parts)}"]
    if col.unique:
        stmts.append(f"CREATE UNIQUE INDEX IF NOT EXISTS {q(f'uq_{table}_{col.name}')} ON {q(table)} ({q(col.name)})")
    desc = " ".join(parts[1:]) + (f"  [{why}]" if why else "")
    return stmts, desc


def plan(conn: sqlite3.Connection, metadata: MetaData) -> ReconcilePlan:
    p = ReconcilePlan()
    for table in metadata.sorted_tables:
        name = table.name
        if not has_table(conn, name):
            sql = [str(CreateTable(table, if_not_exists=True).compile(dialect=_DIALECT)).strip()]
            sql += [str(CreateIndex(ix, if_not_exists=True).compile(dialect=_DIALECT)).strip()
                    for ix in sorted(table.indexes, key=lambda i: i.name or "")]
            p.ops.append(Op("create_table", name, name, sql, f"{len(table.columns)} columns"))
            continue

        present = columns(conn, name)
        will_have = set(present)
        for col in table.columns:
            if col.name in present:
                db = present[col.name]
                if _norm_type(db["type"]) != _norm_type(_type_sql(col)):
                    p.type_mismatches.append(
                        f"{name}.{col.name}: database {db['type'] or '(none)'} vs model {_type_sql(col)}"
                    )
                model_notnull = not col.nullable and not col.primary_key
                if model_notnull and not db["notnull"] and not db["pk"]:
                    p.nullability.append(f"{name}.{col.name}: nullable in database, NOT NULL in model")
                elif db["notnull"] and not model_notnull and not db["pk"]:
                    p.nullability.append(f"{name}.{col.name}: NOT NULL in database, nullable in model")
                continue
            if col.primary_key:
                p.unsupported.append(f"{name}.{col.name}: primary-key column cannot be added by ALTER; "
                                     f"write a versioned migration using rebuild_table()")
                continue
            sql, desc = column_add_sql(name, col)
            p.ops.append(Op("add_column", name, col.name, sql, desc))
            will_have.add(col.name)

        model_cols = {c.name for c in table.columns}
        p.extra_columns += [f"{name}.{c}" for c in present if c not in model_cols]

        existing_ix = index_names(conn, name)
        for ix in sorted(table.indexes, key=lambda i: i.name or ""):
            if ix.name in existing_ix:
                continue
            if any(c.name not in will_have for c in ix.columns):
                continue
            sql = str(CreateIndex(ix, if_not_exists=True).compile(dialect=_DIALECT)).strip()
            p.ops.append(Op("create_index", name, ix.name, [sql],
                            ", ".join(c.name for c in ix.columns)))
    return p


def apply(conn: sqlite3.Connection, p: ReconcilePlan) -> dict:
    """Apply the plan in one transaction; each op in its own savepoint.

    A failing op is logged and rolled back on its own so the rest still land.
    """
    applied, errors = [], []
    if p.ops:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for op in p.ops:
                conn.execute("SAVEPOINT reconcile_op")
                try:
                    for stmt in op.sql:
                        conn.execute(stmt)
                    conn.execute("RELEASE SAVEPOINT reconcile_op")
                    label = f"{op.table}.{op.name}" if op.kind != "create_table" else op.table
                    applied.append(f"{op.kind} {label}")
                    logger.info(f"reconcile: {op.kind} {label} {op.detail}".rstrip())
                except sqlite3.DatabaseError as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT reconcile_op")
                    conn.execute("RELEASE SAVEPOINT reconcile_op")
                    errors.append(f"{op.kind} {op.table}.{op.name}: {exc}")
                    logger.error(f"reconcile: FAILED {op.kind} {op.table}.{op.name}: {exc}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    for item in p.extra_columns:
        logger.info(f"reconcile: column {item} exists in the database but not in the models (left untouched)")
    for item in p.type_mismatches + p.unsupported:
        logger.warning(f"reconcile: {item} (needs a versioned migration with rebuild_table)")
    return {"applied": applied, "errors": errors, **p.as_dict(), "missing": [
        m for m in p.missing if m not in applied]}
