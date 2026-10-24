"""Box issue log: errors (not all logs) kept for iKorex (docs/SUPPORT_ISSUES_CONTRACT.md §3).

A logging handler (installed at startup, after ``log_redaction``) captures:

* every record at ERROR or CRITICAL, and uncaught exceptions (``sys.excepthook``,
  ``threading.excepthook``; asyncio task exceptions arrive as ERROR records of the
  ``asyncio`` logger);
* an explicit allowlist of important WARNING events (``WARNING_RULES``): disk
  nearly full, inference fell back to the CPU / unavailable, database locked,
  migration / backup failures, and a camera offline for more than 10 minutes.

INFO and DEBUG are never captured. Each record is redacted (``redact_text``),
normalised to a message template and aggregated per **fingerprint** = sha256 of
(logger, exception type, template). Thirty-two cameras failing the same way are
one fingerprint with ``affected`` = 32, not 32 rows.

Storage: a small SQLite file of its own (``STORAGE_DIR/support.db``, never the
main database), at most ``MAX_FINGERPRINTS`` rows (the oldest last_seen is
evicted and counted as ``dropped`` if iKorex never had it). The handler only
touches memory under a lock; a background thread writes the file. Nothing in
the handler blocks the event loop, reads the main database, or raises.
``support_reports`` uploads the changed rows and acknowledges count deltas.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.log_redaction import redact as redact_secrets

# This module's own logger and the uploader's: never captured (recursion guard).
logger = logging.getLogger("edge.support")
OWN_LOGGERS = ("edge.support",)

DB_FILENAME = "support.db"
MAX_FINGERPRINTS = 500
MAX_MESSAGE_CHARS = 500
MAX_TRACEBACK_CHARS = 4096
MAX_FRAMES = 12
MAX_LOGGER_CHARS = 100
MAX_AFFECTED_IDS = 1000
MAX_TEMPLATE_CHARS = 300
# A camera warning counts once the same camera has kept failing this long.
CAMERA_OFFLINE_S = 600.0
# No warning for a camera for this long = it came back; its clock restarts.
CAMERA_GAP_S = 20 * 60.0
MAX_TRACKED_CAMERAS = 2000
NAMES_REFRESH_S = 600.0
FLUSH_DELAY_S = 2.0
# Acknowledged rows not seen for this long are removed from the file.
PRUNE_ACKED_AFTER_S = 30 * 86400.0
LEVEL_RANK = {"warning": 1, "error": 2, "critical": 3}

APP_PACKAGE_DIR = Path(__file__).resolve().parents[1]          # edge_backend/app
_LIB_MARKERS = ("site-packages", "dist-packages")


def _iso(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# =========================================================================== redaction

# Records arrive already through log_redaction, so "<redacted>" may sit inside a URL.
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15})://(?:[^\s\"'<>()\[\]{}]|<redacted(?:-jwt)?>)+")
_URL_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https", "rtmp", "rtmps", "onvif", "ws", "wss", "ftp", "file",
                          "srt", "udp", "tcp"})
_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
_MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_IPV4_RE = re.compile(r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
                      r"(?::\d{1,5})?(?![\w.])")
_IPV6_CANDIDATE_RE = re.compile(r"(?i)(?<![\w:])\[?((?:[0-9a-f]{1,4})?(?:::?[0-9a-f]{1,4}){1,7}(?:::)?|::)\]?"
                                r"(?:%[\w.-]+)?(?![\w:])")
_PATH_RE = re.compile(r"(?<![\w<>/.:~-])(/(?:[\w.@+~-]+/)*[\w.@+~-]+/?)")
# URL paths of this app's own API and pages are kept (they help and name nothing).
_KEEP_PATH_PREFIXES = ("/api/", "/static/", "/stream", "/dashboard", "/control/", "/ws", "/health")
_HOST_RE = re.compile(
    r"(?i)(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|edu|gov|au|nz|uk|io|local|lan|home|internal|arpa|localdomain|cloud|info|biz|co|dev|me|tv|us)"
    r"(?![\w-])")
_QUOTED_RE = re.compile(r"(?<![\w\\])(['\"])(?:(?!\1)[^\n\\]|\\.){0,300}\1(?![\w])")
_SPACE_RE = re.compile(r"[ \t]+")

# Display names never replaced by an id: generic words name no store or person,
# and replacing them would garble every message that uses the word.
_GENERIC_NAMES = frozenset({
    "camera", "cameras", "cam", "store", "shop", "zone", "zones", "area", "areas", "front", "back", "door",
    "entrance", "exit", "office", "aisle", "main", "default", "admin", "administrator", "owner", "operator",
    "user", "users", "test", "none", "null", "true", "false", "error", "online", "offline", "system", "layout",
    "map", "floor", "room", "counter", "checkout", "till", "stock", "storage", "staff", "new", "camera 1",
    "unknown", "other", "inside", "outside", "left", "right", "top", "bottom", "warning", "info", "debug",
})


class NameCache:
    """Known camera / zone / user / store display names -> their id or ``<name>``.

    Filled from the main database by the background thread (``refresh_from_db``)
    every ``NAMES_REFRESH_S``; the logging handler only reads the compiled regex.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._regex: Optional[re.Pattern] = None
        self._map: Dict[str, str] = {}
        self.loaded_at: Optional[float] = None

    def set_names(self, pairs: Iterable[Tuple[str, str]]) -> None:
        mapping: Dict[str, str] = {}
        for name, replacement in pairs:
            if not isinstance(name, str):
                continue
            clean = " ".join(name.split())
            key = clean.lower()
            if len(clean) < 3 or key in _GENERIC_NAMES or clean.isdigit() or len(clean) > 128:
                continue
            if key.startswith("<") or key in mapping:
                continue
            mapping[key] = replacement if isinstance(replacement, str) and replacement else "<name>"
            if len(mapping) >= 3000:
                break
        regex = None
        if mapping:
            parts = sorted(mapping, key=len, reverse=True)
            # Spaces in a name match any run of whitespace.
            alt = "|".join(r"\s+".join(re.escape(w) for w in p.split(" ")) for p in parts)
            regex = re.compile(rf"(?i)(?<![\w])(?:{alt})(?![\w])")
        with self._lock:
            self._regex, self._map, self.loaded_at = regex, mapping, time.monotonic()

    def apply(self, text: str) -> str:
        regex, mapping = self._regex, self._map
        if regex is None or not text:
            return text
        return regex.sub(lambda m: mapping.get(" ".join(m.group(0).split()).lower(), "<name>"), text)

    def refresh_from_db(self, db_path: Path) -> bool:
        """Read the names from the main database (read-only). False when it could not."""
        pairs: List[Tuple[str, str]] = []
        try:
            uri = f"file:{Path(db_path)}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
                def rows(sql: str) -> List[tuple]:
                    try:
                        return list(conn.execute(sql))
                    except sqlite3.Error:
                        return []

                for cid, name, location in rows("SELECT id, name, location FROM cameras"):
                    pairs.append((name, str(cid)))
                    pairs.append((location, "<name>"))
                for zid, name in rows("SELECT id, name FROM store_zones"):
                    pairs.append((name, str(zid)))
                for sid, name in rows("SELECT id, name FROM store_structures"):
                    pairs.append((name, str(sid)))
                for (name,) in rows("SELECT name FROM store_layouts"):
                    pairs.append((name, "<name>"))
                for username, display in rows("SELECT username, display_name FROM admin_users"):
                    pairs.append((username, "<name>"))
                    pairs.append((display, "<name>"))
                for key, value in rows("SELECT key, value FROM system_setup WHERE key IN "
                                       "('device_name', 'site_settings')"):
                    if key == "device_name":
                        pairs.append((value, "<name>"))
                    else:
                        try:
                            data = json.loads(value or "{}")
                        except ValueError:
                            data = {}
                        if isinstance(data, dict):
                            for k in ("STORE_NAME", "store_name"):
                                if isinstance(data.get(k), str):
                                    pairs.append((data[k], "<name>"))
                            store = data.get("store")
                            if isinstance(store, dict) and isinstance(store.get("STORE_NAME"), str):
                                pairs.append((store["STORE_NAME"], "<name>"))
        except sqlite3.Error:
            return False
        self.set_names(pairs)
        return True


