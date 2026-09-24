"""Close-up people: the person-size gate is per camera and trusts a skeleton.

``PERSON_MAX_FRAME_FRACTION`` (0.35) dropped a shopper standing ~2 m from a
shelf-facing camera. The limit is now a per-camera setting
(``cameras.features.person_max_frame_fraction``, edited in the dashboard's
camera Config modal) with the global value as default, and a larger box is
still a person when its keypoints form a coherent skeleton.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import live_analytics_engine as lae
from app.services.feature_manager import feature_manager
from app.services.inference_backend import Detection, PersonDetector, _has_coherent_skeleton

W, H = 1280, 720
AREA = float(W * H)
CAM = "cam_close_shelf_01"


def _detector():
    return PersonDetector(model_path="/nonexistent/model.onnx", object_model_path="")


def _close_person(head=True, hips=True, vis=0.9):
    """Keypoints of a shopper filling most of the frame (box 200,0 - 900,720)."""
    k = np.zeros((17, 3), dtype=np.float32)
    k[5] = (420, 260, vis)
    k[6] = (680, 260, vis)
    k[7] = (380, 420, vis)
    k[8] = (720, 420, vis)
    if head:
        k[0] = (550, 120, vis)
        k[1] = (530, 100, vis)
        k[2] = (570, 100, vis)
    if hips:
        k[11] = (460, 600, vis)
        k[12] = (640, 600, vis)
    return k


BOX = (200.0, 0.0, 900.0, 720.0)            # 700 x 720 = 0.547 of the frame, h/w 1.03


# --------------------------------------------------------------------------- detector gate

def test_skeleton_helper_needs_shoulders_plus_head_or_hips():
    assert _has_coherent_skeleton(_close_person())
    assert _has_coherent_skeleton(_close_person(head=False))
    assert _has_coherent_skeleton(_close_person(hips=False))
    assert not _has_coherent_skeleton(_close_person(head=False, hips=False))
    k = _close_person()
    k[6, 2] = 0.1                                  # one shoulder missing
    assert not _has_coherent_skeleton(k)
    assert not _has_coherent_skeleton(None)


def test_large_box_without_keypoints_is_still_rejected_by_default():
    d = _detector()
    assert (BOX[2] - BOX[0]) * (BOX[3] - BOX[1]) / AREA > settings.PERSON_MAX_FRAME_FRACTION
    assert not d._is_plausible_person(*BOX, AREA)
    assert not d._is_plausible_person(*BOX, AREA, _close_person(head=False, hips=False))


def test_large_box_with_a_coherent_skeleton_is_a_close_person():
    d = _detector()
    assert d._is_plausible_person(*BOX, AREA, _close_person())
    assert d._is_plausible_person(*BOX, AREA, _close_person(hips=False))     # hips below the frame
    # ... but not a box covering (almost) the whole frame.
    assert not d._is_plausible_person(0.0, 0.0, 1280.0, 720.0, AREA, _close_person())


def test_per_camera_fraction_overrides_the_global_default():
    d = _detector()
    tall = (300.0, 0.0, 800.0, 720.0)              # 500 x 720 = 0.39 of the frame, upright
    assert not d._is_plausible_person(*tall, AREA)
    assert d._is_plausible_person(*tall, AREA, None, 0.6)
    # A camera may also be stricter than the default.
    small = (100.0, 100.0, 300.0, 500.0)           # 0.087 of the frame
    assert d._is_plausible_person(*small, AREA)
    assert not d._is_plausible_person(*small, AREA, None, 0.05)
    # A per-camera limit above the skeleton ceiling wins for skeleton boxes too.
    assert d._is_plausible_person(0.0, 0.0, 1280.0, 700.0, AREA, _close_person(), 1.0)


# --------------------------------------------------------------------------- setting + API

@pytest.fixture
def api():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    client = TestClient(app)
    client.delete(f"/api/v1/cameras/{CAM}")
    feature_manager.remove_camera(CAM)
    r = client.put(f"/api/v1/cameras/{CAM}", json={"id": CAM, "name": "Shelf close-up", "location": "Aisle 2"})
    assert r.status_code == 200, r.text
    yield client
    client.delete(f"/api/v1/cameras/{CAM}")
    feature_manager.remove_camera(CAM)


def _features(client):
    return client.get(f"/api/v1/cameras/{CAM}").json()["features"]


def test_setting_round_trips_and_survives_toggle_only_clients(api):
    assert _features(api)["person_max_frame_fraction"] is None
    assert feature_manager.get_setting(CAM, "person_max_frame_fraction") is None

    body = {"people_counting": True, "shelf_interaction": True, "theft_detection": True,
            "person_max_frame_fraction": 0.6}
    assert api.put(f"/api/v1/cameras/{CAM}/features", json=body).json()["person_max_frame_fraction"] == 0.6
    assert _features(api)["person_max_frame_fraction"] == 0.6
    assert feature_manager.get_setting(CAM, "person_max_frame_fraction") == 0.6

    # The mobile app sends only the toggles: the setting is kept.
    r = api.put(f"/api/v1/cameras/{CAM}/features",
                json={"people_counting": True, "shelf_interaction": False, "theft_detection": True})
    assert r.json()["person_max_frame_fraction"] == 0.6 and r.json()["shelf_interaction"] is False
    # So does a full camera PUT whose features omit it.
    cam = api.get(f"/api/v1/cameras/{CAM}").json()
    cam["features"] = {"people_counting": True, "shelf_interaction": True, "theft_detection": True}
    assert api.put(f"/api/v1/cameras/{CAM}", json=cam).status_code == 200
    assert _features(api)["person_max_frame_fraction"] == 0.6

    # Explicit null returns to the server default; out-of-range is refused.
    body["person_max_frame_fraction"] = None
    assert api.put(f"/api/v1/cameras/{CAM}/features", json=body).json()["person_max_frame_fraction"] is None
    assert feature_manager.get_setting(CAM, "person_max_frame_fraction") is None
    body["person_max_frame_fraction"] = 1.5
    assert api.put(f"/api/v1/cameras/{CAM}/features", json=body).status_code == 422


def test_setting_is_read_from_the_stored_row_after_a_restart(api):
    api.put(f"/api/v1/cameras/{CAM}/features", json={
        "people_counting": True, "shelf_interaction": True, "theft_detection": True,
        "person_max_frame_fraction": 0.55})
    feature_manager.remove_camera(CAM)            # cold cache, as after a restart
    assert feature_manager.get_setting(CAM, "person_max_frame_fraction") == 0.55


def test_live_worker_passes_the_camera_setting_to_the_detector(api, monkeypatch):
    seen = []

    def detect(frame, **kw):
        seen.append(kw)
        return [Detection(x1=100.0, y1=50.0, x2=180.0, y2=300.0, confidence=0.9, keypoints=None)]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    monkeypatch.setitem(lae._pose_state, "module", None)
    engine = lae.LiveAnalyticsEngine()
    rt = lae.CameraRuntime(camera_id=CAM, name="Shelf close-up", source="/dev/null")
    engine.runtimes[CAM] = rt
    worker = lae.CameraWorker(rt, engine)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)

    worker._analyse(frame, now=5000.0)
    assert "max_frame_fraction" not in seen[-1]          # default: the detector's global value

    api.put(f"/api/v1/cameras/{CAM}/features", json={
        "people_counting": True, "shelf_interaction": True, "theft_detection": True,
        "person_max_frame_fraction": 0.7})
    worker._analyse(frame, now=5001.0)
    assert seen[-1]["max_frame_fraction"] == 0.7
