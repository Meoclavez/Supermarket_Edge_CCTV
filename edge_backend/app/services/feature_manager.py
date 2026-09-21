"""Per-camera feature toggles.

Starts empty. It used to pre-register two cameras ("cam_living_room",
"cam_front_door") that no deployment had, which made the active-feature
count non-zero on a box with no cameras at all.
"""

import threading
from typing import Dict

from ..models.schemas import CameraFeatureConfig


class FeatureManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._camera_features: Dict[str, CameraFeatureConfig] = {}

    def get_camera_features(self, camera_id: str) -> CameraFeatureConfig:
        with self._lock:
            return self._camera_features.get(camera_id, CameraFeatureConfig())

    def set_camera_features(self, camera_id: str, config: CameraFeatureConfig) -> None:
        with self._lock:
            self._camera_features[camera_id] = config

    def remove_camera(self, camera_id: str) -> None:
        with self._lock:
            self._camera_features.pop(camera_id, None)

    def clear(self) -> None:
        with self._lock:
            self._camera_features.clear()

    def count_active_features(self) -> int:
        count = 0
        with self._lock:
            for cfg in self._camera_features.values():
                if cfg.fall_detection: count += 1
                if cfg.door_monitoring: count += 1
                if cfg.package_theft_tracking: count += 1
                if cfg.inactivity_alerts: count += 1
                if cfg.motion_tracking: count += 1
        return count


feature_manager = FeatureManager()
