"""The till (point-of-sale) ingest key.

A dedicated key for the store's till system, separate from the device's
INTERNAL_SERVICE_KEY (which opens every API). It is accepted only by
``POST /api/v1/analytics/pos/ingest`` in the ``X-Edge-API-Key`` header.

Only a SHA-256 hash of the key is stored, in
``<STORAGE_DIR>/secrets/till_key.json`` (directory 0700, file 0600, atomic
writes, the same handling as ``device_secrets.json``). The key itself is
returned once, when it is created or replaced, and can never be shown again.
The key is 256 bits of randomness, so a plain hash is enough (no password
stretching is needed).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.services import secret_store

logger = logging.getLogger("edge.till_key")

TILL_KEY_FILENAME = "till_key.json"
KEY_PREFIX = "till_"
HEADER_NAME = "X-Edge-API-Key"
# "Last used" is written at most this often (the till may post every sale).
LAST_USED_WRITE_S = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class TillKeyStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_mtime: Optional[float] = None
        self._last_write = 0.0

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from app.config import settings

        return Path(settings.STORAGE_DIR) / secret_store.SECRETS_DIRNAME / TILL_KEY_FILENAME

    # ------------------------------------------------------------ storage
    def _load(self) -> Dict[str, Any]:
        path = self.path
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._cache, self._cache_mtime = {}, None
            return {}
        if self._cache is not None and self._cache_mtime == mtime:
            return self._cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError) as exc:
            logger.error(f"Till key file {path} is unreadable ({exc.__class__.__name__}); treating as no key")
            data = {}
        self._cache, self._cache_mtime = data, mtime
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        path = self.path
        secret_store._ensure_private_dir(path.parent)
        with secret_store._locked(path.parent):
            secret_store._write_store_atomic(path, data)
        self._cache = dict(data)
        try:
            self._cache_mtime = path.stat().st_mtime
        except OSError:
            self._cache_mtime = None

    # ------------------------------------------------------------ public API
    def status(self) -> Dict[str, Any]:
        with self._lock:
            d = self._load()
        exists = bool(d.get("key_sha256"))
        return {
            "exists": exists,
            "created_at": d.get("created_at") if exists else None,
            "created_by": d.get("created_by") if exists else None,
            "last_used_at": d.get("last_used_at") if exists else None,
            "hint": d.get("hint") if exists else None,
            "header": HEADER_NAME,
        }

    def create(self, actor: Optional[str] = None) -> str:
        """Create or replace the key; returns the plain key (the only time it exists)."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        record = {
            "key_sha256": _digest(key),
            "hint": key[-4:],
            "created_at": _now_iso(),
            "created_by": (actor or "")[:120] or None,
            "last_used_at": None,
        }
        with self._lock:
            replaced = bool(self._load().get("key_sha256"))
            self._last_write = 0.0
            self._save({k: v for k, v in record.items() if v is not None})
        logger.warning(f"Till key {'replaced' if replaced else 'created'} by {actor or 'unknown'}")
        return key

    def revoke(self) -> bool:
        with self._lock:
            existed = bool(self._load().get("key_sha256"))
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._cache, self._cache_mtime = {}, None
        if existed:
            logger.warning("Till key revoked")
        return existed

    def verify(self, candidate: Optional[str]) -> bool:
        if not candidate or not isinstance(candidate, str) or not candidate.startswith(KEY_PREFIX):
            return False
        with self._lock:
            d = self._load()
            stored = d.get("key_sha256")
            if not stored or not hmac.compare_digest(stored, _digest(candidate)):
                return False
            now = time.monotonic()
            if now - self._last_write >= LAST_USED_WRITE_S:
                self._last_write = now
                try:
                    self._save({**d, "last_used_at": _now_iso()})
                except OSError as exc:  # never fail a sale over the "last used" stamp
                    logger.warning(f"Could not record till key use: {exc}")
        return True


till_key_store = TillKeyStore()
