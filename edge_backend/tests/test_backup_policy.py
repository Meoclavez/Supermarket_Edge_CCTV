"""Backup policy: dedup of unchanged startup snapshots, retention, gzip and restore."""

from __future__ import annotations

import gzip
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.backup_service import BackupService, content_hash


def _make_db(path: Path) -> Path:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('one')")
        conn.commit()
    return path


def _add_row(path: Path, value: str) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("INSERT INTO t (v) VALUES (?)", (value,))
        conn.commit()


def _fake_backup(dirpath: Path, ts: datetime, tag: str) -> Path:
    """A real (tiny) SQLite file with a chosen timestamp in its name."""
    p = dirpath / f"edge_cctv_{ts.strftime('%Y%m%d_%H%M%S')}_{tag}.db"
    with sqlite3.connect(str(p)) as conn:
        conn.execute("CREATE TABLE x (ts TEXT)")
        conn.execute("INSERT INTO x VALUES (?)", (ts.isoformat() + tag,))
    os.utime(p, (ts.timestamp(), ts.timestamp()))
    return p


@pytest.fixture
def svc(tmp_path):
    db = _make_db(tmp_path / "live.db")
    return BackupService(backups_dir=tmp_path / "backups", db_path=db,
                         keep_startup=3, keep_daily_days=14, compress=True)


def test_unchanged_startup_backup_is_skipped(svc):
    first = svc.create_backup("startup")
    assert first["status"] == "success"
    second = svc.create_backup("startup")
    assert second["status"] == "skipped"
    assert second["reason"] == "unchanged"
    assert second["duplicate_of"] == first["filename"]
    assert len(svc.list_backups()) == 1

    _add_row(svc.db_path, "two")
    third = svc.create_backup("startup")
    assert third["status"] == "success"
    assert len(svc.list_backups()) == 2
    # No temp files or WAL sidecars left behind.
    leftovers = [p.name for p in svc.backups_dir.iterdir() if p.name.startswith(".") or p.name.endswith(("-wal", "-shm"))]
    assert leftovers == []


def test_pre_migration_and_manual_backups_are_never_deduplicated(svc):
    a = svc.create_backup("pre-v0009")
    b = svc.create_backup("pre-v0009")
    c = svc.create_backup("manual")
    assert {a["status"], b["status"], c["status"]} == {"success"}
    assert a["filename"] != b["filename"]  # same second -> unique suffix, no overwrite
    assert len(svc.list_backups()) == 3


def test_content_hash_ignores_page_layout(tmp_path):
    a = _make_db(tmp_path / "a.db")
    b = tmp_path / "b.db"
    with sqlite3.connect(str(a)) as src, sqlite3.connect(str(b)) as dst:
        src.backup(dst)
    with sqlite3.connect(str(b)) as conn:
        conn.execute("VACUUM")
    assert content_hash(a) == content_hash(b)
    _add_row(b, "x")
    assert content_hash(a) != content_hash(b)


def test_retention_keeps_last_n_daily_and_pre_migration(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
    made = []
    # Three startups per day for 30 days.
    for day in range(30):
        for hour in (1, 5, 9):
            made.append(_fake_backup(bdir, now - timedelta(days=day, hours=hour), "startup"))
    old_pre = _fake_backup(bdir, now - timedelta(days=90), "pre-v0003")
    manual = _fake_backup(bdir, now - timedelta(days=60), "manual")
    foreign = bdir / "salvage_20260101_000000.db"
    foreign.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

    svc = BackupService(backups_dir=bdir, db_path=tmp_path / "none.db",
                        keep_startup=3, keep_daily_days=14, compress=False)
    report = svc.apply_retention(now=now)

    remaining = {p.name for p in bdir.iterdir()}
    assert old_pre.name in remaining and manual.name in remaining and foreign.name in remaining
    startups = sorted(n for n in remaining if "_startup" in n)
    # Backups sit at 11:00/07:00/03:00 each day; the 14-day window ends at
    # 09-09 12:00, so days 09-10..09-23 qualify. Kept: the 3 newest (all on
    # 09-23) plus the newest of each of the 13 earlier days = 16.
    days = {n.split("_")[2] for n in startups}
    assert len(days) == 14
    assert len(startups) == 16
    newest3 = sorted((p.name for p in made), reverse=True)[:3]
    assert set(newest3) <= remaining
    assert len(report["deleted"]) == len(made) - len(startups)
    assert all("_startup" in n for n in report["deleted"])


def test_old_backups_are_compressed_and_restore_from_gz(tmp_path, svc):
    first = svc.create_backup("startup")
    _add_row(svc.db_path, "two")
    pre = svc.create_backup("pre-v0007")
    _add_row(svc.db_path, "three")
    newest = svc.create_backup("startup")

    report = svc.apply_retention()
    names = {b["filename"] for b in svc.list_backups()}
    assert f"{first['filename']}.gz" in report["compressed"]
    assert f"{first['filename']}.gz" in names
    assert newest["filename"] in names          # newest stays plain
    assert pre["filename"] in names             # pre-migration stays plain .db
    with gzip.open(svc.backups_dir / f"{first['filename']}.gz", "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")

    target = tmp_path / "restored.db"
    BackupService(backups_dir=svc.backups_dir, db_path=target).restore_backup(f"{first['filename']}.gz")
    with sqlite3.connect(str(target)) as conn:
        assert [r[0] for r in conn.execute("SELECT v FROM t ORDER BY id")] == ["one"]
    assert not any(p.name.startswith(".restore_") for p in svc.backups_dir.iterdir())

    # Dedup still works against a compressed newest backup.
    for b in list(svc.backups_dir.iterdir()):
        if b.name != f"{first['filename']}.gz":
            b.unlink()
    svc2 = BackupService(backups_dir=svc.backups_dir, db_path=target)
    assert svc2.create_backup("startup")["status"] == "skipped"


def test_restore_rejects_corrupt_and_unsafe_files(svc):
    bad = svc.backups_dir / "edge_cctv_20260101_000000_manual.db"
    bad.write_bytes(b"not a database at all" * 10)
    with pytest.raises(ValueError):
        svc.restore_backup(bad.name)
    badgz = svc.backups_dir / "edge_cctv_20260101_000001_manual.db.gz"
    badgz.write_bytes(gzip.compress(b"not sqlite" * 10))
    with pytest.raises(ValueError):
        svc.restore_backup(badgz.name)
    for name in ("../../etc/passwd", "x.txt", "../a.db.gz"):
        with pytest.raises(ValueError):
            svc.restore_backup(name)
    # The live DB was not touched.
    with sqlite3.connect(str(svc.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_missing_source_startup_is_skipped_not_an_empty_file(tmp_path):
    svc = BackupService(backups_dir=tmp_path / "b", db_path=tmp_path / "absent.db")
    res = svc.create_backup("startup")
    assert res["status"] == "skipped" and res["reason"] == "no_source"
    assert svc.list_backups() == []


def test_migration_runner_backup_api_still_works(tmp_path):
    """app/migrations/runner.py::_backup calls create_backup(tag) and reads filepath."""
    from app.migrations.runner import _backup

    db = _make_db(tmp_path / "m.db")
    path = _backup(db.resolve(), tmp_path / "mb", "pre-v0042")
    assert Path(path).exists() and "pre-v0042" in path
    # Called twice with identical content it still writes (never deduplicated).
    path2 = _backup(db.resolve(), tmp_path / "mb", "pre-v0042")
    assert path2 != path and Path(path2).exists()
