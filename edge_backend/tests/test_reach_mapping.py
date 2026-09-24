"""Ground truth for hand-reach mapping: reach -> zone / SKU / shelf level -> analytics.

Synthetic but geometrically faithful: every skeleton is built from human
proportions (upper arm 0.19 H, forearm 0.16 H, shoulders at 0.18 H, hips at
0.52 H) with the arm posed by joint angles, so wrists, elbows and the hand tip
land where a real pose model would put them. Scripted keypoint tracks cover
reaches to the top, middle and bottom shelf with both hands, boundary jitter,
occlusion by the shopper's own body, an overhead arm, a foreshortened reach
that projects over the torso, a phone held over a shelf zone, a shopper
walking past with a swinging hand, and a hand in the overlap of two adjacent
shelf polygons. The exact expected ``shelf_interactions`` rows are asserted,
then the downstream numbers (per-product counts, shelf levels, heatmap bins,
funnel engagement) are checked against an independent recomputation.

These skeletons are test inputs only; they are never shown to a user.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import (
    Base, CustomerTrackModel, POSTransactionModel, ShelfInteractionModel, ZoneVisitModel,
)
from app.services.pose_analytics import pose_analytics
from app.services.shelf_interaction_service import (
    PointCoord, ProductShelfZone, shelf_interaction_service,
)
from app.services.timeutil import local_day_bounds_utc, utcnow

W, H_FRAME = 1280, 720
DT = 0.1                      # 10 analysed frames per second
CAM = "cam_rm_side"           # side view of a shelf bay
CAM_BACK = "cam_rm_back"      # camera behind the shopper
FRAME = np.full((H_FRAME, W, 3), 80, dtype=np.uint8)


# --------------------------------------------------------------------------
# Scene: bay A (x 640..940 px) with three stacked product zones, and bay B to
# its right whose MIDDLE zone overlaps bay A's middle zone by 60 px.
# --------------------------------------------------------------------------
def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


ZONES = {
    # id: (camera, name, sku, category, price, shelf_level (None = derive), polygon)
    "rm_top_a": (CAM, "Cereal 500g (top)", "SKU-RM-TOP", "Breakfast", 6.5, None, _rect(0.50, 0.01, 0.734, 0.089)),
    "rm_mid_a": (CAM, "Coffee 250g (eye level)", "SKU-RM-MID", "Coffee", 9.0, None, _rect(0.50, 0.14, 0.734, 0.50)),
    "rm_bot_a": (CAM, "Rice 5kg (bottom)", "SKU-RM-BOT", "Staples", 12.0, None, _rect(0.50, 0.52, 0.734, 0.85)),
    "rm_mid_b": (CAM, "Tea 100 bags", "SKU-RM-TEA", "Tea", 4.0, "MIDDLE", _rect(0.6875, 0.14, 0.92, 0.50)),
    "rm_over": (CAM_BACK, "Snacks (overhead)", "SKU-RM-OVR", "Snacks", 3.0, None, _rect(0.42, 0.10, 0.50, 0.22)),
    "rm_behind": (CAM_BACK, "Biscuits (chest)", "SKU-RM-BIS", "Snacks", 2.0, "MIDDLE", _rect(0.40, 0.36, 0.47, 0.44)),
}


def _save_zones():
    for zid, (cam, name, sku, cat, price, level, poly) in ZONES.items():
        shelf_interaction_service.save_zone(ProductShelfZone(
            id=zid, camera_id=cam, name=name, sku_id=sku, category=cat, price=price,
            shelf_level=level, points=[PointCoord(x=x, y=y) for x, y in poly],
        ))


def _delete_zones():
    for zid in ZONES:
        shelf_interaction_service.delete_zone(zid)


# --------------------------------------------------------------------------
# Skeleton builder (pixel coordinates, COCO order)
# --------------------------------------------------------------------------
REST = (-90.0, -90.0)


def figure(cx, *, H=600.0, top=60.0, half_sh=12.0, half_hip=10.0, crouch=(0.0, 0.0, 0.0),
           right=REST, left=REST, right_pts=None, left_pts=None, vis=0.95,
           rwrist_vis=None, relbow_vis=None, lwrist_vis=None, r_dx=0.0):
    """Standing (or crouching) shopper facing +x.

    ``right`` / ``left`` are (upper-arm angle, forearm angle) in degrees,
    0 = pointing right, 90 = up, -90 = down. ``*_pts`` give (elbow, wrist)
    pixels directly for views where angles are awkward. ``crouch`` =
    (shoulder dx, shoulder dy, hip dy). ``r_dx`` shifts the right elbow and
    wrist sideways (keypoint noise).
    """
    upper, fore = 0.19 * H, 0.16 * H
    sdx, sdy, hdy = crouch
    sy, hy = top + 0.18 * H + sdy, top + 0.52 * H + hdy
    k = np.zeros((17, 3), dtype=np.float32)
    k[:, 2] = vis
    k[0, :2] = (cx + 0.03 * H + sdx, top + 0.07 * H + sdy)
    k[1, :2] = (cx + 0.035 * H + sdx, top + 0.055 * H + sdy)
    k[2, :2] = (cx + 0.025 * H + sdx, top + 0.055 * H + sdy)
    k[3, :2] = (cx + 0.01 * H + sdx, top + 0.06 * H + sdy)
    k[4, :2] = (cx - 0.01 * H + sdx, top + 0.06 * H + sdy)
    k[5, :2] = (cx - half_sh + sdx, sy)
    k[6, :2] = (cx + half_sh + sdx, sy)
    k[11, :2] = (cx - half_hip, hy)
    k[12, :2] = (cx + half_hip, hy)
    k[13, :2] = (cx - half_hip, top + 0.74 * H)
    k[14, :2] = (cx + half_hip, top + 0.74 * H)
    k[15, :2] = (cx - half_hip, top + 0.96 * H)
    k[16, :2] = (cx + half_hip, top + 0.96 * H)

    def arm(sh_idx, el_idx, wr_idx, angles, pts, dx=0.0):
        sx, syy = k[sh_idx, 0], k[sh_idx, 1]
        if pts is not None:
            (ex, ey), (wx, wy) = pts
        else:
            a1, a2 = (math.radians(a) for a in angles)
            ex, ey = sx + upper * math.cos(a1), syy - upper * math.sin(a1)
            wx, wy = ex + fore * math.cos(a2), ey - fore * math.sin(a2)
        k[el_idx, :2] = (ex + dx, ey)
        k[wr_idx, :2] = (wx + dx, wy)

    arm(5, 7, 9, left, left_pts)
    arm(6, 8, 10, right, right_pts, r_dx)
    if rwrist_vis is not None:
        k[10, 2] = rwrist_vis
    if relbow_vis is not None:
        k[8, 2] = relbow_vis
    if lwrist_vis is not None:
        k[9, 2] = lwrist_vis
    return k


def lerp_angles(a, b, n):
    """n poses moving from angles a to angles b (excluding a, including b)."""
    return [tuple(a[j] + (b[j] - a[j]) * (i + 1) / n for j in range(2)) for i in range(n)]


def track_ns(tid, kps, floor):
    xs, ys = kps[:, 0], kps[:, 1]
    return SimpleNamespace(track_id=tid, bbox=(float(xs.min() - 10), float(ys.min() - 20),
                                               float(xs.max() + 10), float(ys.max() + 10)),
                           keypoints=kps, confirmed=True, floor_xy=floor, keypoints_fresh=True)


class Clock:
    """Sequential simulated time inside today's store-local day."""

    def __init__(self):
        start, _end = local_day_bounds_utc()
        now = time.time()
        midnight_ts = (start - datetime(1970, 1, 1)).total_seconds()
        self.t = now - 300 if now - 300 > midnight_ts + 1 else now + 1

    def run(self, cam, tid, poses, floor=None):
        """Feed one skeleton per frame; returns the interactions reported live."""
        got = []
        for kps in poses:
            res = pose_analytics.observe(cam, self.t, FRAME, [track_ns(tid, kps, floor)], [])
            got.extend(res.interactions)
            self.t += DT
        # Let the re-entry window pass with the hand at rest, then gap.
        self.t += 1.0
        return got


