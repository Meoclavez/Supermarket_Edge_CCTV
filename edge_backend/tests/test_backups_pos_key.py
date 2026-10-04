"""Settings > Backups and reset (routes/system.py) and the till key (routes/pos_ingest.py).

Real authentication (the ``client`` fixture from test_auth_setup, AUTH_DISABLED
off) so owner/operator separation and till-key acceptance are exercised for real.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.routes import system as system_routes
from app.services import auth_service as auth_mod
from app.services.backup_service import BackupService, backup_kind, backup_service, validate_sqlite_snapshot
from app.services.till_key import TillKeyStore, till_key_store
from app.services.token_revocation import token_revocations

from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: F401  (fixture)

OWNER_PW = "correct horse 1"
PW = "staff password 1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    token_revocations.clear_cache()
    auth_mod.invalidate_account_cache()
    auth_mod.intrusion_detector.failed_attempts.clear()
    till_key_store.revoke()
    # Never SIGTERM the test process.
    monkeypatch.setattr(system_routes, "restart_supported", lambda: False)
    yield
    till_key_store.revoke()
    # Leave no sales rows behind: other files assert "Not observed" revenue.
    with sqlite3.connect(settings.DATABASE_PATH) as c:
        c.execute("DELETE FROM pos_transactions WHERE transaction_id IN ('tk_tx_1', 'fr')")
    auth_mod.intrusion_detector.failed_attempts.clear()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _owner(client):
    body = _create_admin(client, _issued_code(client), password=OWNER_PW).json()
    assert body.get("access_token"), body
    return body["access_token"]


def _operator(client, owner_tok, name="till_op"):
    r = client.post("/api/v1/users", headers=_h(owner_tok),
                    json={"username": name, "display_name": "Op", "role": "operator", "password": PW})
    assert r.status_code in (200, 201), r.text
    r = client.post("/api/v1/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


SALE = {"transactions": [{"transaction_id": "tk_tx_1", "register_id": "TK-1", "sku_id": "S1", "amount": 2.5}]}


# --------------------------------------------------------------------------- backups

def test_list_backup_now_download_and_kinds(client):
    tok = _owner(client)
    r = client.post("/api/v1/system/backup", headers=_h(tok), json={"tag": "manual"})
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["status"] == "success" and made["kind"] == "Manual" and "filepath" not in made
    lst = client.get("/api/v1/system/backups", headers=_h(tok)).json()
    entry = next(b for b in lst["backups"] if b["filename"] == made["filename"])
    assert entry["kind"] == "Manual" and entry["time_label"] and entry["size_bytes"] > 0
    assert isinstance(lst["restart_supported"], bool)

    d = client.get(f"/api/v1/system/backups/{made['filename']}/download", headers=_h(tok))
    assert d.status_code == 200
    assert d.content.startswith(b"SQLite format 3")
    assert "attachment" in d.headers["content-disposition"] and made["filename"] in d.headers["content-disposition"]
    assert d.headers["cache-control"] == "private, no-store"

    assert backup_kind("startup") == "At start" and backup_kind("pre-v0012") == "Before upgrade"
    assert backup_kind("pre-restore-2") == "Before restore" and backup_kind("uploaded") == "Uploaded"


@pytest.mark.parametrize("name", [
    "..%2F..%2Fetc%2Fpasswd",
    "..%2Fcctv_core.db",
    "%2Fetc%2Fpasswd.db",
    ".tmp_edge_cctv_20260101_000000_manual.db",
    "edge_cctv_20260101_000000_manual.txt",
    "nope.db",
])
def test_download_rejects_unsafe_or_unknown_names(client, name):
    tok = _owner(client)
    r = client.get(f"/api/v1/system/backups/{name}/download", headers=_h(tok))
    assert r.status_code in (400, 404), (name, r.status_code)
    assert not r.content.startswith(b"SQLite")


def test_resolve_backup_path_refuses_symlink_out_of_dir(tmp_path):
    outside = tmp_path / "secret.db"
    outside.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    svc = BackupService(backups_dir=tmp_path / "b", db_path=tmp_path / "x.db")
    (svc.backups_dir / "edge_cctv_20260101_000000_manual.db").symlink_to(outside)
    with pytest.raises((FileNotFoundError, ValueError)):
        svc.resolve_backup_path("edge_cctv_20260101_000000_manual.db")
    for bad in ("../secret.db", "a/b.db", "", ".hidden.db", "x.db\x00"):
        with pytest.raises((FileNotFoundError, ValueError)):
            svc.resolve_backup_path(bad)


def test_restore_takes_safety_backup_and_reports_restart(client):
    tok = _owner(client)
    made = client.post("/api/v1/system/backup", headers=_h(tok), json={"tag": "manual"}).json()
    r = client.post(f"/api/v1/system/backups/{made['filename']}/restore", headers=_h(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success" and body["restart"] == "manual"
    assert "pre-restore" in body["safety_backup"]
    assert (Path(settings.BACKUPS_DIR) / body["safety_backup"]).exists()
    assert "Restart the server" in body["message"]
    # Missing file: 404, and nothing (no safety backup) is taken.
    before = len(backup_service.list_backups())
    assert client.post("/api/v1/system/backups/edge_cctv_20000101_000000_none.db/restore",
                       headers=_h(tok)).status_code == 404
    assert len(backup_service.list_backups()) == before


def test_restore_when_supervised_stops_pipeline_and_schedules_restart(client, monkeypatch):
    tok = _owner(client)
    calls = []
    monkeypatch.setattr(system_routes, "restart_supported", lambda: True)
    monkeypatch.setattr(system_routes, "_schedule_restart", lambda: calls.append("restart"))

    async def _stop():
        calls.append("stop")

    monkeypatch.setattr(system_routes, "_stop_pipeline_for_restore", _stop)
    made = client.post("/api/v1/system/backup", headers=_h(tok), json={}).json()
    r = client.post(f"/api/v1/system/restore/{made['filename']}", headers=_h(tok))  # legacy path
    assert r.status_code == 200, r.text
    assert r.json()["restart"] == "scheduled" and calls == ["stop", "restart"]


def test_restore_refuses_backup_from_newer_release(tmp_path):
    db = tmp_path / "new.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT, "
                  "checksum TEXT, app_version TEXT)")
        c.execute("INSERT INTO schema_migrations VALUES (9999, 'future', '', '', '99')")
    with pytest.raises(ValueError, match="newer release"):
        validate_sqlite_snapshot(db, require_schema=False)


def test_upload_accepts_own_backup_and_rejects_others(client):
    tok = _owner(client)
    made = client.post("/api/v1/system/backup", headers=_h(tok), json={}).json()
    # The suite's database is built with create_all (no migration runner), so
    # give the copy the version table every real installation has.
    copy = Path(settings.BACKUPS_DIR).parent / "versioned_copy.db"
    copy.write_bytes((Path(settings.BACKUPS_DIR) / made["filename"]).read_bytes())
    with sqlite3.connect(copy) as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, "
                  "applied_at TEXT, checksum TEXT, app_version TEXT)")
        c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, 'baseline', '', '', 'test')")
    raw = copy.read_bytes()
    copy.unlink()

    ok = client.post("/api/v1/system/backups/upload?filename=store.db.gz", headers={
        **_h(tok), "Content-Type": "application/octet-stream"}, content=gzip.compress(raw))
    assert ok.status_code == 200, ok.text
    assert ok.json()["kind"] == "Uploaded" and ok.json()["filename"].endswith("_uploaded.db")

    junk = client.post("/api/v1/system/backups/upload", headers={
        **_h(tok), "Content-Type": "application/octet-stream"}, content=b"not a database at all")
    assert junk.status_code == 400 and "not an SQLite" in junk.json()["detail"]

    other = Path(settings.BACKUPS_DIR).parent / "other_app.db"
    with sqlite3.connect(other) as c:
        c.execute("CREATE TABLE notes (id INTEGER)")
    r = client.post("/api/v1/system/backups/upload", headers={
        **_h(tok), "Content-Type": "application/octet-stream"}, content=other.read_bytes())
    other.unlink()
    assert r.status_code == 400 and "not a backup of this system" in r.json()["detail"]
    assert not list(Path(settings.BACKUPS_DIR).glob(".upload_*"))


def test_factory_reset_requires_reset_and_backs_up_first(client):
    tok = _owner(client)
    for bad in ({}, {"confirm": "reset"}, {"confirm": "yes"}):
        r = client.post("/api/v1/system/factory-reset", headers=_h(tok), json=bad)
        assert r.status_code == 400 and "RESET" in r.json()["detail"]
    with sqlite3.connect(settings.DATABASE_PATH) as c:
        c.execute("INSERT INTO pos_transactions (id, transaction_id, timestamp, register_id, sku_id, quantity, amount, "
                  "created_at) VALUES ('pos_fr_1', 'fr', '2026-10-01 10:00:00', 'L1', 'S', 1, 1.0, "
                  "'2026-10-01 10:00:00')")
    r = client.post("/api/v1/system/factory-reset", headers=_h(tok), json={"confirm": "RESET"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reset"] is True and "pre-reset" in body["backup"]
    assert body["removed"]["pos_transactions"] >= 1
    for extra in ("tripwire_events", "queue_visits", "heatmap_snapshots", "analysis_runs"):
        assert extra in body["removed"]
    with sqlite3.connect(settings.DATABASE_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM pos_transactions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0] >= 1   # accounts kept
    with sqlite3.connect(Path(settings.BACKUPS_DIR) / body["backup"]) as c:
        assert c.execute("SELECT COUNT(*) FROM pos_transactions WHERE id='pos_fr_1'").fetchone()[0] == 1


def test_operator_cannot_touch_backups_or_till_key(client):
    tok = _owner(client)
    op = _operator(client, tok)
    made = client.post("/api/v1/system/backup", headers=_h(tok), json={}).json()
    for method, url, kw in [
        ("get", "/api/v1/system/backups", {}),
        ("post", "/api/v1/system/backup", {"json": {}}),
        ("get", f"/api/v1/system/backups/{made['filename']}/download", {}),
        ("post", f"/api/v1/system/backups/{made['filename']}/restore", {}),
        ("post", f"/api/v1/system/restore/{made['filename']}", {}),
        ("post", f"/api/v1/analytics/system/restore/{made['filename']}", {}),
        ("post", "/api/v1/system/factory-reset", {"json": {"confirm": "RESET"}}),
        ("get", "/api/v1/analytics/pos/till-key", {}),
        ("post", "/api/v1/analytics/pos/till-key", {}),
        ("delete", "/api/v1/analytics/pos/till-key", {}),
    ]:
        r = getattr(client, method)(url, headers=_h(op), **kw)
        assert r.status_code == 403, (method, url, r.status_code, r.text)


# --------------------------------------------------------------------------- till key

def test_till_key_create_rotate_revoke_and_ingest(client):
    tok = _owner(client)
    s = client.get("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()
    assert s["exists"] is False and s["header"] == "X-Edge-API-Key"

    # No key / a wrong key: refused.
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE).status_code == 401
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE,
                       headers={"X-Edge-API-Key": "till_wrong"}).status_code in (401, 403)

    c1 = client.post("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()
    key1 = c1["key"]
    assert key1.startswith("till_") and c1["exists"] and c1["hint"] == key1[-4:]
    assert c1["note"] == "Shown once. Store it in the till system now."
    status = client.get("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()
    assert "key" not in status and status["created_at"] and status["last_used_at"] is None
    stored = Path(settings.STORAGE_DIR, "secrets", "till_key.json")
    assert key1 not in stored.read_text() and (stored.stat().st_mode & 0o077) == 0

    r = client.post("/api/v1/analytics/pos/ingest", json=SALE, headers={"X-Edge-API-Key": key1})
    assert r.status_code == 200 and r.json()["ingested_count"] == 1
    assert client.get("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()["last_used_at"]
    # The till key opens nothing but ingest.
    assert client.get("/api/v1/analytics/overview", headers={"X-Edge-API-Key": key1}).status_code in (401, 403)
    assert client.get("/api/v1/system/backups", headers={"X-Edge-API-Key": key1}).status_code in (401, 403)

    # Rotate: the old key stops working, the new one works.
    key2 = client.post("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()["key"]
    assert key2 != key1
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE,
                       headers={"X-Edge-API-Key": key1}).status_code in (401, 403)
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE,
                       headers={"X-Edge-API-Key": key2}).status_code == 200

    # Backward compatibility: the internal service key and a session still work.
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE,
                       headers={"X-Edge-API-Key": settings.INTERNAL_SERVICE_KEY}).status_code == 200
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE, headers=_h(tok)).status_code == 200

    # Revoke.
    rv = client.delete("/api/v1/analytics/pos/till-key", headers=_h(tok)).json()
    assert rv["revoked"] is True and rv["exists"] is False
    assert client.post("/api/v1/analytics/pos/ingest", json=SALE,
                       headers={"X-Edge-API-Key": key2}).status_code in (401, 403)


def test_till_key_store_hashes_and_survives_reload(tmp_path):
    store = TillKeyStore(tmp_path / "secrets" / "till_key.json")
    key = store.create(actor="t")
    again = TillKeyStore(tmp_path / "secrets" / "till_key.json")
    assert again.verify(key) and not again.verify(key + "x") and not again.verify(None)
    assert again.revoke() and not again.verify(key) and again.revoke() is False


def test_pos_status_names_the_header(client):
    tok = _owner(client)
    ing = client.get("/api/v1/analytics/pos/status", headers=_h(tok)).json()["ingest"]
    assert ing["header"] == "X-Edge-API-Key" and "till key" in ing["auth"]