names = NameCache()


def _storage_root() -> Optional[str]:
    try:
        from app.config import settings

        return str(Path(settings.STORAGE_DIR).resolve())
    except Exception:  # noqa: BLE001
        return None


def _path_sub(m: re.Match) -> str:
    path = m.group(1)
    if path.startswith(_KEEP_PATH_PREFIXES) or path == "/":
        return path
    rel = app_relative(path)
    if rel:
        return rel
    for marker in _LIB_MARKERS:
        idx = path.find(f"/{marker}/")
        if idx != -1:
            return "<lib>/" + path[idx + len(marker) + 2:]
    storage = _storage_root()
    if storage and (path == storage or path.startswith(storage + "/")):
        return "<storage-path>"
    return "<path>"


def _url_sub(m: re.Match) -> str:
    scheme = m.group(1).lower()
    return f"<{scheme}-url>" if scheme in _URL_SCHEMES else "<url>"


def _ipv6_sub(m: re.Match) -> str:
    try:
        ipaddress.IPv6Address(m.group(1))
    except ValueError:
        return m.group(0)        # e.g. a time 10:00:00
    return "<ip>"


def redact_text(text: Any, name_cache: Optional[NameCache] = None) -> str:
    """Error text safe to leave the box (the contract's redaction list)."""
    if text is None:
        return ""
    out = str(text)
    out = _URL_RE.sub(_url_sub, out)                # every URL, rtsp://user:pass@host included
    out = redact_secrets(out)                       # tokens, keys, passwords (log_redaction)
    out = _EMAIL_RE.sub("<email>", out)
    out = _MAC_RE.sub("<mac>", out)
    out = _IPV4_RE.sub("<ip>", out)
    out = _IPV6_CANDIDATE_RE.sub(_ipv6_sub, out)
    out = _PATH_RE.sub(_path_sub, out)
    out = _HOST_RE.sub("<host>", out)
    out = (name_cache or names).apply(out)          # known display names -> id / <name>
    out = _QUOTED_RE.sub("<q>", out)
    return out