def reach(cx, target, *, hand="right", n_in=4, hold=12, n_out=4, rest_before=5, rest_after=8, **kw):
    """Rest -> raise to ``target`` angles -> hold -> lower -> rest."""
    key = "right" if hand == "right" else "left"
    seq = [figure(cx, **kw)] * rest_before
    seq += [figure(cx, **{key: a}, **kw) for a in lerp_angles(REST, target, n_in)]
    seq += [figure(cx, **{key: target}, **kw)] * hold
    seq += [figure(cx, **{key: a}, **kw) for a in lerp_angles(target, REST, n_out)]
    seq += [figure(cx, **kw)] * rest_after
    return seq


MID = (-10.0, -10.0)      # straight arm, slightly down: wrist (738.8, 204.5), tip (771.9, 210.3)
TOP = (20.0, 35.0)        # raised: wrist (705.7, 73.9) just BELOW the top zone, tip (733, 54.6) inside
BOT = (-30.0, -40.0)      # from a crouch: wrist (732, 427), tip (758, 448)
CROUCH = (40.0, 140.0, 100.0)


@pytest.fixture(scope="module", autouse=True)
def schema():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    yield


@pytest.fixture
def scene():
    _save_zones()
    pose_analytics.reset_camera(CAM)
    pose_analytics.reset_camera(CAM_BACK)
    yield
    pose_analytics.reset_camera(CAM)
    pose_analytics.reset_camera(CAM_BACK)
    pose_analytics.flush_now()
    _delete_zones()
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        for r in s.execute(select(ShelfInteractionModel).where(
                ShelfInteractionModel.camera_id.in_([CAM, CAM_BACK, CAM_E2E]))).scalars():
            s.delete(r)
        s.commit()
    eng.dispose()


