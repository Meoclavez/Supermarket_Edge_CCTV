"""Evidence storage limit (services/evidence_storage.py) and no continuous recording.

The owner's rule: this device never records continuously (the store NAS
does); it keeps only alert evidence on its own disk under STORAGE_DIR, with a
configurable limit above which the oldest files are deleted automatically.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.services import evidence_storage as es_mod
from app.services.evidence_storage import EvidenceStorage, network_mount_problem

GB = 1024 ** 3
NOW = 2_000_000_000.0          # fixed "now" so ages are exact


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/nvme0n1p2 / ext4 rw,relatime 0 0\nproc /proc proc rw 0 0\n")
    db = tmp_path / "evidence_test.db"
    for name, value in {
        "STORAGE_DIR": root, "THEFT_EVIDENCE_DIR": "", "ZONE_ALERT_EVIDENCE_DIR": "",
        "NIGHT_WATCH_EVIDENCE_DIR": "", "SNAPSHOTS_DIR": root / "snapshots", "CLIPS_DIR": root / "clips",
        "DATABASE_PATH": db, "STORAGE_RETENTION_DAYS": 0, "STORAGE_MAX_DISK_PERCENT": 0.0,
        "EVIDENCE_MAX_GB": 1.0, "NIGHT_WATCH_EVIDENCE_MAX_MB": 1024.0,
    }.items():
        monkeypatch.setattr(settings, name, value)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE theft_incidents (id TEXT PRIMARY KEY, snapshot_path TEXT, evidence_snapshot_url TEXT,
                                      evidence_clip_url TEXT, evidence_expired_at DATETIME);
        CREATE TABLE security_events (id TEXT PRIMARY KEY, snapshot_url TEXT, clip_url TEXT,
                                      evidence_expired_at DATETIME);
    """)
    conn.commit()
    conn.close()
    return EvidenceStorage(mounts_file=str(mounts)), root, db, mounts


