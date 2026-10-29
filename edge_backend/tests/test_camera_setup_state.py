"""Place, then calibrate: the camera setup states every setup screen reports.

Pins services/camera_setup_state.py, which the Today banner, the Getting
started checklist, the Features tab and the Store map all read:

* (a) not placed, (b) placed but not calibrated, (c) calibrated are separate;
* "all placed" is never reported as done while a camera is uncalibrated, and a
  "Counts into zone" link is not calibration;
* the counts in the text are the real counts ("32 cameras placed, 0 calibrated");
* each open step names the action that fixes it (place-cameras / calibrate:<id>);
* GET /api/v1/layout setup and GET /api/v1/analytics/overview coverage carry it.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database import async_session_factory
from app.main import app
from app.models.db_models import CameraModel
from app.services.auth_service import auth_service
from app.services.auth_service import general_rate_limiter
from app.services.camera_setup_state import (STEP_ADD, STEP_CALIBRATE, STEP_DONE, STEP_PLACE,
                                             camera_setup_state)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static" / "js"
H = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def cam(i, placed=True, calibrated=False, zone=None):
    return {"camera_id": f"c{i}", "name": f"Cam {i}", "map_placed": placed,
            "has_homography": calibrated, "watch_zone_id": zone}


def test_no_cameras():
    st = camera_setup_state([])
    assert st["step"] == STEP_ADD and st["action"]["go"] == "add-cameras" and st["total"] == 0


def test_pearcedale_all_placed_none_calibrated_is_a_calibration_warning():
    st = camera_setup_state([cam(i) for i in range(32)])
    assert st["step"] == STEP_CALIBRATE and st["level"] == "todo"
    assert (st["placed"], st["unplaced"], st["calibrated"], st["placed_uncalibrated"]) == (32, 0, 0, 32)
    assert st["counts"] == "32 cameras placed, 0 calibrated"
    assert st["headline"].startswith("32 cameras placed, 0 calibrated: placing is done, calibration is not")
    assert "none is calibrated yet" in st["detail"] and "body heights" in st["detail"]
    assert "estimate" in st["detail"] and "floor metres" in st["detail"]
    assert st["action"] == {"go": "calibrate:c0", "label": "Calibrate a camera"}
    assert "place" not in st["action"]["go"]


def test_not_placed_comes_first_and_says_calibrate_next():
    st = camera_setup_state([cam(1, placed=False), cam(2), cam(3, calibrated=True)])
    assert st["step"] == STEP_PLACE and st["level"] == "partial"
    assert st["counts"] == "2 of 3 cameras placed, 1 calibrated"
    assert st["action"]["go"] == "place-cameras"
    assert 'starting with "Cam 1"' in st["detail"] and "Then calibrate it" in st["detail"]
    assert st["first_unplaced"] == {"camera_id": "c1", "name": "Cam 1"}
    # The camera to calibrate next is one already on the map.
    assert st["first_uncalibrated"]["camera_id"] == "c2"


def test_none_placed():
    st = camera_setup_state([cam(1, placed=False), cam(2, placed=None)])
    assert st["step"] == STEP_PLACE and st["level"] == "todo" and st["unplaced"] == 2
    assert st["headline"] == "No camera is on the store map yet"


def test_partly_calibrated_and_zone_link_is_not_calibration():
    st = camera_setup_state([cam(1, calibrated=True), cam(2, zone="z1"), cam(3, zone="gone")], zone_ids={"z1"})
    assert st["step"] == STEP_CALIBRATE and st["level"] == "partial"
    assert st["counts"] == "3 cameras placed, 1 calibrated" and st["zone_linked"] == 1
    assert "2 are not calibrated yet" in st["detail"] and "not calibration" in st["detail"]
    # Every uncalibrated camera linked to an area still leaves the step open.
    st2 = camera_setup_state([cam(1, zone="z1")], zone_ids={"z1"})
    assert st2["step"] == STEP_CALIBRATE and st2["counts"] == "1 camera placed, 0 calibrated"


def test_all_calibrated_is_done():
    st = camera_setup_state([cam(1, calibrated=True), cam(2, calibrated=True)])
    assert st["step"] == STEP_DONE and st["level"] == "done" and st["action"] is None
    assert st["headline"] == "2 cameras placed, 2 calibrated."


def test_model_rows_are_read_like_dicts():
    rows = [CameraModel(id="m1", name="Door", map_placed=True, homography_matrix=H),
            CameraModel(id="m2", name="Till", map_placed=True, homography_matrix=None)]
    st = camera_setup_state(rows)
    assert (st["placed"], st["calibrated"]) == (2, 1) and st["action"]["go"] == "calibrate:m2"


def _add(rows):
    async def go():
        async with async_session_factory() as s:
            for r in rows:
                s.add(r)
            await s.commit()
    asyncio.run(go())


def _drop(ids):
    async def go():
        async with async_session_factory() as s:
            await s.execute(delete(CameraModel).where(CameraModel.id.in_(ids)))
            await s.commit()
    asyncio.run(go())


def test_layout_and_overview_report_the_same_counts(client, auth_headers):
    general_rate_limiter.history.clear()
    base_layout = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]["camera_setup"]
    tag = uuid.uuid4().hex[:6]
    ids = [f"cs_{tag}_{i}" for i in range(3)]
    _add([CameraModel(id=ids[0], name="cs placed", location="t", rtsp_url="rtsp://x/1", map_placed=True,
                      floor_x=2, floor_y=2),
          CameraModel(id=ids[1], name="cs placed 2", location="t", rtsp_url="rtsp://x/2", map_placed=True,
                      floor_x=4, floor_y=2),
          CameraModel(id=ids[2], name="cs calibrated", location="t", rtsp_url="rtsp://x/3", map_placed=True,
                      floor_x=6, floor_y=2, homography_matrix=H)])
    try:
        lay = client.get("/api/v1/layout", headers=auth_headers).json()["setup"]
        cs = lay["camera_setup"]
        assert cs["placed"] == base_layout["placed"] + 3 and cs["calibrated"] == base_layout["calibrated"] + 1
        assert lay["cameras_placed"] == cs["placed"] and lay["cameras_calibrated"] == cs["calibrated"]
        cov = client.get("/api/v1/analytics/overview", headers=auth_headers).json()["coverage"]
        assert cov["cameras_placed"] == cs["placed"] and cov["camera_setup"]["counts"] == cs["counts"]
        assert cov["camera_setup"]["step"] == cs["step"]
    finally:
        _drop(ids)


def test_dashboard_never_says_place_when_it_means_calibrate():
    today = (STATIC / "today.js").read_text(encoding="utf-8")
    # The old banner said "no camera is placed" when no camera was CALIBRATED.
    assert "no camera is placed on the store map yet" not in today
    assert "data-help-go=\"place-cameras\"" not in today or "cs.step === 'place'" in today
    assert "'Calibrate a camera'" in today
    help_js = (STATIC / "help.js").read_text(encoding="utf-8")
    assert "case 'place-cameras':" in help_js and "a === 'calibrate-next'" in help_js
    assert "window.EdgeCameraGallery = { showUnplaced }" in (STATIC / "camera_gallery.js").read_text(encoding="utf-8")
    analytics = (STATIC / "analytics.js").read_text(encoding="utf-8")
    assert "'on the map' : 'not on the map yet'" not in analytics
    content = (STATIC / "help_content.js").read_text(encoding="utf-8")
    place = re.search(r"id: 'place-cameras'.*?go: '([a-z-]+)'", content, re.S)
    cal = re.search(r"id: 'calibrate', title.*?go: '([a-z-]+)'", content, re.S)
    assert place and place.group(1) == "place-cameras" and cal and cal.group(1) == "calibrate-next"
    assert "Step 2 of 2" in content
