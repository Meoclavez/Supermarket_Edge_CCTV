"""Pairing staff phones with this edge device.

Two ways in, one result:

* **QR / code**: the operator opens a pairing session on the dashboard, which
  shows a one-time ``XXXX-XXXX`` code (10 minute TTL) and a QR code carrying
  this device's id, name, the code and the URLs the phone can reach it on.
  The phone claims it with ``POST /api/v1/pairing/claim``.
* **Username / password**: ``POST /api/v1/auth/login`` with a ``phone`` object.

Both end in a ``paired_devices`` row and a token pair whose claims include
``dev`` (this device's id) and ``pd`` (the paired-device id). A token whose
``dev`` differs from this device, or whose paired device has been revoked, is
refused by the auth guards (:func:`phone_claims_valid`), including on refresh.

At rest: pairing codes are stored as an HMAC keyed with this machine's JWT
secret; refresh tokens as a SHA-256. Neither can be recovered from the DB.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import jwt
from sqlalchemy import select, update

from app.config import settings
from app.services import device_identity

logger = logging.getLogger("edge.pairing")

SESSION_TTL_S = 600
ACCESS_TTL_S = 24 * 3600
REFRESH_TTL_S = 90 * 24 * 3600
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
CODE_LEN = 8
# Many wrong codes from anywhere (not just one IP) inside a session's lifetime
# means someone is guessing: every open session is closed.
GLOBAL_FAILURE_LIMIT = 30
MAX_LAN_URLS = 4

SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "HIGH": 2}
PLATFORMS = ("android", "ios")
PUSH_PROVIDERS = ("fcm",)

_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "podman", "lxc", "vmnet",
                           "vboxnet", "kube", "cali", "tun", "tap", "zt", "wg")


# --------------------------------------------------------------------------- hashing

def normalise_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def hash_code(code: str) -> str:
    key = (settings.JWT_SECRET or "").encode("utf-8")
    return hmac.new(key, b"pairing-code:" + normalise_code(code).encode("ascii", "ignore"),
                    hashlib.sha256).hexdigest()


def hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


# --------------------------------------------------------------------------- prefs

def default_prefs() -> dict:
    return {"event_types": None, "min_severity": "INFO", "camera_ids": None, "quiet_hours": None}


def _hhmm(value: Any) -> str:
    s = str(value or "").strip()
    try:
        t = datetime.strptime(s, "%H:%M")
    except ValueError:
        raise ValueError(f"quiet hours time {s!r} must be HH:MM (24 hour)")
    return t.strftime("%H:%M")


def validate_prefs(prefs: Optional[dict]) -> dict:
    """Normalise alert prefs; raises ValueError with an operator-readable message."""
    from app.models.schemas import EventType

    if prefs is None:
        return default_prefs()
    if not isinstance(prefs, dict):
        raise ValueError("alert_prefs must be an object")
    out = default_prefs()
    et = prefs.get("event_types")
    if et is not None:
        if not isinstance(et, list):
            raise ValueError("event_types must be a list or null")
        valid = {e.value for e in EventType}
        bad = [x for x in et if str(x) not in valid]
        if bad:
            raise ValueError(f"unknown event types: {', '.join(map(str, bad))}")
        out["event_types"] = sorted({str(x) for x in et})
    sev = str(prefs.get("min_severity") or "INFO").upper()
    if sev not in SEVERITY_ORDER:
        raise ValueError("min_severity must be INFO, WARNING or HIGH")
    out["min_severity"] = sev
    cams = prefs.get("camera_ids")
    if cams is not None:
        if not isinstance(cams, list):
            raise ValueError("camera_ids must be a list or null")
        out["camera_ids"] = sorted({str(c) for c in cams if str(c).strip()})
    qh = prefs.get("quiet_hours")
    if qh:
        if not isinstance(qh, dict):
            raise ValueError("quiet_hours must be {start, end} or null")
        start, end = _hhmm(qh.get("start")), _hhmm(qh.get("end"))
        if start == end:
            raise ValueError("quiet hours start and end must differ")
        out["quiet_hours"] = {"start": start, "end": end}
    return out


def in_quiet_hours(quiet: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True when ``now`` (device local time) is inside ``[start, end)``; wraps midnight."""
    if not quiet:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    sh, sm = map(int, quiet["start"].split(":"))
    eh, em = map(int, quiet["end"].split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def prefs_filter_reason(prefs: Optional[dict], event_type: str, severity: str,
                        camera_id: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """Why this device should NOT get the alert, or None when it should."""
    p = prefs or default_prefs()
    if p.get("event_types") is not None and event_type not in p["event_types"]:
        return f"event type {event_type} not selected"
    if SEVERITY_ORDER.get(str(severity).upper(), 2) < SEVERITY_ORDER.get(p.get("min_severity") or "INFO", 0):
        return f"severity {severity} below {p.get('min_severity')}"
    if p.get("camera_ids") is not None and camera_id and camera_id not in p["camera_ids"]:
        return f"camera {camera_id} not selected"
    if in_quiet_hours(p.get("quiet_hours"), now):
        qh = p["quiet_hours"]
        return f"quiet hours {qh['start']}-{qh['end']}"
    return None


# --------------------------------------------------------------------------- tokens

def issue_phone_tokens(paired_device_id: str, user_id: Optional[str], role: str = "owner") -> dict:
    """Access + refresh token bound to this device and this paired device."""
    from app.services.auth_service import current_auth_epoch

    now = int(time.time())
    dev = device_identity.current_device_id()
    if not dev:
        raise RuntimeError("device identity is not initialised")
    ep = current_auth_epoch(force=True)
    sub = user_id or f"paired:{paired_device_id}"
    common = {"sub": sub, "role": role, "ep": ep, "dev": dev, "pd": paired_device_id, "iat": now}
    access = dict(common, type="user_session", exp=now + ACCESS_TTL_S)
    refresh = dict(common, type="refresh", exp=now + REFRESH_TTL_S, jti=uuid.uuid4().hex)
    return {
        "access_token": jwt.encode(access, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM),
        "refresh_token": jwt.encode(refresh, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM),
        "token_type": "bearer",
    }


def reissue_phone_access(payload: dict) -> str:
    now = int(time.time())
    access = {k: payload[k] for k in ("sub", "role", "ep", "dev", "pd") if k in payload}
    access.update(type="user_session", iat=now, exp=now + ACCESS_TTL_S)
    return jwt.encode(access, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# --------------------------------------------------------------------------- sync revocation cache

class _Registry:
    """Cheap per-request check that a paired device still exists and is not revoked.

    Reads the row through a short-lived plain sqlite3 connection, cached for a
    few seconds; a revocation made in this process takes effect immediately.
    ``last_seen_at`` is written at most once a minute per device.
    """

    ACTIVE_TTL_S = 5.0
    TOUCH_EVERY_S = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Dict[str, tuple] = {}     # pd -> (active, checked_at)
        self._revoked: set = set()
        self._seen: Dict[str, float] = {}       # pd -> wall time last seen (memory)
        self._written: Dict[str, float] = {}    # pd -> monotonic time of last DB write

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(Path(settings.DATABASE_PATH).resolve()), timeout=1.0)
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def is_active(self, pd: str) -> bool:
        if not pd or pd in self._revoked:
            return False
        now = time.monotonic()
        with self._lock:
            hit = self._active.get(pd)
        if hit and now - hit[1] < self.ACTIVE_TTL_S:
            return hit[0]
        revoked = False
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT revoked_at FROM paired_devices WHERE id = ?", (pd,)).fetchone()
            finally:
                conn.close()
            active = row is not None and row[0] is None
            revoked = row is not None and row[0] is not None
        except Exception as exc:
            # Unreadable DB: keep the last answer if there is one, else refuse.
            logger.warning("paired device lookup failed: %s", exc)
            active = hit[0] if hit else False
        with self._lock:
            self._active[pd] = (active, now)
            if revoked:  # revocation is permanent
                self._revoked.add(pd)
        return active

    def mark_revoked(self, pd: str) -> None:
        with self._lock:
            self._revoked.add(pd)
            self._active[pd] = (False, time.monotonic())

    def forget(self, pd: str) -> None:
        with self._lock:
            self._active.pop(pd, None)
            self._revoked.discard(pd)

    def touch(self, pd: str) -> None:
        now_wall = time.time()
        now = time.monotonic()
        with self._lock:
            self._seen[pd] = now_wall
            last = self._written.get(pd)
            if last is not None and now - last < self.TOUCH_EVERY_S:
                return
            self._written[pd] = now
        try:
            conn = self._connect()
            try:
                conn.execute("UPDATE paired_devices SET last_seen_at = ? WHERE id = ?",
                             (datetime.utcfromtimestamp(now_wall).isoformat(sep=" "), pd))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # never fail a request over a timestamp
            logger.debug("last_seen update skipped: %s", exc)

    def seen_at(self, pd: str) -> Optional[datetime]:
        ts = self._seen.get(pd)
        return datetime.utcfromtimestamp(ts) if ts else None

    def clear(self) -> None:
        with self._lock:
            self._active.clear()
            self._revoked.clear()
            self._seen.clear()
            self._written.clear()


registry = _Registry()


def phone_claims_valid(payload: Optional[dict], touch: bool = True) -> bool:
    """False when a token is bound to another device or to a revoked phone.

    Tokens without ``dev``/``pd`` (dashboard sessions) are unaffected.
    """
    if not payload:
        return False
    dev = payload.get("dev")
    pd = payload.get("pd")
    if dev is None and pd is None:
        return True
    if dev is not None and dev != device_identity.current_device_id():
        return False
    if pd is not None:
        if not registry.is_active(str(pd)):
            return False
        if touch:
            registry.touch(str(pd))
    return True


def refresh_token_matches(pd: str, refresh_token: str) -> bool:
    """The refresh token is the one currently recorded for this (active) phone."""
    try:
        conn = registry._connect()
        try:
            row = conn.execute(
                "SELECT refresh_token_hash, revoked_at FROM paired_devices WHERE id = ?", (pd,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("refresh check failed: %s", exc)
        return False
    if not row or row[1] is not None or not row[0]:
        return False
    return hmac.compare_digest(row[0], hash_refresh(refresh_token))


# --------------------------------------------------------------------------- URLs

def remote_access_settings_sync() -> Optional[dict]:
    try:
        conn = registry._connect()
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = 'remote_access'").fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None


def remote_url() -> Optional[str]:
    """``https://<hostname>`` when remote access is enabled with a hostname, else None."""
    cfg = remote_access_settings_sync()
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    host = str(cfg.get("hostname") or "").strip().strip("/")
    if not host:
        return None
    if host.startswith("http://") or host.startswith("https://"):
        host = host.split("://", 1)[1]
    return f"https://{host}"


def _lan_addresses() -> List[str]:
    try:
        import psutil
    except ImportError:
        return []
    stats = psutil.net_if_stats()
    out: List[tuple] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        st = stats.get(iface)
        if st is not None and not st.isup:
            continue
        for a in addrs:
            if getattr(a.family, "name", "") != "AF_INET":
                continue
            try:
                ip = ipaddress.ip_address(a.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                continue
            out.append((0 if ip.is_private else 1, iface, str(ip)))
    out.sort()
    return [ip for _, _, ip in out]


def lan_urls(request=None) -> List[str]:
    """Base URLs a phone on the store network can use to reach this server.

    Host addresses come from the machine's own interfaces (virtual bridges
    skipped); the port is the one the request arrived on, else ``PORT``.
    """
    port = None
    scheme = "http"
    host_hint = None
    if request is not None:
        server = request.scope.get("server") if hasattr(request, "scope") else None
        if server and len(server) > 1 and server[1]:
            port = int(server[1])
        if request.url.scheme == "https":
            scheme = "https"
        host_hint = request.url.hostname
    port = port or int(settings.PORT)
    urls: List[str] = []
    if host_hint:
        try:
            ip = ipaddress.ip_address(host_hint)
            usable = not (ip.is_loopback or ip.is_unspecified)
        except ValueError:
            usable = host_hint not in ("localhost",) and not host_hint.endswith(".localhost")
        if usable and remote_url() != f"https://{host_hint}":
            urls.append(f"{scheme}://{_bracket(host_hint)}:{port}")
    for ip in _lan_addresses():
        u = f"{scheme}://{ip}:{port}"
        if u not in urls:
            urls.append(u)
    return urls[:MAX_LAN_URLS]


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def reachable_urls(request=None) -> List[str]:
    urls = lan_urls(request)
    r = remote_url()
    if r and r not in urls:
        urls.append(r)
    return urls


def qr_payload(device_id: str, device_name: str, code: str, urls: List[str]) -> str:
    parts = [f"v=1", f"d={quote(device_id, safe='')}", f"n={quote(device_name or '', safe='')}",
             f"c={quote(code, safe='')}"]
    parts += [f"u={quote(u, safe='')}" for u in urls]
    return "edgecctv://pair?" + "&".join(parts)


def qr_svg(payload: str) -> str:
    import io

    import segno

    # A standalone document (with xmlns): usable inline in HTML *and* as an
    # <img> data URI. segno's svg_inline() omits xmlns, which renders as a
    # broken image when used as an image source.
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", scale=5, border=4, dark="#000000",
                                        light="#ffffff", xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode("ascii")


# --------------------------------------------------------------------------- DB operations

def _utcnow() -> datetime:
    return datetime.utcnow()


_global_failures: List[float] = []
_gf_lock = threading.Lock()


def record_global_failure() -> int:
    now = time.monotonic()
    with _gf_lock:
        _global_failures[:] = [t for t in _global_failures if now - t < SESSION_TTL_S]
        _global_failures.append(now)
        return len(_global_failures)


async def create_session(session, created_by: Optional[str], request=None) -> dict:
    """Open a pairing session; any older unused session is closed."""
    from app.models.db_models import PairingSessionModel

    ident = device_identity.get_identity()
    if not ident["device_id"]:
        raise RuntimeError("device identity is not initialised")
    now = _utcnow()
    await session.execute(
        update(PairingSessionModel)
        .where(PairingSessionModel.used_at.is_(None), PairingSessionModel.expires_at > now)
        .values(expires_at=now)
    )
    code = new_code()
    sid = f"ps_{uuid.uuid4().hex[:16]}"
    expires = now + timedelta(seconds=SESSION_TTL_S)
    session.add(PairingSessionModel(id=sid, code_hash=hash_code(code), created_by=created_by,
                                    created_at=now, expires_at=expires))
    await session.commit()
    with _gf_lock:
        _global_failures.clear()
    urls = reachable_urls(request)
    payload = qr_payload(ident["device_id"], ident["device_name"], code, urls)
    return {
        "session_id": sid,
        "code": code,
        "expires_at": expires.isoformat() + "Z",
        "expires_in": SESSION_TTL_S,
        "device_id": ident["device_id"],
        "device_name": ident["device_name"],
        "urls": urls,
        "qr_payload": payload,
        "qr_svg": qr_svg(payload),
    }


async def cancel_session(session, session_id: str) -> bool:
    from app.models.db_models import PairingSessionModel

    now = _utcnow()
    res = await session.execute(
        update(PairingSessionModel)
        .where(PairingSessionModel.id == session_id, PairingSessionModel.used_at.is_(None),
               PairingSessionModel.expires_at > now)
        .values(expires_at=now)
    )
    await session.commit()
    return bool(res.rowcount)


async def close_all_sessions(session) -> None:
    from app.models.db_models import PairingSessionModel

    now = _utcnow()
    await session.execute(
        update(PairingSessionModel)
        .where(PairingSessionModel.used_at.is_(None), PairingSessionModel.expires_at > now)
        .values(expires_at=now)
    )
    await session.commit()


async def consume_code(session, code: str) -> Optional[Any]:
    """Mark the session holding ``code`` used and return it; None if bad/expired/used."""
    from app.models.db_models import PairingSessionModel

    if len(normalise_code(code)) != CODE_LEN:
        return None
    now = _utcnow()
    row = (await session.execute(
        select(PairingSessionModel).where(PairingSessionModel.code_hash == hash_code(code))
    )).scalars().first()
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    # Conditional update: two phones racing on one code -> exactly one wins.
    res = await session.execute(
        update(PairingSessionModel)
        .where(PairingSessionModel.id == row.id, PairingSessionModel.used_at.is_(None))
        .values(used_at=now)
    )
    if not res.rowcount:
        await session.rollback()
        return None
    await session.commit()
    return row


def _clean_phone(phone: dict) -> dict:
    name = " ".join(str(phone.get("name") or "").split())[:128] or "Phone"
    platform = str(phone.get("platform") or "").lower()
    if platform not in PLATFORMS:
        raise ValueError("phone.platform must be 'android' or 'ios'")
    inst = str(phone.get("app_instance_id") or "").strip()
    if not inst or len(inst) > 128:
        raise ValueError("phone.app_instance_id is required (max 128 characters)")
    provider = phone.get("push_provider")
    token = phone.get("push_token")
    if provider is not None and provider not in PUSH_PROVIDERS:
        raise ValueError("phone.push_provider must be 'fcm' or null")
    if token is not None:
        token = str(token).strip()
        if len(token) > 4096:
            raise ValueError("phone.push_token is too long")
        token = token or None
    if token and not provider:
        raise ValueError("phone.push_provider is required with a push_token")
    return {"name": name, "platform": platform, "app_instance_id": inst,
            "push_provider": provider if token else None, "push_token": token}


async def register_phone(session, phone: dict, user_id: Optional[str], paired_via: str,
                         role: str = "owner") -> dict:
    """Create (or refresh) the paired device for this app instance and issue its tokens.

    The same app instance pairing again reuses its active row, so a phone
    never shows up twice. A revoked row is never revived.
    """
    from app.models.db_models import PairedDeviceModel

    p = _clean_phone(phone)
    now = _utcnow()
    row = (await session.execute(
        select(PairedDeviceModel).where(
            PairedDeviceModel.app_instance_id == p["app_instance_id"],
            PairedDeviceModel.revoked_at.is_(None),
        )
    )).scalars().first()
    if row is None:
        row = PairedDeviceModel(
            id=f"pd_{uuid.uuid4().hex[:16]}", name=p["name"], platform=p["platform"],
            app_instance_id=p["app_instance_id"], user_id=user_id, paired_via=paired_via,
            alert_prefs=default_prefs(), created_at=now,
        )
        session.add(row)
    else:
        row.name = p["name"] or row.name
        row.platform = p["platform"]
        row.user_id = user_id or row.user_id
        row.paired_via = paired_via
    if p["push_token"] and p["push_token"] != row.push_token:
        row.push_token_updated_at = now
        row.last_push_status = None
        row.last_push_error = None
    row.push_provider = p["push_provider"]
    row.push_token = p["push_token"]
    row.last_seen_at = now
    tokens = issue_phone_tokens(row.id, user_id, role)
    row.refresh_token_hash = hash_refresh(tokens["refresh_token"])
    await session.commit()
    registry.forget(row.id)
    ident = device_identity.get_identity()
    logger.info("Phone paired: %s (%s, via %s) as %s", row.name, row.platform, paired_via, row.id)
    return {
        "paired_device_id": row.id,
        "device_id": ident["device_id"],
        "device_name": ident["device_name"],
        **tokens,
    }


def device_to_dict(row) -> dict:
    seen_mem = registry.seen_at(row.id)
    last_seen = row.last_seen_at
    if seen_mem and (last_seen is None or seen_mem > last_seen):
        last_seen = seen_mem

    def iso(d):
        return d.isoformat() + "Z" if d else None

    return {
        "id": row.id,
        "name": row.name,
        "platform": row.platform,
        "paired_via": row.paired_via,
        "paired_at": iso(row.created_at),
        "last_seen_at": iso(last_seen),
        "revoked": row.revoked_at is not None,
        "revoked_at": iso(row.revoked_at),
        "push": {
            "provider": row.push_provider,
            "token_registered": bool(row.push_token),
            "token_updated_at": iso(row.push_token_updated_at),
            "last_status": row.last_push_status,
            "last_at": iso(row.last_push_at),
            "last_error": row.last_push_error,
        },
        "alert_prefs": validate_prefs_safe(row.alert_prefs),
    }


def validate_prefs_safe(prefs) -> dict:
    try:
        return validate_prefs(prefs)
    except ValueError:
        return default_prefs()


async def list_devices(session, include_revoked: bool = False) -> List[dict]:
    from app.models.db_models import PairedDeviceModel

    stmt = select(PairedDeviceModel).order_by(PairedDeviceModel.created_at.desc())
    if not include_revoked:
        stmt = stmt.where(PairedDeviceModel.revoked_at.is_(None))
    return [device_to_dict(r) for r in (await session.execute(stmt)).scalars().all()]


async def get_device(session, pd: str):
    from app.models.db_models import PairedDeviceModel

    return (await session.execute(select(PairedDeviceModel).where(PairedDeviceModel.id == pd))).scalar_one_or_none()


async def update_device(session, pd: str, name: Optional[str] = None, alert_prefs: Optional[dict] = None,
                        prefs_given: bool = False) -> Optional[dict]:
    row = await get_device(session, pd)
    if row is None or row.revoked_at is not None:
        return None
    if name is not None:
        cleaned = " ".join(str(name).split())
        if not cleaned or len(cleaned) > 128:
            raise ValueError("Name must be 1-128 characters.")
        row.name = cleaned
    if prefs_given:
        row.alert_prefs = validate_prefs(alert_prefs)
    await session.commit()
    return device_to_dict(row)


async def revoke_device(session, pd: str) -> bool:
    row = await get_device(session, pd)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _utcnow()
    row.push_token = None
    row.refresh_token_hash = None
    await session.commit()
    registry.mark_revoked(pd)
    logger.warning("Paired phone revoked: %s (%s)", row.name, pd)
    return True


async def set_push_token(session, pd: str, provider: Optional[str], token: Optional[str]) -> Optional[dict]:
    row = await get_device(session, pd)
    if row is None or row.revoked_at is not None:
        return None
    if provider is not None and provider not in PUSH_PROVIDERS:
        raise ValueError("push_provider must be 'fcm' or null")
    token = (str(token).strip() or None) if token is not None else None
    if token and not provider:
        raise ValueError("push_provider is required with a push_token")
    if token and len(token) > 4096:
        raise ValueError("push_token is too long")
    if token != row.push_token:
        row.push_token_updated_at = _utcnow()
        row.last_push_status = None
        row.last_push_error = None
    row.push_token = token
    row.push_provider = provider if token else None
    await session.commit()
    return device_to_dict(row)