# ======================================================================= fingerprints

# Opaque ids of this app (cam_<hex>, evt_<hex>, zone ids, ...) and uuids.
_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_ID_RE = re.compile(r"\b(?:cam|zone|zn|evt|nw|za|ps|trk|tx|sync|inc|usr|rec|nvr|dev|lay|str|z|c)_[A-Za-z0-9_-]+\b"
                    r"|\b[a-z]{1,8}_[0-9a-f]{6,32}\b")
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
                    r"|\b\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?\b")
_HEX_RE = re.compile(r"(?i)\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,}\b|\b[0-9a-f]{12,}\b")
_NUM_RE = re.compile(r"(?<![A-Za-z_<])[-+]?\d+(?:[.,]\d+)*(?:e[-+]?\d+)?")
# The same failure of many cameras: one template whatever the details.
_CAMERA_COLLAPSE_RE = re.compile(r"^Camera <id> (error|login failed): ([^(;:\n]*)")


def message_template(redacted: str) -> str:
    """The redacted message with everything that varies between occurrences normalised."""
    t = _UUID_RE.sub("<id>", redacted)
    t = _ID_RE.sub("<id>", t)
    t = _TS_RE.sub("<ts>", t)
    t = _HEX_RE.sub("<hex>", t)
    t = _NUM_RE.sub("<n>", t)
    t = _SPACE_RE.sub(" ", t).strip()
    m = _CAMERA_COLLAPSE_RE.match(t)
    if m:
        t = f"Camera <id> {m.group(1)}: {m.group(2).strip()}"
    return t[:MAX_TEMPLATE_CHARS]


def fingerprint(logger_name: str, exc_type: Optional[str], template: str) -> str:
    raw = f"{logger_name}\n{exc_type or ''}\n{template}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def affected_ids(redacted: str) -> List[str]:
    return list(dict.fromkeys(_UUID_RE.findall(redacted) + _ID_RE.findall(redacted)))


def app_relative(filename: str) -> Optional[str]:
    """``app/...`` for a file of this app, else None."""
    if not filename:
        return None
    try:
        full = os.path.abspath(filename)
    except (TypeError, ValueError):
        return None
    base = str(APP_PACKAGE_DIR)
    if full == base or full.startswith(base + os.sep):
        return "app/" + os.path.relpath(full, base).replace(os.sep, "/")
    return None


