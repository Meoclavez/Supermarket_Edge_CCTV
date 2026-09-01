"""Authentication, token validation, and path traversal protection service."""

import re
import time
import secrets
from pathlib import Path
from typing import Optional, Dict, Any
import jwt
from fastapi import HTTPException, Security, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyQuery, APIKeyHeader

from app.config import settings

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
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.cleanup(now)
        
        self.history[ip] = [t for t in self.history[ip] if now - t < self.window]
        if len(self.history[ip]) >= self.requests:
            logger.warning(f"[RateLimiter] IP {ip} exceeded rate limit")
            raise HTTPException(status_code=429, detail="Too Many Requests")
        self.history[ip].append(now)

class IntrusionDetector:
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
            
    def record_failure(self, ip: str):
        now = time.time()
        self.cleanup(now)
        self.failed_attempts[ip] = [t for t in self.failed_attempts[ip] if now - t < self.window]
        self.failed_attempts[ip].append(now)
        logger.warning(f"[IntrusionDetector] Failed auth attempt from IP {ip}. Attempt {len(self.failed_attempts[ip])}/{self.max_attempts}")
        
    def check_lockout(self, ip: str):
        now = time.time()
        self.cleanup(now)
        valid_timestamps = [t for t in self.failed_attempts.get(ip, []) if now - t < self.window]
        if len(valid_timestamps) >= self.max_attempts:
            logger.error(f"[IntrusionDetector] Account lockout for IP {ip} due to {len(valid_timestamps)} failed attempts")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account locked out due to too many failed attempts")
            
    def record_success(self, ip: str, token_type: str):
        logger.info(f"[Auth] Successful authentication from IP {ip}, type={token_type}")
        if ip in self.failed_attempts:
            del self.failed_attempts[ip]

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

    def verify_stream_access(
        self,
        camera_id: str,
        request: Request,
        token: Optional[str] = Depends(query_token_scheme),
        bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer)
    ) -> Dict[str, Any]:
        """FastAPI dependency: Enforces valid token for WebRTC / live video feeds."""
        ip = request.client.host if request.client else "unknown"
        intrusion_detector.check_lockout(ip)
        
        raw_token = token or (bearer.credentials if bearer else None)
        if not raw_token:
            # Allow open access if development mode, but log warning
            if settings.DEBUG:
                return {"sub": "dev_client", "camera_id": camera_id}
            intrusion_detector.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication stream token"
            )

        payload = self.verify_token(raw_token)
        if not payload or payload.get("type") != "stream_access" or payload.get("camera_id") != camera_id:
            intrusion_detector.record_failure(ip)
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
        ip = request.client.host if request.client else "unknown"
        intrusion_detector.check_lockout(ip)
        
        raw_token = token or (bearer.credentials if bearer else None)
        if not raw_token:
            if settings.DEBUG:
                return {"sub": "dev_client", "type": "clip_access"}
            intrusion_detector.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing clip authorization token"
            )

        payload = self.verify_token(raw_token)
        if not payload or payload.get("type") != "clip_access":
            intrusion_detector.record_failure(ip)
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
        ip = request.client.host if request.client else "unknown"
        intrusion_detector.check_lockout(ip)
        
        if not api_key:
            if settings.DEBUG:
                return True
            intrusion_detector.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Edge-API-Key header"
            )

        if not secrets.compare_digest(api_key, settings.INTERNAL_SERVICE_KEY):
            intrusion_detector.record_failure(ip)
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
        bearer: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer)
    ) -> bool:
        """General API access for mobile apps / dashboards."""
        ip = request.client.host if request.client else "unknown"
        intrusion_detector.check_lockout(ip)
        
        # Safely resolve API key from dependency or request header
        resolved_api_key = api_key if isinstance(api_key, str) else request.headers.get("X-Edge-API-Key")
        if resolved_api_key and secrets.compare_digest(resolved_api_key, settings.INTERNAL_SERVICE_KEY):
            intrusion_detector.record_success(ip, "api_key")
            return True

        # Safely resolve bearer token from dependency or Authorization header
        raw_bearer = bearer.credentials if isinstance(bearer, HTTPAuthorizationCredentials) else None
        if not raw_bearer:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                raw_bearer = auth_header.split(" ", 1)[1]

        if raw_bearer:
            payload = self.verify_token(raw_bearer)
            if payload and payload.get("type") in ("user_session", "stream_access", "clip_access"):
                intrusion_detector.record_success(ip, "bearer_token")
                request.state.user = payload
                return True

        if settings.DEBUG:
            return True
        intrusion_detector.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication"
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

    _pairing_codes: Dict[str, Dict[str, Any]] = {}

    def generate_app_pairing_code(self, user_id: str = "primary_admin") -> str:
        code = "".join([str(secrets.randbelow(10)) for _ in range(6)])
        self._pairing_codes[code] = {
            "user_id": user_id,
            "expires_at": time.time() + 300,  # 5 minutes
        }
        return code

    async def verify_app_pairing_code(self, session, code: str) -> Optional[Dict[str, str]]:
        now = time.time()
        # Clean expired codes
        expired = [c for c, data in self._pairing_codes.items() if data["expires_at"] < now]
        for c in expired:
            del self._pairing_codes[c]

        if code not in self._pairing_codes:
            return None

        entry = self._pairing_codes.pop(code)
        user_id = entry["user_id"]

        # Generate tokens
        access_payload = {
            "sub": user_id,
            "type": "user_session",
            "role": "owner",
            "iat": int(time.time()),
            "exp": int(time.time()) + (24 * 3600),
        }
        refresh_payload = {
            "sub": user_id,
            "type": "refresh",
            "iat": int(time.time()),
            "exp": int(time.time()) + (30 * 24 * 3600),
        }

        return {
            "access_token": jwt.encode(access_payload, self.secret, algorithm=self.algorithm),
            "refresh_token": jwt.encode(refresh_payload, self.secret, algorithm=self.algorithm),
        }

    async def create_admin_user(self, session, username, password, display_name, role="owner"):
        from passlib.hash import bcrypt
        from app.models.db_models import AdminUserModel
        import uuid
        from sqlalchemy import select

        stmt = select(AdminUserModel).where(AdminUserModel.username == username)
        result = await session.execute(stmt)
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="User already exists")

        hashed = bcrypt.hash(password)
        new_user = AdminUserModel(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=hashed,
            display_name=display_name,
            role=role
        )
        session.add(new_user)
        await session.commit()
        return new_user

    async def authenticate_user(self, session, username, password):
        from passlib.hash import bcrypt
        from app.models.db_models import AdminUserModel
        from sqlalchemy import select

        stmt = select(AdminUserModel).where(AdminUserModel.username == username)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not bcrypt.verify(password, user.password_hash):
            return None

        # Create JWT access and refresh tokens
        access_payload = {
            "sub": user.id,
            "type": "user_session",
            "role": user.role,
            "iat": int(time.time()),
            "exp": int(time.time()) + (24 * 3600),
        }
        refresh_payload = {
            "sub": user.id,
            "type": "refresh",
            "iat": int(time.time()),
            "exp": int(time.time()) + (30 * 24 * 3600),
        }
        
        return {
            "access_token": jwt.encode(access_payload, self.secret, algorithm=self.algorithm),
            "refresh_token": jwt.encode(refresh_payload, self.secret, algorithm=self.algorithm)
        }
        
    def refresh_access_token(self, refresh_token: str):
        payload = self.verify_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")
            
        access_payload = {
            "sub": payload.get("sub"),
            "type": "user_session",
            "iat": int(time.time()),
            "exp": int(time.time()) + (24 * 3600),
        }
        return jwt.encode(access_payload, self.secret, algorithm=self.algorithm)

    async def change_password(self, session, user_id, old_password, new_password):
        from passlib.hash import bcrypt
        from app.models.db_models import AdminUserModel
        from sqlalchemy import select
        
        stmt = select(AdminUserModel).where(AdminUserModel.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user or not bcrypt.verify(old_password, user.password_hash):
            raise HTTPException(status_code=403, detail="Invalid old password")
            
        user.password_hash = bcrypt.hash(new_password)
        await session.commit()
        return True

general_rate_limiter = RateLimiter(requests=100, window=60)
auth_service = AuthService()
