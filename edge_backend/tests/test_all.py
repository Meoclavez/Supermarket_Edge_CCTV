import unittest
import time
from pathlib import Path
import sys

BASE_DIR = Path("/home/meoclavezz/Projects-1/Edge_AI_CCTV/edge_backend")
sys.path.insert(0, str(BASE_DIR))

from fastapi.testclient import TestClient
from app.main import app
from app.services.hardware_detector import HardwareDetector
from app.services.feature_manager import FeatureManager
from app.services.kinematic_fall_engine import KinematicFallEngine
from app.services.ai_zone_service import PolygonGeometry, ai_zone_service
from app.services.camera_network_manager import CameraScanner
from app.models.schemas import Point2D, CameraFeatureConfig, EventType, EventSeverity

class TestHardwareAndFeatures(unittest.TestCase):
    def test_hardware_detector(self):
        profile = HardwareDetector.detect_hardware()
        self.assertGreater(profile.total_ram_gb, 0)
        self.assertIn(profile.decoder_type, ["cuda", "vaapi_intel", "vaapi_amd", "cpu"])
        self.assertIn(profile.ring_buffer_seconds, [3, 5, 10])

    def test_feature_manager(self):
        fm = FeatureManager()
        cfg = CameraFeatureConfig(fall_detection=True, door_monitoring=False)
        fm.set_camera_features("cam_test", cfg)
        self.assertTrue(fm.get_camera_features("cam_test").fall_detection)
        cfg.fall_detection = False
        fm.set_camera_features("cam_test", cfg)
        self.assertFalse(fm.get_camera_features("cam_test").fall_detection)

class TestZonesAndKinematics(unittest.TestCase):
    def test_polygon_geometry_and_crossing(self):
        poly = [Point2D(x=0.2, y=0.2), Point2D(x=0.8, y=0.2), Point2D(x=0.8, y=0.8), Point2D(x=0.2, y=0.8)]
        self.assertTrue(PolygonGeometry.is_point_in_polygon(0.5, 0.5, poly))
        self.assertFalse(PolygonGeometry.is_point_in_polygon(0.1, 0.1, poly))

        # Line crossing
        p1, p2 = Point2D(x=0.0, y=0.5), Point2D(x=1.0, y=0.5)
        q1, q2 = Point2D(x=0.5, y=0.2), Point2D(x=0.5, y=0.8)
        crossed, direction = PolygonGeometry.check_line_crossing(p1, p2, q1, q2)
        self.assertTrue(crossed)
        self.assertIsNotNone(direction)

    def test_kinematic_fall(self):
        engine = KinematicFallEngine()
        standing = [(100.0, 50.0 + i*10, 0.9) for i in range(17)]
        bbox_standing = (80.0, 40.0, 120.0, 220.0)
        self.assertIsNone(engine.evaluate_pose("cam1", "Living Room", 1, standing, bbox_standing))

        time.sleep(0.1)
        fallen = [(100.0, 300.0, 0.9) for _ in range(17)]
        bbox_fallen = (40.0, 280.0, 200.0, 320.0)
        evt = engine.evaluate_pose("cam1", "Living Room", 1, fallen, bbox_fallen)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.event_type, EventType.FALL_DETECTED)

class TestScannerAndAPIs(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_camera_scanner(self):
        sources = CameraScanner.discover_all()
        self.assertGreater(len(sources), 0)
        self.assertTrue(any(s["type"] == "SYNTHETIC" for s in sources))

    def test_dashboard_and_studio_html(self):
        res1 = self.client.get("/dashboard")
        self.assertEqual(res1.status_code, 200)
        self.assertIn("EDGE AI CCTV", res1.text)

        res2 = self.client.get("/dashboard/studio")
        self.assertEqual(res2.status_code, 200)
        self.assertIn("interactiveCanvas", res2.text)

    def test_zones_api_persistence(self):
        # Create tripwire
        res = self.client.post("/api/zones/tripwire", json={
            "name": "Front Porch Line",
            "x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5,
            "direction": "BIDIRECTIONAL"
        })
        self.assertEqual(res.status_code, 200)
        tw_id = res.json()["tripwire"]["id"]

        # Fetch zones
        get_res = self.client.get("/api/zones")
        self.assertEqual(get_res.status_code, 200)
        tripwires = get_res.json()["tripwires"]
        self.assertTrue(any(tw["id"] == tw_id for tw in tripwires))

        # Delete tripwire
        del_res = self.client.delete(f"/api/zones/tripwire/{tw_id}")
        self.assertEqual(del_res.status_code, 200)

    def test_cameras_scan_endpoint(self):
        res = self.client.post("/api/v1/cameras/scan")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertGreater(len(data["sources"]), 0)

if __name__ == "__main__":
    unittest.main()
