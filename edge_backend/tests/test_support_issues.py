"""Report a problem, the box issue log and their upload (docs/SUPPORT_ISSUES_CONTRACT.md).

Redaction and fingerprints (services/issue_log.py), the capture handler (never
raises, never recurses, caps), the uploader's batching / acknowledgement /
backoff against a fake VPS (httpx.MockTransport: nothing leaves this machine),
the report API (routes/support.py), report_updates from the check-in, and the
signed policy's support contact.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import entitlements as ent
from app.services import fleet_control as fc
from app.services import issue_log as il
from app.services import support_reports as sr
from tests.test_auth_setup import client  # noqa: F401  (fixture: real authentication)

STORE = "teststore"
TARGET = {"url": "https://tunnel.example.com/control/v1/checkin", "store_id": STORE,
          "token": "store-token-abc", "ca_file": None}


# --------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """A throwaway support.db, empty name cache and a new uploader for every test."""
    engine = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(engine)
    engine.dispose()
    store = il.IssueStore(tmp_path / "support.db")
    reports = sr.ReportStore(tmp_path / "support.db")
    il.set_store(store)
    sr.set_reports(reports)
    monkeypatch.setattr(sr, "uploader", sr.SupportUploader())
    il.names.set_names([])

    def wipe():
        with sqlite3.connect(str(settings.DATABASE_PATH), timeout=15) as conn:
            conn.execute("DELETE FROM system_setup WHERE key = ?", (ent.STATE_KEY,))
        ent.invalidate()

    wipe()
    yield store
    wipe()
    il.set_store(None)
    sr.set_reports(None)
    il.names.set_names([])


@pytest.fixture()
def cap(_fresh):
    """A capture handler on a logger of its own (no propagation: the suite's root stays quiet)."""
    h = il.IssueCaptureHandler(_fresh)
    clock = {"t": 1000.0}
    h.clock = lambda: clock["t"]
    lg = logging.getLogger("edge.test_issue_capture")
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    yield h, lg, clock
    lg.removeHandler(h)


class FakeIssuesVPS:
    """POST /control/v1/issues (and /checkin) like cctv-control: accepts everything unless told otherwise."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.accept = True              # False: accept nothing; a set: accept only these ids
        self.headers = {}
        self.checkin_answer = {"control_public_key": None, "policy": None, "commands": [], "next_checkin_s": 300}
        # Signed acks like cctv-control (an Ed25519 private key), with report_updates_sha256:
        # hash_mode "good" | "omit" | "wrong"; tamper(updates) changes the list after hashing.
        self.signer = None
        self.hash_mode = "good"
        self.tamper = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/checkin"):
            answer = json.loads(json.dumps(dict(self.checkin_answer, server_time=fc.iso())))
            if self.signer is not None:
                answer["control_public_key"] = _b64_public(self.signer)
                ack = {"type": "ack", "store_id": STORE, "nonce": body.get("nonce"),
                       "server_time": answer["server_time"], "policy_version": 0}
                updates = answer.get("report_updates")
                if updates and self.hash_mode == "good":
                    ack["report_updates_sha256"] = hashlib.sha256(fc.canonical_json(updates)).hexdigest()
                elif updates and self.hash_mode == "wrong":
                    ack["report_updates_sha256"] = hashlib.sha256(b"something else").hexdigest()
                if self.tamper and updates:
                    self.tamper(updates)
                payload = fc.canonical_json(ack)
                answer["ack"] = {"payload": fc.b64url_encode(payload),
                                 "sig": fc.b64url_encode(self.signer.sign(payload))}
            return httpx.Response(200, json=answer)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"}, headers=self.headers)

        def take(items, key):
            ids = [i[key] for i in items]
            if self.accept is True:
                return ids
            if self.accept is False:
                return []
            return [i for i in ids if i in self.accept]
        return httpx.Response(200, json={"accepted_reports": take(body["reports"], "id"),
                                         "accepted_issues": take(body["issues"], "fingerprint")})

    def issue_bodies(self):
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/issues")]


def _b64_public(priv) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _signer():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


@pytest.fixture()
def vps(monkeypatch):
    monkeypatch.setattr(settings, "FLEET_CONTROL_PUBLIC_KEY", "")
    srv = FakeIssuesVPS()
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(srv.handler))
    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: dict(TARGET)))
    return srv


def _upload():
    return asyncio.run(sr.uploader.upload_once())


def _error(store, msg="Something broke", logger_name="edge.x", n=1):
    h = il.IssueCaptureHandler(store)
    for _ in range(n):
        h.capture(level="error", logger_name=logger_name, exc_type=None, message=msg, tb_text=None)


# --------------------------------------------------------------------------- redaction

def test_redaction_rtsp_credentials_ips_email_mac_hosts_paths_and_quotes():
    text = ("Camera cam_ab12cd34ef error: open rtsp://admin:Secr3t!@192.168.1.64:554/cam/realmonitor?channel=1 "
            "failed; login jane.citizen@example.com.au from 10.0.0.5:8080 mac AA:BB:CC:DD:EE:FF host nvr01.local "
            "v6 fe80::1ff:fe23:4567:890a; file /home/meoclavezz/storage/x.db; at 10:00:00 key 'Front Door'")
    out = il.redact_text(text)
    for leak in ("admin", "Secr3t", "192.168", "10.0.0.5", "jane", "example.com", "AA:BB", "nvr01", "fe80",
                 "/home/meoclavezz", "meoclavezz", "Front Door"):
        assert leak not in out, (leak, out)
    assert "<rtsp-url>" in out and "<email>" in out and "<ip>" in out and "<mac>" in out
    assert "<host>" in out and "<path>" in out and "<q>" in out
    assert "cam_ab12cd34ef" in out                     # opaque ids are fine
    assert "10:00:00" in out                           # a time is not an IPv6 address
    # Tokens and passwords (log_redaction) as well.
    assert "abcdefghijklmnop" not in il.redact_text("Authorization: Bearer abcdefghijklmnop")
    assert "hunter2" not in il.redact_text("login failed password=hunter2")


def test_redaction_keeps_app_paths_and_api_routes_but_not_other_paths():
    app_file = str(il.APP_PACKAGE_DIR / "services" / "live_analytics_engine.py")
    out = il.redact_text(f"crash in {app_file} via /api/v1/cameras/cam_1/reconnect; "
                         "lib /usr/lib/python3/site-packages/onnxruntime/capi/x.py; dev /dev/dri/renderD128")
    assert "app/services/live_analytics_engine.py" in out and str(il.APP_PACKAGE_DIR) not in out
    assert "/api/v1/cameras/cam_1/reconnect" in out
    assert "<lib>/onnxruntime/capi/x.py" in out and "/dev/dri" not in out


def test_known_names_become_ids_or_name_placeholders():
    il.names.set_names([("Front Door Cam", "cam_ab12cd34ef"), ("Jane Citizen", "<name>"),
                        ("Pearcedale IGA", "<name>"), ("Fruit  Aisle", "zone_77aa88bb"),
                        ("Camera", "cam_generic"), ("ab", "x")])
    out = il.redact_text("Front Door Cam froze for Jane  Citizen at Pearcedale IGA (fruit aisle); Camera ok; ab")
    assert out == "cam_ab12cd34ef froze for <name> at <name> (zone_77aa88bb); Camera ok; ab"


def test_names_cache_reads_the_main_database_without_the_handler(tmp_path):
    db = tmp_path / "main.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE cameras (id TEXT, name TEXT, location TEXT)")
        conn.execute("INSERT INTO cameras VALUES ('cam_00aa11bb22', 'Bakery Corner', 'Back Dock West')")
        conn.execute("CREATE TABLE admin_users (username TEXT, display_name TEXT)")
        conn.execute("INSERT INTO admin_users VALUES ('jsmith', 'John Smith')")
        conn.execute("CREATE TABLE system_setup (key TEXT, value TEXT)")
        conn.execute("INSERT INTO system_setup VALUES ('device_name', 'Smiths Fresh Market')")
    cache = il.NameCache()
    assert cache.refresh_from_db(db)
    out = il.redact_text("Bakery Corner by jsmith (John Smith) at Smiths Fresh Market, Back Dock West", cache)
    assert out == "cam_00aa11bb22 by <name> (<name>) at <name>, <name>"
    assert il.NameCache().refresh_from_db(tmp_path / "missing.db") is False


# --------------------------------------------------------------------------- fingerprints

def test_thirty_two_camera_errors_at_restart_are_one_fingerprint(cap):
    h, lg, clock = cap
    whys = ["timeout", "Connection refused 10.0.0.%d:554", "401 Unauthorized"]

    def burst():
        for i in range(32):
            why = whys[i % 3] % i if "%d" in whys[i % 3] else whys[i % 3]
            lg.warning(f"Camera cam_{i:010x} error: Stream ended or frame read failed ({why}); retrying in 5s")

    burst()                                   # restart: cameras not reachable yet
    assert len(h.store) == 0                  # a camera counts only after 10 min offline
    clock["t"] += 300
    burst()
    assert len(h.store) == 0
    clock["t"] += 301                         # > 10 min for every camera
    burst()
    rows = h.store.rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["count"] == 32 and row["affected"] == 32 and row["level"] == "warning"
    assert "10.0.0" not in row["message"]
    # Back for 20+ min: the clock restarts for that camera.
    clock["t"] += il.CAMERA_GAP_S + 1
    lg.warning("Camera cam_0000000001 error: Stream ended or frame read failed (timeout); retrying in 5s")
    assert h.store.rows()[0]["count"] == 32


def test_template_normalises_numbers_ids_hex_timestamps_and_quotes():
    a = il.message_template(il.redact_text("Job 17 for evt_1a2b3c4d at 2026-10-23T10:00:01Z took 3.5 s (0x7f3a9c) 'x'"))
    b = il.message_template(il.redact_text("Job 99 for evt_9f8e7d6c at 2026-10-24T11:59:59Z took 12 s (0x1) \"y z\""))
    assert a == b
    assert il.fingerprint("edge.a", "ValueError", a) != il.fingerprint("edge.b", "ValueError", a)
    assert il.fingerprint("edge.a", "ValueError", a) != il.fingerprint("edge.a", "KeyError", a)


# --------------------------------------------------------------------------- capture handler

def test_handler_captures_errors_allowlisted_warnings_and_nothing_else(cap):
    h, lg, _ = cap
    lg.info("routine info")
    lg.debug("debug")
    lg.warning("Some ordinary warning about fps")
    assert len(h.store) == 0
    lg.warning("Request failed on a locked database: database is locked")
    lg.warning("PREFLIGHT WARNING [gpu] GPU present (CUDAExecutionProvider) but a real session ran on CPU")
    lg.warning("Evidence storage: deleted 3 oldest file(s), 1.0 MB (disk limit); limited by disk above "
               "STORAGE_MAX_DISK_PERCENT=90")
    lg.error("Daily backup failed: disk I/O error")
    lg.critical("Disk usage > 90% (93.1%), skipping clip recording for evt_12345678")
    levels = sorted(r["level"] for r in h.store.rows())
    assert levels == ["critical", "error", "warning", "warning", "warning"]


def test_traceback_is_trimmed_to_app_frames_and_redacted(cap):
    h, lg, _ = cap

    def deep(n):
        if n == 0:
            raise RuntimeError("cannot open rtsp://u:p@10.1.2.3/x for 'Back Office'")
        return deep(n - 1)
    try:
        deep(30)
    except RuntimeError:
        lg.exception("Worker crashed")
    row = h.store.rows()[0]
    tb = row["traceback"]
    assert row["exc_type"] == "RuntimeError"
    assert tb.startswith("Traceback (most recent call last; app frames only")
    assert tb.count('  File "') <= il.MAX_FRAMES and len(tb) <= il.MAX_TRACEBACK_CHARS
    assert "10.1.2.3" not in tb and "u:p" not in tb and "Back Office" not in tb
    assert "/home/" not in tb                        # test frames are outside app/: left out
    tb2 = il.format_traceback(*_exc_info_from_app())
    assert 'File "app/services/issue_log.py"' in tb2


def _exc_info_from_app():
    try:
        il.IssueStore.acknowledge(None, [{"fingerprint": "x"}], [], 0)   # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return type(exc), exc, exc.__traceback__
    raise AssertionError("expected an exception")


def test_handler_never_raises(cap, monkeypatch):
    h, lg, _ = cap
    # A format error inside the record, and an odd msg type (straight to the handler:
    # pytest's own log handler would complain about the first).
    h.handle(logging.LogRecord("edge.x", logging.ERROR, __file__, 1, "bad %d format", ("not a number",), None))
    h.handle(logging.LogRecord("edge.x", logging.ERROR, __file__, 1, object(), None, None))
    assert len(h.store) >= 1

    def boom(**_kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(h.store, "capture", boom)
    lg.error("this capture fails internally")            # must not raise
    assert h.errors >= 1
    h.handleError(logging.makeLogRecord({}))              # never prints / raises


def test_recursion_guard_and_own_loggers_are_ignored(cap, monkeypatch):
    h, lg, _ = cap
    real = h.store.capture
    calls = {"n": 0}

    def logging_capture(**kw):
        calls["n"] += 1
        lg.error("error raised while capturing an error")    # re-enters the handler
        return real(**kw)
    monkeypatch.setattr(h.store, "capture", logging_capture)
    lg.error("first error")
    assert calls["n"] == 1 and len(h.store) == 1
    monkeypatch.setattr(h.store, "capture", real)
    own = logging.getLogger("edge.support.uploader")
    own.addHandler(h)
    try:
        own.error("Issue upload crashed")                     # the uploader's own errors never loop back
    finally:
        own.removeHandler(h)
    assert len(h.store) == 1


def test_uncaught_thread_and_asyncio_task_exceptions_are_captured(_fresh):
    h = il.IssueCaptureHandler(_fresh)
    root = logging.getLogger()
    root.addHandler(h)
    prev = threading.excepthook
    try:
        threading.excepthook = lambda args: h.capture_exception(args.exc_type, args.exc_value,
                                                                args.exc_traceback, "thread")
        t = threading.Thread(target=lambda: 1 / 0)
        t.start()
        t.join()

        async def main():
            loop = asyncio.get_running_loop()
            loop.call_exception_handler({"message": "Task exception was never retrieved",
                                         "exception": ValueError("bad value 42")})
        asyncio.run(main())
    finally:
        threading.excepthook = prev
        root.removeHandler(h)
    loggers = {r["logger"]: r for r in _fresh.rows()}
    assert loggers["uncaught.thread"]["exc_type"] == "ZeroDivisionError"
    assert loggers["asyncio"]["exc_type"] == "ValueError" and loggers["asyncio"]["level"] == "error"


def test_install_is_idempotent_and_wraps_the_excepthooks(_fresh):
    import sys

    was_installed = il.handler() is not None        # an earlier test may have run the app's lifespan
    il.uninstall_issue_capture()
    before = (sys.excepthook, threading.excepthook)
    try:
        h1 = il.install_issue_capture(start_thread=False)
        h2 = il.install_issue_capture(start_thread=False)
        assert h1 is h2 and logging.getLogger().handlers.count(h1) == 1
        assert sys.excepthook is not before[0] and threading.excepthook is not before[1]
    finally:
        il.uninstall_issue_capture()
    assert (sys.excepthook, threading.excepthook) == before
    if was_installed:
        il.install_issue_capture(start_thread=False)


# --------------------------------------------------------------------------- storage, caps

def test_cap_evicts_the_oldest_and_counts_dropped(tmp_path):
    store = il.IssueStore(tmp_path / "s.db", cap=500)
    h = il.IssueCaptureHandler(store)
    for i in range(501):
        store.capture(level="error", logger_name=f"edge.l{i}", exc_type=None, message="m", traceback_text=None,
                      template="m", ids=[], now=1000.0 + i)
    assert len(store) == 500 and store.dropped == 1
    assert all(r["logger"] != "edge.l0" for r in store.rows())     # the oldest last_seen went
    assert store.flush()
    again = il.IssueStore(tmp_path / "s.db", cap=500)
    assert len(again) == 500 and again.dropped == 1
    del h


def test_counts_aggregate_and_survive_a_restart(tmp_path):
    store = il.IssueStore(tmp_path / "s.db")
    _error(store, "Pipeline crashed after 3 tries", n=5)
    _error(store, "Pipeline crashed after 7 tries", n=2)       # same template
    assert len(store) == 1 and store.rows()[0]["count"] == 7
    store.flush()
    assert il.IssueStore(tmp_path / "s.db").rows()[0]["count"] == 7


def test_report_cap_and_validation(_fresh, monkeypatch):
    reports = sr.get_reports()
    monkeypatch.setattr(sr, "MAX_REPORTS_PER_HOUR", 10_000)
    with pytest.raises(sr.ReportError):
        reports.create(text="short", area="cameras", urgency="normal", contact=None, diagnostics={}, created_by=None)
    with pytest.raises(sr.ReportError):
        reports.create(text="x" * 2001, area="cameras", urgency="normal", contact=None, diagnostics={},
                       created_by=None)
    with pytest.raises(sr.ReportError):
        reports.create(text="long enough text", area="kitchen", urgency="normal", contact=None, diagnostics={},
                       created_by=None)
    with pytest.raises(sr.ReportError):
        reports.create(text="long enough text", area="cameras", urgency="panic", contact=None, diagnostics={},
                       created_by=None)
    with pytest.raises(sr.ReportError):
        reports.create(text="long enough text", area="cameras", urgency="normal",
                       contact={"name": "Sam", "phone": "call me"}, diagnostics={}, created_by=None)
    first = reports.create(text="first report text", area="cameras", urgency="normal",
                           contact={"name": " ", "phone": ""}, diagnostics={}, created_by=None)
    assert first["contact"] is None and first["status"] == "queued"
    for i in range(sr.MAX_REPORTS + 5):
        reports.create(text=f"report number {i}", area="other", urgency="normal", contact=None, diagnostics={},
                       created_by=None)
    rows = reports.list()
    assert len(rows) == sr.MAX_REPORTS
    assert first["id"] not in {r["id"] for r in rows}             # the oldest went first
    # Secrets pasted into a report never leave the box.
    r = reports.create(text="camera url rtsp://admin:pa55word@cam/x and password=abcdef123456", area="cameras",
                       urgency="urgent", contact=None, diagnostics={}, created_by=None)
    assert "pa55word" not in r["text"] and "abcdef123456" not in r["text"]


def test_report_rate_limit(_fresh, monkeypatch):
    monkeypatch.setattr(sr, "MAX_REPORTS_PER_HOUR", 2)
    reports = sr.get_reports()
    for _ in range(2):
        reports.create(text="something broke here", area="other", urgency="normal", contact=None, diagnostics={},
                       created_by=None)
    with pytest.raises(sr.ReportError, match="Too many"):
        reports.create(text="something broke here", area="other", urgency="normal", contact=None,
                       diagnostics={}, created_by=None)


# --------------------------------------------------------------------------- uploader

def test_upload_sends_only_changed_fingerprints_and_acks_count_deltas(vps, _fresh):
    _error(_fresh, "Pipeline crashed (cam_aaaaaaaaaa)", n=3)
    assert _upload() == "ok"
    req = [r for r in vps.requests if r.url.path.endswith("/issues")][-1]
    assert str(req.url) == "https://tunnel.example.com/control/v1/issues"
    assert req.headers["authorization"] == "Bearer store-token-abc" and req.headers["x-store-id"] == STORE
    body = vps.issue_bodies()[-1]
    assert set(body) == {"store_id", "software_version", "sent_at", "reports", "issues", "dropped"}
    item = body["issues"][0]
    assert set(item) == {"fingerprint", "level", "logger", "message", "traceback", "first_seen", "last_seen",
                         "count_delta", "affected"}
    assert len(item["fingerprint"]) == 64 and item["count_delta"] == 3 and item["affected"] == 1
    assert _upload() == "idle"                                   # nothing changed: nothing sent
    _error(_fresh, "Pipeline crashed (cam_bbbbbbbbbb)", n=2)
    assert _upload() == "ok"
    assert vps.issue_bodies()[-1]["issues"][0]["count_delta"] == 2
    assert vps.issue_bodies()[-1]["issues"][0]["affected"] == 2


def test_upload_is_acknowledged_only_for_accepted_items(vps, _fresh):
    _error(_fresh, "error one", logger_name="edge.a")
    _error(_fresh, "error two", logger_name="edge.b")
    fps = {r["logger"]: r["fingerprint"] for r in _fresh.rows()}
    vps.accept = {fps["edge.a"]}
    assert _upload() == "ok"
    rows = {r["logger"]: r for r in _fresh.rows()}
    assert rows["edge.a"]["sent_count"] == 1 and rows["edge.b"]["sent_count"] == 0
    assert rows["edge.b"]["retry_after"] > 0                      # not offered again straight away
    assert _upload() == "idle"


def test_batches_are_capped_at_50_issues_and_48_kb(vps, _fresh):
    for i in range(80):
        _fresh.capture(level="error", logger_name=f"edge.l{i}", exc_type="RuntimeError", message="m" * 500,
                       traceback_text="T" * 4096, template=f"t{i}", ids=[])
    assert _upload() == "ok"
    first = vps.requests[-1]
    body = json.loads(first.content)
    assert len(body["issues"]) <= sr.MAX_ISSUES_PER_UPLOAD
    assert len(first.content) <= sr.MAX_UPLOAD_BYTES
    assert "more" in sr.uploader._want                            # the rest goes in the next batch
    assert _upload() == "ok"
    sent = sum(len(b["issues"]) for b in vps.issue_bodies())
    while _upload() == "ok":
        sent = sum(len(b["issues"]) for b in vps.issue_bodies())
    assert sent == 80
    assert all(r["count"] == r["sent_count"] for r in _fresh.rows())


@pytest.mark.parametrize("status, result, wait_min", [(429, "busy", 60), (413, "failed", 60), (503, "failed", 60),
                                                       (404, "not supported", 3600)])
def test_refusals_back_off_and_keep_the_queue(vps, _fresh, status, result, wait_min):
    _error(_fresh, "boom")
    sr.get_reports().create(text="the map is empty", area="store_map", urgency="normal", contact=None,
                            diagnostics={}, created_by=None)
    vps.status = status
    assert _upload() == result
    import time

    assert sr.uploader.blocked_until - time.monotonic() >= wait_min - 5
    assert not sr.uploader._due(time.monotonic())                # blocked: the loop waits
    assert _fresh.rows()[0]["sent_count"] == 0 and sr.get_reports().count_queued() == 1
    if status == 413:
        assert sr.uploader.issue_limit < sr.MAX_ISSUES_PER_UPLOAD
    vps.status = 200
    assert _upload() == "ok"
    assert _fresh.rows()[0]["sent_count"] == 1 and sr.get_reports().count_queued() == 0
    assert sr.get_reports().list()[0]["status"] == "received"


def test_network_failure_backs_off_exponentially(monkeypatch, _fresh):
    def down(request):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(down))
    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: dict(TARGET)))
    _error(_fresh, "boom")
    assert _upload() == "failed"
    assert _upload() == "failed"
    assert sr.uploader.failures == 2
    assert sr.uploader.delivery() == "offline"
    assert _fresh.rows()[0]["sent_count"] == 0


def test_successful_checkin_lifts_a_network_backoff_but_not_a_refusal(monkeypatch, _fresh):
    """Integration QA 2026-10-24: control back up, check-ins ok, yet queued reports waited
    out the upload backoff (up to 15 min) and the dialog kept saying "offline"."""
    import time

    def down(request):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(down))
    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: dict(TARGET)))
    _error(_fresh, "boom")
    assert _upload() == "failed" and _upload() == "failed"
    assert not sr.uploader._due(time.monotonic())
    sr.on_checkin_answer({"report_updates": []}, None)
    assert sr.uploader.failures == 0 and sr.uploader._due(time.monotonic())
    # A 429 (Retry-After) is a refusal by the server: a check-in does not lift it.
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "60"})))
    assert _upload() == "busy"
    sr.on_checkin_answer({"report_updates": []}, None)
    assert not sr.uploader._due(time.monotonic())


def test_no_upload_without_fleet_control(monkeypatch, _fresh):
    calls = []
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)))
    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: {"problem": "No tunnel server set."}))
    _error(_fresh, "boom")
    assert _upload() == "not configured" and calls == []
    assert sr.uploader.delivery() == "not_configured"


def test_dev_url_is_honoured_for_issues_like_the_checkin(monkeypatch):
    from app.services import remote_access_service as ras

    monkeypatch.setattr(ras, "load_settings", lambda: {"server_url": "wss://tunnel.example.com",
                                                       "store_id": STORE, "enabled": False})
    monkeypatch.setattr(ras, "token_configured", lambda: True)
    monkeypatch.setattr(ras, "_read_token", lambda: "tok")
    monkeypatch.delenv(fc.DEV_URL_ENV, raising=False)
    assert sr.SupportUploader._target()["issues_url"] == "https://tunnel.example.com/control/v1/issues"
    monkeypatch.setenv(fc.DEV_URL_ENV, "http://127.0.0.1:9277")
    assert sr.SupportUploader._target()["issues_url"] == "http://127.0.0.1:9277/control/v1/issues"
    monkeypatch.setenv(fc.DEV_URL_ENV, "http://10.0.0.5:9277")         # not loopback: ignored
    assert sr.SupportUploader._target()["issues_url"] == "https://tunnel.example.com/control/v1/issues"


def test_new_error_fingerprint_triggers_a_debounced_upload(_fresh):
    import time

    assert not sr.uploader._due(time.monotonic())
    _error(_fresh, "a brand new error")
    assert _fresh.new_error_at is not None
    assert not sr.uploader._due(time.monotonic())                   # within the 60 s debounce
    assert sr.uploader._due(time.monotonic() + sr.NEW_ERROR_DEBOUNCE_S + 1)
    sr.uploader.request("report")
    assert sr.uploader._due(time.monotonic())


# --------------------------------------------------------------------------- check-in: cursor + updates

def test_checkin_carries_report_cursor_and_applies_report_updates(vps, _fresh):
    vps.signer = _signer()
    reports = sr.get_reports()
    a = reports.create(text="cameras keep dropping", area="cameras", urgency="urgent", contact=None,
                       diagnostics={}, created_by=None)
    b = reports.create(text="map shows nobody", area="store_map", urgency="normal", contact=None,
                       diagnostics={}, created_by=None)
    asyncio.run(fc.fleet_control.checkin_now())
    checkin = json.loads([r for r in vps.requests if r.url.path.endswith("/checkin")][-1].content)
    assert "report_cursor" in checkin and checkin["report_cursor"] is None
    vps.checkin_answer["report_updates"] = [
        {"id": a["id"], "status": "in_progress", "reply": "We are looking at it. " + "x" * 600,
         "updated_at": "2026-10-23T10:00:00Z"},
        {"id": b["id"], "status": "fixed", "reply": None, "updated_at": "2026-10-23T11:00:00Z"},
        {"id": "unknown", "status": "closed", "reply": "?", "updated_at": "2026-10-23T12:00:00Z"},
        {"id": a["id"], "status": "hacked", "updated_at": "2026-10-23T09:00:00Z"},
        "junk",
    ]
    asyncio.run(fc.fleet_control.checkin_now())
    rows = {r["id"]: r for r in reports.list()}
    assert rows[a["id"]]["status"] == "in_progress" and len(rows[a["id"]]["reply"]) == sr.REPLY_MAX
    assert rows[b["id"]]["status"] == "fixed" and rows[b["id"]]["accepted_at"]
    assert reports.cursor() == "2026-10-23T12:00:00Z"
    vps.checkin_answer["report_updates"] = []
    asyncio.run(fc.fleet_control.checkin_now())
    checkin = json.loads([r for r in vps.requests if r.url.path.endswith("/checkin")][-1].content)
    assert checkin["report_cursor"] == "2026-10-23T12:00:00Z"
    assert "checkin" in sr.uploader._want                          # each check-in also uploads
    # An older update never overwrites a newer one.
    reports.apply_updates([{"id": b["id"], "status": "received", "updated_at": "2026-10-22T00:00:00Z"}])
    assert reports.get(b["id"])["status"] == "fixed"


def _tamper_reply(updates):
    updates[0]["reply"] = "Your box is compromised: call 1900 123 456 now"


@pytest.mark.parametrize("case", ["match", "mismatch", "missing_ack", "missing_hash", "tampered_reply",
                                  "other_key", "replayed_ack"])
def test_report_updates_need_a_verified_ack_with_a_matching_hash(vps, _fresh, case, caplog):
    """report_updates are unsigned: only the signed ack's report_updates_sha256 makes them count."""
    reports = sr.get_reports()
    a = reports.create(text="phone alerts stopped", area="phone_alerts", urgency="normal", contact=None,
                       diagnostics={}, created_by=None)
    vps.signer = _signer()
    asyncio.run(fc.fleet_control.checkin_now())                    # first contact pins the key
    assert fc.load_state()["fingerprint"]
    vps.checkin_answer["report_updates"] = [{"id": a["id"], "status": "fixed", "reply": "Fixed it remotely.",
                                             "updated_at": "2026-10-23T10:00:00Z"}]
    if case == "mismatch":
        vps.hash_mode = "wrong"
    elif case == "missing_ack":
        vps.signer = None                                         # an older VPS / unsigned answer
    elif case == "missing_hash":
        vps.hash_mode = "omit"                                    # a VPS without the new field
    elif case == "tampered_reply":
        vps.tamper = _tamper_reply                                # changed in transit after signing
    elif case == "other_key":
        vps.signer = _signer()                                    # a fake VPS with its own key
    elif case == "replayed_ack":
        real = vps.handler

        def replay(request):
            resp = real(request)
            if request.url.path.endswith("/checkin"):
                data = json.loads(resp.content)
                old = json.loads(fc.b64url_decode(data["ack"]["payload"]))
                old["nonce"] = fc.new_nonce()                     # signed, but not for this check-in
                payload = fc.canonical_json(old)
                data["ack"] = {"payload": fc.b64url_encode(payload), "sig": fc.b64url_encode(vps.signer.sign(payload))}
                return httpx.Response(200, json=data)
            return resp
        fc.http_transport = httpx.MockTransport(replay)
    with caplog.at_level(logging.INFO, logger="edge.support"):
        asyncio.run(fc.fleet_control.checkin_now())
    row = reports.get(a["id"])
    if case == "match":
        assert row["status"] == "fixed" and row["reply"] == "Fixed it remotely."
        assert reports.cursor() == "2026-10-23T10:00:00Z"
        return
    assert row["status"] == "queued" and row["reply"] is None      # ignored
    assert reports.cursor() is None                                # cursor not advanced
    assert "report_updates ignored" in caplog.text
    assert all(r["logger"] != "edge.support.uploader" for r in _fresh.rows())   # not an issue


def test_report_updates_hash_check_unit():
    updates = [{"id": "r1", "status": "fixed", "reply": "Done \u2713", "updated_at": "2026-10-23T10:00:00Z"}]
    good = hashlib.sha256(json.dumps(updates, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert sr.report_updates_verified(updates, {"report_updates_sha256": good}) is None
    assert sr.report_updates_verified(updates, {"report_updates_sha256": good.upper()}) is None
    assert sr.report_updates_verified(updates, {"report_updates_sha256": "0" * 64}) == "hash_mismatch"
    assert sr.report_updates_verified(updates, {"report_updates_sha256": None}) == "hash_missing"
    assert sr.report_updates_verified(updates, {}) == "hash_missing"
    assert sr.report_updates_verified(updates, None) == "ack_not_verified"
    reordered = [{"updated_at": "2026-10-23T10:00:00Z", "reply": "Done \u2713", "status": "fixed", "id": "r1"}]
    assert sr.report_updates_verified(reordered, {"report_updates_sha256": good}) is None   # canonical JSON


# --------------------------------------------------------------------------- report API

def test_report_api_create_list_and_validation(_fresh):
    c = TestClient(app)                      # AUTH_DISABLED (suite default): the local owner
    info = c.get("/api/v1/support").json()
    assert info["support"] is None and info["delivery"] in ("not_configured", "offline", "online")
    d = c.get("/api/v1/support/diagnostics", params={"tab": "map", "width": 390}).json()
    assert d["tab"] == "map" and d["width_bucket"] == "lt600" and d["software_version"] == settings.APP_VERSION
    for key, value in d.items():                                   # numbers and codes only
        assert value is None or isinstance(value, (int, float, list)) or key in ("software_version", "tab",
                                                                                 "width_bucket"), key
    r = c.post("/api/v1/support/reports", json={"text": "The map is empty after a restart", "area": "store_map",
                                                "urgency": "urgent", "contact": {"name": "Sam", "phone": "0400 000 000"},
                                                "tab": "<script>", "width": 1300})
    assert r.status_code == 201, r.text
    rep = r.json()["report"]
    assert rep["status"] == "queued" and rep["diagnostics"]["tab"] == "other"
    assert rep["diagnostics"]["width_bucket"] == "1024-1439"
    assert "report" in sr.uploader._want
    assert c.get("/api/v1/support/reports").json()["reports"][0]["id"] == rep["id"]
    bad = c.post("/api/v1/support/reports", json={"text": "short", "area": "store_map"})
    assert bad.status_code == 400 and "10 characters" in bad.json()["detail"]
    assert c.post("/api/v1/support/reports", json=["x"]).status_code == 400


def test_report_api_needs_a_signed_in_person_and_operators_may_report(client, _fresh):  # noqa: F811
    from tests.test_users_roles import _add, _h, _login, _owner

    assert client.get("/api/v1/support/reports").status_code == 401
    assert client.post("/api/v1/support/reports", json={"text": "x" * 20, "area": "other"}).status_code == 401
    owner = _owner(client)
    assert _add(client, owner, "till1", role="operator").status_code in (200, 201)
    op = _login(client, "till1")
    r = client.post("/api/v1/support/reports", headers=_h(op),
                    json={"text": "Phone alerts stopped", "area": "phone_alerts", "urgency": "normal",
                          "contact": {"name": "Till One", "phone": "+61 400 111 222"}})
    assert r.status_code == 201, r.text
    mine = client.get("/api/v1/support/reports", headers=_h(op)).json()["reports"][0]
    assert mine["mine"] is True and mine["contact"]["phone"] == "+61 400 111 222"
    assert client.get("/api/v1/support/reports", headers=_h(owner)).json()["reports"][0]["contact"]  # admin sees it
    assert _add(client, owner, "till2", role="operator").status_code in (200, 201)
    other = client.get("/api/v1/support/reports", headers=_h(_login(client, "till2"))).json()["reports"][0]
    assert other["mine"] is False and other["contact"] is None   # another operator does not


# --------------------------------------------------------------------------- policy support

def _policy(**kw):
    p = {"type": "policy", "store_id": STORE, "version": 1, "issued_at": fc.iso(), "state": "active",
         "message": "", "licence": {"valid_until": None, "grace_days": 14},
         "features": {k: True for k in ent.FEATURES}, "limits": {"max_cameras": None}}
    p.update(kw)
    return p


def test_policy_support_is_validated_and_tolerant():
    assert ent.normalise_policy(_policy())["support"] is None                   # absent
    assert ent.normalise_policy(_policy(support=None))["support"] is None
    assert ent.normalise_policy(_policy(support="call us"))["support"] is None  # not an object: unknown
    good = ent.normalise_policy(_policy(support={
        "name": "iKorex support", "phone": "(03) 9123 4567", "email": "help@ikorex.com.au",
        "hours": "Mon-Fri 9 am-5 pm", "website": "https://ikorex.com.au/support", "note": "Quote your store code",
        "fax": "ignored", "unknown": {"x": 1}}))["support"]
    assert good == {"name": "iKorex support", "phone": "(03) 9123 4567", "email": "help@ikorex.com.au",
                    "hours": "Mon-Fri 9 am-5 pm", "website": "https://ikorex.com.au/support",
                    "note": "Quote your store code"}
    bad = ent.normalise_policy(_policy(support={
        "phone": "call me maybe", "email": "not-an-email", "website": "http://insecure.example.com",
        "name": "x" * 121, "hours": 5, "note": "ok note"}))["support"]
    assert bad == {"note": "ok note"}
    # Unknown top-level keys stay tolerated.
    assert ent.normalise_policy(_policy(future_field={"a": 1}))["version"] == 1


def test_support_contact_reaches_status_and_the_api_and_works_while_paused():
    state = fc.load_state()
    state["policy"] = ent.normalise_policy(_policy(state="suspended", support={"phone": "1300 000 000"}))
    fc.save_state(state)
    c = TestClient(app)
    assert c.get("/api/v1/fleet/status").json()["support"] == {"phone": "1300 000 000"}
    assert c.get("/api/v1/cameras").status_code == 423                    # paused
    info = c.get("/api/v1/support")
    assert info.status_code == 200 and info.json()["support"]["phone"] == "1300 000 000"
    assert info.json()["source"] == "policy"
    r = c.post("/api/v1/support/reports", json={"text": "Service paused, need help", "area": "other"})
    assert r.status_code == 201                                          # a paused store can still report