def _rows(*cams):
    pose_analytics.flush_now()
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        rows = list(s.execute(select(ShelfInteractionModel).where(
            ShelfInteractionModel.camera_id.in_(cams))).scalars())
    eng.dispose()
    return rows


# --------------------------------------------------------------------------
# 0. Geometry sanity: the scripted poses are where the docstrings say
# --------------------------------------------------------------------------
def test_pose_geometry_matches_design():
    k = figure(520, right=MID)
    assert k[10, 0] == pytest.approx(738.8, abs=0.2) and k[10, 1] == pytest.approx(204.5, abs=0.2)
    k = figure(520, left=TOP)
    wrist_y, top_edge = k[9, 1], 0.089 * H_FRAME
    tip_y = k[9, 1] + 0.35 * (k[9, 1] - k[7, 1])
    assert wrist_y > top_edge > tip_y          # wrist below the top shelf, fingers inside it


def test_shelf_levels_derived_from_polygon_position(scene):
    attrs = shelf_interaction_service.attributes()
    assert (attrs["rm_top_a"]["shelf_level"], attrs["rm_top_a"]["shelf_level_source"]) == ("TOP", "derived")
    assert (attrs["rm_mid_a"]["shelf_level"], attrs["rm_mid_a"]["shelf_level_source"]) == ("MIDDLE", "derived")
    assert (attrs["rm_bot_a"]["shelf_level"], attrs["rm_bot_a"]["shelf_level_source"]) == ("BOTTOM", "derived")
    assert (attrs["rm_mid_b"]["shelf_level"], attrs["rm_mid_b"]["shelf_level_source"]) == ("MIDDLE", "operator")
    assert attrs["rm_over"]["shelf_level"] == "TOP"
    assert attrs["rm_behind"]["shelf_level_source"] == "operator"


