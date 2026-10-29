"""Versioned schema migrations for the edge SQLite database.

Every ``app/migrations/mNNNN_<name>.py`` module is one migration with an
idempotent ``upgrade(conn)`` taking a ``sqlite3.Connection``. Applied versions
are recorded in ``schema_migrations`` (version, name, applied_at, checksum,
app_version). ``run_migrations()``:

1. takes an exclusive file lock (``<db>.migrate.lock``) so a reloader and a CLI
   cannot migrate the same file at once;
2. refuses to continue if the database was migrated by newer code;
3. backs up a database that holds data before applying anything;
4. applies each pending migration in its own transaction (foreign keys off,
   ``foreign_key_check`` before commit, per the SQLite ALTER procedure);
5. runs the additive reconcile (:mod:`app.migrations.reconcile`).

It uses only the standard library ``sqlite3`` module plus SQLAlchemy metadata
(already a dependency) for the reconcile step.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import logging
import os
import pkgutil
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

from . import reconcile as _reconcile
from .sqlite_utils import foreign_key_violations, table_names, user_tables_have_rows

logger = logging.getLogger("edge.migrations")

_MODULE_RE = re.compile(r"^m(\d{4})_([a-z0-9_]+)$")

SCHEMA_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        VARCHAR(128) NOT NULL,
    applied_at  VARCHAR(32)  NOT NULL,
    checksum    VARCHAR(64)  NOT NULL,
    app_version VARCHAR(32)
)
"""


class MigrationError(RuntimeError):
    """A migration failed; the database was rolled back to the last good version."""


class SchemaTooNewError(MigrationError):
    """The database was migrated by a newer build than this one (a downgrade)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    module: ModuleType
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"

    def upgrade(self, conn: sqlite3.Connection) -> None:
        self.module.upgrade(conn)


def discover() -> list[Migration]:
    """All migration modules in this package, ordered by version."""
    pkg_dir = Path(__file__).resolve().parent
    found: dict[int, Migration] = {}
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        m = _MODULE_RE.match(info.name)
        if not m:
            continue
        module = importlib.import_module(f"{__package__}.{info.name}")
        if not callable(getattr(module, "upgrade", None)):
            raise MigrationError(f"{info.name} has no upgrade(conn) function")
        version = int(m.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version}: {found[version].name} and {info.name}")
        checksum = hashlib.sha256((pkg_dir / f"{info.name}.py").read_bytes()).hexdigest()
        found[version] = Migration(version, getattr(module, "NAME", m.group(2)), module, checksum)
    return [found[v] for v in sorted(found)]


def head_version(migrations: Optional[list[Migration]] = None) -> int:
    migrations = discover() if migrations is None else migrations
    return migrations[-1].version if migrations else 0


# --------------------------------------------------------------------------- utils

def _default_db_path() -> Path:
    from app.config import settings
    return Path(settings.DATABASE_PATH).resolve()


def _app_version() -> str:
    try:
        from app.config import settings
        return str(settings.APP_VERSION)
    except Exception:
        return "unknown"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def applied_versions(conn: sqlite3.Connection) -> dict[int, dict]:
    if "schema_migrations" not in table_names(conn):
        return {}
    rows = conn.execute(
        "SELECT version, name, applied_at, checksum, app_version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {r[0]: {"version": r[0], "name": r[1], "applied_at": r[2], "checksum": r[3], "app_version": r[4]}
            for r in rows}


def check_not_too_new(applied: dict[int, dict], migrations: list[Migration], db_path) -> None:
    head = head_version(migrations)
    newest = max(applied) if applied else 0
    if newest > head:
        unknown = sorted(v for v in applied if v > head)
        names = ", ".join(f"{v:04d}_{applied[v]['name']} (app {applied[v]['app_version']})" for v in unknown)
        raise SchemaTooNewError(
            f"Database {db_path} is at schema version {newest}, but this build only knows up to "
            f"version {head}. It was upgraded by a newer release ({names}). Refusing to start to "
            f"avoid corrupting it: run the newer release, or restore a backup taken before the "
            f"upgrade (storage/backups/*pre-v*.db)."
        )


@contextlib.contextmanager
def migration_lock(db_path: Path, timeout: float = 120.0):
    """Exclusive advisory lock on ``<db>.migrate.lock`` (flock on POSIX, msvcrt on Windows)."""
    lock_path = Path(str(db_path) + ".migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":  # pragma: no cover - deployment is Linux
            import msvcrt
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise MigrationError(f"timed out waiting for migration lock {lock_path}")
                    time.sleep(0.2)
        else:
            import fcntl
            waited = False
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waited:
                        logger.info(f"another process is migrating {db_path}; waiting for {lock_path}")
                        waited = True
                    if time.monotonic() > deadline:
                        raise MigrationError(f"timed out waiting for migration lock {lock_path}")
                    time.sleep(0.2)
        yield lock_path
    finally:
        try:
            if os.name != "nt":
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _backup(db_path: Path, backups_dir: Optional[Path], tag: str) -> str:
    """Snapshot via the project's BackupService (SQLite online backup API)."""
    from app.config import settings
    from app.services.backup_service import BackupService

    if backups_dir is None:
        same_db = Path(settings.DATABASE_PATH).resolve() == db_path
        backups_dir = Path(settings.BACKUPS_DIR) if same_db else db_path.parent / "backups"
    res = BackupService(backups_dir=Path(backups_dir), db_path=db_path).create_backup(tag)
    return res["filepath"]


