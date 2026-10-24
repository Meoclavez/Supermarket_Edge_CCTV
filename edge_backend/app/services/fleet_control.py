"""iKorex fleet control, box side (docs/FLEET_CONTROL_CONTRACT.md).

The box dials out to ``POST https://<tunnel host>/control/v1/checkin`` with the
same store id + token as the frp tunnel and the offline watchdog (no inbound
port, works without online access running). Each check-in carries health and
usage *numbers only* (``ALLOWED_BODY`` is enforced on the way out: no camera,
zone or user names, no addresses, no images, no people counts). The answer may
carry a signed policy and signed commands.

Trust: Ed25519, pinned on first use. The first authenticated check-in stores
the VPS's control public key in ``system_setup['fleet_control']``; a different
key later is refused (Settings shows "Control key changed"), until a local
administrator resets the pin. ``FLEET_CONTROL_PUBLIC_KEY`` in .env overrides
the pin. Policies with a version <= the applied one, policies or commands for
another store, expired commands and command ids already seen are refused.

Offline rule (owner decision 2026-10-21): the last verified policy stays in
force (``entitlements``) and its licence date is evaluated on the box's own
clock. When the policy sets ``licence.offline_grace_hours``, the box stands by
(same effects as paused) once it has had no *verified* contact for longer than
that. Every check-in sends a fresh random ``nonce``; the VPS answers with a
signed ``ack`` (type, store_id, nonce, server_time, policy_version). Contact is
verified only when that ack verifies against the pinned key, names this store
and echoes this nonce, and the policy it names is applied (``verify_ack``).
Unsigned or invalid acks never count, so a fake VPS (DNS hijack) cannot keep a
box out of standby or bring it back.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import platform
import random
import re
import shutil
import signal
import socket
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import settings
from app.services import entitlements

logger = logging.getLogger("edge.fleet_control")
audit_log = logging.getLogger("edge.audit")

STATE_KEY = entitlements.STATE_KEY
CHECKIN_PATH = "/control/v1/checkin"
# Report a problem + box issue log (docs/SUPPORT_ISSUES_CONTRACT.md, services/support_reports.py).
ISSUES_PATH = "/control/v1/issues"
START_DELAY_S = 20.0
INTERVAL_S = 300.0
JITTER_S = 30.0
POKE_DELAY_S = 2.0
BACKOFF_MIN_S = 60.0
BACKOFF_MAX_S = 15 * 60.0
NEXT_CHECKIN_MIN_S = 60.0
NEXT_CHECKIN_MAX_S = 3600.0
HTTP_TIMEOUT_S = 15.0
MAX_BODY_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SEEN_IDS = 200
MAX_RESULTS = 20
MAX_COMMANDS = 20
MAX_AUDIT = 50
RESTART_DEBOUNCE_S = 5.0
RESTART_COMMAND_DELAY_S = 1.0
# Ack server_time further than this from the box clock: still accepted (the
# nonce proves freshness) but recorded as clock_skew and the server time used.
ACK_MAX_SKEW_S = 600.0
NONCE_BYTES = 16
ALLOWED_ACTIONS = ("restart_app", "checkin_now", "run_health_check")
# Started once at boot from the policy; a change needs an app restart.
RESTART_FEATURES = ("heatmaps", "business_insights", "phone_alerts")
_COMMAND_ID_RE = re.compile(r"^c_[A-Za-z0-9_-]{1,64}$")

# Fixed machine codes only: nothing else may appear in health.errors.
ERROR_CODES = frozenset({
    "inference_unavailable", "inference_fallback_cpu", "disk_low", "evidence_over_limit",
    "online_access_error", "backup_failed", "camera_offline", "pipeline_stopped",
    "control_key_changed", "telemetry_partial", "clock_skew",
})

# Every field the check-in may carry (docs/FLEET_CONTROL_CONTRACT.md). A dict
# is a nested object, [x] a list of x, None a scalar (str/int/float/bool/null).
ALLOWED_BODY: Dict[str, Any] = {
    "version": None, "store_id": None, "sent_at": None, "nonce": None,
    "box": {k: None for k in ("software_version", "git_commit", "boot_id", "uptime_s", "app_uptime_s",
                               "hostname_hash", "os", "python")},
    "health": {
        **{k: None for k in ("cpu_pct", "ram_pct", "disk_pct", "disk_free_gb", "gpu_pct", "gpu_mem_pct",
                              "cpu_temp_c", "gpu_temp_c", "inference_provider", "inference_fps",
                              "cameras_total", "cameras_enabled", "cameras_online", "pipeline_state",
                              "last_backup_at")},
        "services": {k: None for k in ("pipeline", "online_access", "phone_alerts", "backups", "night_watch")},
        "errors": [None],
    },
    "usage": {"alerts_24h": {"low": None, "medium": None, "high": None},
              "incidents_24h": None, "users_total": None, "phones_registered": None},
    "policy": {"version_applied": None, "state": None, "licence_status": None, "standby": None,
               "last_verified_contact_age_s": None},
    "control_key_fingerprint": None,
    "command_results": [{"id": None, "status": None, "detail": None, "finished_at": None}],
    # The newest report_updates.updated_at this box has applied (SUPPORT_ISSUES_CONTRACT.md).
    "report_cursor": None,
}
MAX_STRING = 120

# Tests replace this with httpx.MockTransport; None = real network.
http_transport: Optional[httpx.AsyncBaseTransport] = None
# Tests replace this; the default ends the process so systemd starts it again.
restart_hook: Optional[Callable[[], None]] = None

APP_STARTED_AT = time.time()

# Developer / QA only: FLEET_CONTROL_DEV_URL=http://127.0.0.1:<port> sends the
# check-in to a cctv-control running on this machine. Honoured ONLY for a
# loopback host (127.0.0.1, ::1, localhost); anything else is ignored with a
# warning, so a production box can never be pointed at plain HTTP elsewhere.
DEV_URL_ENV = "FLEET_CONTROL_DEV_URL"
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def dev_checkin_url() -> Optional[str]:
    return dev_control_url(CHECKIN_PATH)


def dev_control_url(path: str) -> Optional[str]:
    """FLEET_CONTROL_DEV_URL + ``path`` (loopback only), or None."""
    raw = (os.environ.get(DEV_URL_ENV) or "").strip()
    if not raw:
        return None
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        host, port, parts = "", None, None
    if parts is None or parts.scheme not in ("http", "https") or host not in _LOOPBACK_HOSTS:
        logger.warning("%s ignored: only http(s)://127.0.0.1|localhost|[::1][:port] is allowed", DEV_URL_ENV)
        return None
    netloc = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{netloc}{f':{port}' if port else ''}{path}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- crypto

class Rejected(Exception):
    """A signed item refused, with a fixed machine code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def b64url_decode(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise Rejected("bad_encoding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise Rejected("bad_encoding") from None


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_public_key(value: Any) -> Optional[bytes]:
    """Raw 32-byte Ed25519 key from base64 (standard or url-safe); None if not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(text + "=" * (-len(text) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 32:
            try:
                Ed25519PublicKey.from_public_bytes(raw)
            except ValueError:
                return None
            return raw
    return None


def fingerprint(raw: Optional[bytes]) -> Optional[str]:
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16] if raw else None


def new_nonce() -> str:
    """128 random bits, base64url without padding (22 characters)."""
    return b64url_encode(os.urandom(NONCE_BYTES))


def verify_ack(envelope: Any, raw_key: Optional[bytes], store_id: str, nonce: str,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """The check-in ack's fields if it proves fresh contact with the key holder.

    Raises Rejected: ack_missing, no_control_key, bad_signature / bad_payload /
    bad_encoding / bad_envelope, not_an_ack, wrong_store, wrong_nonce (also a
    replayed ack: every check-in has a new nonce), bad_server_time,
    bad_policy_version. A server_time more than ACK_MAX_SKEW_S away from the box
    clock is accepted, with ``skew_s`` set.
    """
    if envelope is None:
        raise Rejected("ack_missing")
    if raw_key is None:
        raise Rejected("no_control_key")
    ack = verify_envelope(envelope, raw_key)
    if ack.get("type") != "ack":
        raise Rejected("not_an_ack")
    if ack.get("store_id") != store_id:
        raise Rejected("wrong_store")
    got = ack.get("nonce")
    if not isinstance(got, str) or not nonce or not hmac.compare_digest(got.encode(), nonce.encode()):
        raise Rejected("wrong_nonce")
    server_time = parse_iso(ack.get("server_time"))
    if server_time is None:
        raise Rejected("bad_server_time")
    pv = ack.get("policy_version")
    if not isinstance(pv, int) or isinstance(pv, bool) or pv < 0:
        raise Rejected("bad_policy_version")
    skew = ((now or _now()) - server_time).total_seconds()
    # sha256 (hex) of the canonical JSON of this answer's report_updates
    # (SUPPORT_ISSUES_CONTRACT.md): only signed, matching updates are applied.
    ru_hash = ack.get("report_updates_sha256")
    return {"server_time": server_time, "policy_version": pv,
            "skew_s": skew if abs(skew) > ACK_MAX_SKEW_S else None,
            "report_updates_sha256": ru_hash if isinstance(ru_hash, str) else None}


def verify_envelope(envelope: Any, raw_key: bytes) -> Dict[str, Any]:
    """The JSON payload of {"payload","sig"} if Ed25519-signed by ``raw_key``."""
    if not isinstance(envelope, dict):
        raise Rejected("bad_envelope")
    payload = b64url_decode(envelope.get("payload"))
    sig = b64url_decode(envelope.get("sig"))
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(sig, payload)
    except (InvalidSignature, ValueError):
        raise Rejected("bad_signature") from None
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected("bad_payload") from None
    if not isinstance(obj, dict):
        raise Rejected("bad_payload")
    return obj


# --------------------------------------------------------------------------- persisted state

def default_state() -> Dict[str, Any]:
    return {
        "control_public_key": None, "fingerprint": None, "pinned_at": None,
        "key_mismatch": None,          # {"fingerprint", "seen_at"} of a refused new key
        "policy": None, "policy_applied_at": None, "last_policy_error": None,
        "seen_command_ids": [], "pending_results": [],
        "last_checkin_at": None, "last_ok_at": None, "last_result": "not run yet", "last_error": None,
        # Offline standby (entitlements.offline_info): the last check-in whose
        # signed ack verified, the clocks at that moment, the last ack problem.
        "last_verified_contact_at": None, "contact_anchor": None, "clock_skew_s": None,
        "last_ack_error": None,
        "audit": [],
    }


def load_state() -> Dict[str, Any]:
    state = default_state()
    stored = entitlements.read_stored_state()
    state.update({k: stored[k] for k in state if k in stored})
    return state


def save_state(state: Dict[str, Any]) -> None:
    clean = {k: state.get(k) for k in default_state()}
    clean["seen_command_ids"] = list(clean.get("seen_command_ids") or [])[-MAX_SEEN_IDS:]
    clean["pending_results"] = list(clean.get("pending_results") or [])[-MAX_RESULTS:]
    clean["audit"] = list(clean.get("audit") or [])[-MAX_AUDIT:]
    with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=10.0)) as conn:
        conn.execute(
            "INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (STATE_KEY, json.dumps(clean), _now().replace(tzinfo=None).isoformat(sep=" ")),
        )
        conn.commit()
    entitlements.invalidate()


def _audit(state: Dict[str, Any], actor: str, action: str) -> None:
    entry = {"at": iso(), "actor": str(actor)[:128], "action": str(action)[:200]}
    state.setdefault("audit", []).append(entry)
    audit_log.info("fleet control: %s by %s", action, actor)


def refresh_contact_floor(state: Dict[str, Any]) -> None:
    """Persist the time since the last verified contact as a lower bound (clock set back)."""
    anchor = state.get("contact_anchor")
    if isinstance(anchor, dict):
        elapsed = entitlements.contact_elapsed_s(anchor)
        if elapsed is not None:
            state["contact_anchor"] = dict(anchor, floor_s=round(elapsed, 1), floor_boot_id=entitlements.boot_id(),
                                           floor_boottime_s=entitlements.boot_clock())


def env_public_key() -> Optional[bytes]:
    return decode_public_key(getattr(settings, "FLEET_CONTROL_PUBLIC_KEY", "") or "")


# --------------------------------------------------------------------------- telemetry

def filter_allowed(value: Any, schema: Any) -> Any:
    """``value`` reduced to the fields ``schema`` allows (see ALLOWED_BODY)."""
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return None
        return {k: filter_allowed(value[k], schema[k]) for k in schema if k in value}
    if isinstance(schema, list):
        if not isinstance(value, list):
            return []
        return [filter_allowed(v, schema[0]) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING]
    return None


def _read_text(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


_box_static: Optional[Dict[str, Any]] = None


def _git_commit() -> Optional[str]:
    root = Path(__file__).resolve().parents[3] / ".git"
    head = _read_text(str(root / "HEAD"))
    if not head:
        return None
    sha = head
    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1].strip()
        sha = _read_text(str(root / ref)) or ""
        if not sha:
            packed = _read_text(str(root / "packed-refs")) or ""
            for line in packed.splitlines():
                if line.endswith(" " + ref):
                    sha = line.split(" ", 1)[0]
                    break
    return sha[:7] if re.fullmatch(r"[0-9a-f]{40}", sha or "") else None


def _os_name() -> Optional[str]:
    text = _read_text("/etc/os-release") or ""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            # The VPS accepts ^[0-9A-Za-z][0-9A-Za-z .()_+-]{0,47}$ ("Debian GNU/Linux" -> "Debian GNU Linux").
            name = re.sub(r"[^0-9A-Za-z .()_+-]", " ", line.split("=", 1)[1].strip().strip('"'))
            return " ".join(name.split())[:48] or None
    return platform.system() or None


def box_info() -> Dict[str, Any]:
    global _box_static
    if _box_static is None:
        _box_static = {
            "software_version": settings.APP_VERSION,
            "git_commit": _git_commit(),
            "hostname_hash": "sha256-" + hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12],
            "os": _os_name(),
            "python": platform.python_version(),
        }
    uptime = None
    raw = _read_text("/proc/uptime")
    if raw:
        try:
            uptime = int(float(raw.split()[0]))
        except (ValueError, IndexError):
            uptime = None
    boot_id = _read_text("/proc/sys/kernel/random/boot_id")
    return {**_box_static, "boot_id": boot_id if boot_id and re.fullmatch(r"[0-9a-f-]{36}", boot_id) else None,
            "uptime_s": uptime, "app_uptime_s": int(time.time() - APP_STARTED_AT)}


def _temps() -> tuple:
    cpu = gpu = None
    try:
        import psutil

        sensors = psutil.sensors_temperatures() or {}
    except Exception:  # noqa: BLE001
        return None, None
    for name in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
        entries = sensors.get(name) or []
        if entries:
            cpu = max((e.current for e in entries if e.current), default=None)
            break
    for name in ("amdgpu", "nouveau", "nvidia"):
        entries = sensors.get(name) or []
        if entries:
            gpu = entries[0].current
            break
    return (round(cpu, 1) if cpu else None), (round(gpu, 1) if gpu else None)


def _gpu_mem_pct() -> Optional[float]:
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        used, total = _read_text(str(card / "device/mem_info_vram_used")), \
            _read_text(str(card / "device/mem_info_vram_total"))
        try:
            if used and total and int(total) > 0:
                return round(100.0 * int(used) / int(total), 1)
        except ValueError:
            continue
    return None


def _last_backup_at() -> Optional[datetime]:
    try:
        from app.services.backup_service import backup_service

        entries = backup_service.list_backups()
    except Exception:  # noqa: BLE001
        return None
    if not entries:
        return None
    return parse_iso(entries[0].get("timestamp"))


def _usage_counts() -> Dict[str, Any]:
    since = (_now().replace(tzinfo=None) - timedelta(hours=24)).isoformat(sep=" ")
    alerts = {"low": 0, "medium": 0, "high": 0}
    out: Dict[str, Any] = {"alerts_24h": alerts, "incidents_24h": None, "users_total": None,
                           "phones_registered": None}
    with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=5.0)) as conn:
        def one(sql, *args):
            try:
                row = conn.execute(sql, args).fetchone()
                return int(row[0] or 0) if row else 0
            except sqlite3.Error:
                return None
        try:
            for sev, n in conn.execute("SELECT UPPER(severity), COUNT(*) FROM security_events "
                                       "WHERE timestamp >= ? GROUP BY UPPER(severity)", (since,)):
                level = {"HIGH": "high", "CRITICAL": "high", "WARNING": "medium", "MEDIUM": "medium"}.get(sev, "low")
                alerts[level] += int(n)
        except sqlite3.Error:
            out["alerts_24h"] = None
        out["incidents_24h"] = one("SELECT COUNT(*) FROM theft_incidents WHERE timestamp >= ?", since)
        out["users_total"] = one("SELECT COUNT(*) FROM admin_users")
        out["phones_registered"] = one("SELECT COUNT(*) FROM web_push_subscriptions")
    return out


def _camera_counts() -> tuple:
    try:
        with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=5.0)) as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_ai_enabled THEN 1 ELSE 0 END), 0) "
                               "FROM cameras").fetchone()
        return int(row[0]), int(row[1])
    except sqlite3.Error:
        return None, None


def _health_sync() -> Dict[str, Any]:
    """Health numbers (runs in a thread: CPU sampling and disk calls block)."""
    errors: List[str] = []
    h: Dict[str, Any] = {}
    eff = entitlements.effective_state()
    try:
        from app.routes.system import _cpu_percent

        h["cpu_pct"] = _cpu_percent()
    except Exception:  # noqa: BLE001
        h["cpu_pct"] = None
    try:
        import psutil

        h["ram_pct"] = round(float(psutil.virtual_memory().percent), 1)
    except Exception:  # noqa: BLE001
        h["ram_pct"] = None
    try:
        total, used, free = shutil.disk_usage(str(settings.STORAGE_DIR))
        h["disk_pct"] = round(100.0 * used / total, 1)
        h["disk_free_gb"] = round(free / 1024 ** 3, 1)
        if h["disk_pct"] >= 95:
            errors.append("disk_low")
    except OSError:
        h["disk_pct"] = h["disk_free_gb"] = None
    try:
        from app.services.hardware_detector import probe_gpu_utilisation

        h["gpu_pct"] = probe_gpu_utilisation()
    except Exception:  # noqa: BLE001
        h["gpu_pct"] = None
    h["gpu_mem_pct"] = _gpu_mem_pct()
    h["cpu_temp_c"], h["gpu_temp_c"] = _temps()

    pipeline: Dict[str, Any] = {}
    try:
        from app.services.live_analytics_engine import live_engine

        pipeline = live_engine.status()
    except Exception:  # noqa: BLE001
        errors.append("telemetry_partial")
    detector = pipeline.get("detector") or {}
    provider = detector.get("provider")
    h["inference_provider"] = provider if isinstance(provider, str) else None
    if detector and not detector.get("available"):
        errors.append("inference_unavailable")
    elif provider == "CPUExecutionProvider" and h["gpu_pct"] is not None:
        errors.append("inference_fallback_cpu")
    try:
        from app.services.inference_scheduler import inference_scheduler

        h["inference_fps"] = inference_scheduler.status().get("measured_frames_per_s")
    except Exception:  # noqa: BLE001
        h["inference_fps"] = None
    total, enabled = _camera_counts()
    h["cameras_total"], h["cameras_enabled"] = total, enabled
    online = pipeline.get("cameras_online")
    h["cameras_online"] = int(online) if isinstance(online, (int, float)) else None
    running = bool(pipeline.get("running"))
    if eff["paused"]:
        h["pipeline_state"] = "paused"
    elif not running:
        h["pipeline_state"] = "stopped"
        errors.append("pipeline_stopped")
    elif enabled and h["cameras_online"] is not None and h["cameras_online"] < enabled:
        h["pipeline_state"] = "degraded"
        errors.append("camera_offline")
    else:
        h["pipeline_state"] = "running"

    services: Dict[str, str] = {"pipeline": "paused" if eff["paused"] else (
        "degraded" if h["pipeline_state"] == "degraded" else ("ok" if running else "error"))}
    try:
        from app.services.remote_access_service import remote_access_service

        ra = remote_access_service.status()
        if not ra.get("enabled") or not entitlements.feature_safe("online_access"):
            services["online_access"] = "off"
        elif ra.get("process") == "connected":
            services["online_access"] = "ok"
        else:
            services["online_access"] = "error"
            errors.append("online_access_error")
    except Exception:  # noqa: BLE001
        services["online_access"] = "error"
    services["phone_alerts"] = "ok" if entitlements.feature_safe("phone_alerts") else "off"
    services["night_watch"] = "ok" if entitlements.feature_safe("night_watch") else "off"
    last_backup = _last_backup_at()
    h["last_backup_at"] = iso(last_backup) if last_backup else None
    stale = (last_backup is not None and _now() - last_backup > timedelta(hours=50)) or \
        (last_backup is None and time.time() - APP_STARTED_AT > 26 * 3600)
    services["backups"] = "error" if stale else "ok"
    if stale:
        errors.append("backup_failed")
    try:
        from app.services.evidence_storage import evidence_storage

        if (evidence_storage.status() or {}).get("status") == "over_limit":
            errors.append("evidence_over_limit")
    except Exception:  # noqa: BLE001
        pass
    h["services"] = services
    h["errors"] = sorted({e for e in errors if e in ERROR_CODES})
    return h


def _report_cursor() -> Optional[str]:
    try:
        from app.services import support_reports

        return support_reports.report_cursor()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- client

class FleetControl:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._wake_loop = None
        self._poke = False
        self._failures = 0
        self._next_s: Optional[float] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._started = False
        # What the services were started with (main.py lifespan): a later
        # policy that differs in these needs an app restart.
        self.boot: Optional[Dict[str, Any]] = None

    # ---- configuration
    @staticmethod
    def target() -> Dict[str, Any]:
        """Where and how to check in, or {"problem": ...} (idle)."""
        from app.services import remote_access_service as ras

        s = ras.load_settings()
        try:
            server = ras.parse_server_url(s.get("server_url"))
        except Exception:  # noqa: BLE001
            return {"problem": "The tunnel server address in Settings > Online access is not valid."}
        if not server:
            return {"problem": "No tunnel server set (Settings > Online access)."}
        if server.get("protocol") != "wss":
            return {"problem": "iKorex service needs a wss:// tunnel server address."}
        store_id = s.get("store_id") or ""
        token = ras._read_token() if ras.token_configured() else None
        if not store_id or not token:
            return {"problem": "Store ID or store token missing (Settings > Online access)."}
        port = "" if server["port"] == 443 else f":{server['port']}"
        url = dev_checkin_url() or f"https://{server['host']}{port}{CHECKIN_PATH}"
        issues_url = dev_control_url(ISSUES_PATH) if dev_checkin_url() else f"https://{server['host']}{port}{ISSUES_PATH}"
        return {"url": url, "issues_url": issues_url, "store_id": store_id,
                "token": token, "ca_file": ras.find_ca_bundle()}

    # ---- status for the dashboard (no token, no key material beyond the fingerprint)
    def status(self) -> Dict[str, Any]:
        state = load_state()
        eff = entitlements.effective_state()
        tgt = self.target()
        env_key = env_public_key()
        mismatch = state.get("key_mismatch") if isinstance(state.get("key_mismatch"), dict) else None
        off = eff["offline"]
        return {
            "managed": eff["managed"],
            "software_version": settings.APP_VERSION,
            "git_commit": box_info().get("git_commit"),
            "state": eff["state"],
            "paused": eff["paused"],
            "paused_reason": eff["paused_reason"],
            "standby": eff["standby"],
            "offline_grace_hours": off["grace_hours"],
            "last_verified_contact_at": off["last_verified_contact_at"],
            "last_verified_contact_age_s": off["elapsed_s"],
            "standby_in_s": off["remaining_s"],
            "standby_warning": off["warning"],
            "clock_skew_s": off["clock_skew_s"],
            "last_ack_error": state.get("last_ack_error"),
            "message": eff["message"],
            "licence_status": eff["licence"]["status"],
            "licence_valid_until": eff["licence"]["valid_until"],
            "licence_grace_until": eff["licence"]["grace_until"],
            "grace_days": eff["licence"]["grace_days"],
            "features": {k: bool(eff["features"].get(k, True)) for k in entitlements.FEATURES},
            "feature_labels": dict(entitlements.FEATURE_LABELS),
            "limits": eff["limits"],
            "policy_version": eff["version"],
            "policy_applied_at": state.get("policy_applied_at"),
            "last_checkin_at": state.get("last_checkin_at"),
            "last_ok_at": state.get("last_ok_at"),
            "last_result": state.get("last_result"),
            "last_error": state.get("last_error"),
            "configured": "problem" not in tgt,
            "problem": tgt.get("problem"),
            "control_key_fingerprint": fingerprint(env_key) if env_key else state.get("fingerprint"),
            "control_key_source": "env" if env_key else ("pinned" if state.get("control_public_key") else None),
            "key_changed": bool(mismatch),
            "offered_key_fingerprint": mismatch.get("fingerprint") if mismatch else None,
            # The support contact from the signed policy (None: the dashboard uses its built-in one).
            "support": eff.get("support"),
        }

    # ---- loop plumbing (same pattern as offline_watchdog)
    def _event(self) -> Optional[asyncio.Event]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._wake
        if self._wake_loop is not loop:
            self._wake = asyncio.Event()
            self._wake_loop = loop
        return self._wake

    def _alock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def poke(self) -> None:
        """Check in soon (after applying a policy, finishing a command, or on request)."""
        self._poke = True
        ev = self._event()
        if ev is not None and self._wake_loop is not None:
            self._wake_loop.call_soon_threadsafe(ev.set)

    # ---- the check-in
    async def build_body(self, state: Dict[str, Any], store_id: str, nonce: Optional[str] = None) -> Dict[str, Any]:
        eff = entitlements.effective_state()
        try:
            health = await asyncio.to_thread(_health_sync)
        except Exception:  # noqa: BLE001 - send what we have rather than nothing
            logger.exception("fleet health telemetry failed")
            health = {"errors": ["telemetry_partial"]}
        try:
            usage = await asyncio.to_thread(_usage_counts)
        except Exception:  # noqa: BLE001
            usage = {}
        if state.get("key_mismatch"):
            health.setdefault("errors", []).append("control_key_changed")
        if state.get("clock_skew_s") is not None:
            health.setdefault("errors", []).append("clock_skew")
        env_key = env_public_key()
        body = {
            "version": 1, "store_id": store_id, "sent_at": iso(), "nonce": nonce,
            "box": box_info(), "health": health, "usage": usage,
            "policy": {"version_applied": eff["version"], "state": eff["state"],
                       "licence_status": eff["licence"]["status"], "standby": eff["standby"],
                       "last_verified_contact_age_s": eff["offline"]["elapsed_s"]},
            "control_key_fingerprint": fingerprint(env_key) if env_key else state.get("fingerprint"),
            "command_results": list(state.get("pending_results") or [])[-MAX_RESULTS:],
            "report_cursor": await asyncio.to_thread(_report_cursor),
        }
        body = filter_allowed(body, ALLOWED_BODY)
        while len(canonical_json(body)) > MAX_BODY_BYTES and body.get("command_results"):
            body["command_results"] = body["command_results"][1:]
        return body

    async def checkin_now(self) -> Dict[str, Any]:
        async with self._alock():
            return await self._checkin()

    async def _checkin(self) -> Dict[str, Any]:
        state = load_state()
        tgt = self.target()
        state["last_checkin_at"] = iso()
        refresh_contact_floor(state)
        if "problem" in tgt:
            state.update(last_result="not configured", last_error=tgt["problem"])
            await asyncio.to_thread(save_state, state)
            self._next_s = None
            return self.status()
        nonce = new_nonce()
        body = await self.build_body(state, tgt["store_id"], nonce)
        sent_ids = {r.get("id") for r in body.get("command_results") or []}
        headers = {"Authorization": f"Bearer {tgt['token']}", "X-Store-Id": tgt["store_id"],
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(transport=http_transport, timeout=HTTP_TIMEOUT_S,
                                         verify=tgt["ca_file"] or True) as client:
                resp = await client.post(tgt["url"], content=canonical_json(body), headers=headers)
        except httpx.HTTPError as exc:
            return await self._failed(state, f"iKorex server not reachable ({type(exc).__name__})")
        if resp.status_code != 200:
            return await self._failed(state, f"iKorex server refused the check-in (HTTP {resp.status_code})")
        if len(resp.content) > MAX_RESPONSE_BYTES:
            return await self._failed(state, "iKorex server answer too large")
        try:
            answer = resp.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            return await self._failed(state, "iKorex server answer is not valid")

        self._failures = 0
        # Results the VPS now has are done with.
        state["pending_results"] = [r for r in state.get("pending_results") or [] if r.get("id") not in sent_ids]
        state.update(last_ok_at=state["last_checkin_at"], last_result="ok", last_error=None)
        state.pop("_verified_ack", None)
        follow_up = await self._handle_answer(state, answer, tgt["store_id"], nonce)
        verified_ack = state.pop("_verified_ack", None)
        await asyncio.to_thread(save_state, state)
        try:
            # Report statuses / replies from iKorex (only when this check-in's signed ack
            # verified and covers them), then send queued reports and changed issues.
            from app.services import support_reports

            await asyncio.to_thread(support_reports.on_checkin_answer, answer, verified_ack)
        except Exception:  # noqa: BLE001 - support upload must never break the check-in
            logger.info("support report updates not applied", exc_info=True)
        nxt = answer.get("next_checkin_s")
        self._next_s = float(min(max(nxt, NEXT_CHECKIN_MIN_S), NEXT_CHECKIN_MAX_S)) \
            if isinstance(nxt, (int, float)) and not isinstance(nxt, bool) else None
        if follow_up:
            self.poke()
        if state.pop("_standby_lifted", False):
            self.check_restart()  # e.g. "Check connection now": resume without waiting for the loop
        return self.status()

    async def _failed(self, state: Dict[str, Any], message: str) -> Dict[str, Any]:
        self._failures += 1
        state.update(last_result="failed", last_error=message, last_ack_error="no_contact")
        logger.warning("Fleet check-in failed: %s", message)
        await asyncio.to_thread(save_state, state)
        return self.status()

    def _trusted_key(self, state: Dict[str, Any], offered: Optional[bytes]) -> Optional[bytes]:
        """The key to verify with, pinning it on first use; None = refuse everything."""
        env_key = env_public_key()
        if env_key:
            if offered and offered != env_key:
                state["key_mismatch"] = {"fingerprint": fingerprint(offered), "seen_at": iso()}
            else:
                state["key_mismatch"] = None
            return env_key
        pinned = decode_public_key(state.get("control_public_key"))
        if pinned:
            if offered and offered != pinned:
                if (state.get("key_mismatch") or {}).get("fingerprint") != fingerprint(offered):
                    logger.error("Fleet control key changed (%s, pinned %s): refusing policies and commands "
                                 "until an administrator resets the pin", fingerprint(offered), fingerprint(pinned))
                state["key_mismatch"] = {"fingerprint": fingerprint(offered), "seen_at": iso()}
                return None
            state["key_mismatch"] = None
            return pinned
        if offered:
            state.update(control_public_key=base64.b64encode(offered).decode("ascii"),
                         fingerprint=fingerprint(offered), pinned_at=iso(), key_mismatch=None)
            _audit(state, "first check-in", f"control key pinned {fingerprint(offered)}")
            return offered
        return None

    async def _handle_answer(self, state: Dict[str, Any], answer: Dict[str, Any], store_id: str,
                             nonce: Optional[str] = None) -> bool:
        """Apply a verified policy, run verified commands, check the ack. True = check in again soon."""
        key = self._trusted_key(state, decode_public_key(answer.get("control_public_key")))
        follow_up = False
        envelope = answer.get("policy")
        if envelope is not None:
            if key is None:
                state["last_policy_error"] = "control_key_changed" if state.get("key_mismatch") else "no_control_key"
            else:
                try:
                    follow_up |= await self._apply_policy(state, verify_envelope(envelope, key), store_id)
                except (Rejected, entitlements.PolicyError) as exc:
                    state["last_policy_error"] = exc.code
                    logger.warning("Fleet policy refused: %s", exc.code)
        commands = answer.get("commands") or []
        if isinstance(commands, list):
            for envelope in commands[:MAX_COMMANDS]:
                follow_up |= await self._run_command(state, envelope, key, store_id)
        self._check_ack(state, answer.get("ack"), key, store_id, nonce)
        return follow_up

    def _check_ack(self, state: Dict[str, Any], envelope: Any, key: Optional[bytes], store_id: str,
                   nonce: Optional[str]) -> bool:
        """Record a verified contact (the offline standby clock restarts). True = verified."""
        try:
            if key is None and envelope is not None and state.get("key_mismatch"):
                raise Rejected("control_key_changed")
            ack = verify_ack(envelope, key, store_id, nonce or "")
            applied = int((entitlements.stored_policy() or {}).get("version") or 0)
            if ack["policy_version"] > applied:
                # iKorex has a newer policy this box did not apply (refused or not
                # sent): not in step with iKorex, so not a verified contact.
                raise Rejected("policy_not_applied")
        except Rejected as exc:
            if state.get("last_ack_error") != exc.code:
                logger.warning("Fleet check-in ack not verified: %s", exc.code)
            state["last_ack_error"] = exc.code
            return False
        skew = ack["skew_s"]
        when = ack["server_time"] if skew is not None else _now()
        if skew is not None and state.get("clock_skew_s") is None:
            logger.warning("Box clock is %+d s off the iKorex server clock", int(skew))
        if entitlements.effective_state()["standby"]:
            logger.warning("Fleet: verified contact with iKorex again; leaving standby")
            _audit(state, "iKorex (signed ack)", "standby lifted: verified contact")
            state["_standby_lifted"] = True  # not persisted (save_state keeps known keys only)
        state.update(last_verified_contact_at=iso(when), contact_anchor=entitlements.contact_anchor("ack"),
                     clock_skew_s=int(skew) if skew is not None else None, last_ack_error=None)
        state["_verified_ack"] = ack  # not persisted: this check-in's verified ack (report_updates check)
        return True

    async def _apply_policy(self, state: Dict[str, Any], payload: Dict[str, Any], store_id: str) -> bool:
        if payload.get("type") != "policy":
            raise Rejected("not_a_policy")
        if payload.get("store_id") != store_id:
            raise Rejected("wrong_store")
        applied = entitlements.stored_policy()
        applied_version = int((applied or {}).get("version") or 0)
        version = payload.get("version")
        if isinstance(version, int) and not isinstance(version, bool) and version <= applied_version:
            return False  # the VPS may resend the current policy every time: not an error
        policy = entitlements.normalise_policy(payload, store_id)
        before = entitlements.effective_state()
        state.update(policy=policy, policy_applied_at=iso(), last_policy_error=None)
        if policy["licence"].get("offline_grace_hours") and not isinstance(state.get("contact_anchor"), dict):
            # Never a verified contact yet: the grace counts from now (the first
            # policy with an offline limit).
            state["contact_anchor"] = entitlements.contact_anchor("policy")
        await asyncio.to_thread(save_state, state)
        after = entitlements.effective_state()
        logger.info("Fleet policy v%d applied: state=%s licence=%s off=%s", policy["version"], after["state"],
                    after["licence"]["status"], [k for k, v in after["features"].items() if not v])
        await self._after_policy_change(before, after)
        return True

    async def _after_policy_change(self, before: Dict[str, Any], after: Dict[str, Any]) -> None:
        """Apply what is cheap live; restart (debounced) for what was started at boot."""
        if before["features"].get("online_access", True) != after["features"].get("online_access", True):
            try:
                from app.services.remote_access_service import remote_access_service

                await asyncio.to_thread(remote_access_service.apply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Online access not updated for the new policy: %s", exc)
        if before["limits"].get("max_cameras") != after["limits"].get("max_cameras"):
            try:
                from app.services.pipeline_supervisor import pipeline_supervisor

                if getattr(pipeline_supervisor, "_running", False):
                    await asyncio.wait_for(pipeline_supervisor.reconcile_cameras(), timeout=15)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cameras not re-checked for the new camera limit: %s", exc)
        self.check_restart()

    # ---- commands
    async def _run_command(self, state: Dict[str, Any], envelope: Any, key: Optional[bytes], store_id: str) -> bool:
        def result(cid: Optional[str], status: str, detail: str) -> None:
            if cid and _COMMAND_ID_RE.match(cid):
                state.setdefault("pending_results", []).append(
                    {"id": cid, "status": status, "detail": detail, "finished_at": iso()})

        if key is None:
            return False  # nothing to verify with: unknown ids cannot be trusted enough to answer
        try:
            cmd = verify_envelope(envelope, key)
        except Rejected as exc:
            logger.warning("Fleet command refused: %s", exc.code)
            return False
        cid = cmd.get("id") if isinstance(cmd.get("id"), str) else None
        seen = state.setdefault("seen_command_ids", [])
        if cid in seen:
            return False  # replay: already run (and answered); never answer twice
        reason = None
        if cmd.get("type") != "command" or not cid or not _COMMAND_ID_RE.match(cid):
            reason = "bad_command"
        elif cmd.get("store_id") != store_id:
            reason = "wrong_store"
        elif cmd.get("action") not in ALLOWED_ACTIONS:
            reason = "action_not_allowed"
        else:
            not_after = parse_iso(cmd.get("not_after"))
            if not_after is None or not_after <= _now():
                reason = "expired"
        if cid and _COMMAND_ID_RE.match(cid):
            seen.append(cid)
            del seen[:-MAX_SEEN_IDS]
        if reason:
            logger.warning("Fleet command %s refused: %s", cid, reason)
            result(cid, "rejected", reason)
            return True
        action = cmd["action"]
        logger.info("Fleet command %s: %s", cid, action)
        if action == "checkin_now":
            result(cid, "done", "checked_in")
            return True
        if action == "run_health_check":
            try:
                health = await asyncio.to_thread(_health_sync)
                errs = health.get("errors") or []
                result(cid, "done", "ok" if not errs else "issues:" + ",".join(errs))
            except Exception:  # noqa: BLE001
                result(cid, "failed", "health_check_failed")
            return True
        # restart_app: answered by the first check-in after the restart.
        result(cid, "done", "restarted")
        _audit(state, "iKorex (signed command)", f"restart_app {cid}")
        await asyncio.to_thread(save_state, state)
        self.schedule_restart("restart command", delay=RESTART_COMMAND_DELAY_S, force=True)
        return False

    # ---- restart
    def needs_restart(self) -> bool:
        if self.boot is None:
            return False
        eff = entitlements.effective_state()
        if eff["paused"] != self.boot["paused"]:
            return True
        return any(bool(eff["features"].get(k, True)) != bool(self.boot["features"].get(k, True))
                   for k in RESTART_FEATURES)

    def check_restart(self) -> None:
        if self.needs_restart():
            self.schedule_restart("plan changed")

    def schedule_restart(self, reason: str, delay: Optional[float] = None, force: bool = False) -> None:
        if not self._started:
            logger.info("Fleet: restart wanted (%s) but the app is not running under its lifespan", reason)
            return
        if self._restart_task is not None and not self._restart_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = RESTART_DEBOUNCE_S if delay is None else delay
        self._restart_task = loop.create_task(self._restart_later(reason, delay, force), name="fleet-restart")

    async def _restart_later(self, reason: str, delay: float, force: bool) -> None:
        await asyncio.sleep(delay)
        if not force and not self.needs_restart():
            return  # changed back within the debounce
        logger.warning("Fleet control: restarting the app (%s)", reason)
        hook = restart_hook or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        hook()

    # ---- local actions (dashboard)
    async def reset_key(self, actor: str) -> Dict[str, Any]:
        """Forget the pinned key: the next authenticated check-in pins the VPS's key again."""
        async with self._alock():
            state = await asyncio.to_thread(load_state)
            old = state.get("fingerprint")
            state.update(control_public_key=None, fingerprint=None, pinned_at=None, key_mismatch=None)
            _audit(state, actor, f"control key pin reset (was {old or 'none'})")
            await asyncio.to_thread(save_state, state)
        return await asyncio.to_thread(self.status)

    # ---- loop
    async def _loop(self) -> None:
        delay = START_DELAY_S
        while True:
            ev = self._event()
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
                ev.clear()
                if self._poke:
                    await asyncio.sleep(POKE_DELAY_S)  # let a burst settle
            except asyncio.TimeoutError:
                pass
            self._poke = False
            try:
                await self.checkin_now()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("fleet check-in crashed")
                self._failures += 1
            # The licence date may have passed while offline.
            self.check_restart()
            if self._failures:
                delay = min(BACKOFF_MIN_S * (2 ** (self._failures - 1)), BACKOFF_MAX_S)
            else:
                base = self._next_s or INTERVAL_S
                delay = max(1.0, base + random.uniform(-JITTER_S, JITTER_S))
            delay = self._cap_for_standby(delay)

    @staticmethod
    def _cap_for_standby(delay: float) -> float:
        """Wake up when the offline grace runs out, so standby starts on time."""
        try:
            eff = entitlements.effective_state()
        except Exception:  # noqa: BLE001
            return delay
        remaining = eff["offline"]["remaining_s"]
        if eff["standby"] or remaining is None:
            return delay
        return max(1.0, min(delay, remaining + 1.0))

    def record_boot(self) -> Dict[str, Any]:
        eff = entitlements.effective_state()
        self.boot = {"paused": eff["paused"], "features": dict(eff["features"])}
        return self.boot

    async def start(self) -> None:
        self._started = True
        if self.boot is None:
            self.record_boot()
        if self._task is None or self._task.done():
            self._event()
            self._task = asyncio.create_task(self._loop(), name="fleet-checkin")

    async def stop(self) -> None:
        self._started = False
        for task in (self._task, self._restart_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._restart_task = None


fleet_control = FleetControl()