def format_traceback(exc_type: Any, exc: Any, tb: Any, name_cache: Optional[NameCache] = None) -> Optional[str]:
    """The last ``MAX_FRAMES`` app frames and the redacted exception line, <= 4 KB."""
    if exc_type is None:
        return None
    try:
        frames = traceback.extract_tb(tb) if tb is not None else []
    except Exception:  # noqa: BLE001
        frames = []
    app_frames = [(f, rel) for f in frames if (rel := app_relative(f.filename))]
    skipped = len(frames) - len(app_frames)
    shown = app_frames[-MAX_FRAMES:]
    lines = []
    for f, rel in shown:
        lines.append(f'  File "{rel}", line {f.lineno}, in {f.name}')
        if f.line:
            lines.append("    " + redact_text(f.line.strip()[:200], name_cache))
    head = "Traceback (most recent call last; app frames only"
    if skipped:
        head += f", {skipped} library frame{'s' if skipped != 1 else ''} left out"
    if len(app_frames) > len(shown):
        head += f", {len(app_frames) - len(shown)} older app frame(s) left out"
    head += "):"
    name = getattr(exc_type, "__name__", str(exc_type))
    try:
        detail = str(exc) if exc is not None else ""
    except Exception:  # noqa: BLE001
        detail = ""
    last = name + (f": {redact_text(detail, name_cache)[:MAX_MESSAGE_CHARS]}" if detail else "")
    body = lines
    text = "\n".join([head, *body, last])
    while len(text) > MAX_TRACEBACK_CHARS and body:
        body = body[2:]                 # drop the oldest frame (and its code line)
        text = "\n".join([head, "  ...", *body, last])
    return text[-MAX_TRACEBACK_CHARS:]


# ============================================================================ storage

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS issues (fingerprint TEXT PRIMARY KEY, level TEXT NOT NULL, logger TEXT NOT NULL,"
    " exc_type TEXT, message TEXT NOT NULL, traceback TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
    " last_seen_ts REAL NOT NULL, count INTEGER NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0,"
    " affected TEXT, retry_after REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, area TEXT NOT NULL,"
    " urgency TEXT NOT NULL, text TEXT NOT NULL, contact TEXT, diagnostics TEXT, status TEXT NOT NULL,"
    " reply TEXT, updated_at TEXT, created_by TEXT, accepted_at TEXT)",
)