# --------------------------------------------------------------------------
# 1. Scripted tracks -> exact rows
# --------------------------------------------------------------------------
def _script(clock):
    """Run every scenario; return {track_id: live-reported interactions}."""
    live = {}
    # S1 right hand, eye-level shelf.
    live["t_mid_r"] = clock.run(CAM, "t_mid_r", reach(520, MID), floor=(10.2, 5.1))
    # S2 left hand, top shelf: only the extrapolated hand tip is inside.
    live["t_top_l"] = clock.run(CAM, "t_top_l", reach(520, TOP, hand="left"), floor=(10.4, 5.3))
    # S3 right hand, bottom shelf, from a crouch.
    live["t_bot_r"] = clock.run(CAM, "t_bot_r", reach(520, BOT, crouch=CROUCH), floor=(11.6, 5.2))
    # S4 boundary jitter: tip crosses the zone's left edge (x=640) repeatedly.
    cx = 388.0                               # tip x = cx + 251.9 = 639.9 at r_dx=0 (edge at 640)
    seq = [figure(cx)] * 5 + [figure(cx, right=a) for a in lerp_angles(REST, MID, 4)]
    for d, n in ((15, 3), (-15, 1), (15, 2), (-15, 2), (15, 3), (-15, 1), (15, 2)):
        seq += [figure(cx, right=MID, r_dx=d)] * n
    seq += [figure(cx, right=a) for a in lerp_angles(MID, REST, 4)] + [figure(cx)] * 8
    live["t_jitter"] = clock.run(CAM, "t_jitter", seq, floor=(10.2, 5.1))
    # S5 both hands at once: left top, right middle.
    seq = [figure(520)] * 5
    seq += [figure(520, left=a, right=b) for a, b in zip(lerp_angles(REST, TOP, 4), lerp_angles(REST, MID, 4))]
    seq += [figure(520, left=TOP, right=MID)] * 12
    seq += [figure(520, left=a, right=b) for a, b in zip(lerp_angles(TOP, REST, 4), lerp_angles(MID, REST, 4))]
    seq += [figure(520)] * 8
    live["t_both"] = clock.run(CAM, "t_both", seq, floor=(10.3, 5.2))
    # S7 walking past at ~3 torso lengths/s with the hand swinging over the bottom shelf.
    swing = (-70.0, -65.0)
    seq = [figure(300 + 61.2 * i, right=swing) for i in range(12)]
    live["t_pass"] = clock.run(CAM, "t_pass", seq, floor=(12.0, 5.0))
    # S8 hand occluded by the shopper's own body for 0.6 s mid-reach.
    seq = [figure(520)] * 5 + [figure(520, right=a) for a in lerp_angles(REST, MID, 4)]
    seq += [figure(520, right=MID)] * 5
    seq += [figure(520, right=MID, rwrist_vis=0.08, relbow_vis=0.1)] * 6
    seq += [figure(520, right=MID)] * 5
    seq += [figure(520, right=a) for a in lerp_angles(MID, REST, 4)] + [figure(520)] * 8
    live["t_occl"] = clock.run(CAM, "t_occl", seq, floor=(10.2, 5.4))
    # S9 hand in the overlap of bay A's and bay B's middle zones: B is deeper.
    live["t_overlap"] = clock.run(CAM, "t_overlap", reach(673, MID), floor=(14.1, 5.1))
    # S10 elbow not visible: the wrist alone is tested.
    live["t_noelbow"] = clock.run(CAM, "t_noelbow", reach(520, MID, relbow_vis=0.2), floor=(10.2, 5.1))

    # Camera behind a smaller (further) shopper: frontal torso, H=400.
    back = dict(H=400.0, top=200.0, half_sh=48.0, half_hip=28.0)
    # S6a arm straight up into the overhead shelf (wrist above the shoulders).
    live["t_over"] = clock.run(CAM_BACK, "t_over", reach(520, (80.0, 85.0), **back), floor=(20.1, 8.2))
    # S6b foreshortened reach across the body: wrist projects over the torso box.
    fwd = ((512.0, 280.0), (552.0, 288.0))
    seq = [figure(520, **back)] * 5 + [figure(520, left_pts=fwd, **back)] * 12 + [figure(520, **back)] * 8
    live["t_fwd"] = clock.run(CAM_BACK, "t_fwd", seq, floor=(20.4, 8.3))
    # S6c phone held at the chest over the same zone: a bent arm is not a reach.
    phone = ((480.0, 330.0), (540.0, 300.0))
    seq = [figure(520, **back)] * 5 + [figure(520, left_pts=phone, **back)] * 20 + [figure(520, **back)] * 5
    live["t_phone"] = clock.run(CAM_BACK, "t_phone", seq, floor=(20.4, 8.3))
    return live


EXPECTED = sorted([
    # (track, hand, zone, sku, shelf_level, contact_point)
    ("t_mid_r", "right", "rm_mid_a", "SKU-RM-MID", "MIDDLE", "hand_tip"),
    ("t_top_l", "left", "rm_top_a", "SKU-RM-TOP", "TOP", "hand_tip"),
    ("t_bot_r", "right", "rm_bot_a", "SKU-RM-BOT", "BOTTOM", "hand_tip"),
    ("t_jitter", "right", "rm_mid_a", "SKU-RM-MID", "MIDDLE", "hand_tip"),
    ("t_both", "left", "rm_top_a", "SKU-RM-TOP", "TOP", "hand_tip"),
    ("t_both", "right", "rm_mid_a", "SKU-RM-MID", "MIDDLE", "hand_tip"),
    ("t_occl", "right", "rm_mid_a", "SKU-RM-MID", "MIDDLE", "hand_tip"),
    ("t_overlap", "right", "rm_mid_b", "SKU-RM-TEA", "MIDDLE", "hand_tip"),
    ("t_noelbow", "right", "rm_mid_a", "SKU-RM-MID", "MIDDLE", "wrist"),
    ("t_over", "right", "rm_over", "SKU-RM-OVR", "TOP", "hand_tip"),
    ("t_fwd", "left", "rm_behind", "SKU-RM-BIS", "MIDDLE", "hand_tip"),
])


