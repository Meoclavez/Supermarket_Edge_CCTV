import unittest
import time
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from fastapi.testclient import TestClient
from app.main import app
from app.services.hardware_detector import HardwareDetector
from app.services.feature_manager import FeatureManager
from app.services.ai_zone_service import PolygonGeometry, ai_zone_service
from app.models.schemas import Point2D, CameraFeatureConfig

class TestHardwareAndFeatures(unittest.TestCase):
    def test_hardware_detector(self):
        profile = HardwareDetector.detect_hardware()
        self.assertGreater(profile.total_ram_gb, 0)
        self.assertIn(profile.decoder_type, ["cuda", "vaapi_intel", "vaapi_amd", "cpu"])
        self.assertIn(profile.ring_buffer_seconds, [3, 5, 10])

    def test_feature_manager(self):
        fm = FeatureManager()
        cfg = CameraFeatureConfig(theft_detection=True, shelf_interaction=False)
        fm.set_camera_features("cam_test", cfg)
        self.assertTrue(fm.is_enabled("cam_test", "theft_detection"))
        self.assertFalse(fm.is_enabled("cam_test", "shelf_interaction"))
        cfg.theft_detection = False
        fm.set_camera_features("cam_test", cfg)
        self.assertFalse(fm.get_camera_features("cam_test").theft_detection)

class TestZones(unittest.TestCase):
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

class TestScannerAndAPIs(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_dashboard_and_studio_html(self):
        res1 = self.client.get("/dashboard")
        self.assertEqual(res1.status_code, 200)
        self.assertIn("Edge AI CCTV", res1.text)

        res2 = self.client.get("/dashboard/studio")
        self.assertEqual(res2.status_code, 200)
        self.assertIn("interactiveCanvas", res2.text)

        # analytics.html was a duplicate of index.html and is gone; both
        # analytics routes serve the one dashboard page.
        for path in ("/dashboard/analytics", "/analytics"):
            res3 = self.client.get(path)
            self.assertEqual(res3.status_code, 200)
            self.assertEqual(res3.text, res1.text)

    def test_privacy_mask_api_persistence(self):
        res = self.client.post("/api/zones/exclusion", json={
            "name": "Staff room doorway",
            "camera_id": "cam_test",
            "points": [{"x": 0.1, "y": 0.1}, {"x": 0.4, "y": 0.1}, {"x": 0.4, "y": 0.5}],
            "mask_mode": "BLUR",
        })
        self.assertEqual(res.status_code, 200)
        mask_id = res.json()["exclusion_mask"]["id"]

        get_res = self.client.get("/api/zones")
        self.assertEqual(get_res.status_code, 200)
        self.assertTrue(any(m["id"] == mask_id for m in get_res.json()["exclusion_masks"]))

        del_res = self.client.delete(f"/api/zones/exclusion/{mask_id}")
        self.assertEqual(del_res.status_code, 200)

    def test_every_mask_mode_persists_and_can_be_changed(self):
        from app.services.ai_zone_service import AIZoneService
        pts = [{"x": 0.1, "y": 0.1}, {"x": 0.4, "y": 0.1}, {"x": 0.4, "y": 0.5}]
        created = []
        for mode in ("BLUR", "MOSAIC", "BLACKOUT", "COLOR", "AI_IGNORE"):
            body = {"name": f"mask {mode}", "camera_id": "cam_modes", "points": pts, "mask_mode": mode}
            if mode == "COLOR":
                body["mask_color_bgr"] = [10, 200, 30]
            res = self.client.post("/api/zones/exclusion", json=body)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["exclusion_mask"]["mask_mode"], mode)
            created.append(res.json()["exclusion_mask"]["id"])

        # Persisted to disk: a fresh service instance reads the same modes back.
        stored = {m["id"]: m for m in AIZoneService().get_all_zones("cam_modes")["exclusion_masks"]}
        self.assertEqual([stored[i]["mask_mode"] for i in created], ["BLUR", "MOSAIC", "BLACKOUT", "COLOR", "AI_IGNORE"])
        self.assertEqual(stored[created[3]]["mask_color_bgr"], [10, 200, 30])

        # Unknown modes are rejected, not stored as something the renderer guesses at.
        bad = self.client.post("/api/zones/exclusion", json={"camera_id": "cam_modes", "points": pts, "mask_mode": "SEPIA"})
        self.assertEqual(bad.status_code, 422)
        bad_cam = self.client.post("/api/v1/cameras/cam_modes/zones", json={"zone_type": "EXCLUSION", "points": pts, "mask_mode": "SEPIA"})
        self.assertEqual(bad_cam.status_code, 422)

        # Change the mode of an existing mask in place.
        upd = self.client.patch(f"/api/zones/exclusion/{created[0]}", json={"mask_mode": "AI_IGNORE"})
        self.assertEqual(upd.status_code, 200, upd.text)
        self.assertEqual(upd.json()["exclusion_mask"]["mask_mode"], "AI_IGNORE")
        self.assertEqual(upd.json()["exclusion_mask"]["points"], pts)
        self.assertEqual(self.client.patch("/api/zones/exclusion/nope", json={"mask_mode": "BLUR"}).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/zones/exclusion/{created[0]}", json={"mask_mode": "SEPIA"}).status_code, 422)

        for i in created:
            self.assertEqual(self.client.delete(f"/api/zones/exclusion/{i}").status_code, 200)

    def test_cameras_scan_endpoint(self):
        """Scan reports what discovery actually found -- possibly nothing.

        This used to assert at least one source, which only held because the
        endpoint returned a canned list. It now performs real USB/ONVIF
        discovery, so on a machine with no cameras attached zero is the correct
        and honest answer.
        """
        res = self.client.post("/api/v1/cameras/scan")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIsInstance(data["sources"], list)
        self.assertGreaterEqual(data["count"], 0)
        self.assertEqual(data["count"], len(data["sources"]))
        for source in data["sources"]:
            self.assertIn("id", source)
            self.assertIn("transport", source)
            self.assertIn("driver", source)
            self.assertIn("reachable", source)
            self.assertIsInstance(source["stream_urls"], list)

if __name__ == "__main__":
    unittest.main()
