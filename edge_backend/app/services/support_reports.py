"""Report a problem + the issue uploader (docs/SUPPORT_ISSUES_CONTRACT.md §2, §3, Transport).

Reports: written by any signed-in person in the dashboard, stored on the box
first (``STORAGE_DIR/support.db``, table ``reports``, at most ``MAX_REPORTS``),
status ``queued`` until the VPS accepts them, then the VPS's status
(``received``, ``in_progress``, ``fixed``, ``closed``) and an optional short
reply, which arrive in the fleet check-in answer (``report_updates``, after the
``report_cursor`` the box sends in the check-in body). They are applied only
when that check-in's signed ack verified and its ``report_updates_sha256``
matches the list (``report_updates_verified``): an unsigned reply could be phishing.

Uploader: ``POST <server>/control/v1/issues`` with the check-in's store token,
store id and URL rules (``fleet_control.FleetControl.target``, including
``FLEET_CONTROL_DEV_URL``). Sends queued reports and changed issue fingerprints
(``issue_log``): at most 50 issues per upload and 48 KB per body, right after a
report (~2 s), ~60 s after a new ERROR fingerprint, and after every check-in.
A count delta or report counts as delivered only when a 200 answer lists it in
``accepted_issues`` / ``accepted_reports``. 429 / 413 / 5xx / no network back
off (60 s -> 15 min); 404 (a VPS without the endpoint yet) waits an hour; the
queue is kept throughout. Nothing is sent while fleet control is not configured.

Only numbers and codes are attached as diagnostics; no pictures, video, camera
or zone names, addresses or people counts leave the box.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.services import issue_log
from app.services.log_redaction import redact as redact_secrets

logger = logging.getLogger("edge.support.uploader")     # under edge.support: never captured

ISSUES_PATH = "/control/v1/issues"
MAX_REPORTS = 200
TEXT_MIN, TEXT_MAX = 10, 2000
REPLY_MAX = 500
CONTACT_NAME_MAX = 80
CONTACT_PHONE_MAX = 32
MAX_REPORTS_PER_HOUR = 20
MAX_UPDATES = 100

AREAS = {
    "cameras": "Cameras", "store_map": "Store map", "alerts": "Alerts", "insights": "Insights",
    "phone_alerts": "Phone alerts", "online_access": "Online access", "other": "Other",
}
URGENCIES = ("normal", "urgent")
VPS_STATUSES = ("received", "in_progress", "fixed", "closed")
STATUSES = ("queued",) + VPS_STATUSES
# Dashboard tabs and browser width buckets the diagnostics may name (codes only).
TABS = ("today", "cameras", "map", "insights", "loss", "settings", "studio", "paused", "other")
WIDTH_BUCKETS = ("lt600", "600-1023", "1024-1439", "ge1440")

MAX_ISSUES_PER_UPLOAD = 50
MIN_ISSUES_PER_UPLOAD = 5
MAX_REPORTS_PER_UPLOAD = 10
MAX_UPLOAD_BYTES = 48 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT_S = 15.0
NEW_ERROR_DEBOUNCE_S = 60.0
REPORT_DELAY_S = 2.0
MORE_DELAY_S = 5.0
TICK_S = 5.0
BACKOFF_MIN_S = 60.0
BACKOFF_MAX_S = 15 * 60.0
NOT_FOUND_BACKOFF_S = 3600.0
# Accepted 200 answer that left a fingerprint out (e.g. the VPS is at its cap): try it again later.
NOT_ACCEPTED_RETRY_S = 6 * 3600.0

# Tests replace this with httpx.MockTransport; None = fleet_control's (None = real network).
http_transport: Optional[httpx.AsyncBaseTransport] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable() or ch in "\n\t")
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").split("\n")]
    out = "\n".join(lines).strip()
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out[:limit]


class ReportError(ValueError):
    """A report the dashboard sent that cannot be kept (plain message for people)."""


# =========================================================================== reports

class ReportStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cursor: Optional[str] = None
        self._cursor_loaded = False
        self._created: List[float] = []

    def _conn(self):
        return closing(issue_log.connect(self.path))

    @staticmethod
    def _row(r: tuple) -> Dict[str, Any]:
        import json

        def j(v):
            try:
                return json.loads(v) if v else None
            except ValueError:
                return None
        return {"id": r[0], "created_at": r[1], "area": r[2], "urgency": r[3], "text": r[4], "contact": j(r[5]),
                "diagnostics": j(r[6]) or {}, "status": r[7], "reply": r[8], "updated_at": r[9],
                "created_by": r[10], "accepted_at": r[11]}

    _COLS = "id, created_at, area, urgency, text, contact, diagnostics, status, reply, updated_at, created_by, accepted_at"

    def create(self, *, text: Any, area: Any, urgency: Any, contact: Any, diagnostics: Dict[str, Any],
               created_by: Optional[str]) -> Dict[str, Any]:
        import json

        body = _clean_text(text, TEXT_MAX + 1)
        if len(body) < TEXT_MIN:
            raise ReportError(f"Please describe what went wrong in at least {TEXT_MIN} characters.")
        if len(body) > TEXT_MAX:
            raise ReportError(f"Please keep the description under {TEXT_MAX} characters.")
        if area not in AREAS:
            raise ReportError("Choose where in the dashboard the problem is.")
        if urgency not in URGENCIES:
            raise ReportError("Choose how urgent the problem is.")
        clean_contact = None
        if contact is not None:
            if not isinstance(contact, dict):
                raise ReportError("The call-back details are not valid.")
            name = _clean_text(contact.get("name"), CONTACT_NAME_MAX + 1).replace("\n", " ")
            phone = _clean_text(contact.get("phone"), CONTACT_PHONE_MAX + 1).replace("\n", " ")
            if len(name) > CONTACT_NAME_MAX:
                raise ReportError(f"Please keep the name under {CONTACT_NAME_MAX} characters.")
            if phone and (len(phone) > CONTACT_PHONE_MAX or sum(c.isdigit() for c in phone) < 6
                          or any(c not in "0123456789+ ()-." for c in phone)):
                raise ReportError("The call-back phone number doesn't look right. Use digits, spaces and +.")
            if name or phone:
                clean_contact = {"name": name, "phone": phone}
        # Passwords, tokens or rtsp://user:pass@ pasted into the text never leave the box.
        body = redact_secrets(body)
        now_mono = time.monotonic()
        with self._lock:
            self._created = [t for t in self._created if now_mono - t < 3600.0]
            if len(self._created) >= MAX_REPORTS_PER_HOUR:
                raise ReportError("Too many reports in the last hour. Please wait a little, or call iKorex support.")
            report = {"id": str(uuid.uuid4()), "created_at": _now_iso(), "area": area, "urgency": urgency,
                      "text": body, "contact": clean_contact, "diagnostics": diagnostics, "status": "queued",
                      "reply": None, "updated_at": None, "created_by": created_by, "accepted_at": None}
            with self._conn() as conn:
                with conn:
                    conn.execute(f"INSERT INTO reports ({self._COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (report["id"], report["created_at"], area, urgency, body,
                                  json.dumps(clean_contact) if clean_contact else None, json.dumps(diagnostics),
                                  "queued", None, None, created_by, None))
                    self._enforce_cap(conn)
            self._created.append(now_mono)
        return report

    @staticmethod
    def _enforce_cap(conn) -> None:
        n = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        extra = n - MAX_REPORTS
        if extra <= 0:
            return
        # Oldest first; reports iKorex already has before ones still waiting to be sent.
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM reports ORDER BY CASE WHEN status = 'queued' THEN 1 ELSE 0 END, created_at, rowid "
            "LIMIT ?", (extra,))]
        conn.executemany("DELETE FROM reports WHERE id = ?", [(i,) for i in ids])

    def list(self, limit: int = MAX_REPORTS) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._COLS} FROM reports ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._COLS} FROM reports WHERE id = ?", (report_id,)).fetchone()
        return self._row(r) if r else None

    def queued(self, limit: int = MAX_REPORTS_PER_UPLOAD) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._COLS} FROM reports WHERE accepted_at IS NULL "
                                "ORDER BY CASE urgency WHEN 'urgent' THEN 0 ELSE 1 END, created_at, rowid LIMIT ?",
                                (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def count_queued(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM reports WHERE accepted_at IS NULL").fetchone()[0])

    def mark_accepted(self, ids: Iterable[str]) -> int:
        ids = [i for i in ids if isinstance(i, str)]
        if not ids:
            return 0
        now = _now_iso()
        with self._lock, self._conn() as conn:
            with conn:
                cur = conn.executemany(
                    "UPDATE reports SET accepted_at = ?, status = CASE WHEN status = 'queued' THEN 'received' "
                    "ELSE status END WHERE id = ? AND accepted_at IS NULL", [(now, i) for i in ids])
        return cur.rowcount if cur.rowcount is not None else 0

    # ---- report_updates from the check-in answer
    def cursor(self) -> Optional[str]:
        if not self._cursor_loaded:
            try:
                with self._conn() as conn:
                    row = conn.execute("SELECT value FROM meta WHERE key = 'report_cursor'").fetchone()
                self._cursor = row[0] if row and row[0] else None
            except Exception:  # noqa: BLE001
                self._cursor = None
            self._cursor_loaded = True
        return self._cursor

    def apply_updates(self, updates: Any) -> int:
        """Apply ``[{id, status, reply, updated_at}]``; returns how many local reports changed."""
        if not isinstance(updates, list):
            return 0
        changed = 0
        newest = self.cursor()
        rows = []
        for u in updates[:MAX_UPDATES]:
            if not isinstance(u, dict):
                continue
            rid, status, updated_at = u.get("id"), u.get("status"), u.get("updated_at")
            if not isinstance(updated_at, str) or not updated_at or len(updated_at) > 40:
                continue
            if newest is None or updated_at > newest:
                newest = updated_at
            if not isinstance(rid, str) or len(rid) > 64 or status not in VPS_STATUSES:
                continue
            reply = u.get("reply")
            reply = _clean_text(reply, REPLY_MAX) or None if isinstance(reply, str) else None
            rows.append((status, reply, updated_at, rid, updated_at))
        with self._lock, self._conn() as conn:
            with conn:
                for status, reply, updated_at, rid, _ in rows:
                    cur = conn.execute(
                        "UPDATE reports SET status = ?, reply = ?, updated_at = ?, "
                        "accepted_at = COALESCE(accepted_at, ?) WHERE id = ? AND (updated_at IS NULL OR updated_at <= ?)",
                        (status, reply, updated_at, _now_iso(), rid, updated_at))
                    changed += cur.rowcount or 0
                if newest and newest != self._cursor:
                    conn.execute("INSERT INTO meta (key, value) VALUES ('report_cursor', ?) ON CONFLICT(key) "
                                 "DO UPDATE SET value = excluded.value", (newest,))
        self._cursor, self._cursor_loaded = newest, True
        return changed


_reports: Optional[ReportStore] = None


def get_reports() -> ReportStore:
    global _reports
    if _reports is None:
        _reports = ReportStore(issue_log.default_db_path())
    return _reports


def set_reports(store: Optional[ReportStore]) -> None:
    global _reports
    _reports = store


def report_cursor() -> Optional[str]:
    try:
        return get_reports().cursor()
    except Exception:  # noqa: BLE001
        return None


# ======================================================================= diagnostics

def width_bucket(value: Any) -> Optional[str]:
    if isinstance(value, str) and value in WIDTH_BUCKETS:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        w = float(value)
        return "lt600" if w < 600 else "600-1023" if w < 1024 else "1024-1439" if w < 1440 else "ge1440"
    return None


def collect_diagnostics(tab: Any = None, width: Any = None) -> Dict[str, Any]:
    """Numbers and codes only (shown to the person before sending). Blocking: call in a thread."""
    from app.config import settings
    from app.services import fleet_control as fc

    out: Dict[str, Any] = {"software_version": settings.APP_VERSION, "uptime_s": None, "app_uptime_s": None,
                           "cameras_online": None, "cameras_total": None, "disk_pct": None}
    try:
        box = fc.box_info()
        out["uptime_s"], out["app_uptime_s"] = box.get("uptime_s"), box.get("app_uptime_s")
    except Exception:  # noqa: BLE001
        pass
    try:
        total, _enabled = fc._camera_counts()
        out["cameras_total"] = total
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.live_analytics_engine import live_engine

        online = live_engine.status().get("cameras_online")
        out["cameras_online"] = int(online) if isinstance(online, (int, float)) else None
    except Exception:  # noqa: BLE001
        pass
    try:
        total_b, used_b, _free = shutil.disk_usage(str(settings.STORAGE_DIR))
        out["disk_pct"] = round(100.0 * used_b / total_b, 1)
    except (OSError, ZeroDivisionError):
        pass
    try:
        out["recent_issues"] = [{"fingerprint": r["fingerprint"], "count": r["count"]}
                                for r in issue_log.get_store().recent(10)]
    except Exception:  # noqa: BLE001
        out["recent_issues"] = []
    out["tab"] = tab if isinstance(tab, str) and tab in TABS else ("other" if tab else None)
    out["width_bucket"] = width_bucket(width)
    return out


# ========================================================================== uploader

class SupportUploader:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._wake_loop = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._want: Dict[str, float] = {}          # reason -> monotonic time it is due
        self.failures = 0
        self.blocked_until = 0.0                   # monotonic
        self.issue_limit = MAX_ISSUES_PER_UPLOAD
        self._report_retry: Dict[str, float] = {}  # report id -> monotonic time it may be offered again
        self.state: Dict[str, Any] = {"last_attempt_at": None, "last_ok_at": None, "last_result": "not run yet",
                                      "last_status": None}

    # ---- plumbing
    def _event(self) -> Optional[asyncio.Event]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._wake
        if self._wake_loop is not loop:
            self._wake, self._wake_loop = asyncio.Event(), loop
        return self._wake

    def _alock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def request(self, reason: str, delay: float = 0.0) -> None:
        """Upload soon: "report" (~2 s), "checkin" (now), "more" (a batch left something over)."""
        due = time.monotonic() + max(0.0, delay)
        if reason not in self._want or self._want[reason] > due:
            self._want[reason] = due
        ev, loop = self._wake, self._wake_loop
        if ev is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass

    def delivery(self) -> str:
        """online | offline | not_configured | not_supported (for the dashboard)."""
        from app.services.fleet_control import fleet_control

        if "problem" in fleet_control.target():
            return "not_configured"
        if self.state["last_result"] == "not supported" and time.monotonic() < self.blocked_until:
            return "not_supported"
        if self.state["last_result"] in ("failed", "busy") and time.monotonic() < self.blocked_until:
            return "offline"
        try:
            from app.services.fleet_control import load_state

            st = load_state()
        except Exception:  # noqa: BLE001
            st = {}
        if st.get("last_result") == "failed":
            return "offline"
        return "online"

    def status(self) -> Dict[str, Any]:
        left = max(0.0, self.blocked_until - time.monotonic())
        return dict(self.state, retry_in_s=int(left) if left else None)

    def checkin_ok(self) -> None:
        """A check-in just reached the same server: a connection-failure backoff no longer
        applies (queued reports go now, not up to 15 min later). 429/413/404 backoffs stay."""
        if self.state.get("last_result") == "failed" and self.state.get("last_status") is None:
            self.failures = 0
            self.blocked_until = 0.0

    def _backoff(self, seconds: Optional[float] = None) -> None:
        self.failures += 1
        wait = seconds if seconds is not None else min(BACKOFF_MIN_S * (2 ** (self.failures - 1)), BACKOFF_MAX_S)
        self.blocked_until = time.monotonic() + wait

    # ---- one upload
    @staticmethod
    def _target() -> Dict[str, Any]:
        from app.services import fleet_control as fc

        tgt = fc.fleet_control.target()
        if "problem" in tgt:
            return tgt
        url = tgt.get("issues_url") or str(tgt["url"])
        if not tgt.get("issues_url") and url.endswith(fc.CHECKIN_PATH):
            url = url[: -len(fc.CHECKIN_PATH)] + ISSUES_PATH
        return dict(tgt, issues_url=url)

    def build_body(self, store_id: str) -> Optional[Dict[str, Any]]:
        """The next batch, or None when there is nothing to send."""
        from app.config import settings
        from app.services.fleet_control import canonical_json

        store = issue_log.get_store()
        now = time.monotonic()
        self._report_retry = {k: t for k, t in self._report_retry.items() if t > now}
        reports = [r for r in get_reports().queued(MAX_REPORTS_PER_UPLOAD + len(self._report_retry))
                   if r["id"] not in self._report_retry][:MAX_REPORTS_PER_UPLOAD]
        issues = store.pending(self.issue_limit)
        dropped = store.dropped
        if not reports and not issues and not dropped:
            return None
        # Redact again at upload time: the camera / user names may have loaded since capture.
        for item in issues:
            item["message"] = issue_log.redact_text(item["message"])[:issue_log.MAX_MESSAGE_CHARS]
        body = {"store_id": store_id, "software_version": settings.APP_VERSION, "sent_at": _now_iso(),
                "reports": [{"id": r["id"], "created_at": r["created_at"], "area": r["area"],
                             "urgency": r["urgency"], "text": r["text"], "contact": r["contact"],
                             "diagnostics": r["diagnostics"]} for r in reports],
                "issues": issues, "dropped": int(dropped)}

        def size() -> int:
            return len(canonical_json({**body, "issues": [{k: v for k, v in i.items() if k != "_count"}
                                                          for i in body["issues"]]}))
        # Too big: first the tracebacks (newest kept), then whole issues, then reports.
        i = len(issues) - 1
        while size() > MAX_UPLOAD_BYTES and i >= 0:
            issues[i]["traceback"] = None
            i -= 1
        while size() > MAX_UPLOAD_BYTES and body["issues"]:
            body["issues"].pop()
        while size() > MAX_UPLOAD_BYTES and len(body["reports"]) > 1:
            body["reports"].pop()
        return body

    async def upload_once(self) -> str:
        """One POST if there is something to send: ok | idle | not configured | failed | ..."""
        async with self._alock():
            return await self._upload()

    async def _upload(self) -> str:
        from app.services import fleet_control as fc

        tgt = self._target()
        self.state["last_attempt_at"] = _now_iso()
        if "problem" in tgt:
            self.state.update(last_result="not configured", last_status=None)
            return "not configured"
        body = await asyncio.to_thread(self.build_body, tgt["store_id"])
        if body is None:
            self.state["last_result"] = "idle" if self.state["last_result"] in ("not run yet", "not configured") \
                else self.state["last_result"]
            return "idle"
        sent_issues = body["issues"]
        wire = {**body, "issues": [{k: v for k, v in i.items() if k != "_count"} for i in sent_issues]}
        headers = {"Authorization": f"Bearer {tgt['token']}", "X-Store-Id": tgt["store_id"],
                   "Content-Type": "application/json"}
        transport = http_transport if http_transport is not None else fc.http_transport
        try:
            async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S,
                                         verify=tgt.get("ca_file") or True) as client:
                resp = await client.post(tgt["issues_url"], content=fc.canonical_json(wire), headers=headers)
        except httpx.HTTPError as exc:
            self._backoff()
            self.state.update(last_result="failed", last_status=None)
            logger.info("Issue upload failed: iKorex server not reachable (%s)", type(exc).__name__)
            return "failed"
        code = resp.status_code
        self.state["last_status"] = code
        if code in (404, 405, 501):
            self.failures = 0
            self.blocked_until = time.monotonic() + NOT_FOUND_BACKOFF_S
            self.state["last_result"] = "not supported"
            logger.info("Issue upload: the iKorex server does not take issues yet (HTTP %d); trying again in 1 h", code)
            return "not supported"
        if code == 413:
            self.issue_limit = max(MIN_ISSUES_PER_UPLOAD, self.issue_limit // 2)
            self._backoff()
            self.state["last_result"] = "failed"
            logger.info("Issue upload refused as too large (HTTP 413); next batch has at most %d issues",
                        self.issue_limit)
            return "failed"
        if code == 429:
            retry = None
            try:
                retry = min(max(float(resp.headers.get("retry-after", "")), BACKOFF_MIN_S), BACKOFF_MAX_S)
            except ValueError:
                retry = None
            self._backoff(retry)
            self.state["last_result"] = "busy"
            logger.info("Issue upload rate limited (HTTP 429)")
            return "busy"
        if code != 200:
            self._backoff()
            self.state["last_result"] = "failed"
            logger.info("Issue upload refused (HTTP %d)", code)
            return "failed"
        try:
            answer = resp.json() if len(resp.content) <= MAX_RESPONSE_BYTES else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            self._backoff()
            self.state["last_result"] = "failed"
            logger.info("Issue upload: the iKorex server's answer is not valid")
            return "failed"

        def ids(key: str) -> List[str]:
            v = answer.get(key)
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

        accepted_reports, accepted_issues = ids("accepted_reports"), ids("accepted_issues")
        sent_report_ids = {r["id"] for r in body["reports"]}
        store = issue_log.get_store()
        await asyncio.to_thread(get_reports().mark_accepted, [i for i in accepted_reports if i in sent_report_ids])
        store.acknowledge(sent_issues, accepted_issues, NOT_ACCEPTED_RETRY_S)
        store.acknowledge_dropped(body["dropped"])
        await asyncio.to_thread(store.flush)
        self.failures = 0
        self.blocked_until = 0.0
        if self.issue_limit < MAX_ISSUES_PER_UPLOAD:
            self.issue_limit = min(MAX_ISSUES_PER_UPLOAD, self.issue_limit * 2)
        self.state.update(last_result="ok", last_ok_at=self.state["last_attempt_at"])
        # A report the VPS did not accept waits before it is offered again.
        later = time.monotonic() + NOT_ACCEPTED_RETRY_S / 36   # 10 min
        for rid in sent_report_ids - set(accepted_reports):
            self._report_retry[rid] = later
        # More left over (over 50 issues, over 10 reports): another batch shortly.
        more = await asyncio.to_thread(self.build_body, tgt["store_id"])
        if more is not None and (more["reports"] or more["issues"]):
            self.request("more", MORE_DELAY_S)
        return "ok"

    # ---- loop
    def _due(self, now: float) -> bool:
        if now < self.blocked_until:
            return False
        if any(t <= now for t in self._want.values()):
            return True
        new_at = issue_log.get_store().new_error_at
        return new_at is not None and now - new_at >= NEW_ERROR_DEBOUNCE_S

    async def _loop(self) -> None:
        while True:
            ev = self._event()
            try:
                await asyncio.wait_for(ev.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass
            ev.clear()
            now = time.monotonic()
            if not self._due(now):
                continue
            self._want = {k: t for k, t in self._want.items() if t > now}
            issue_log.get_store().new_error_at = None
            try:
                await self.upload_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never kill the loop (logged under edge.support: not captured)
                logger.warning("Issue upload crashed", exc_info=True)
                self._backoff()
            await asyncio.sleep(random.uniform(0.0, 0.5))

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._event()
            self._task = asyncio.create_task(self._loop(), name="support-uploader")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.to_thread(issue_log.flush_now)
        except Exception:  # noqa: BLE001
            pass


uploader = SupportUploader()


def report_updates_verified(updates: Any, verified_ack: Optional[Dict[str, Any]]) -> Optional[str]:
    """None when ``updates`` may be applied, else why not (a fixed code).

    The check-in answer is not signed as a whole, so a reply text could be
    spoofed (phishing: "call this number"). The signed ack carries
    ``report_updates_sha256`` = sha256 hex of the canonical JSON of the exact
    list; the updates count only when this check-in's ack verified (signature,
    store, nonce, policy applied: fleet_control.verify_ack / _check_ack) and the
    hash matches. An older VPS without the field: not applied (sent again later).
    """
    import hashlib
    import hmac

    from app.services.fleet_control import canonical_json

    if not isinstance(verified_ack, dict):
        return "ack_not_verified"
    expected = verified_ack.get("report_updates_sha256")
    if not isinstance(expected, str) or not expected:
        return "hash_missing"
    actual = hashlib.sha256(canonical_json(updates)).hexdigest()
    if not hmac.compare_digest(actual, expected.strip().lower()):
        return "hash_mismatch"
    return None


def on_checkin_answer(answer: Dict[str, Any], verified_ack: Optional[Dict[str, Any]] = None) -> None:
    """After a successful check-in: apply signed report_updates, then upload what is waiting."""
    updates = answer.get("report_updates")
    if updates:
        try:
            why = report_updates_verified(updates, verified_ack)
            if why is None:
                get_reports().apply_updates(updates)
            else:
                # INFO under edge.support: never captured as an issue; report_cursor not advanced.
                logger.info("report_updates ignored (%s)", why)
        except Exception:  # noqa: BLE001
            logger.info("report_updates not applied", exc_info=True)
    uploader.checkin_ok()
    uploader.request("checkin")