def test_scripted_reaches_produce_exact_rows(scene):
    live = _script(Clock())
    pose_analytics.reset_camera(CAM)
    pose_analytics.reset_camera(CAM_BACK)
    rows = _rows(CAM, CAM_BACK)
    got = sorted((r.person_track_id, r.hand, r.shelf_zone_id, r.sku_id, r.shelf_level, r.contact_point) for r in rows)
    assert got == EXPECTED

    # What the engine was told live matches what was persisted (one per reach).
    reported = sorted((t, z) for t, items in live.items() for (_tid, z) in items)
    assert reported == sorted((e[0], e[2]) for e in EXPECTED)
    assert live["t_pass"] == [] and live["t_phone"] == []

    by = {(r.person_track_id, r.hand): r for r in rows}
    for r in rows:
        assert r.zone_name == ZONES[r.shelf_zone_id][1]
        assert r.product_category == ZONES[r.shelf_zone_id][3].upper()
        assert r.camera_id == ZONES[r.shelf_zone_id][0]
        assert r.action_type == "REACH" and r.zone_space == "image"
        assert r.started_at <= r.ended_at and r.duration_sec >= 0.2
        assert r.floor_x is not None and r.floor_y is not None
        # Stored image position is the contact point, inside the attributed zone.
        x0, y0 = ZONES[r.shelf_zone_id][6][0]
        x1, y1 = ZONES[r.shelf_zone_id][6][2]
        assert x0 <= r.image_x <= x1 and y0 <= r.image_y <= y1
        # Stored as naive UTC: within the simulated minutes around "now" in UTC
        # (a local-time write would be off by the UTC offset).
        assert abs((r.timestamp - utcnow()).total_seconds()) < 400

    # Held reach: the tip is in the zone for the 12-frame hold plus the last
    # raise frame(s); duration is measured from entry to last in-zone frame.
    assert 1.0 <= by[("t_mid_r", "right")].duration_sec <= 1.75
    # Jitter did not split the reach, and it spans the whole jittery episode.
    assert by[("t_jitter", "right")].duration_sec >= 1.2
    # Occlusion did not split the reach: one row spanning hold + occlusion.
    assert by[("t_occl", "right")].duration_sec >= 1.5


def test_hand_tip_is_what_catches_the_top_shelf(scene, monkeypatch):
    """Wrist-only testing (the old behaviour) misses the top-shelf reach entirely."""
    monkeypatch.setattr(settings, "INTERACTION_HAND_EXTEND_FRAC", 0.0)
    live = Clock().run(CAM, "t_top_wrist_only", reach(520, TOP, hand="left"))
    assert live == []


def test_old_rules_would_have_split_or_invented_reaches(scene, monkeypatch):
    """Each new rule is load-bearing: switching it off changes the count."""
    clock = Clock()
    # No re-entry merge -> the jittery reach splits in two.
    monkeypatch.setattr(settings, "INTERACTION_REENTRY_MERGE_SEC", 0.0)
    cx = 388.0
    seq = [figure(cx)] * 5 + [figure(cx, right=a) for a in lerp_angles(REST, MID, 4)]
    for d, n in ((15, 3), (-15, 2), (15, 3)):
        seq += [figure(cx, right=MID, r_dx=d)] * n
    seq += [figure(cx)] * 8
    assert len(clock.run(CAM, "t_jit_nomerge", seq)) == 2
    monkeypatch.setattr(settings, "INTERACTION_REENTRY_MERGE_SEC", 0.5)
    assert len(clock.run(CAM, "t_jit_merge", seq)) == 1
    # No walking filter -> the swinging hand of a passer-by counts.
    monkeypatch.setattr(settings, "INTERACTION_MAX_BODY_SPEED", 1e9)
    seq = [figure(300 + 61.2 * i, right=(-70.0, -65.0)) for i in range(12)]
    assert len(clock.run(CAM, "t_pass_nofilter", seq)) == 1