def connect(path: Path) -> sqlite3.Connection:
    """The support database (issues, reports, meta), created on first use."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    for sql in SCHEMA:
        conn.execute(sql)
    return conn


def default_db_path() -> Path:
    from app.config import settings

    return Path(settings.STORAGE_DIR) / DB_FILENAME


class IssueStore:
    """Fingerprints in memory (the handler's side) and in ``support.db`` (the flusher's)."""

    def __init__(self, path: Path, cap: int = MAX_FINGERPRINTS):
        self.path = Path(path)
        self.cap = cap
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._dirty: set = set()
        self._deleted: set = set()
        self.dropped = 0
        self._dropped_dirty = False
        # Monotonic time of the first ERROR fingerprint not seen before (the
        # uploader sends ~60 s after it); None when there is none.
        self.new_error_at: Optional[float] = None
        self.wake = threading.Event()
        self.load_error: Optional[str] = None
        self.load()

    # ---- file
    def load(self) -> None:
        rows: Dict[str, Dict[str, Any]] = {}
        dropped = 0
        try:
            with closing(connect(self.path)) as conn:
                for r in conn.execute("SELECT fingerprint, level, logger, exc_type, message, traceback, first_seen,"
                                      " last_seen, last_seen_ts, count, sent_count, affected, retry_after"
                                      " FROM issues"):
                    try:
                        aff = json.loads(r[11]) if r[11] else []
                    except ValueError:
                        aff = []
                    rows[r[0]] = {"fingerprint": r[0], "level": r[1], "logger": r[2], "exc_type": r[3],
                                  "message": r[4], "traceback": r[5], "first_seen": r[6], "last_seen": r[7],
                                  "last_seen_ts": float(r[8] or 0), "count": int(r[9] or 0),
                                  "sent_count": int(r[10] or 0),
                                  "affected": {a for a in (aff if isinstance(aff, list) else [])
                                               if isinstance(a, str)},
                                  "retry_after": float(r[12] or 0)}
                row = conn.execute("SELECT value FROM meta WHERE key = 'dropped'").fetchone()
                dropped = int(row[0]) if row and str(row[0]).isdigit() else 0
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.load_error = type(exc).__name__
            # An unreadable file must never stop capturing: start empty, keep the old file aside.
            try:
                if self.path.exists():
                    self.path.rename(self.path.with_suffix(f".broken-{int(time.time())}"))
            except OSError:
                pass
        with self._lock:
            self._rows, self.dropped = rows, dropped
            while len(self._rows) > self.cap:
                self._evict_oldest()

    def flush(self) -> bool:
        """Write changed rows to the file (background thread / tests). False on failure."""
        with self._io_lock:
            with self._lock:
                dirty = [self._export(self._rows[fp]) for fp in self._dirty if fp in self._rows]
                deleted = list(self._deleted)
                dropped = self.dropped if self._dropped_dirty else None
                self._dirty.clear()
                self._deleted.clear()
                self._dropped_dirty = False
            if not dirty and not deleted and dropped is None:
                return True
            try:
                with closing(connect(self.path)) as conn:
                    with conn:
                        conn.executemany(
                            "INSERT INTO issues (fingerprint, level, logger, exc_type, message, traceback, first_seen,"
                            " last_seen, last_seen_ts, count, sent_count, affected, retry_after)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET"
                            " level=excluded.level, logger=excluded.logger, exc_type=excluded.exc_type,"
                            " message=excluded.message, traceback=excluded.traceback, last_seen=excluded.last_seen,"
                            " last_seen_ts=excluded.last_seen_ts, count=excluded.count,"
                            " sent_count=excluded.sent_count, affected=excluded.affected,"
                            " retry_after=excluded.retry_after", dirty)
                        conn.executemany("DELETE FROM issues WHERE fingerprint = ?", [(fp,) for fp in deleted])
                        if dropped is not None:
                            conn.execute("INSERT INTO meta (key, value) VALUES ('dropped', ?) ON CONFLICT(key)"
                                         " DO UPDATE SET value = excluded.value", (str(dropped),))
                return True
            except (sqlite3.Error, OSError):
                with self._lock:            # try again next time
                    self._dirty.update(r[0] for r in dirty)
                    self._deleted.update(deleted)
                    if dropped is not None:
                        self._dropped_dirty = True
                return False

    @staticmethod
    def _export(r: Dict[str, Any]) -> tuple:
        return (r["fingerprint"], r["level"], r["logger"], r["exc_type"], r["message"], r["traceback"],
                r["first_seen"], r["last_seen"], r["last_seen_ts"], r["count"], r["sent_count"],
                json.dumps(sorted(r["affected"])[:MAX_AFFECTED_IDS]), r["retry_after"])

    # ---- the handler's side (memory only)
    def capture(self, *, level: str, logger_name: str, exc_type: Optional[str], message: str,
                traceback_text: Optional[str], template: str, ids: Iterable[str],
                now: Optional[float] = None) -> Tuple[str, bool]:
        """Count one occurrence. Returns (fingerprint, new)."""
        now = time.time() if now is None else now
        logger_name = (logger_name or "root")[:MAX_LOGGER_CHARS]
        fp = fingerprint(logger_name, exc_type, template)
        with self._lock:
            row = self._rows.get(fp)
            new = row is None
            if new:
                row = {"fingerprint": fp, "level": level, "logger": logger_name, "exc_type": exc_type,
                       "message": message, "traceback": traceback_text, "first_seen": _iso(now),
                       "last_seen": _iso(now), "last_seen_ts": now, "count": 0, "sent_count": 0,
                       "affected": set(), "retry_after": 0.0}
                self._rows[fp] = row
            row["count"] += 1
            row["last_seen"], row["last_seen_ts"] = _iso(now), now
            row["message"] = message
            if traceback_text:
                row["traceback"] = traceback_text
            if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(row["level"], 0):
                row["level"] = level
            aff = row["affected"]
            for i in ids:
                if len(aff) >= MAX_AFFECTED_IDS:
                    break
                aff.add(i)
            self._dirty.add(fp)
            if new and LEVEL_RANK.get(level, 0) >= LEVEL_RANK["error"] and self.new_error_at is None:
                self.new_error_at = time.monotonic()
            while len(self._rows) > self.cap:
                self._evict_oldest(keep=fp)
        self.wake.set()
        return fp, new

    def _evict_oldest(self, keep: Optional[str] = None) -> None:
        candidates = [r for fp, r in self._rows.items() if fp != keep]
        if not candidates:
            return
        oldest = min(candidates, key=lambda r: r["last_seen_ts"])
        if oldest["count"] > oldest["sent_count"]:
            self.dropped += 1
            self._dropped_dirty = True
        self._rows.pop(oldest["fingerprint"], None)
        self._dirty.discard(oldest["fingerprint"])
        self._deleted.add(oldest["fingerprint"])

    # ---- the uploader's side
    def pending(self, limit: int, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Changed fingerprints (count not yet acknowledged), most severe and newest first."""
        now = time.time() if now is None else now
        with self._lock:
            rows = [r for r in self._rows.values() if r["count"] > r["sent_count"] and r["retry_after"] <= now]
            rows.sort(key=lambda r: (-LEVEL_RANK.get(r["level"], 0), -r["last_seen_ts"]))
            return [{"fingerprint": r["fingerprint"], "level": r["level"], "logger": r["logger"],
                     "message": r["message"][:MAX_MESSAGE_CHARS],
                     "traceback": (r["traceback"] or None) and r["traceback"][-MAX_TRACEBACK_CHARS:],
                     "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                     "count_delta": r["count"] - r["sent_count"],
                     "affected": len(r["affected"]) or None,
                     "_count": r["count"]} for r in rows[:limit]]

    def acknowledge(self, sent: List[Dict[str, Any]], accepted: Iterable[str], retry_after_s: float,
                    now: Optional[float] = None) -> int:
        """Count deltas the VPS accepted are done; the rest wait ``retry_after_s``."""
        now = time.time() if now is None else now
        ok = set(accepted)
        n = 0
        with self._lock:
            for item in sent:
                row = self._rows.get(item["fingerprint"])
                if row is None:
                    continue
                if item["fingerprint"] in ok:
                    row["sent_count"] = max(row["sent_count"], min(int(item["_count"]), row["count"]))
                    row["retry_after"] = 0.0
                    n += 1
                else:
                    row["retry_after"] = now + retry_after_s
                self._dirty.add(item["fingerprint"])
        self.wake.set()
        return n

    def acknowledge_dropped(self, n: int) -> None:
        with self._lock:
            if n > 0:
                self.dropped = max(0, self.dropped - n)
                self._dropped_dirty = True

    def prune(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            old = [fp for fp, r in self._rows.items()
                   if r["count"] <= r["sent_count"] and now - r["last_seen_ts"] > PRUNE_ACKED_AFTER_S]
            for fp in old:
                self._rows.pop(fp, None)
                self._dirty.discard(fp)
                self._deleted.add(fp)
        return len(old)

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: -r["last_seen_ts"])[:n]
            return [{"fingerprint": r["fingerprint"], "count": r["count"], "level": r["level"]} for r in rows]

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r, affected=len(r["affected"])) for r in self._rows.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