def put(path: Path, size: int, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def cap_bytes(monkeypatch, n: int) -> None:
    monkeypatch.setattr(settings, "EVIDENCE_MAX_GB", n / GB)


# ------------------------------------------------------------------ limits

def test_oldest_first_across_all_kinds(store, monkeypatch):
    es, root, _, _ = store
    files = [  # interleaved ages across the five kinds, oldest first
        put(root / "theft_evidence" / "inc_a.jpg", 100, NOW - 900),
        put(root / "night_watch" / "nw_0000000000a1.jpg", 100, NOW - 800),
        put(root / "zone_alerts" / "za_000000000001.jpg", 100, NOW - 700),
        put(root / "clips" / "evt_00000001.mp4", 100, NOW - 600),
        put(root / "snapshots" / "evt_00000001.jpg", 100, NOW - 500),
        put(root / "theft_evidence" / "inc_b.jpg", 100, NOW - 400),
        put(root / "night_watch" / "nw_0000000000a2.mp4", 100, NOW - 300),
    ]
    cap_bytes(monkeypatch, 350)
    rep = es.enforce(now=NOW)
    gone = [f for f in files if not f.exists()]
    kept = [f for f in files if f.exists()]
    assert gone == files[:4] and kept == files[4:]
    assert rep["status"] == "ok" and rep["deleted"] == 4
    assert rep["used_bytes"] == 300 and rep["files"] == 3
    assert rep["by_kind"]["theft_evidence"]["files"] == 1
    assert es.status()["deleted_files_total"] == 4 and es.status()["last_cleanup_at"]


def test_retention_days_delete_old_files_even_under_the_cap(store, monkeypatch):
    es, root, _, _ = store
    old = put(root / "theft_evidence" / "inc_old.jpg", 10, NOW - 8 * 86400)
    new = put(root / "theft_evidence" / "inc_new.jpg", 10, NOW - 6 * 86400)
    monkeypatch.setattr(settings, "STORAGE_RETENTION_DAYS", 7)
    rep = es.enforce(now=NOW)
    assert not old.exists() and new.exists()
    assert rep["deleted"] == 1


def test_cap_and_retention_are_both_enforced(store, monkeypatch):
    es, root, _, _ = store
    ancient = put(root / "zone_alerts" / "za_old.jpg", 10, NOW - 30 * 86400)
    a = put(root / "zone_alerts" / "za_a.jpg", 200, NOW - 300)
    b = put(root / "zone_alerts" / "za_b.jpg", 200, NOW - 200)
    c = put(root / "zone_alerts" / "za_c.jpg", 200, NOW - 100)
    monkeypatch.setattr(settings, "STORAGE_RETENTION_DAYS", 7)
    cap_bytes(monkeypatch, 450)
    es.enforce(now=NOW)
    assert not ancient.exists() and not a.exists() and b.exists() and c.exists()


def test_night_watch_sub_cap_inside_the_total(store, monkeypatch):
    es, root, _, _ = store
    nw = [put(root / "night_watch" / f"nw_{i:012x}.jpg", 400_000, NOW - 100 + i) for i in range(5)]
    theft = put(root / "theft_evidence" / "inc_x.jpg", 400_000, NOW - 1000)   # older, but not night watch
    monkeypatch.setattr(settings, "NIGHT_WATCH_EVIDENCE_MAX_MB", 1.0)
    assert es.enforce_kind_cap("night_watch") == 3
    assert [p.exists() for p in nw] == [False, False, False, True, True] and theft.exists()


def test_disk_limit_trims_evidence(store, monkeypatch):
    es, root, _, _ = store
    a = put(root / "clips" / "evt_a.mp4", 1000, NOW - 200)
    b = put(root / "clips" / "evt_b.mp4", 1000, NOW - 100)
    DU = namedtuple("DU", "total used free")
    monkeypatch.setattr(es_mod.shutil, "disk_usage", lambda p: DU(100_000, 86_000, 14_000))
    monkeypatch.setattr(settings, "STORAGE_MAX_DISK_PERCENT", 85.0)   # 1000 bytes over
    rep = es.enforce(now=NOW)
    assert not a.exists() and b.exists()
    assert "STORAGE_MAX_DISK_PERCENT" in rep["limited_by"]


def test_auto_cap_is_a_share_of_the_disk(store, monkeypatch):
    es, _, _, _ = store
    monkeypatch.setattr(settings, "EVIDENCE_MAX_GB", 0.0)
    DU = namedtuple("DU", "total used free")
    monkeypatch.setattr(es_mod.shutil, "disk_usage", lambda p: DU(200 * GB, 10 * GB, 190 * GB))
    assert es.cap() == (20 * GB, "automatic: 10% of the disk, 1-50 GB")
    monkeypatch.setattr(es_mod.shutil, "disk_usage", lambda p: DU(4 * GB, 1 * GB, 3 * GB))
    assert es.cap()[0] == 1 * GB
    monkeypatch.setattr(es_mod.shutil, "disk_usage", lambda p: DU(2000 * GB, 1 * GB, 1999 * GB))
    assert es.cap()[0] == 50 * GB


def test_a_write_over_the_limit_triggers_a_pass(store, monkeypatch):
    es, root, _, _ = store
    old = put(root / "theft_evidence" / "inc_1.jpg", 300, time.time() - 100)
    cap_bytes(monkeypatch, 400)
    es.enforce()
    assert old.exists()
    new = put(root / "theft_evidence" / "inc_2.jpg", 300, time.time())
    es.note_write(new)
    deadline = time.time() + 5
    while old.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert not old.exists() and new.exists()


# ------------------------------------------------------------------ database

def test_rows_of_deleted_files_read_evidence_expired(store, monkeypatch):
    es, root, db, _ = store
    put(root / "theft_evidence" / "inc_old.jpg", 100, NOW - 300)
    put(root / "theft_evidence" / "inc_new.jpg", 100, NOW - 10)
    put(root / "night_watch" / "nw_00000000000a.jpg", 100, NOW - 200)
    put(root / "night_watch" / "nw_00000000000a.mp4", 100, NOW - 199)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO theft_incidents (id, snapshot_path, evidence_snapshot_url) VALUES (?,?,?)", [
        ("inc_old", str(root / "theft_evidence" / "inc_old.jpg"), "/api/v1/theft/incidents/inc_old/evidence"),
        ("inc_new", str(root / "theft_evidence" / "inc_new.jpg"), "/api/v1/theft/incidents/inc_new/evidence")])
    conn.executemany("INSERT INTO security_events (id, snapshot_url, clip_url) VALUES (?,?,?)", [
        ("evt_theft", "/api/v1/theft/incidents/inc_old/evidence", None),
        ("nw_00000000000a", "/api/v1/night-watch/evidence/nw_00000000000a.jpg",
         "/api/v1/night-watch/evidence/nw_00000000000a.mp4"),
        ("evt_keep", "/api/v1/theft/incidents/inc_new/evidence", None)])
    conn.commit()
    conn.close()
    cap_bytes(monkeypatch, 100)
    es.enforce(now=NOW)
    conn = sqlite3.connect(db)
    t = dict(conn.execute("SELECT id, evidence_expired_at IS NOT NULL FROM theft_incidents").fetchall())
    urls = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, snapshot_url, clip_url, evidence_expired_at IS NOT NULL FROM security_events")}
    turl = dict(conn.execute("SELECT id, evidence_snapshot_url FROM theft_incidents").fetchall())
    conn.close()
    assert t == {"inc_old": 1, "inc_new": 0}
    assert turl["inc_old"] is None and turl["inc_new"]
    assert urls["evt_theft"] == (None, None, 1)
    assert urls["nw_00000000000a"] == (None, None, 1)
    assert urls["evt_keep"][0] and urls["evt_keep"][2] == 0


