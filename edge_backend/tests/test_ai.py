import pytest
import time
from app.services.kinematic_fall_engine import KinematicFallEngine
from app.models.schemas import EventType, EventSeverity

def test_kinematic_fall_detection_trigger():
    engine = KinematicFallEngine()
    
    standing_keypoints = [(100.0, 50.0 + i*10, 0.9) for i in range(17)]
    bbox_standing = (80.0, 40.0, 120.0, 220.0)
    
    evt1 = engine.evaluate_pose("cam1", "Living Room", 1, standing_keypoints, bbox_standing)
    assert evt1 is None
    
    time.sleep(0.1)
    fallen_keypoints = [(100.0, 300.0, 0.9) for _ in range(17)]
    bbox_fallen = (40.0, 280.0, 200.0, 320.0)
    
    evt2 = engine.evaluate_pose("cam1", "Living Room", 1, fallen_keypoints, bbox_fallen)
    assert evt2 is not None
    assert evt2.event_type == EventType.FALL_DETECTED
    assert evt2.severity == EventSeverity.CRITICAL