# ======================================================================== the handler

# Important WARNING events (the contract's allowlist). (logger prefix or None, regex on the message, kind).
WARNING_RULES: Tuple[Tuple[Optional[str], re.Pattern, str], ...] = tuple(
    (prefix, re.compile(rx), kind) for prefix, rx, kind in (
        # Disk nearly full: evidence_storage deleting because the disk passed its limit,
        # preflight storage problems, clip_recorder "Disk usage > 90%" (CRITICAL anyway).
        (None, r"(?i)disk (?:usage|space|full)|nearly full|no space left|limited by disk above|"
               r"STORAGE_MAX_DISK_PERCENT", "disk_low"),
        (None, r"^PREFLIGHT WARNING \[storage\]", "disk_low"),
        # Inference fell back to the CPU / no person detection (inference_backend, preflight).
        (None, r"^PREFLIGHT WARNING \[gpu\]", "inference_fallback_cpu"),
        (None, r"^Inference provider \S+ passed over|^Person detection unavailable|(?i:fell back to (?:the )?CPU)",
         "inference_fallback_cpu"),
        # Database locked (database.get_db and anything else that says so).
        (None, r"(?i)database is locked|locked database", "db_locked"),
        # Migrations (edge.migrations warnings that report a problem).
        ("edge.migrations", r"(?i)fail|not moved|not restored|changed after it was applied|does not ship|orphan",
         "migration_failed"),
        (None, r"(?i)\bmigration\b.*\bfail", "migration_failed"),
        # Backups (BackupService, SystemBackups).
        ("BackupService", r"(?i)fail|could not|error", "backup_failed"),
        ("SystemBackups", r"(?i)fail", "backup_failed"),
        (None, r"(?i)\bbackup\b.*\bfail", "backup_failed"),
        # A camera that keeps failing (live_analytics_engine): counted after CAMERA_OFFLINE_S.
        (None, r"^Camera (\S+) (?:error|login failed): ", "camera_offline"),
    ))