# --------------------------------------------------------------------------- backup safety
#
# A pre-migration backup is a full copy of the database (2.8 GB at Pearcedale).
# Without a space check a nearly full disk turned every restart into another
# failed multi-GB copy and a refusal to start: the store went dark until someone
# freed space over ssh. Now:
#   * the copy is attempted only with free space >= BACKUP_SPACE_FACTOR x the
#     database (plus WAL) size;
#   * when space is short, or the copy failed recently for the same target
#     version, pending migrations that are all ADDITIVE (module attribute
#     ``ADDITIVE = True``: only CREATE ... IF NOT EXISTS / ADD COLUMN) run without
#     the copy, with an ERROR in the log (reaches the iKorex issue log) and a
#     marker file that storage health reports;
#   * anything else still refuses to start (the original data stays untouched),
#     but without copying again on every systemd restart.

BACKUP_SPACE_FACTOR = 1.2
_RETRY_AFTER_S = 30 * 60


def _marker_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".backup-status.json")


def _db_bytes(db_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(str(db_path) + suffix)
        except OSError:
            pass
    return total


def _resolve_backups_dir(db_path: Path, backups_dir: Optional[Path]) -> Path:
    if backups_dir is not None:
        return Path(backups_dir)
    try:
        from app.config import settings
        if Path(settings.DATABASE_PATH).resolve() == Path(db_path).resolve():
            return Path(settings.BACKUPS_DIR)
    except Exception:
        pass
    return Path(db_path).parent / "backups"


def _free_bytes(path: Path) -> int:
    import shutil
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(str(p)).free


def backup_space_ok(db_path: Path, backups_dir: Optional[Path] = None) -> tuple[bool, str]:
    """(ok, reason): is there room for a full copy of ``db_path`` in the backups folder?"""
    need = int(_db_bytes(Path(db_path)) * BACKUP_SPACE_FACTOR)
    try:
        free = _free_bytes(_resolve_backups_dir(Path(db_path), backups_dir))
    except OSError as exc:
        return False, f"cannot read free disk space: {exc}"
    if free >= need:
        return True, ""
    return False, (f"only {free / 1e9:.1f} GB free, a backup copy needs about {need / 1e9:.1f} GB "
                   f"({BACKUP_SPACE_FACTOR:g}x the database)")


def _read_marker(db_path: Path) -> dict:
    try:
        import json
        return json.loads(_marker_path(db_path).read_text())
    except Exception:
        return {}


def _write_marker(db_path: Path, data: Optional[dict]) -> None:
    import json
    path = _marker_path(db_path)
    try:
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(data, sort_keys=True))
    except OSError as exc:
        logger.warning(f"could not update {path.name}: {exc}")