def test_theft_api_says_expired_not_broken():
    from app.models.db_models import TheftIncidentModel
    from app.routes.theft import _serialize

    inc = TheftIncidentModel(id="inc_x", camera_id="c", camera_name="C", department="GENERAL",
                             theft_type="CONCEALMENT", rule="CONCEALMENT", severity="HIGH", status="ACTIVE",
                             confidence=0.8, evidence_summary="", snapshot_path="/nonexistent/inc_x.jpg",
                             evidence_expired_at=datetime(2026, 9, 1, 12, 0), items_involved=[],
                             timestamp=datetime(2026, 8, 30), created_at=datetime(2026, 8, 30),
                             estimated_loss_value=0.0)
    out = _serialize(inc)
    assert out.snapshot_url is None and out.evidence_snapshot_url is None
    assert out.evidence_expired_at == datetime(2026, 9, 1, 12, 0)


# ------------------------------------------------------------------ safety

def test_a_symlinked_kind_directory_leading_out_is_left_alone(store, monkeypatch, tmp_path):
    es, root, _, _ = store
    outside = tmp_path / "outside"
    victim = put(outside / "precious.jpg", 500, NOW - 10_000)
    (root / "theft_evidence").symlink_to(outside, target_is_directory=True)
    cap_bytes(monkeypatch, 1)
    rep = es.enforce(now=NOW)
    assert victim.exists()
    assert rep["by_kind"]["theft_evidence"]["managed"] is False
    assert "outside STORAGE_DIR" in rep["by_kind"]["theft_evidence"]["note"]


def test_a_symlinked_file_is_never_followed_or_deleted(store, monkeypatch, tmp_path):
    es, root, _, _ = store
    victim = put(tmp_path / "outside" / "precious.jpg", 500, NOW - 10_000)
    link = root / "zone_alerts" / "za_link.jpg"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim)
    real = put(root / "zone_alerts" / "za_real.jpg", 500, NOW - 5)
    cap_bytes(monkeypatch, 1)
    es.enforce(now=NOW)
    assert victim.exists() and link.is_symlink() and not real.exists()


def test_a_directory_outside_storage_dir_is_never_touched(store, monkeypatch, tmp_path):
    es, root, _, _ = store
    elsewhere = tmp_path / "elsewhere"
    victim = put(elsewhere / "inc_1.jpg", 500, NOW - 10_000)
    monkeypatch.setattr(settings, "THEFT_EVIDENCE_DIR", str(elsewhere))
    monkeypatch.setattr(settings, "STORAGE_RETENTION_DAYS", 1)
    cap_bytes(monkeypatch, 1)
    rep = es.enforce(now=NOW)
    assert victim.exists() and rep["by_kind"]["theft_evidence"]["managed"] is False


def test_only_evidence_files_directly_in_a_kind_directory_are_deleted(store, monkeypatch):
    es, root, _, _ = store
    keep = [put(root / "theft_evidence" / "notes.txt", 500, NOW - 10_000),
            put(root / "theft_evidence" / "sub" / "inc_deep.jpg", 500, NOW - 10_000),
            put(root / "cctv_core_copy.jpg", 500, NOW - 10_000),
            put(root / "dvr" / "cam1" / "20260101_000000.mp4", 500, NOW - 10_000),
            put(root / "archives" / "arch_1.mp4", 500, NOW - 10_000)]
    cap_bytes(monkeypatch, 1)
    monkeypatch.setattr(settings, "STORAGE_RETENTION_DAYS", 1)
    rep = es.enforce(now=NOW)
    assert all(p.exists() for p in keep)
    assert rep["not_managed"]["dvr"]["files"] == 1 and rep["note"]


def test_refuses_to_run_on_a_network_mount(store, monkeypatch):
    es, root, _, mounts = store
    victim = put(root / "theft_evidence" / "inc_1.jpg", 500, NOW - 10_000)
    mounts.write_text("/dev/sda1 / ext4 rw 0 0\n"
                      f"nas.local:/cctv {str(root).replace(' ', chr(92) + '040')} nfs4 rw 0 0\n")
    cap_bytes(monkeypatch, 1)
    rep = es.enforce(now=NOW)
    assert rep["status"] == "refused" and "network filesystem" in rep["error"]
    assert victim.exists()
    assert es.status()["status"] == "refused"
    assert es.enforce_kind_cap("night_watch") == 0

    from app.services.resilience import ServiceHealthTracker
    from app.routes.health import _evidence_storage

    monkeypatch.setattr(es_mod, "evidence_storage", es)
    block, entry = _evidence_storage()
    assert block["status"] == "refused" and entry["status"] == ServiceHealthTracker.FAILED
    ServiceHealthTracker.report_status("evidence_storage", ServiceHealthTracker.HEALTHY)