class IssueCaptureHandler(logging.Handler):
    """Captures issues into ``IssueStore``; never raises, never blocks on I/O, never recurses."""

    def __init__(self, store: IssueStore, name_cache: Optional[NameCache] = None):
        super().__init__(level=logging.WARNING)
        self.store = store
        self.names = name_cache or names
        self._local = threading.local()
        self._cam_lock = threading.Lock()
        self._cameras: Dict[str, Tuple[float, float]] = {}
        self.clock = time.monotonic          # tests replace it
        self.errors = 0                      # internal failures (never raised)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        self.errors += 1                     # never print or raise from logging

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            self._emit(record)
        except Exception:  # noqa: BLE001 - logging must never fail because of this
            self.errors += 1
        finally:
            self._local.busy = False

    def _classify(self, record: logging.LogRecord, message: str) -> Optional[Tuple[str, Optional[str]]]:
        if record.levelno >= logging.CRITICAL:
            return "critical", None
        if record.levelno >= logging.ERROR:
            return "error", None
        if record.levelno < logging.WARNING:
            return None
        name = record.name or ""
        for prefix, rx, kind in WARNING_RULES:
            if prefix is not None and not (name == prefix or name.startswith(prefix + ".")):
                continue
            m = rx.search(message)
            if not m:
                continue
            if kind == "camera_offline" and not self._camera_long_enough(m.group(1)):
                return None
            return "warning", kind
        return None

    def _camera_long_enough(self, camera_id: str) -> bool:
        now = self.clock()
        with self._cam_lock:
            first, last = self._cameras.get(camera_id, (now, now))
            if now - last > CAMERA_GAP_S:
                first = now                  # it was back in between
            self._cameras[camera_id] = (first, now)
            if len(self._cameras) > MAX_TRACKED_CAMERAS:
                stale = min(self._cameras, key=lambda k: self._cameras[k][1])
                self._cameras.pop(stale, None)
        return now - first >= CAMERA_OFFLINE_S

    def _emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if name.startswith(OWN_LOGGERS) or name.startswith("uvicorn.access"):
            return
        if getattr(record, "_edge_issue", False):
            return                           # the same record reached a second handler of ours
        try:
            record._edge_issue = True        # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format args: use the template as is
            message = str(record.msg)
        verdict = self._classify(record, message)
        if verdict is None:
            return
        level, _kind = verdict
        exc_type_name = None
        tb_text = None
        info = record.exc_info
        if info and isinstance(info, tuple) and info[0] is not None:
            exc_type_name = getattr(info[0], "__name__", None)
            tb_text = format_traceback(info[0], info[1], info[2], self.names)
        self.capture(level=level, logger_name=name, exc_type=exc_type_name, message=message, tb_text=tb_text)

    def capture(self, *, level: str, logger_name: str, exc_type: Optional[str], message: str,
                tb_text: Optional[str]) -> Tuple[str, bool]:
        redacted = redact_text(message, self.names)
        redacted = redacted[:MAX_MESSAGE_CHARS * 4]
        template = message_template(redacted)
        return self.store.capture(level=level, logger_name=logger_name, exc_type=exc_type,
                                  message=redacted[:MAX_MESSAGE_CHARS], traceback_text=tb_text,
                                  template=template, ids=affected_ids(redacted))

    def capture_exception(self, exc_type: Any, exc: Any, tb: Any, where: str) -> None:
        """An uncaught exception (sys / threading excepthook). Never raises."""
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            name = getattr(exc_type, "__name__", "Exception")
            self.capture(level="critical" if where == "main" else "error", logger_name=f"uncaught.{where}",
                         exc_type=name, message=f"Uncaught {name}: {exc}",
                         tb_text=format_traceback(exc_type, exc, tb, self.names))
        except Exception:  # noqa: BLE001
            self.errors += 1
        finally:
            self._local.busy = False


# ===================================================================== install / flush