def backup_warning(db_path: Optional[Path] = None) -> Optional[dict]:
    """The last skipped/failed pre-migration backup (for storage health), or None."""
    data = _read_marker(Path(db_path or _default_db_path()).resolve())
    return data or None


def _pre_migration_backup(db_path: Path, backups_dir: Optional[Path], pending: list[Migration],
                          backup_fn, report: dict) -> Optional[str]:
    target = pending[-1].version
    tag = f"pre-v{target:04d}"
    additive = all(bool(getattr(m.module, "ADDITIVE", False)) for m in pending)
    marker = _read_marker(db_path)
    recent_failure = (marker.get("target") == target and marker.get("state") == "failed"
                      and time.time() - float(marker.get("at") or 0) < _RETRY_AFTER_S)
    ok, why = backup_space_ok(db_path, backups_dir)
    if ok and recent_failure:
        ok, why = False, f"the backup copy failed {int(time.time() - float(marker['at']))} s ago: {marker.get('error')}"

    if ok:
        try:
            path = backup_fn(db_path, backups_dir, tag)
        except Exception as exc:
            _write_marker(db_path, {"target": target, "state": "failed", "at": time.time(),
                                    "error": str(exc)[:300], "additive": additive})
            if not additive:
                raise MigrationError(f"pre-migration backup failed, not migrating: {exc}") from exc
            why = f"the backup copy failed: {exc}"
        else:
            _write_marker(db_path, None)
            logger.info(f"backup before migrating to v{target}: {path}")
            return path

    if not additive:
        _write_marker(db_path, {"target": target, "state": marker.get("state") or "no_space", "at":
                                marker.get("at") or time.time(), "error": why, "additive": False})
        raise MigrationError(f"pre-migration backup not possible ({why}); not migrating. "
                             f"Free disk space (old backups, clips) and restart.")
    logger.error(f"Migrating to v{target} WITHOUT a backup copy ({why}). "
                 f"The pending changes only add tables/columns/indexes. Free disk space soon.")
    _write_marker(db_path, {"target": target, "state": "skipped", "at": time.time(), "error": why,
                            "additive": True})
    report["backup_skipped"] = why
    return None


# --------------------------------------------------------------------------- main API

def status(db_path: Optional[Path] = None, metadata=None) -> dict:
    """Read-only report: current/head version, applied, pending, checksum drift, schema drift."""
    db_path = Path(db_path or _default_db_path()).resolve()
    migrations = discover()
    out = {"db_path": str(db_path), "exists": db_path.exists(), "head": head_version(migrations)}
    if not db_path.exists():
        out.update(current=0, applied=[], pending=[m.label for m in migrations], modified=[], drift=None)
        return out
    conn = connect_readonly(db_path)
    try:
        applied = applied_versions(conn)
        known = {m.version: m for m in migrations}
        out["current"] = max(applied) if applied else 0
        out["applied"] = list(applied.values())
        out["pending"] = [m.label for m in migrations if m.version not in applied]
        out["unknown"] = [f"{v:04d}_{applied[v]['name']}" for v in applied if v not in known]
        out["modified"] = [known[v].label for v in applied
                           if v in known and applied[v]["checksum"] != known[v].checksum]
        if metadata is None:
            from app.models.db_models import Base
            metadata = Base.metadata
        out["drift"] = _reconcile.plan(conn, metadata).as_dict()
        out["tables"] = len(table_names(conn))
    finally:
        conn.close()
    return out


