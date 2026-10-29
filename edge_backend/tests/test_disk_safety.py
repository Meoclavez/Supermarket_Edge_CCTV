"""Disk safety (audit 2026-10-29, L2/L3): upgrades never boot-loop on a full disk,
backups stay inside a byte budget, evidence is not wiped because of backups."""

from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.migrations import runner
from app.migrations.runner import Migration, MigrationError, run_migrations
from app.services import backup_service as bs
from app.services.backup_service import BackupService
from app.services.evidence_storage import evidence_floor_bytes, MB


def _db(path: Path) -> Path:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('x')")
    return path


def _migration(version: int, additive: bool, sql: str) -> Migration:
    mod = types.ModuleType(f"m{version:04d}_fake")
    mod.ADDITIVE = additive
    mod.upgrade = lambda conn: conn.execute(sql)
    return Migration(version, f"fake{version}", mod, f"sum{version}")


def _run(db, tmp_path, migrations, calls):
    def backup_fn(db_path, backups_dir, tag):
        calls.append(tag)
        raise OSError("No space left on device")
    return run_migrations(db, backups_dir=tmp_path / "b", run_reconcile=False,
                          migrations=migrations, backup_fn=backup_fn)


def test_additive_migration_runs_without_backup_when_disk_is_full(tmp_path, monkeypatch):
    db = _db(tmp_path / "live.db")
    monkeypatch.setattr(runner, "_free_bytes", lambda p: 10)
    calls = []
    mig = [_migration(1, True, "CREATE INDEX IF NOT EXISTS ix_t_v ON t (v)")]
    report = _run(db, tmp_path, mig, calls)
    assert calls == []                                  # no copy attempted at all
    assert report["applied"] == ["0001_fake1"] and report["backup"] is None
    assert "GB free" in report["backup_skipped"]
    warn = runner.backup_warning(db)
    assert warn["state"] == "skipped" and warn["target"] == 1


def test_non_additive_migration_refuses_without_copying_again(tmp_path, monkeypatch):
    db = _db(tmp_path / "live.db")
    mig = [_migration(1, False, "ALTER TABLE t ADD COLUMN w TEXT")]
    calls = []
    # Enough space, but the copy fails (disk filled during the copy).
    monkeypatch.setattr(runner, "_free_bytes", lambda p: 10 ** 12)
    with pytest.raises(MigrationError):
        _run(db, tmp_path, mig, calls)
    assert calls == ["pre-v0001"]
    # systemd restarts the service: no second multi-GB copy within the retry window.
    with pytest.raises(MigrationError):
        _run(db, tmp_path, mig, calls)
    assert calls == ["pre-v0001"]
    assert runner.backup_warning(db)["state"] == "failed"


def test_additive_migration_after_a_failed_copy_proceeds(tmp_path, monkeypatch):
    db = _db(tmp_path / "live.db")
    monkeypatch.setattr(runner, "_free_bytes", lambda p: 10 ** 12)
    calls = []
    mig = [_migration(1, True, "CREATE INDEX IF NOT EXISTS ix_t_v ON t (v)")]
    report = _run(db, tmp_path, mig, calls)            # copy fails, change is additive
    assert calls == ["pre-v0001"] and report["applied"] == ["0001_fake1"]


def test_successful_backup_clears_the_skipped_marker(tmp_path, monkeypatch):
    db = _db(tmp_path / "live.db")
    monkeypatch.setattr(runner, "_free_bytes", lambda p: 10)
    _run(db, tmp_path, [_migration(1, True, "CREATE INDEX IF NOT EXISTS ix_t_v ON t (v)")], [])
    assert runner.backup_warning(db)
    BackupService(backups_dir=tmp_path / "b2", db_path=db).create_backup("manual")
    assert runner.backup_warning(db) is None


def test_m0024_is_additive_and_idempotent(tmp_path):
    db = tmp_path / "fresh.db"
    first = run_migrations(db, backups_dir=tmp_path / "b")
    assert first["to_version"] >= 24
    import app.migrations.m0024_hot_query_indexes as m24
    assert m24.ADDITIVE is True
    conn = sqlite3.connect(str(db))
    m24.upgrade(conn)
    m24.upgrade(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_security_events_type_ts", "ix_security_events_camera_ts",
            "ix_zone_visits_open", "ix_theft_incidents_status_ts"} <= names


