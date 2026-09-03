"""Unit tests for product shelf zone mapping and hand tracking."""

import pytest
from app.services.shelf_interaction_service import (
    shelf_interaction_service,
    ProductShelfZone,
    PointCoord,
    StudyMetricsConfig
)


def test_product_shelf_zone_crud():
    test_zone = ProductShelfZone(
        id="test_shelf_zone_99",
        camera_id="cam_01",
        name="Test Chocolate Bar 50g",
        points=[
            PointCoord(x=0.1, y=0.1),
            PointCoord(x=0.4, y=0.1),
            PointCoord(x=0.4, y=0.4),
            PointCoord(x=0.1, y=0.4)
        ],
        sku_id="SKU-CHOC-50G",
        category="Snacks",
        price=2.50,
        facing_count=5,
        shelf_tier="EYE_LEVEL",
        study_metrics=StudyMetricsConfig(
            track_hand_reach=True,
            track_dwell_time=True,
            track_put_back_friction=True
        )
    )

    # Save
    saved = shelf_interaction_service.save_zone(test_zone)
    assert saved.id == "test_shelf_zone_99"

    # Get
    fetched = shelf_interaction_service.get_zone("test_shelf_zone_99")
    assert fetched is not None
    assert fetched.name == "Test Chocolate Bar 50g"

    # Stats
    stats = shelf_interaction_service.get_zone_stats("test_shelf_zone_99")
    assert stats["sku_id"] == "SKU-CHOC-50G"
    assert "friction_index" in stats

    # Delete
    deleted = shelf_interaction_service.delete_zone("test_shelf_zone_99")
    assert deleted is True
    assert shelf_interaction_service.get_zone("test_shelf_zone_99") is None


def test_hand_reach_state_transitions():
    # Setup test zone
    zone = ProductShelfZone(
        id="test_reach_zone",
        camera_id="cam_test",
        name="Test Juice 1L",
        points=[
            PointCoord(x=0.3, y=0.3),
            PointCoord(x=0.7, y=0.3),
            PointCoord(x=0.7, y=0.7),
            PointCoord(x=0.3, y=0.7)
        ],
        sku_id="SKU-JUICE-1L",
        category="Beverages",
        price=3.99,
        facing_count=4
    )
    shelf_interaction_service.save_zone(zone)

    # 1. Wrist outside polygon
    outside_keypoints = [(0.1, 0.1, 0.9)] * 17
    bbox = (0.05, 0.05, 0.25, 0.6)
    events = shelf_interaction_service.process_person_pose("cam_test", 101, outside_keypoints, bbox, now_ts=100.0)
    assert len(events) == 0

    # 2. Right wrist (index 10) enters polygon -> REACH_IN event
    inside_keypoints = [(0.1, 0.1, 0.9)] * 17
    inside_keypoints[10] = (0.5, 0.5, 0.95)  # Right wrist in center of zone
    events = shelf_interaction_service.process_person_pose("cam_test", 101, inside_keypoints, bbox, now_ts=100.5)
    assert len(events) == 1
    assert events[0].action_type == "REACH_IN"
    assert events[0].zone_id == "test_reach_zone"

    # 3. Dwell inspection for >= 1s -> INSPECT_DWELL event
    events = shelf_interaction_service.process_person_pose("cam_test", 101, inside_keypoints, bbox, now_ts=102.0)
    assert len(events) == 1
    assert events[0].action_type == "INSPECT_DWELL"

    # 4. Wrist exits -> ITEM_PICK event
    events = shelf_interaction_service.process_person_pose("cam_test", 101, outside_keypoints, bbox, now_ts=103.5)
    assert len(events) == 1
    assert events[0].action_type == "ITEM_PICK"

    # Cleanup
    shelf_interaction_service.delete_zone("test_reach_zone")