# --------------------------------------------------------------------------
# 2. Downstream analytics from those rows
# --------------------------------------------------------------------------
def _layout_dims(client):
    lay = client.get("/api/v1/layout").json()
    return float(lay["width_m"]), float(lay["height_m"])


def test_analytics_numbers_match_the_rows(scene):
    _script(Clock())
    pose_analytics.reset_camera(CAM)
    pose_analytics.reset_camera(CAM_BACK)
    rows = _rows(CAM, CAM_BACK)
    assert len(rows) == len(EXPECTED)

    with TestClient(app) as client:
        # Per product, per camera.
        s = client.get(f"/api/v1/analytics/products/summary?camera_id={CAM}").json()
        per = {p["zone_id"]: p for p in s["products"]}
        assert {z: per[z]["reaches"] for z in ("rm_top_a", "rm_mid_a", "rm_bot_a", "rm_mid_b")} == \
            {"rm_top_a": 2, "rm_mid_a": 5, "rm_bot_a": 1, "rm_mid_b": 1}
        assert per["rm_mid_a"]["shoppers"] == 5
        assert (per["rm_mid_a"]["left_hand"], per["rm_mid_a"]["right_hand"]) == (0, 5)
        assert (per["rm_top_a"]["left_hand"], per["rm_top_a"]["right_hand"]) == (2, 0)
        assert per["rm_mid_a"]["shelf_level"] == "MIDDLE" and per["rm_mid_a"]["sku_id"] == "SKU-RM-MID"
        assert s["totals"]["reaches"] == 9 and s["totals"]["products_reached"] == 4
        assert s["totals"]["shoppers_reaching"] == 8        # t_both counted once
        levels = {row["level"]: row for row in s["shelf_levels"]}
        assert (levels["TOP"]["zones"], levels["TOP"]["reaches"]) == (1, 2)
        assert (levels["MIDDLE"]["zones"], levels["MIDDLE"]["reaches"]) == (2, 6)
        assert levels["MIDDLE"]["reaches_per_zone"] == 3.0
        assert (levels["BOTTOM"]["zones"], levels["BOTTOM"]["reaches"]) == (1, 1)
        if not s["pos_connected"]:
            assert per["rm_mid_a"]["conversion_status"] == "needs POS data"
            assert per["rm_mid_a"]["pos_units_sold"] is None

        # Legacy per-zone stats endpoint now reads the database.
        st = client.get("/api/v1/analytics/products/rm_mid_a/stats").json()
        assert st["reaches"] == st["touches"] == 5
        assert st["picks"] is None and st["friction_index"] is None

        # Interaction list carries the attribution.
        lst = client.get(f"/api/v1/analytics/products/interactions?camera_id={CAM}").json()
        assert lst["total"] == 9
        assert {i["shelf_level"] for i in lst["interactions"]} == {"TOP", "MIDDLE", "BOTTOM"}
        assert all(i["started_at"].endswith("Z") for i in lst["interactions"])

        # Zone listing exposes the effective level and its source.
        zl = {z["id"]: z for z in client.get(f"/api/v1/analytics/products/zones?camera_id={CAM}").json()["zones"]}
        assert zl["rm_top_a"]["effective_shelf_level"] == "TOP" and zl["rm_top_a"]["shelf_level_source"] == "derived"

        # Heatmap kind=interaction: recompute the expected grid from every row
        # recorded today and compare cell by cell.
        gw, gh = 50, 30
        wm, hm = _layout_dims(client)
        start, end = local_day_bounds_utc()
        eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
        with Session(eng) as sess:
            all_rows = sess.execute(select(ShelfInteractionModel.floor_x, ShelfInteractionModel.floor_y).where(
                ShelfInteractionModel.timestamp >= start, ShelfInteractionModel.timestamp < end)).all()
        eng.dispose()
        grid = [[0.0] * gw for _ in range(gh)]
        n = 0
        for fx, fy in all_rows:
            if fx is None or fy is None or not (0 <= fx <= wm and 0 <= fy <= hm):
                continue
            grid[min(int(fy / hm * gh), gh - 1)][min(int(fx / wm * gw), gw - 1)] += 1
            n += 1
        peak = max(max(r) for r in grid)
        heat = client.get(f"/api/v1/analytics/heatmaps?kind=interaction&resolution_w={gw}&resolution_h={gh}").json()
        assert heat["observed"] is True and heat["samples"] == n
        assert heat["density_matrix"] == [[round(v / peak, 4) for v in r] for r in grid]
        # Our reaches at (10.2..10.4, 5.1..5.4) share one cell on a 50 x 30 m plan.
        if (wm, hm) == (50.0, 30.0):
            assert grid[5][10] >= 7


