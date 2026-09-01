import pytest
from app.models.schemas import CameraFeatureConfig
from app.services.feature_manager import FeatureManager

def test_feature_manager_hot_reload():
    fm = FeatureManager()
    cfg = CameraFeatureConfig(fall_detection=True, door_monitoring=False)
    fm.set_camera_features("test_cam", cfg)
    
    retrieved = fm.get_camera_features("test_cam")
    assert retrieved.fall_detection is True
    assert retrieved.door_monitoring is False
    
    cfg.fall_detection = False
    fm.set_camera_features("test_cam", cfg)
    assert fm.get_camera_features("test_cam").fall_detection is False
