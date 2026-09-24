"""Stable identity of this edge device.

Several stores (or several boxes in one store) can run this same software on
one network. A phone must never pair with, or keep talking to, the wrong one,
so every box has a ``device_id`` (uuid4) generated once on first start and
stored in ``system_setup``; it never changes afterwards. Tokens issued to
phones carry it, the pairing QR carries it, and
``GET /api/v1/device/identity`` returns it so a client (or the remote-access
reachability check) can prove a URL really reaches *this* box.

``device_name`` is the operator-editable store name shown on phones.

All functions are synchronous and thread-safe (plain ``sqlite3`` against
``settings.DATABASE_PATH``), so they can be called from request handlers,
worker threads and the auth guards alike.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("edge.identity")

DEVICE_ID_KEY = "device_id"
DEVICE_NAME_KEY = "device_name"
API_VERSION = 1
DEVICE_NAME_MAX = 64

_lock = threading.Lock()
_cache: dict = {}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(settings.DATABASE_PATH).resolve()), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _default_name(device_id: str) -> str:
    return f"Edge CCTV {device_id[:4].upper()}"


def _now() -> str:
    return datetime.utcnow().isoformat(sep=" ")


def ensure_identity() -> dict:
    """Create ``device_id`` / ``device_name`` if absent and cache them.

    Idempotent: ``INSERT OR IGNORE`` means an existing id is never replaced,
    even if two processes start at once.
    """
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO system_setup (key, value, updated_at) VALUES (?, ?, ?)",
                (DEVICE_ID_KEY, str(uuid.uuid4()), _now()),
            )
            device_id = conn.execute(
                "SELECT value FROM system_setup WHERE key = ?", (DEVICE_ID_KEY,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO system_setup (key, value, updated_at) VALUES (?, ?, ?)",
                (DEVICE_NAME_KEY, _default_name(device_id), _now()),
            )
            conn.commit()
            name = conn.execute(
                "SELECT value FROM system_setup WHERE key = ?", (DEVICE_NAME_KEY,)
            ).fetchone()[0]
        finally:
            conn.close()
        _cache.update(device_id=device_id, device_name=name, db=str(settings.DATABASE_PATH))
    return get_identity()


def _cached() -> Optional[dict]:
    # A different DATABASE_PATH (tests switch it) is a different device.
    if _cache.get("device_id") and _cache.get("db") == str(settings.DATABASE_PATH):
        return _cache
    return None


def get_identity() -> dict:
    """``{"device_id", "device_name", "app_version", "api_version"}``.

    ``device_id`` is None only if the database is not initialised yet.
    """
    cached = _cached()
    if cached is None:
        try:
            ensure_identity()
            cached = _cached()
        except Exception as exc:  # table missing before init_db, locked file
            logger.debug("device identity not available yet: %s", exc)
    return {
        "device_id": cached.get("device_id") if cached else None,
        "device_name": cached.get("device_name") if cached else None,
        "app_version": settings.APP_VERSION,
        "api_version": API_VERSION,
    }


def current_device_id() -> Optional[str]:
    return get_identity()["device_id"]


def normalise_device_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("Device name cannot be empty.")
    if len(cleaned) > DEVICE_NAME_MAX:
        raise ValueError(f"Device name must be at most {DEVICE_NAME_MAX} characters.")
    return cleaned


def set_device_name(name: str) -> dict:
    """Rename this device (the store name phones display). Returns the identity."""
    cleaned = normalise_device_name(name)
    ensure_identity()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE system_setup SET value = ?, updated_at = ? WHERE key = ?",
                (cleaned, _now(), DEVICE_NAME_KEY),
            )
            conn.commit()
        finally:
            conn.close()
        _cache["device_name"] = cleaned
    return get_identity()


def _reset_cache_for_tests() -> None:
    _cache.clear()
