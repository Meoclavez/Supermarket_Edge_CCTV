from app.models.schemas import CameraFeatureConfig
from app.services.feature_manager import FEATURE_FLAGS, FeatureManager


def test_feature_manager_hot_reload():
    fm = FeatureManager()
    cfg = CameraFeatureConfig(theft_detection=True, shelf_interaction=False)
    fm.set_camera_features("test_cam", cfg)

    retrieved = fm.get_camera_features("test_cam")
    assert retrieved.theft_detection is True
    assert retrieved.shelf_interaction is False
    assert fm.is_enabled("test_cam", "shelf_interaction") is False

    cfg.theft_detection = False
    fm.set_camera_features("test_cam", cfg)
    assert fm.get_camera_features("test_cam").theft_detection is False


def test_only_retail_flags_exist():
    assert set(FEATURE_FLAGS) == {"people_counting", "shelf_interaction", "theft_detection"}


def test_legacy_stored_keys_are_ignored():
    """Camera rows written by the home-security build still carry old keys."""
    legacy = {
        "motion_tracking": True, "fall_detection": True, "door_monitoring": False,
        "package_theft_tracking": False, "inactivity_alerts": False,
        "tripwires_enabled": True, "intrusion_zones_enabled": True,
        "dwell_tracking": True, "shelf_interaction": False, "theft_detection": True,
        "queue_monitoring": True, "sub_stream_fps": 5, "main_stream_fps": 25,
    }
    cfg = CameraFeatureConfig.model_validate(legacy)
    assert cfg.model_dump() == {"people_counting": True, "shelf_interaction": False, "theft_detection": True,
                                "person_max_frame_fraction": None, "night_watch": None,
                                "decode_max_width": None}

    fm = FeatureManager()
    assert fm.get_camera_features("cam_old", stored=legacy).shelf_interaction is False
    # Hydrated once, then served from the cache.
    assert fm.is_enabled("cam_old", "shelf_interaction") is False


def test_unknown_flag_is_an_error():
    import pytest

    with pytest.raises(KeyError):
        FeatureManager().is_enabled("cam", "fall_detection")
