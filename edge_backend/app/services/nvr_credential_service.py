"""Persistent on-disk storage service for NVR and camera credentials.

Stores NVR configurations in storage/nvr_credentials.json with restricted
file permissions (0600) so credentials survive restarts and software updates
without needing to be re-entered.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_NVR_CONFIG_FILE = "nvr_credentials.json"


class NVRCredentialService:
    """Manages persistent on-disk NVR credentials and connection settings."""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or Path(settings.STORAGE_DIR)
        self.config_path = self.storage_dir / DEFAULT_NVR_CONFIG_FILE

    def _ensure_storage(self) -> None:
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to ensure storage directory {self.storage_dir}: {e}")

    def load_credentials(self) -> Dict[str, Any]:
        """Load the raw persistent NVR credentials from disk."""
        self._ensure_storage()
        if not self.config_path.exists():
            return {
                "default_username": "admin",
                "default_password": "",
                "default_port": 554,
                "default_channels": 16,
                "nvrs": {},
            }

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                data.setdefault("default_username", "admin")
                data.setdefault("default_password", "")
                data.setdefault("default_port", 554)
                data.setdefault("default_channels", 16)
                data.setdefault("nvrs", {})
                return data
        except Exception as e:
            logger.error(f"Error reading NVR credentials from {self.config_path}: {e}")
            return {
                "default_username": "admin",
                "default_password": "",
                "default_port": 554,
                "default_channels": 16,
                "nvrs": {},
            }

    def save_credentials(
        self,
        username: str,
        password: str,
        host: Optional[str] = None,
        port: int = 554,
        default_channels: int = 16,
    ) -> Dict[str, Any]:
        """Save NVR credentials to disk with safe file permissions."""
        self._ensure_storage()
        current = self.load_credentials()

        username = (username or "").strip()
        # Keep existing password if empty string was passed (user didn't change it)
        if not password and current.get("default_password"):
            password = current["default_password"]

        current["default_username"] = username or "admin"
        current["default_password"] = password or ""
        current["default_port"] = int(port or 554)
        current["default_channels"] = max(1, min(int(default_channels or 16), 64))

        if host:
            host_clean = host.strip()
            if host_clean:
                nvrs = current.setdefault("nvrs", {})
                nvrs[host_clean] = {
                    "host": host_clean,
                    "username": current["default_username"],
                    "password": current["default_password"],
                    "port": current["default_port"],
                    "channels": current["default_channels"],
                }

        temp_path = self.config_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)

            # Restrict permissions to owner read/write only (chmod 0600)
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass

            temp_path.replace(self.config_path)
            logger.info(f"Persisted NVR credentials to disk at {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to persist NVR credentials to {self.config_path}: {e}")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        return self.get_safe_credentials()

    def get_safe_credentials(self) -> Dict[str, Any]:
        """Return credentials masked for frontend consumption (passwords obscured)."""
        raw = self.load_credentials()
        has_pwd = bool(raw.get("default_password"))
        
        safe_nvrs = {}
        for host, conf in raw.get("nvrs", {}).items():
            safe_nvrs[host] = {
                "host": conf.get("host", host),
                "username": conf.get("username", "admin"),
                "port": conf.get("port", 554),
                "channels": conf.get("channels", 16),
                "has_password": bool(conf.get("password")),
            }

        return {
            "default_username": raw.get("default_username", "admin"),
            "default_port": raw.get("default_port", 554),
            "default_channels": raw.get("default_channels", 16),
            "has_password": has_pwd,
            "password_masked": "••••••••" if has_pwd else "",
            "nvrs": safe_nvrs,
        }

    def get_auth_for_host(self, host: Optional[str] = None) -> Tuple[str, str]:
        """Get the username and password for a specific host, falling back to defaults."""
        raw = self.load_credentials()
        if host and host in raw.get("nvrs", {}):
            entry = raw["nvrs"][host]
            return entry.get("username", "admin"), entry.get("password", "")
        return raw.get("default_username", "admin"), raw.get("default_password", "")


nvr_credential_service = NVRCredentialService()
