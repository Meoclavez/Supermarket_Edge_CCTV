"""Server-side sign-out: revoked session tokens, kept until they expire.

JWT sessions are stateless, so without this a token the operator signed out
of stays usable until its ``exp``. ``POST /api/v1/auth/logout`` records the
presented token here (and its login session, ``sid``, which also covers the
paired refresh token and any access token later refreshed from it). Every
session check (``verify_api_access``, ``verify_session_token``, the alert
websocket, ``/auth/refresh``) asks :func:`is_revoked`.

Keys (``revoked_tokens.jti``, at most 64 characters):

* ``jti:<jti>``  -- a token carrying a ``jti`` claim (every token issued now);
* ``h:<sha256>`` -- a token issued before ``jti`` existed (48 hex chars of the
  token's SHA-256), so sessions from an older build can be revoked too;
* ``sid:<sid>``  -- a whole login session (access + refresh tokens).

Rows are written by this process and cached in memory; the cache is reloaded
from the table every ``RELOAD_SEC`` (another worker or a restart sees them).
Expired rows are pruned on every insert and whenever the cache reloads.
The table is created by migration m0011.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger("AuthService")

RELOAD_SEC = 15.0


def token_key(raw_token: str, payload: Mapping[str, Any]) -> str:
    """Revocation key of one token: its ``jti``, else a hash of the token."""
    jti = payload.get("jti") if payload else None
    if isinstance(jti, str) and jti:
        return f"jti:{jti[:56]}"
    return "h:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:48]


def session_key(payload: Mapping[str, Any]) -> Optional[str]:
    sid = payload.get("sid") if payload else None
    return f"sid:{sid[:56]}" if isinstance(sid, str) and sid else None


def keys_for(raw_token: str, payload: Mapping[str, Any]) -> List[str]:
    keys = [token_key(raw_token, payload)]
    sk = session_key(payload)
    if sk:
        keys.append(sk)
    return keys


class TokenRevocations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: Dict[str, int] = {}      # key -> expires_at (unix seconds)
        self._loaded_at = 0.0
        self._db_path: Optional[str] = None

    # ---------------------------------------------------------------- storage

    @staticmethod
    def _current_db() -> str:
        from app.config import settings

        return str(Path(settings.DATABASE_PATH).resolve())

    def _reload_locked(self, now_wall: int) -> None:
        db = self._current_db()
        if db != self._db_path:
            # Another database (tests): nothing cached applies to it.
            self._revoked.clear()
            self._db_path = db
        self._loaded_at = time.monotonic()
        self._revoked = {k: e for k, e in self._revoked.items() if e > now_wall}
        if not Path(db).exists():
            return
        try:
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)) as conn:
                rows = conn.execute(
                    "SELECT jti, expires_at FROM revoked_tokens WHERE expires_at > ?", (now_wall,)
                ).fetchall()
        except sqlite3.Error:
            # Table not migrated yet or the file is busy: keep what is cached.
            return
        for key, exp in rows:
            try:
                self._revoked[str(key)] = max(int(exp), self._revoked.get(str(key), 0))
            except (TypeError, ValueError):
                continue

    def _maybe_reload(self) -> None:
        now_wall = int(time.time())
        with self._lock:
            if self._db_path != self._current_db() or time.monotonic() - self._loaded_at >= RELOAD_SEC:
                self._reload_locked(now_wall)

    # ------------------------------------------------------------------ API

    def is_revoked(self, raw_token: str, payload: Mapping[str, Any]) -> bool:
        if not raw_token or not payload:
            return False
        self._maybe_reload()
        now_wall = int(time.time())
        with self._lock:
            for key in keys_for(raw_token, payload):
                exp = self._revoked.get(key)
                if exp is not None and exp > now_wall:
                    return True
        return False

    def revoke(self, entries: Iterable[Tuple[str, str, int]]) -> int:
        """Record ``(key, token_type, expires_at)`` entries. Returns how many.

        The in-memory cache is updated first, so the revocation holds in this
        process even if the database write fails (logged).
        """
        items = [(str(k)[:64], str(t)[:16], int(e)) for k, t, e in entries if k]
        if not items:
            return 0
        now_wall = int(time.time())
        self._maybe_reload()
        with self._lock:
            for key, _t, exp in items:
                self._revoked[key] = max(exp, self._revoked.get(key, 0))
        revoked_at = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        try:
            with closing(sqlite3.connect(self._current_db(), timeout=5.0)) as conn:
                with conn:
                    conn.executemany(
                        "INSERT INTO revoked_tokens (jti, token_type, expires_at, revoked_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(jti) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
                        [(k, t, e, revoked_at) for k, t, e in items],
                    )
                    conn.execute("DELETE FROM revoked_tokens WHERE expires_at <= ?", (now_wall,))
        except sqlite3.Error as exc:
            logger.error(f"Could not persist a token revocation ({type(exc).__name__}: {exc}); "
                         "it holds in memory until restart")
        return len(items)

    def clear_cache(self) -> None:
        with self._lock:
            self._revoked.clear()
            self._loaded_at = 0.0
            self._db_path = None


token_revocations = TokenRevocations()
