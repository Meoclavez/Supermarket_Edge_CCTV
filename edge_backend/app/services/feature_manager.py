"""Per-camera feature toggles.

Starts empty. It used to pre-register two cameras that no deployment had,
which made the active-feature count non-zero on a box with no cameras at all.

The durable copy of a camera's toggles is the ``cameras.features`` JSON
column; this manager is the in-process cache that pipeline threads read
through :meth:`is_enabled` without touching the database. A camera that is
not cached yet is hydrated from the stored JSON the first time the API serves
it (``stored=``). Keys written by older builds (``fall_detection``,
``door_monitoring`` ...) are ignored by :class:`CameraFeatureConfig`.
"""

import threading
from typing import Any, Dict, Optional

from ..models.schemas import CameraFeatureConfig

FEATURE_FLAGS = tuple(
    name for name, field in CameraFeatureConfig.model_fields.items() if field.annotation is bool
)


def _read_stored_features(camera_id: str) -> Optional[Dict[str, Any]]:
    """The camera row's ``features`` JSON, read synchronously (thread safe)."""
    import json
    import sqlite3
    from contextlib import closing

    from ..config import settings

    try:
        with closing(sqlite3.connect(str(settings.DATABASE_PATH), timeout=2.0)) as conn:
            row = conn.execute("SELECT features FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class FeatureManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._camera_features: Dict[str, CameraFeatureConfig] = {}

    def get_camera_features(self, camera_id: str, stored: Optional[Dict[str, Any]] = None) -> CameraFeatureConfig:
        with self._lock:
            cfg = self._camera_features.get(camera_id)
            if cfg is None and isinstance(stored, dict) and stored:
                cfg = CameraFeatureConfig.model_validate(stored)
                self._camera_features[camera_id] = cfg
            return cfg if cfg is not None else CameraFeatureConfig()

    def set_camera_features(self, camera_id: str, config: CameraFeatureConfig) -> None:
        with self._lock:
            self._camera_features[camera_id] = config

    def is_enabled(self, camera_id: str, flag: str) -> bool:
        """Cheap, thread-safe check for pipeline stages.

        On the first call for a camera that is not cached (e.g. right after a
        restart) the stored toggles are read once from the database, so an
        operator's "off" survives restarts. Defaults apply only to a camera
        that has never been configured.
        """
        if flag not in FEATURE_FLAGS:
            raise KeyError(f"Unknown camera feature flag: {flag}")
        with self._lock:
            cfg = self._camera_features.get(camera_id)
        if cfg is None:
            stored = _read_stored_features(camera_id)
            loaded = CameraFeatureConfig.model_validate(stored) if stored else CameraFeatureConfig()
            with self._lock:
                cfg = self._camera_features.setdefault(camera_id, loaded)
        return bool(getattr(cfg, flag))

    def remove_camera(self, camera_id: str) -> None:
        with self._lock:
            self._camera_features.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._camera_features.clear()

    def count_active_features(self) -> int:
        with self._lock:
            return sum(
                1 for cfg in self._camera_features.values() for flag in FEATURE_FLAGS if getattr(cfg, flag)
            )


feature_manager = FeatureManager()