def test_automatic_backup_skips_and_manual_refuses_without_space(tmp_path, monkeypatch):
    svc = BackupService(backups_dir=tmp_path / "b", db_path=_db(tmp_path / "live.db"))
    monkeypatch.setattr(svc, "space_check", lambda: (False, "only 0.0 GB free"))
    res = svc.create_backup("startup")
    assert res["status"] == "skipped" and res["reason"] == "no_space"
    with pytest.raises(OSError):
        svc.create_backup("manual")
    assert svc.list_backups() == []


def _fake(dirpath: Path, ts: datetime, tag: str, size: int) -> Path:
    p = dirpath / f"edge_cctv_{ts.strftime('%Y%m%d_%H%M%S')}_{tag}.db"
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * size)
    return p


def test_backups_stay_inside_the_byte_budget_and_keep_two_per_kind(tmp_path):
    bdir = tmp_path / "b"
    bdir.mkdir()
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    for i in range(4):
        _fake(bdir, now - timedelta(days=30 + i), f"pre-v00{20 + i}", 1000)
    for i in range(3):
        _fake(bdir, now - timedelta(days=10 + i), "manual" if i == 0 else f"manual-{i + 1}", 1000)
    autos = [_fake(bdir, now - timedelta(hours=h), "startup", 1000) for h in range(1, 6)]
    svc = BackupService(backups_dir=bdir, db_path=tmp_path / "none.db", keep_startup=7,
                        keep_daily_days=14, compress=False, max_bytes=5500)
    report = svc.apply_retention(now=now)
    left = sorted(p.name for p in bdir.iterdir())
    # Two newest pre-v kept by kind, the oldest two go regardless of the budget.
    assert sum("pre-v" in n for n in left) <= 2
    assert report["total_bytes"] <= 5500
    assert autos[0].name in left                       # the newest backup always stays
    # Automatic snapshots are pruned before the kept upgrade/manual ones.
    assert any("pre-v0020" in n for n in left)


def test_budget_defaults_to_a_share_of_the_disk(tmp_path):
    svc = BackupService(backups_dir=tmp_path / "b", db_path=_db(tmp_path / "live.db"), max_bytes=0)
    import shutil
    total = shutil.disk_usage(str(tmp_path)).total
    assert svc.budget_bytes() >= int(total * 0.15) - 1


def test_evidence_floor_is_a_quarter_of_the_cap_and_at_least_512_mb():
    assert evidence_floor_bytes(100 * 1024 * MB) == 25 * 1024 * MB
    assert evidence_floor_bytes(1024 * MB) == 512 * MB
    assert evidence_floor_bytes(100 * MB) == 100 * MB


def test_nightly_upkeep_empties_old_paths_and_deletes_ancient_tracks(tmp_path):
    from app.services import db_maintenance as dm
    db = tmp_path / "live.db"
    run_migrations(db, backups_dir=tmp_path / "b")
    now = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
    conn = sqlite3.connect(str(db))
    rows = []
    for i, age in enumerate([1, 10, 40, 60, 500]):
        st = (now - timedelta(days=age)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        rows.append((f"t{i}", f"k{i}", "cam", st, '[{"x": 1, "y": 2}]'))
    conn.executemany("INSERT INTO customer_tracks (id, track_id, camera_id, start_time, trajectory_points, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", [r + (r[3],) for r in rows])
    conn.commit()
    conn.close()
    res = dm.run_once(db, now=now)
    assert res["rows_deleted"] == 1 and res["points_cleared"] == 2 and res["complete"]
    conn = sqlite3.connect(str(db))
    pts = dict(conn.execute("SELECT id, trajectory_points FROM customer_tracks"))
    assert pts == {"t0": '[{"x": 1, "y": 2}]', "t1": '[{"x": 1, "y": 2}]', "t2": "[]", "t3": "[]"}
    # Idempotent.
    again = dm.run_once(db, now=now)
    assert again["rows_deleted"] == 0 and again["points_cleared"] == 0


def test_nightly_upkeep_stops_at_its_time_budget(tmp_path):
    from app.services import db_maintenance as dm
    db = tmp_path / "live.db"
    run_migrations(db, backups_dir=tmp_path / "b")
    now = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
    conn = sqlite3.connect(str(db))
    st = (now - timedelta(days=60)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    conn.executemany("INSERT INTO customer_tracks (id, track_id, camera_id, start_time, trajectory_points, created_at) "
                     "VALUES (?, ?, ?, ?, '[1]', ?)", [(f"t{i}", "k", "cam", st, st) for i in range(10)])
    conn.commit()
    conn.close()
    res = dm.run_once(db, now=now, max_seconds=0)
    assert res["complete"] is False and res["points_cleared"] == 0

