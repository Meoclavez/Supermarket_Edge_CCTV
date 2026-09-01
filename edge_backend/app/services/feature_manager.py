from typing import Dict
import threading
from ..models.schemas import CameraFeatureConfig

class FeatureManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._camera_features: Dict[str, CameraFeatureConfig] = {}
        
        self.set_camera_features("cam_living_room", CameraFeatureConfig(
            motion_tracking=True,
            fall_detection=True,
            door_monitoring=False,
            package_theft_tracking=False,
            inactivity_alerts=True
        ))
        self.set_camera_features("cam_front_door", CameraFeatureConfig(
            motion_tracking=True,
            fall_detection=False,
            door_monitoring=True,
            package_theft_tracking=True,
            inactivity_alerts=False
        ))

    def get_camera_features(self, camera_id: str) -> CameraFeatureConfig:
        with self._lock:
            return self._camera_features.get(camera_id, CameraFeatureConfig())

    def set_camera_features(self, camera_id: str, config: CameraFeatureConfig) -> None:
        with self._lock:
            self._camera_features[camera_id] = config

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