def test_pos_conversion_only_with_pos_rows(scene):
    """units per reaching shopper = POS units for the SKU / distinct shoppers who reached it."""
    _script(Clock())
    pose_analytics.reset_camera(CAM)
    pose_analytics.reset_camera(CAM_BACK)
    _rows(CAM, CAM_BACK)
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    with Session(eng) as s:
        s.add(POSTransactionModel(id="pos_rm_1", transaction_id="tx_rm_1", register_id="r1",
                                  sku_id="SKU-RM-MID", quantity=2, amount=18.0, timestamp=utcnow()))
        s.commit()
    try:
        with TestClient(app) as client:
            s2 = client.get(f"/api/v1/analytics/products/summary?camera_id={CAM}").json()
            per = {p["zone_id"]: p for p in s2["products"]}
            assert s2["pos_connected"] is True
            assert per["rm_mid_a"]["pos_units_sold"] == 2
            assert per["rm_mid_a"]["sku_reaching_shoppers"] == 5
            assert per["rm_mid_a"]["units_per_reaching_shopper"] == 0.4
            assert per["rm_top_a"]["pos_units_sold"] == 0
    finally:
        with Session(eng) as s:
            s.delete(s.get(POSTransactionModel, "pos_rm_1"))
            s.commit()
        eng.dispose()


def test_business_rules_over_product_summary():
    from app.services.business_analysis_service import _detect_products

    def prod(name, sku, level, reaches, cam_reaches=40, shoppers=None, units=None, reachers=None):
        return {"zone_id": name, "name": name, "sku_id": sku, "shelf_level": level, "configured": True,
                "enabled": True, "reaches": reaches, "camera_reaches": cam_reaches, "camera_shoppers": shoppers,
                "pos_units_sold": units, "sku_reaching_shoppers": reachers,
                "units_per_reaching_shopper": (units / reachers) if (units is not None and reachers) else None}

    base = {
        "pos_connected": False,
        "products": [prod("Coffee", "S1", "MIDDLE", 30, shoppers=50), prod("Cereal", "S2", "TOP", 4, shoppers=50),
                     prod("Rice", "S3", "BOTTOM", 0, shoppers=50)],
        "shelf_levels": [{"level": "TOP", "zones": 1, "reaches": 4, "reaches_per_zone": 4.0},
                         {"level": "MIDDLE", "zones": 1, "reaches": 30, "reaches_per_zone": 30.0},
                         {"level": "BOTTOM", "zones": 1, "reaches": 0, "reaches_per_zone": 0.0}],
    }
    findings, suppressed = _detect_products(base)
    texts = [f.finding for f in findings]
    assert any("No shopper reached for Rice" in t for t in texts)                 # dead shelf
    assert any(t.startswith("Top-shelf products drew 4.0") for t in texts)        # 4 vs 30 per product
    assert any(t.startswith("Bottom-shelf products drew 0.0") for t in texts)
    assert any("needs POS data" in s for s in suppressed)
    assert not any("POS recorded" in t for t in texts)                            # no invented conversion

    # A camera that recorded almost nothing proves nothing about a dead shelf.
    quiet = dict(base, products=[prod("Rice", "S3", "BOTTOM", 0, cam_reaches=3, shoppers=50)])
    assert not any("No shopper reached" in f.finding for f in _detect_products(quiet)[0])

    # With POS rows: 12 reaching shoppers, 1 unit sold -> high reach, low conversion.
    pos = dict(base, pos_connected=True,
               products=[prod("Coffee", "S1", "MIDDLE", 30, shoppers=50, units=1, reachers=12)])
    f = [x for x in _detect_products(pos)[0] if "POS recorded" in x.finding]
    assert len(f) == 1 and f[0].evidence["units_per_reaching_shopper"] == pytest.approx(1 / 12)


# --------------------------------------------------------------------------
# 3. End to end through the live engine: tracker, zone visit, funnel
# --------------------------------------------------------------------------
CAM_E2E = "cam_rm_e2e"


