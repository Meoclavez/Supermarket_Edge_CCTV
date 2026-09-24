"""Schema migration system (app/migrations).

Every test works on its own throwaway SQLite file under pytest's tmp_path.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import Boolean, Column, Float, Integer, MetaData, String, Table

from app.migrations import (
    MigrationError,
    SchemaTooNewError,
    discover,
    head_version,
    rebuild_table,
    run_migrations,
    status,
)
from app.migrations.runner import Migration, migration_lock
from app.models.db_models import Base

LEGACY_SQL = Path(__file__).parent / "fixtures" / "legacy_schema_v0.sql"


def _conn(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), isolation_level=None)
    return c


def _cols(conn, table) -> dict:
    return {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _schema(conn) -> list:
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def _make_legacy(path: Path, with_data: bool = True) -> None:
    """An un-versioned database exactly as devices had it before migrations existed."""
    c = _conn(path)
    c.executescript(LEGACY_SQL.read_text())
    if with_data:
        c.executescript(
            """
            INSERT INTO cameras (id, name, location, rtsp_url, status, fps, resolution, is_ai_enabled,
                ai_models, dvr_enabled, dvr_retention_days, dvr_quota_gb, created_at, channel_number,
                department, floor_x, floor_y, floor_z, azimuth_deg, fov_deg)
            VALUES ('cam1', 'Aisle 3', 'Aisle 3', 'rtsp://10.0.0.5/1', 'ONLINE', 25, '1920x1080', 1,
                '[]', 1, 7, 100.0, '2026-09-01 10:00:00', 1, 'GROCERY', 4.5, 7.25, 3.2, 90.0, 85.0);
            INSERT INTO security_events (id, camera_id, camera_name, location, event_type, severity,
                confidence, timestamp, kinematics, metadata_json, acknowledged)
            VALUES ('ev1', 'cam1', 'Aisle 3', 'Aisle 3', 'THEFT_ALERT', 'HIGH', 0.7,
                '2026-09-02 11:00:00', '{"v": 1}', '{"rule": "concealment"}', 0);
            INSERT INTO customer_tracks (id, track_id, camera_id, start_time, trajectory_points, created_at)
            VALUES ('ct1', 't-42', 'cam1', '2026-09-02 11:00:00', '[[1,2],[3,4]]', '2026-09-02 11:00:05');
            INSERT INTO system_setup ("key", value, updated_at) VALUES ('setup_completed', 'true', '2026-09-05');
            """
        )
    c.close()


def _counts(path: Path) -> dict:
    c = _conn(path)
    try:
        return {
            t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for (t,) in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'schema_migrations'"
            )
        }
    finally:
        c.close()


# --------------------------------------------------------------------------- core behaviour

def test_migrations_are_ordered_and_unique():
    migrations = discover()
    versions = [m.version for m in migrations]
    assert versions == sorted(set(versions)) and versions[0] == 1
    assert head_version(migrations) == versions[-1]


def test_fresh_empty_db_goes_to_head(tmp_path):
    db = tmp_path / "fresh.db"
    report = run_migrations(db, backups_dir=tmp_path / "backups")

    assert report["from_version"] == 0
    assert report["to_version"] == head_version()
    assert len(report["applied"]) == len(discover())
    assert report["backup"] is None  # nothing to protect in an empty database

    st = status(db)
    assert st["current"] == st["head"] and st["pending"] == []
    assert st["drift"]["missing"] == []
    assert st["drift"]["type_mismatches"] == []
    c = _conn(db)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {t.name for t in Base.metadata.sorted_tables} <= {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    c.close()


def test_legacy_unversioned_db_upgrades_and_rows_survive(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy(db)
    before = _counts(db)

    report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert report["from_version"] == 0 and report["to_version"] == head_version()
    # Every pre-existing table keeps its rows; later migrations may add new (empty) tables.
    after = _counts(db)
    assert {t: after.get(t) for t in before} == before

    c = _conn(db)
    cam = c.execute("SELECT name, fps, floor_x, floor_y, department FROM cameras WHERE id='cam1'").fetchone()
    assert cam == ("Aisle 3", 25, 4.5, 7.25, "GROCERY")
    ev = c.execute("SELECT event_type, confidence, metadata_json, acknowledged FROM security_events").fetchone()
    assert ev == ("THEFT_ALERT", 0.7, '{"rule": "concealment"}', 0)
    assert "kinematics" not in _cols(c, "security_events")          # m0004 dropped it
    assert {"rule", "evidence"} <= set(_cols(c, "theft_incidents"))  # m0003
    assert {"hand", "image_x", "floor_y"} <= set(_cols(c, "shelf_interactions"))
    assert "ix_theft_incidents_rule" in {r[1] for r in c.execute("PRAGMA index_list(theft_incidents)")}
    # the foreign key survived the security_events rebuild
    fks = c.execute("PRAGMA foreign_key_list(security_events)").fetchall()
    assert fks and fks[0][2] == "cameras" and fks[0][6] == "CASCADE"
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()

    # an upgraded device and a fresh install end up with the same columns
    fresh = tmp_path / "fresh.db"
    run_migrations(fresh, backups_dir=tmp_path / "b2")
    cf, cl = _conn(fresh), _conn(db)
    for table in Base.metadata.tables:
        assert list(_cols(cf, table)) == list(_cols(cl, table)), table
    cf.close(), cl.close()


def test_running_twice_is_a_noop(tmp_path):
    db = tmp_path / "twice.db"
    _make_legacy(db)
    run_migrations(db, backups_dir=tmp_path / "backups")
    c = _conn(db)
    schema1, rows1 = _schema(c), c.execute("SELECT * FROM schema_migrations").fetchall()
    c.close()

    report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert report["applied"] == []
    assert report["reconcile"]["applied"] == []
    assert report["backup"] is None
    c = _conn(db)
    assert _schema(c) == schema1
    assert c.execute("SELECT * FROM schema_migrations").fetchall() == rows1
    c.close()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_missing_model_column_and_table_are_auto_added(tmp_path):
    db = tmp_path / "reconcile.db"
    run_migrations(db, backups_dir=tmp_path / "backups")
    c = _conn(db)
    c.execute("INSERT INTO store_layouts (id, store_id, name, width_m, height_m, is_active, created_at, updated_at) "
              "VALUES ('L1', 's', 'Floor', 50, 30, 1, '2026-01-01', '2026-01-01')")
    c.close()

    # A model that gained columns and a table since this database was created.
    md = MetaData()
    for t in Base.metadata.sorted_tables:
        t.to_metadata(md)
    layouts = md.tables["store_layouts"]
    layouts.append_column(Column("floor_count", Integer, nullable=False, default=1))
    layouts.append_column(Column("is_public", Boolean, nullable=False, default=True))
    layouts.append_column(Column("label", String(32), nullable=False))           # no default at all
    layouts.append_column(Column("scale", Float, nullable=True, index=True))
    Table("new_feature", md, Column("id", String(64), primary_key=True), Column("value", Float))

    report = run_migrations(db, metadata=md, backups_dir=tmp_path / "backups")
    added = report["reconcile"]["applied"]
    assert "add_column store_layouts.floor_count" in added
    assert "add_column store_layouts.label" in added
    assert "create_table new_feature" in added
    assert "create_index store_layouts.ix_store_layouts_scale" in added
    assert report["reconcile"]["errors"] == []
    assert report["backup"]  # the database had data, so it was backed up first

    c = _conn(db)
    cols = _cols(c, "store_layouts")
    assert cols["floor_count"][3] == 1 and cols["floor_count"][4] == "1"   # NOT NULL DEFAULT 1
    assert cols["label"][3] == 1 and cols["label"][4] == "''"              # type fallback
    row = c.execute("SELECT floor_count, is_public, label, scale FROM store_layouts WHERE id='L1'").fetchone()
    assert row == (1, 1, "", None)
    c.close()

    # and a second reconcile with the same models has nothing to do
    assert run_migrations(db, metadata=md, backups_dir=tmp_path / "backups")["reconcile"]["applied"] == []


def test_reconcile_never_drops_unknown_columns(tmp_path):
    db = tmp_path / "extra.db"
    run_migrations(db, backups_dir=tmp_path / "backups")
    c = _conn(db)
    c.execute("ALTER TABLE cameras ADD COLUMN vendor_blob TEXT")
    c.close()
    report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert "cameras.vendor_blob" in report["reconcile"]["extra_columns"]
    c = _conn(db)
    assert "vendor_blob" in _cols(c, "cameras")
    c.close()


# --------------------------------------------------------------------------- rebuild helper

def test_rebuild_preserves_rows_indexes_triggers_and_foreign_keys(tmp_path):
    db = tmp_path / "rebuild.db"
    c = _conn(db)
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE item (id VARCHAR(8) PRIMARY KEY, qty TEXT, note TEXT, legacy TEXT,
                           parent_id INTEGER REFERENCES parent(id));
        CREATE INDEX ix_item_qty ON item (qty);
        CREATE INDEX ix_item_legacy ON item (legacy);
        CREATE TABLE audit (msg TEXT);
        CREATE TRIGGER trg_item_ins AFTER INSERT ON item BEGIN INSERT INTO audit VALUES (NEW.id); END;
        CREATE VIEW v_item AS SELECT id, qty FROM item;
        INSERT INTO parent VALUES (1, 'p');
        INSERT INTO item VALUES ('a', '3', 'x', 'old', 1), ('b', '5', NULL, 'old', 1);
        """
    )
    rowids = dict(c.execute("SELECT id, rowid FROM item").fetchall())
    c.execute("DELETE FROM audit")

    # type change TEXT -> INTEGER, NOT NULL tightening, and a dropped column
    result = rebuild_table(
        c, "item",
        """CREATE TABLE item (id VARCHAR(8) PRIMARY KEY, qty INTEGER NOT NULL, note TEXT NOT NULL,
                              parent_id INTEGER REFERENCES parent(id))""",
        column_exprs={"qty": "CAST(qty AS INTEGER)", "note": "COALESCE(note, '')"},
    )
    assert result["rows"] == 2
    assert result["dropped_columns"] == ["legacy"]
    assert result["indexes_skipped"] == ["ix_item_legacy"]
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # restored after the rebuild

    assert c.execute("SELECT id, qty, note, typeof(qty) FROM item ORDER BY id").fetchall() == [
        ("a", 3, "x", "integer"), ("b", 5, "", "integer")]
    assert dict(c.execute("SELECT id, rowid FROM item").fetchall()) == rowids
    assert "ix_item_qty" in {r[1] for r in c.execute("PRAGMA index_list(item)")}
    assert c.execute("SELECT COUNT(*) FROM v_item").fetchone()[0] == 2
    c.execute("INSERT INTO item (id, qty, note, parent_id) VALUES ('c', 1, 'n', 1)")
    assert c.execute("SELECT msg FROM audit").fetchall() == [("c",)]  # trigger recreated
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO item (id, qty, note, parent_id) VALUES ('d', 1, 'n', 99)")
    c.close()