def run_migrations(
    db_path: Optional[Path] = None,
    *,
    metadata=None,
    backups_dir: Optional[Path] = None,
    run_reconcile: bool = True,
    lock_timeout: float = 120.0,
    migrations: Optional[list[Migration]] = None,
    backup_fn: Optional[Callable[[Path, Optional[Path], str], str]] = None,
) -> dict:
    """Bring the database to head, then reconcile it with the models. Returns a report."""
    db_path = Path(db_path or _default_db_path()).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrations = discover() if migrations is None else migrations
    head = head_version(migrations)
    backup_fn = backup_fn or _backup
    report = {"db_path": str(db_path), "head": head, "applied": [], "backup": None, "reconcile": None}

    with migration_lock(db_path, timeout=lock_timeout):
        conn = _connect(db_path)
        try:
            applied = applied_versions(conn)
            check_not_too_new(applied, migrations, db_path)
            report["from_version"] = max(applied) if applied else 0

            known = {m.version for m in migrations}
            for v in sorted(set(applied) - known):
                logger.warning(f"schema_migrations has version {v} ({applied[v]['name']}) that this build does not ship")
            for m in migrations:
                if m.version in applied and applied[m.version]["checksum"] != m.checksum:
                    logger.warning(f"migration {m.label} changed after it was applied (checksum differs); "
                                   f"it will not be re-run")

            pending = [m for m in migrations if m.version not in applied]
            has_data = user_tables_have_rows(conn)
            if pending and has_data:
                report["backup"] = _pre_migration_backup(
                    db_path, backups_dir, pending, backup_fn, report)
            # Created after the backup so the snapshot is the untouched original.
            conn.execute(SCHEMA_TABLE_DDL)

            if pending:
                was_unversioned = not applied and bool(set(table_names(conn)) - {"schema_migrations"})
                logger.info(
                    f"database {db_path.name}: schema v{report['from_version']} -> v{head}, "
                    f"{len(pending)} pending"
                    + (" (adopting an existing un-versioned database)" if was_unversioned else "")
                )
            app_version = _app_version()
            for m in pending:
                _apply_one(conn, m, app_version)
                report["applied"].append(m.label)

            report["to_version"] = max(applied_versions(conn)) if applied_versions(conn) else 0
            if not pending:
                logger.info(f"database {db_path.name}: schema v{report['to_version']} is current (head v{head}); "
                            f"no migrations to apply")

            if run_reconcile:
                if metadata is None:
                    from app.models.db_models import Base
                    metadata = Base.metadata
                plan = _reconcile.plan(conn, metadata)
                if plan.ops and has_data and report["backup"] is None and not report.get("backup_skipped"):
                    # Reconcile only adds tables/columns/indexes: never refuse to start over it.
                    ok, why = backup_space_ok(db_path, backups_dir)
                    if ok:
                        try:
                            report["backup"] = backup_fn(db_path, backups_dir, "pre-reconcile")
                            logger.info(f"backup before reconcile: {report['backup']}")
                        except Exception as exc:
                            logger.error(f"backup before reconcile failed, continuing (additive change): {exc}")
                    else:
                        logger.error(f"backup before reconcile skipped: {why}")
                report["reconcile"] = _reconcile.apply(conn, plan)
                if not plan.ops:
                    logger.info("reconcile: schema matches the models; nothing to add")
        finally:
            conn.close()
    return report


def _apply_one(conn: sqlite3.Connection, m: Migration, app_version: str) -> None:
    started = time.monotonic()
    fk_before = foreign_key_violations(conn)
    if fk_before:
        logger.warning(f"{len(fk_before)} pre-existing foreign-key orphan row(s) in the database "
                       f"(foreign keys were not enforced by older builds); left untouched")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m.upgrade(conn)
            introduced = foreign_key_violations(conn) - fk_before
            if introduced:
                raise MigrationError(f"migration {m.label} introduced foreign-key violations: "
                                     f"{sorted(introduced)[:5]}")
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum, app_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (m.version, m.name, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 m.checksum, app_version),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"migration {m.label} failed and was rolled back: {exc}") from exc
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    logger.info(f"applied migration {m.label} in {(time.monotonic() - started) * 1000:.0f} ms")
