"""Authentication: password hashing, JWT sessions and API access guards."""

import re
import time
import secrets
from pathlib import Path
from typing import Optional, Dict, Any
import jwt
from fastapi import HTTPException, Security, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyQuery, APIKeyHeader

from app.config import settings


def _client_ip(request) -> str:
    """Real client address: trusts proxy headers only from a loopback peer
    (cloudflared / local Caddy). See services/public_exposure.py."""
    from app.services.public_exposure import client_ip

    return client_ip(request)

# --- password hashing -------------------------------------------------------
#
# Calls the bcrypt library directly rather than going through passlib.
# passlib 1.7.4 detects its backend by hashing an over-length probe string;
# bcrypt >= 4.1 refuses passwords longer than 72 bytes instead of silently
# truncating, so that probe raises and every hash/verify fails. passlib has
# had no release since 2020, so the dependency was dropped rather than pinned.


def hash_password(password: str) -> str:
    """Hash a password with bcrypt, returning the standard modular string."""
    import bcrypt as _bcrypt

    # bcrypt only considers the first 72 bytes; truncate explicitly so a long
    # passphrase is accepted rather than rejected at the library boundary.
    raw = password.encode("utf-8")[:72]
    return _bcrypt.hashpw(raw, _bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verify. Returns False on any malformed stored hash."""
    import bcrypt as _bcrypt

    if not password or not stored_hash:
        return False
    try:
        return _bcrypt.checkpw(
            password.encode("utf-8")[:72], stored_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# --- account policy -----------------------------------------------------------
#
# One definition shared by the API (first-run, change-password) and the local
# recovery CLI (scripts/manage_operator.py), so neither can set a password the
# other would refuse.

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


def password_policy_error(password: str, username: Optional[str] = None) -> Optional[str]:
    """Return why ``password`` is unacceptable, or None when it is fine."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters."
    if not password.strip():
        return "Password cannot be only spaces."
    if username and password.strip().lower() == username.strip().lower():
        return "Password must not be the same as the username."
    return None


def username_policy_error(username: str) -> Optional[str]:
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return ("Username must be 3-64 characters: letters, digits, dot, dash or "
                "underscore, starting with a letter or digit.")
    return None


# --- session epoch ------------------------------------------------------------
#
# JWT sessions are stateless, so deleting or re-keying an operator account
# would otherwise leave every already-issued token valid until it expires.
# Each session token carries the "auth epoch" current when it was issued
# (claim "ep"); the recovery CLI bumps the epoch stored in system_setup, and a
# token from an older epoch is refused. The CLI runs out of process, hence the
# value is read from the database file rather than held in memory; it is
# cached briefly so the per-request cost is negligible.

AUTH_EPOCH_KEY = "auth_epoch"
_EPOCH_TTL_S = 2.0
_epoch_cache: Dict[str, Any] = {"value": 0, "at": 0.0}


def current_auth_epoch(force: bool = False) -> int:
    now = time.monotonic()
    if not force and now - _epoch_cache["at"] < _EPOCH_TTL_S:
        return _epoch_cache["value"]
    value = 0
    try:
        import sqlite3

        db_path = Path(settings.DATABASE_PATH)
        if db_path.exists():
            conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT value FROM system_setup WHERE key = ?", (AUTH_EPOCH_KEY,)
                ).fetchone()
            finally:
                conn.close()
            if row and str(row[0]).isdigit():
                value = int(row[0])
    except Exception:
        # Table not created yet, or the file is momentarily locked: keep the
        # last known value rather than failing every request.
        value = _epoch_cache["value"]
    _epoch_cache.update(value=value, at=now)
    return value


def token_epoch_is_current(payload: Dict[str, Any]) -> bool:
    try:
        return int(payload.get("ep", 0)) == current_auth_epoch()
    except (TypeError, ValueError):
        return False


