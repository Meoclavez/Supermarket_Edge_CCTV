"""Unit tests for the Kinematic Fall Engine and Spatial Polygon/Tripwire Geometry."""

import time
import pytest
from app.models.schemas import BoundingBox, Keypoint, EventType, EventSeverity, TripwireDirection
from app.services.hailo_inference_service import KinematicFallEngine
from app.services.ai_zone_service import PolygonGeometry


def create_keypoints(hip_y: float, shoulder_y: float = 0.2, shoulder_x: float = 0.5, hip_x: float = 0.5) -> list:
    return [
        Keypoint(id=5, name="left_shoulder", x=shoulder_x - 0.05, y=shoulder_y, confidence=0.9),
        Keypoint(id=6, name="right_shoulder", x=shoulder_x + 0.05, y=shoulder_y, confidence=0.9),
        Keypoint(id=11, name="left_hip", x=hip_x - 0.05, y=hip_y, confidence=0.9),
        Keypoint(id=12, name="right_hip", x=hip_x + 0.05, y=hip_y, confidence=0.9),
    ]


def test_kinematic_fall_detection_trajectory():
    engine = KinematicFallEngine(cooldown_seconds=0.0)
    camera_id = "cam_living_room"
    track_id = 101

    # Frame 1: Standing
    bbox_standing = BoundingBox(x_min=0.35, y_min=0.1, x_max=0.65, y_max=0.7, confidence=0.9, label="person")
    kp_standing = create_keypoints(hip_y=0.45, shoulder_y=0.15, shoulder_x=0.5, hip_x=0.5)
    engine.analyze_pose(camera_id, track_id, kp_standing, bbox_standing)

    # Frame 2: Mid-fall
    time.sleep(0.08)
    bbox_mid = BoundingBox(x_min=0.35, y_min=0.3, x_max=0.65, y_max=0.85, confidence=0.9, label="person")
    kp_mid = create_keypoints(hip_y=0.65, shoulder_y=0.40, shoulder_x=0.5, hip_x=0.5)
    engine.analyze_pose(camera_id, track_id, kp_mid, bbox_mid)

    # Frame 3: Fallen horizontal on ground
    time.sleep(0.08)
    bbox_fallen = BoundingBox(x_min=0.15, y_min=0.75, x_max=0.85, y_max=0.95, confidence=0.9, label="person")
    kp_fallen = create_keypoints(hip_y=0.88, shoulder_y=0.86, shoulder_x=0.25, hip_x=0.75)
    engine.analyze_pose(camera_id, track_id, kp_fallen, bbox_fallen)

    assert engine.tracks[track_id].is_fallen is True
    engine.tracks[track_id].fallen_timestamp = time.time() - 6.0

    # Frame 4: Sustained immobility
    result_immobile = engine.analyze_pose(camera_id, track_id, kp_fallen, bbox_fallen)
    assert result_immobile is not None
    event_type, severity, confidence, kinematics = result_immobile

    assert event_type == EventType.FALL_DETECTED
    assert severity == EventSeverity.CRITICAL
    assert kinematics.aspect_ratio_initial >= 1.4
    assert kinematics.aspect_ratio_final <= 0.8
    assert kinematics.immobility_duration_sec >= 5.0


def test_polygon_raycasting_point_in_polygon():
    polygon = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]

    # Test point inside
    inside_pt = (0.5, 0.5)
    assert PolygonGeometry.point_in_polygon_raycasting(inside_pt, polygon) is True

    # Test point outside
    outside_pt = (0.1, 0.5)
    assert PolygonGeometry.point_in_polygon_raycasting(outside_pt, polygon) is False


def test_tripwire_directional_line_crossing():
    w_start = (0.5, 0.1)
    w_end = (0.5, 0.9)

    # Cross from left (Side A) to right (Side B)
    p_prev = (0.4, 0.5)
    p_curr = (0.6, 0.5)
    crossing = PolygonGeometry.check_line_crossing(p_prev, p_curr, w_start, w_end)
    assert crossing == TripwireDirection.A_TO_B

    # Cross from right (Side B) to left (Side A)
    crossing_back = PolygonGeometry.check_line_crossing(p_curr, p_prev, w_start, w_end)
    assert crossing_back == TripwireDirection.B_TO_A