def test_engine_marks_visit_interacted_and_funnel_counts_it(monkeypatch):
    from app.services import live_analytics_engine as lae
    from app.services.inference_backend import Detection
    from app.services.live_analytics_engine import CameraRuntime, CameraWorker, live_engine
    from app.services.pipeline_supervisor import pipeline_supervisor
    from app.services.tracking_service import floor_projector

    shelf_interaction_service.save_zone(ProductShelfZone(
        id="rm_e2e_mid", camera_id=CAM_E2E, name="E2E Coffee", sku_id="SKU-RM-E2E", category="Coffee",
        shelf_level="MIDDLE", points=[PointCoord(x=x, y=y) for x, y in _rect(0.50, 0.14, 0.734, 0.50)],
    ))
    poses = [figure(520)] * 40 + [figure(520, right=a) for a in lerp_angles(REST, MID, 4)]
    poses += [figure(520, right=MID)] * 15 + [figure(520, right=a) for a in lerp_angles(MID, REST, 4)]
    poses += [figure(520)] * 30
    state = {"i": 0}

    def detect(frame, conf_threshold=None, iou_threshold=None):
        i = state["i"]
        if i >= len(poses):
            return []
        k = poses[i]
        xs, ys = k[:, 0], k[:, 1]
        return [Detection(x1=float(xs.min() - 10), y1=float(ys.min() - 20), x2=float(xs.max() + 10),
                          y2=float(ys.max() + 10), confidence=0.9, keypoints=k.copy())]

    monkeypatch.setattr(lae.person_detector, "detect", detect)
    prev_zones = list(live_engine._zones)
    rt = CameraRuntime(camera_id=CAM_E2E, name="E2E", source="/dev/null")
    live_engine.runtimes[CAM_E2E] = rt
    worker = CameraWorker(rt, live_engine)
    floor_projector.set_homography(CAM_E2E, [[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1]])
    clock = Clock()
    try:
        with TestClient(app) as client:
            # After startup (which publishes the stored blueprint's zones).
            live_engine.set_zones([{"id": "fz_rm_e2e", "category": "AISLE",
                                    "polygon": [{"x": 3, "y": 3}, {"x": 9, "y": 3}, {"x": 9, "y": 9}, {"x": 3, "y": 9}]}])
            before = client.get("/api/v1/analytics/funnels").json()
            eng_before = next(s for s in before["stages"] if s["stage"] == "Engaged")["count"] or 0
            # Run the scripted frames, then enough empty frames for the track to finish.
            for _ in range(len(poses) + settings.TRACK_MAX_AGE_FRAMES + 5):
                worker._analyse(FRAME, now=clock.t)
                state["i"] += 1
                clock.t += DT
            client.portal.call(pipeline_supervisor.flush_once)
            pose_analytics.reset_camera(CAM_E2E)
            rows = _rows(CAM_E2E)
            assert [(r.shelf_zone_id, r.hand, r.shelf_level, r.sku_id) for r in rows] == \
                [("rm_e2e_mid", "right", "MIDDLE", "SKU-RM-E2E")]
            track_id = rows[0].person_track_id

            eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
            with Session(eng) as s:
                visits = list(s.execute(select(ZoneVisitModel).where(ZoneVisitModel.track_id == track_id)).scalars())
                tracks = list(s.execute(select(CustomerTrackModel).where(CustomerTrackModel.track_id == track_id)).scalars())
            eng.dispose()
            assert len(visits) == 1 and visits[0].zone_id == "fz_rm_e2e" and visits[0].interacted is True
            assert len(tracks) == 1
            # Reach row floor position is the shopper's projected foot point
            # (box bottom-centre; the box widens with the extended arm).
            assert 5.0 <= rows[0].floor_x <= 6.5 and rows[0].floor_y == pytest.approx(6.46, abs=0.05)

            after = client.get("/api/v1/analytics/funnels").json()
            eng_after = next(s for s in after["stages"] if s["stage"] == "Engaged")["count"]
            assert eng_after == eng_before + 1
            summ = client.get(f"/api/v1/analytics/products/summary?camera_id={CAM_E2E}").json()
            p = summ["products"][0]
            assert (p["reaches"], p["shoppers"], p["camera_shoppers"]) == (1, 1, 1)
    finally:
        live_engine.runtimes.pop(CAM_E2E, None)
        live_engine.set_zones(prev_zones)
        floor_projector.set_homography(CAM_E2E, None)
        shelf_interaction_service.delete_zone("rm_e2e_mid")
        pose_analytics.reset_camera(CAM_E2E)
        pose_analytics.flush_now()