async def bump_auth_epoch(session) -> int:
    """Invalidate every issued session and refresh token. Returns the new epoch."""
    from sqlalchemy import select
    from app.models.db_models import SystemSetupModel

    rec = (await session.execute(
        select(SystemSetupModel).where(SystemSetupModel.key == AUTH_EPOCH_KEY)
    )).scalar_one_or_none()
    new = (int(rec.value) + 1) if rec and str(rec.value).isdigit() else 1
    if rec:
        rec.value = str(new)
    else:
        session.add(SystemSetupModel(key=AUTH_EPOCH_KEY, value=str(new)))
    await session.commit()
    _epoch_cache.update(value=new, at=time.monotonic())
    return new

def phone_claims_ok(payload: Optional[Dict[str, Any]]) -> bool:
    """Tokens issued to a paired phone carry ``dev`` and ``pd`` claims.

    Refuse one minted for another edge device (``dev`` mismatch) or for a
    phone that has been revoked; note the phone as seen. Tokens without these
    claims (dashboard sessions) pass unchanged. See pairing_service.
    """
    if not payload or ("dev" not in payload and "pd" not in payload):
        return bool(payload)
    try:
        from app.services.pairing_service import phone_claims_valid
        return phone_claims_valid(payload)
    except Exception as exc:  # fail closed for phone tokens
        logging.getLogger("AuthService").error(f"phone token check failed: {exc}")
        return False


"""Authentication, token validation, and path traversal protection service."""


security_bearer = HTTPBearer(auto_error=False)
query_token_scheme = APIKeyQuery(name="token", auto_error=False)
api_key_header = APIKeyHeader(name="X-Edge-API-Key", auto_error=False)

FILENAME_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+\.(mp4|jpg|jpeg)$")


# Simple Rate Limiter & Intrusion Detection
from starlette.requests import Request
from collections import defaultdict
import time
import logging

logger = logging.getLogger("AuthService")

class RateLimiter:
    def __init__(self, requests: int, window: int):
        self.requests = requests
        self.window = window
        self.history = defaultdict(list)
        self.last_cleanup = time.time()

    def cleanup(self, now: float):
        if now - self.last_cleanup > 60:
            keys_to_delete = []
            for ip, timestamps in self.history.items():
                valid_timestamps = [t for t in timestamps if now - t < self.window]
                if not valid_timestamps:
                    keys_to_delete.append(ip)
                else:
                    self.history[ip] = valid_timestamps
            for ip in keys_to_delete:
                del self.history[ip]
            self.last_cleanup = now

    def __call__(self, request: Request):
        ip = _client_ip(request)
        now = time.time()
        self.cleanup(now)
        
        self.history[ip] = [t for t in self.history[ip] if now - t < self.window]
        if len(self.history[ip]) >= self.requests:
            logger.warning(f"[RateLimiter] IP {ip} exceeded rate limit")
            raise HTTPException(status_code=429, detail="Too Many Requests")
        self.history[ip].append(now)