def test_rebuild_rolls_back_when_it_would_break_foreign_keys(tmp_path):
    db = tmp_path / "fk.db"
    c = _conn(db)
    c.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));
        INSERT INTO parent VALUES (1); INSERT INTO child VALUES (10, 1);
        """
    )
    before = _schema(c)
    with pytest.raises(Exception):
        rebuild_table(c, "child", "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))",
                      column_exprs={"parent_id": "parent_id + 100"})
    assert _schema(c) == before
    assert c.execute("SELECT * FROM child").fetchall() == [(10, 1)]
    c.close()


# --------------------------------------------------------------------------- safety

def test_newer_database_refuses_to_start(tmp_path):
    db = tmp_path / "newer.db"
    run_migrations(db, backups_dir=tmp_path / "backups")
    c = _conn(db)
    c.execute("INSERT INTO schema_migrations VALUES (?, 'from_the_future', '2027-01-01', 'x', '9.9.9')",
              (head_version() + 1,))
    c.close()
    with pytest.raises(SchemaTooNewError, match="newer release"):
        run_migrations(db, backups_dir=tmp_path / "backups")

    from app.migrations.__main__ import main
    assert main(["check", "--db", str(db)]) == 2


def test_backup_is_taken_before_migrating_a_db_with_data(tmp_path):
    db = tmp_path / "data.db"
    _make_legacy(db, with_data=True)
    backups = tmp_path / "backups"
    report = run_migrations(db, backups_dir=backups)

    backup = Path(report["backup"])
    assert backup.exists() and backup.parent == backups
    assert f"pre-v{head_version():04d}" in backup.name
    b = _conn(backup)
    # the backup is the pre-migration database: old column still there, data intact
    assert "kinematics" in _cols(b, "security_events")
    assert b.execute("SELECT COUNT(*) FROM security_events").fetchone()[0] == 1
    assert "schema_migrations" not in {r[0] for r in b.execute("SELECT name FROM sqlite_master")}
    b.close()

    empty = tmp_path / "empty.db"
    _make_legacy(empty, with_data=False)
    assert run_migrations(empty, backups_dir=tmp_path / "b-empty")["backup"] is None


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_path):
    db = tmp_path / "fail.db"
    good = discover()

    class Boom:
        NAME = "boom"

        @staticmethod
        def upgrade(conn):
            conn.execute("CREATE TABLE half_done (id INTEGER)")
            raise RuntimeError("simulated failure")

    bad = good + [Migration(head_version() + 1, "boom", Boom, "0" * 64)]
    with pytest.raises(MigrationError, match="rolled back"):
        run_migrations(db, migrations=bad, backups_dir=tmp_path / "backups")
    c = _conn(db)
    assert c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == head_version()
    assert c.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None
    c.close()


def test_stale_setup_flag_is_reset_when_no_admin_exists(tmp_path):
    db = tmp_path / "setup.db"
    _make_legacy(db)  # setup_completed='true', no admin_users rows
    run_migrations(db, backups_dir=tmp_path / "backups")
    c = _conn(db)
    assert c.execute("SELECT value FROM system_setup WHERE key='setup_completed'").fetchone() == ("false",)
    c.close()

    db2 = tmp_path / "setup_admin.db"
    _make_legacy(db2)
    c = _conn(db2)
    c.execute("INSERT INTO admin_users VALUES ('u1', 'admin', 'h', 'Admin', 'ADMIN', 1, '2026-09-01', NULL)")
    c.close()
    run_migrations(db2, backups_dir=tmp_path / "backups2")
    c = _conn(db2)
    assert c.execute("SELECT value FROM system_setup WHERE key='setup_completed'").fetchone() == ("true",)
    c.close()


def test_migration_lock_is_exclusive(tmp_path):
    db = tmp_path / "lock.db"
    held, release = threading.Event(), threading.Event()

    def holder():
        with migration_lock(db):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(MigrationError, match="timed out"):
            run_migrations(db, lock_timeout=0.5, backups_dir=tmp_path / "backups")
    finally:
        release.set()
        t.join()
    assert run_migrations(db, backups_dir=tmp_path / "backups")["to_version"] == head_version()