_handler: Optional[IssueCaptureHandler] = None
_store: Optional[IssueStore] = None
_flusher: Optional[threading.Thread] = None
_stop_flusher = threading.Event()
_prev_hooks: Dict[str, Any] = {}
# Loggers that do not propagate to the root under uvicorn's logging config.
_ATTACH_TO = ("", "uvicorn")


def get_store() -> IssueStore:
    global _store
    if _store is None:
        _store = IssueStore(default_db_path())
    return _store


def set_store(store: Optional[IssueStore]) -> None:
    """Tests: use this store (and its file) instead of STORAGE_DIR/support.db."""
    global _store
    _store = store
    if _handler is not None:
        _handler.store = store if store is not None else get_store()


def handler() -> Optional[IssueCaptureHandler]:
    return _handler


def _main_db_path() -> Optional[Path]:
    try:
        from app.config import settings

        return Path(settings.DATABASE_PATH)
    except Exception:  # noqa: BLE001
        return None


def _flush_loop() -> None:
    next_names = 0.0
    next_prune = 0.0
    while not _stop_flusher.is_set():
        store = get_store()
        woke = store.wake.wait(timeout=60.0)
        if _stop_flusher.is_set():
            break
        if woke:
            store.wake.clear()
            _stop_flusher.wait(FLUSH_DELAY_S)       # let a burst settle into one write
        now = time.monotonic()
        try:
            if now >= next_names:
                db = _main_db_path()
                if db is not None and db.exists():
                    names.refresh_from_db(db)
                next_names = now + NAMES_REFRESH_S
            if now >= next_prune:
                store.prune()
                next_prune = now + 3600.0
            store.flush()
        except Exception:  # noqa: BLE001 - this thread must keep running
            pass
    try:
        get_store().flush()
    except Exception:  # noqa: BLE001
        pass


def install_issue_capture(start_thread: bool = True) -> IssueCaptureHandler:
    """Attach the handler (root + uvicorn), wrap the excepthooks, start the writer. Idempotent."""
    global _handler, _flusher
    store = get_store()
    if _handler is None:
        _handler = IssueCaptureHandler(store)
    for name in _ATTACH_TO:
        lg = logging.getLogger(name)
        if _handler not in lg.handlers:
            lg.addHandler(_handler)

    if "sys" not in _prev_hooks:
        _prev_hooks["sys"] = sys.excepthook

        def _sys_hook(exc_type, exc, tb):
            if _handler is not None and not (isinstance(exc_type, type) and issubclass(exc_type, KeyboardInterrupt)):
                _handler.capture_exception(exc_type, exc, tb, "main")
            _prev_hooks["sys"](exc_type, exc, tb)

        sys.excepthook = _sys_hook
    if "threading" not in _prev_hooks:
        _prev_hooks["threading"] = threading.excepthook

        def _thread_hook(args):
            if _handler is not None and args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
                _handler.capture_exception(args.exc_type, args.exc_value, args.exc_traceback, "thread")
            _prev_hooks["threading"](args)

        threading.excepthook = _thread_hook

    if names.loaded_at is None:
        # Once at startup (not in the handler): later refreshes run on the writer thread.
        db = _main_db_path()
        try:
            if db is not None and db.exists():
                names.refresh_from_db(db)
        except Exception:  # noqa: BLE001
            pass
    if start_thread and (_flusher is None or not _flusher.is_alive()):
        _stop_flusher.clear()
        _flusher = threading.Thread(target=_flush_loop, name="issue-log-writer", daemon=True)
        _flusher.start()
    return _handler


def uninstall_issue_capture() -> None:
    """Detach everything (tests, shutdown). The file keeps what was written."""
    global _handler, _flusher
    if _handler is not None:
        for name in _ATTACH_TO:
            logging.getLogger(name).removeHandler(_handler)
    if "sys" in _prev_hooks:
        sys.excepthook = _prev_hooks.pop("sys")
    if "threading" in _prev_hooks:
        threading.excepthook = _prev_hooks.pop("threading")
    if _flusher is not None:
        _stop_flusher.set()
        if _store is not None:
            _store.wake.set()
        _flusher.join(timeout=5.0)
        _flusher = None
    _handler = None


def flush_now() -> None:
    try:
        get_store().flush()
    except Exception:  # noqa: BLE001
        pass