class IntrusionDetector:
    """Per-address failed-credential counter with a lockout.

    Two independent buckets per address, so one kind of failure can never lock
    out the other:

    * ``"login"`` (key = the address): wrong passwords, setup codes and
      pairing codes -- secrets a person types;
    * ``"token"`` (key = ``"<address>#token"``): bearer tokens whose signature
      does not verify, and wrong API keys.

    Only *unverifiable* credentials count. A token with a valid signature that
    is merely expired, from an older auth epoch, signed out, or bound to a
    revoked phone is a stale session, not a guess: it gets a 401 and is not
    counted. (A dashboard opened with a stale token fired ~10 polls at once
    and locked 127.0.0.1 -- and with it the operator's own sign-in.)
    """

    SCOPES = ("login", "token")

    def __init__(self, max_attempts: int = 10, window_minutes: int = 5):
        self.max_attempts = max_attempts
        self.window = window_minutes * 60
        self.failed_attempts = defaultdict(list)
        self.last_cleanup = time.time()
        
    def cleanup(self, now: float):
        if now - self.last_cleanup > 60:
            keys_to_delete = []
            for ip, timestamps in self.failed_attempts.items():
                valid_timestamps = [t for t in timestamps if now - t < self.window]
                if not valid_timestamps:
                    keys_to_delete.append(ip)
                else:
                    self.failed_attempts[ip] = valid_timestamps
            for ip in keys_to_delete:
                del self.failed_attempts[ip]
            self.last_cleanup = now
            
    @staticmethod
    def key(ip: str, scope: str = "login") -> str:
        return ip if scope == "login" else f"{ip}#{scope}"

    def record_failure(self, ip: str, scope: str = "login"):
        now = time.time()
        self.cleanup(now)
        k = self.key(ip, scope)
        self.failed_attempts[k] = [t for t in self.failed_attempts[k] if now - t < self.window]
        self.failed_attempts[k].append(now)
        logger.warning(f"[IntrusionDetector] Failed {scope} attempt from IP {ip}. "
                       f"Attempt {len(self.failed_attempts[k])}/{self.max_attempts}")

    def recent_failures(self, ip: str, scope: str = "login") -> int:
        now = time.time()
        return sum(1 for t in self.failed_attempts.get(self.key(ip, scope), []) if now - t < self.window)

    def is_locked_out(self, ip: str, scope: str = "login") -> bool:
        return self.recent_failures(ip, scope) >= self.max_attempts

    def check_lockout(self, ip: str, scope: str = "login"):
        now = time.time()
        self.cleanup(now)
        n = self.recent_failures(ip, scope)
        if n >= self.max_attempts:
            logger.error(f"[IntrusionDetector] {scope} lockout for IP {ip} due to {n} failed attempts")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account locked out due to too many failed attempts")

    def record_success(self, ip: str, token_type: str):
        logger.info(f"[Auth] Successful authentication from IP {ip}, type={token_type}")
        for scope in self.SCOPES:
            self.failed_attempts.pop(self.key(ip, scope), None)

intrusion_detector = IntrusionDetector()