def test_a_kind_directory_mounted_from_the_nas_is_not_managed(store, monkeypatch):
    es, root, _, mounts = store
    victim = put(root / "night_watch" / "nw_000000000001.jpg", 500, NOW - 10_000)
    mounts.write_text(f"/dev/sda1 / ext4 rw 0 0\n//nas/share {root / 'night_watch'} cifs rw 0 0\n")
    cap_bytes(monkeypatch, 1)
    rep = es.enforce(now=NOW)
    assert victim.exists() and rep["by_kind"]["night_watch"]["managed"] is False


@pytest.mark.parametrize("fstype,network", [("nfs", True), ("nfs4", True), ("cifs", True), ("smb3", True),
                                            ("fuse.sshfs", True), ("ext4", False), ("xfs", False),
                                            ("btrfs", False), ("tmpfs", False)])
def test_network_filesystem_types(tmp_path, fstype, network):
    mounts = tmp_path / "m"
    mounts.write_text(f"/dev/sda1 / ext4 rw 0 0\nsrc /srv/x {fstype} rw 0 0\n")
    assert bool(network_mount_problem(Path("/srv/x/storage"), str(mounts))) is network
    assert network_mount_problem(Path("/srv/xy/storage"), str(mounts)) is None


# ------------------------------------------------------------------ no continuous recording

def test_continuous_recording_is_off_by_default():
    from app.models.db_models import CameraModel
    from app.models.schemas import CameraCreate, CameraFeed

    assert CameraModel.__table__.c.dvr_enabled.default.arg is False
    assert CameraFeed(id="c", name="n", location="l").dvr_enabled is False
    assert CameraCreate(id="c", name="n", location="l", rtsp_url="rtsp://x").dvr_enabled is False


def test_clients_cannot_turn_continuous_recording_on():
    from app.models.schemas import CameraFeed
    from app.routes.cameras import _writable_payload

    assert _writable_payload(CameraFeed(id="c", name="n", location="l", dvr_enabled=True))["dvr_enabled"] is False


def test_the_continuous_recorder_is_gone():
    from app.services import dvr_recorder

    assert not hasattr(dvr_recorder, "DVRCameraWorker")
    assert dvr_recorder.dvr_recorder_service.start_camera_dvr("cam", "rtsp://x") is False


def test_migration_turns_existing_dvr_flags_off(tmp_path):
    from app.migrations import m0015_evidence_only_storage as m

    conn = sqlite3.connect(tmp_path / "m.db")
    conn.executescript("""
        CREATE TABLE cameras (id TEXT PRIMARY KEY, dvr_enabled BOOLEAN NOT NULL);
        INSERT INTO cameras VALUES ('a', 1), ('b', 0);
        CREATE TABLE theft_incidents (id TEXT PRIMARY KEY);
        CREATE TABLE security_events (id TEXT PRIMARY KEY);
    """)
    m.upgrade(conn)
    m.upgrade(conn)   # idempotent
    assert conn.execute("SELECT SUM(dvr_enabled) FROM cameras").fetchone()[0] == 0
    for t in ("theft_incidents", "security_events"):
        assert "evidence_expired_at" in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}


def test_health_and_system_stats_report_usage_honestly(store, monkeypatch):
    es, root, _, _ = store
    put(root / "theft_evidence" / "inc_1.jpg", 250, NOW - 100)
    put(root / "theft_evidence" / "inc_2.jpg", 250, NOW - 50)
    cap_bytes(monkeypatch, 300)
    monkeypatch.setattr(es_mod, "evidence_storage", es)
    from app.routes.health import _evidence_storage
    from app.routes.system import _evidence_summary

    assert _evidence_summary()["status"] == "not_run_yet" and _evidence_summary()["used_bytes"] is None
    es.enforce(now=NOW)
    block, entry = _evidence_storage()
    assert entry["status"] == "HEALTHY"
    assert block["used_bytes"] == 250 and block["files"] == 1 and block["cap_bytes"] == 300
    assert block["oldest"].startswith("2033-05-18") and block["last_deleted"] == 1
    assert block["deleted_files_total"] == 1 and block["last_cleanup_at"]
    stats = _evidence_summary()
    assert stats["used_bytes"] == 250 and stats["cap_source"].startswith("EVIDENCE_MAX_GB")