class AuthService:
    def __init__(self):
        self.secret = settings.JWT_SECRET
        self.algorithm = settings.JWT_ALGORITHM
        self.stream_expiry = settings.STREAM_TOKEN_EXPIRY_SECONDS
        self.clip_expiry = settings.CLIP_TOKEN_EXPIRY_SECONDS

    def generate_stream_token(self, camera_id: str, client_id: str = "mobile_app") -> str:
        """Generate a short-lived token to authenticate WebRTC/HLS stream access."""
        payload = {
            "sub": client_id,
            "camera_id": camera_id,
            "type": "stream_access",
            "iat": int(time.time()),
            "exp": int(time.time()) + self.stream_expiry,
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def create_access_token(self, data: dict, expires_delta: Optional[int] = None) -> str:
        """Create a signed JWT session/access token."""
        to_encode = data.copy()
        now = int(time.time())
        expire = now + (expires_delta if expires_delta else (24 * 3600))
        to_encode.update({"iat": now, "exp": expire, "type": data.get("type", "user_session")})
        return jwt.encode(to_encode, self.secret, algorithm=self.algorithm)

    def generate_clip_token(self, event_id: str) -> str:
        """Generate a signed expiring token for downloading/streaming event clips."""
        payload = {
            "event_id": event_id,
            "type": "clip_access",
            "iat": int(time.time()),
            "exp": int(time.time()) + self.clip_expiry,
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Verify JWT signature and expiry."""
        try:
            payload = jwt.decode(token, self.secret, algorithms=[self.algorithm])
            return payload
        except jwt.PyJWTError:
            return None

    def inspect_token(self, token: str) -> tuple:
        """``(payload, signature_ok)``.

        ``payload`` is the claims when the signature and time claims verify,
        else None. ``signature_ok`` is True whenever the token was signed with
        this device's key -- including an expired one -- and False for garbage,
        a forged or foreign signature, or a token signed with another key.
        Only the latter is a guess worth counting toward the lockout.
        """
        if not token or not isinstance(token, str):
            return None, False
        try:
            return jwt.decode(token, self.secret, algorithms=[self.algorithm]), True
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError, jwt.InvalidKeyError, jwt.DecodeError):
            return None, False
        except jwt.PyJWTError:
            # Signature verified, a claim did not (expired, not yet valid...).
            return None, True

    @staticmethod
    def is_revoked(token: str, payload: Optional[Dict[str, Any]]) -> bool:
        """Signed out via /auth/logout (this token or its whole login session)."""
        if not payload:
            return False
        try:
            from app.services.token_revocation import token_revocations
            return token_revocations.is_revoked(token, payload)
        except Exception as exc:  # the store must never take auth down
            logger.error(f"revocation check failed: {exc}")
            return False

    def verify_session_token(self, token: str) -> Optional[Dict[str, Any]]:
        """A signed, unexpired, not signed-out operator session from the current auth epoch."""
        payload = self.verify_token(token) if token else None
        if not payload or payload.get("type") != "user_session":
            return None
        if not token_epoch_is_current(payload):
            return None
        if self.is_revoked(token, payload):
            return None
        if not phone_claims_ok(payload):
            return None
        return payload

    def issue_session_tokens(self, user_id: str, role: str, sid: Optional[str] = None) -> Dict[str, str]:
        """Access + refresh token for one login session.

        Both carry the same ``sid`` (refreshed access tokens keep it) and their
        own ``jti``, so /auth/logout can revoke one token or the whole session.
        """
        import uuid

        now = int(time.time())
        ep = current_auth_epoch(force=True)
        sid = sid or uuid.uuid4().hex
        access_payload = {"sub": user_id, "type": "user_session", "role": role,
                          "ep": ep, "iat": now, "exp": now + 24 * 3600,
                          "sid": sid, "jti": uuid.uuid4().hex}
        refresh_payload = {"sub": user_id, "type": "refresh", "role": role,
                           "ep": ep, "iat": now, "exp": now + 30 * 24 * 3600,
                           "sid": sid, "jti": uuid.uuid4().hex}
        return {
            "access_token": jwt.encode(access_payload, self.secret, algorithm=self.algorithm),
            "refresh_token": jwt.encode(refresh_payload, self.secret, algorithm=self.algorithm),
        }

    def verify_stream_access(
        self,
        camera_id: str,
        request: Request,
        token: Optional[str] = Depends(query_token_scheme),
        bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer)
    ) -> Dict[str, Any]:
        """FastAPI dependency: Enforces valid token for WebRTC / live video feeds.

        A valid token always works; only an unverifiable one counts toward
        (and is refused by) the token lockout.
        """
        ip = _client_ip(request)

        raw_token = token or (bearer.credentials if bearer else None)
        if not raw_token:
            # Allow open access if development mode, but log warning
            if settings.AUTH_DISABLED:
                return {"sub": "dev_client", "camera_id": camera_id}
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication stream token"
            )

        payload, signed = self.inspect_token(raw_token)
        if (not payload or payload.get("type") != "stream_access" or payload.get("camera_id") != camera_id
                or not phone_claims_ok(payload)):
            if not signed:
                intrusion_detector.record_failure(ip, "token")
                intrusion_detector.check_lockout(ip, "token")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired stream token for this camera"
            )
        intrusion_detector.record_success(ip, "stream_access")
        return payload

    def verify_clip_access(
        self,
        request: Request,
        token: Optional[str] = Depends(query_token_scheme),
        bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer)
    ) -> Dict[str, Any]:
        """FastAPI dependency: Enforces valid token for event clips and snapshots."""
        ip = _client_ip(request)

        raw_token = token or (bearer.credentials if bearer else None)
        if not raw_token:
            if settings.AUTH_DISABLED:
                return {"sub": "dev_client", "type": "clip_access"}
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing clip authorization token"
            )

        payload, signed = self.inspect_token(raw_token)
        if not payload or payload.get("type") != "clip_access" or not phone_claims_ok(payload):
            if not signed:
                intrusion_detector.record_failure(ip, "token")
                intrusion_detector.check_lockout(ip, "token")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired clip access token"
            )
        intrusion_detector.record_success(ip, "clip_access")
        return payload

    def verify_internal_key(
        self,
        request: Request,
        api_key: Optional[str] = Security(api_key_header)
    ) -> bool:
        """FastAPI dependency: Verifies internal vision engine API key on /events/trigger."""
        ip = _client_ip(request)

        if not api_key:
            if settings.AUTH_DISABLED:
                return True
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Edge-API-Key header"
            )

        if not secrets.compare_digest(api_key, settings.INTERNAL_SERVICE_KEY):
            intrusion_detector.record_failure(ip, "token")
            intrusion_detector.check_lockout(ip, "token")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid internal service key"
            )
        intrusion_detector.record_success(ip, "internal_key")
        return True

    def verify_api_access(
        self,
        request: Request,
        api_key: Optional[str] = Security(api_key_header),
        bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
        token: Optional[str] = Depends(query_token_scheme),
    ) -> bool:
        """General API access for mobile apps / dashboards.

        Credentials are checked *before* the lockout is enforced, so a valid
        token always works and clears the counter. Enforcing the lockout first
        deadlocked the operator out of their own system: once tripped, even a
        correct sign-in was refused until the window expired.

        A request carrying **no** credential is not counted as a failed
        attempt. A signed-out dashboard polls several endpoints every few
        seconds, which would otherwise trip the 10-attempt lockout within
        seconds of opening the page. Brute force requires presenting a
        credential, so only an *invalid* one counts against the limit.
        """
        ip = _client_ip(request)

        # Safely resolve API key from dependency or request header
        resolved_api_key = api_key if isinstance(api_key, str) else request.headers.get("X-Edge-API-Key")
        if resolved_api_key and secrets.compare_digest(resolved_api_key, settings.INTERNAL_SERVICE_KEY):
            intrusion_detector.record_success(ip, "api_key")
            return True

        # Safely resolve bearer token from dependency, Authorization header, query param, or cookie
        raw_bearer = bearer.credentials if isinstance(bearer, HTTPAuthorizationCredentials) else None
        if not raw_bearer:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                raw_bearer = auth_header.split(" ", 1)[1]
        if not raw_bearer:
            raw_bearer = token or request.query_params.get("token")
        if not raw_bearer:
            raw_bearer = request.cookies.get("edge_cctv_token")

        bearer_signed = False
        if raw_bearer:
            payload, bearer_signed = self.inspect_token(raw_bearer)
            if payload and payload.get("type") == "user_session":
                if not token_epoch_is_current(payload):
                    payload = None  # issued before an operator reset
                elif self.is_revoked(raw_bearer, payload):
                    payload = None  # signed out via /auth/logout
            if payload and not phone_claims_ok(payload):
                payload = None  # phone token for another device, or a revoked phone
            if payload and payload.get("type") in ("user_session", "stream_access", "clip_access"):
                intrusion_detector.record_success(ip, "bearer_token")
                request.state.user = payload
                return True

        if settings.AUTH_DISABLED:
            return True

        # Only a credential that cannot be verified is a guess: a wrong API
        # key, or a bearer token not signed with this device's key. A token
        # we signed that is expired, from an old epoch, signed out or bound to
        # a revoked phone is a stale session: 401, not counted.
        unverifiable = bool(resolved_api_key) or bool(raw_bearer and not bearer_signed)
        if unverifiable:
            intrusion_detector.record_failure(ip, "token")
            intrusion_detector.check_lockout(ip, "token")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication",
        )

    @staticmethod
    def sanitize_and_resolve_file(base_dir: Path, filename: str) -> Path:
        """Protects against Path Traversal by enforcing strict regex and canonical path containment."""
        if not FILENAME_REGEX.match(filename) or ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid filename format"
            )

        try:
            base_resolved = base_dir.resolve()
            target_resolved = (base_dir / filename).resolve()
            if not str(target_resolved).startswith(str(base_resolved)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: Directory traversal detected"
                )
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid path resolution")

        if not target_resolved.is_file():
            raise HTTPException(status_code=404, detail="Requested file not found")

    async def create_admin_user(self, session, username, password, display_name, role="owner"):
        from app.models.db_models import AdminUserModel
        import uuid
        from sqlalchemy import select

        username = (username or "").strip()
        problem = username_policy_error(username) or password_policy_error(password, username)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

        stmt = select(AdminUserModel).where(AdminUserModel.username == username)
        result = await session.execute(stmt)
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="User already exists")

        new_user = AdminUserModel(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=hash_password(password),
            display_name=(display_name or username).strip()[:128],
            role=role,
            is_active=True,
        )
        session.add(new_user)
        await session.commit()
        return new_user

    async def authenticate_user(self, session, username, password):
        from datetime import datetime, timezone
        from app.models.db_models import AdminUserModel
        from sqlalchemy import select

        stmt = select(AdminUserModel).where(AdminUserModel.username == (username or "").strip())
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not verify_password(password, user.password_hash):
            return None
        if user.is_active is False:
            return None

        user.last_login = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return self.issue_session_tokens(user.id, user.role)

    def refresh_access_token(self, refresh_token: str):
        payload = self.verify_token(refresh_token)
        if not payload or payload.get("type") != "refresh" or not token_epoch_is_current(payload):
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        if self.is_revoked(refresh_token, payload):
            raise HTTPException(status_code=401, detail="This session was signed out")
        if "dev" in payload or "pd" in payload:
            # Paired phone: same device, not revoked, and the refresh token
            # the phone currently holds (only its hash is stored).
            from app.services import pairing_service
            if (not phone_claims_ok(payload) or not payload.get("pd")
                    or not pairing_service.refresh_token_matches(str(payload["pd"]), refresh_token)):
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            return pairing_service.reissue_phone_access(payload)
        return self.issue_session_tokens(payload.get("sub"), payload.get("role", "owner"),
                                         sid=payload.get("sid"))["access_token"]

    def revoke_session(self, access_token: Optional[str], refresh_token: Optional[str] = None) -> int:
        """Sign out: revoke the presented session token and its login session.

        Revoking the session (``sid``) also kills the paired refresh token and
        any access token refreshed from it; a refresh token presented as well
        is revoked by its own key too. Tokens that do not verify (garbage,
        expired) are ignored: there is nothing left to revoke. Returns the
        number of revocation entries recorded.
        """
        from app.services.token_revocation import keys_for, token_key, token_revocations

        now = int(time.time())
        session_ttl = 31 * 24 * 3600
        try:
            from app.services import pairing_service
            session_ttl = max(session_ttl, int(pairing_service.REFRESH_TTL_S) + int(pairing_service.ACCESS_TTL_S))
        except Exception:
            pass
        entries = []
        for raw, expected in ((access_token, "user_session"), (refresh_token, "refresh")):
            if not raw:
                continue
            payload = self.verify_token(raw)
            if not payload or payload.get("type") != expected:
                continue
            exp = int(payload.get("exp") or now)
            entries.append((token_key(raw, payload), expected, exp))
            for key in keys_for(raw, payload)[1:]:
                entries.append((key, "session", now + session_ttl))
        return token_revocations.revoke(entries)

    async def change_password(self, session, user_id, old_password, new_password):
        from app.models.db_models import AdminUserModel
        from sqlalchemy import select

        stmt = select(AdminUserModel).where(AdminUserModel.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not verify_password(old_password, user.password_hash):
            raise HTTPException(status_code=403, detail="Invalid old password")
        problem = password_policy_error(new_password, user.username)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

        user.password_hash = hash_password(new_password)
        await session.commit()
        return True

general_rate_limiter = RateLimiter(requests=100, window=60)
auth_service = AuthService()
