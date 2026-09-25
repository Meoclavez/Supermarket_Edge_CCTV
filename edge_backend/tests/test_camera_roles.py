"""Camera roles: presets, apply-defaults semantics, setup status from real state,
store coverage, role-aware footfall / theft behaviour, queue areas, migration m0012.

Everything runs against the throwaway test database (conftest.py) through the
real API; ids carry a ``cr_`` prefix and are cleaned up.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from app.config import settings
from app.database import async_session_factory
from app.main import app
from app.models.db_models import (
    Base,
    CameraModel,
    CustomerTrackModel,
    POSTransactionModel,
    QueueVisitModel,
    StoreZoneModel,
    TripwireEventModel,
    ZoneVisitModel,
)
from app.models.schemas import CameraFeatureConfig
from app.services import camera_roles as cr
from app.services import tripwire_engine as te
from app.services.ai_zone_service import ai_zone_service
from app.services.feature_manager import feature_manager
from app.services.shelf_interaction_service import ProductShelfZone, shelf_interaction_service
from app.services.theft_detection_service import detect_shelf_sweeping

CAMS = ("cr_entrance", "cr_exit", "cr_checkout", "cr_aisle", "cr_hv", "cr_stock", "cr_over", "cr_new")
CONTRACT_ROLES = {"entrance", "exit", "entrance_exit", "checkout", "aisle", "high_value", "stockroom", "overview"}


def _run(coro):
    return asyncio.run(coro)


def _cleanup_db():
    async def go():
        async with async_session_factory() as db:
            for model, col in ((TripwireEventModel, TripwireEventModel.camera_id),
                               (ZoneVisitModel, ZoneVisitModel.camera_id),
                               (CustomerTrackModel, CustomerTrackModel.camera_id),
                               (QueueVisitModel, QueueVisitModel.camera_id)):
                await db.execute(delete(model).where(col.like("cr_%")))
            await db.execute(delete(POSTransactionModel).where(POSTransactionModel.register_id.like("CR-%")))
            await db.execute(delete(StoreZoneModel).where(StoreZoneModel.id.like("cr_%")))
            await db.commit()
    _run(go())


def _cleanup_overlays():
    for kind, store in (("tripwires", ai_zone_service.tripwires), ("intrusion_zones", ai_zone_service.intrusion_zones),
                        ("exclusion_masks", ai_zone_service.exclusion_masks), ("queue_zones", ai_zone_service.queue_zones)):
        for zid, z in list(store.items()):
            if str(z.get("camera_id", "")).startswith("cr_"):
                store.pop(zid, None)
    ai_zone_service._save_persistent_zones()
    for z in shelf_interaction_service.get_zones():
        if z.camera_id.startswith("cr_"):
            shelf_interaction_service.delete_zone(z.id)


@pytest.fixture
def api():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    eng.dispose()
    client = TestClient(app)

    def wipe():
        for cid in CAMS:
            client.delete(f"/api/v1/cameras/{cid}")
            feature_manager.remove_camera(cid)
        _cleanup_overlays()
        _cleanup_db()
        cr.role_cache.set_override(None)
        cr.role_cache.invalidate()

    wipe()
    yield client
    wipe()


def _add(client, cid, name="Cam"):
    r = client.put(f"/api/v1/cameras/{cid}", json={"id": cid, "name": name, "location": "Floor"})
    assert r.status_code == 200, r.text


def _role(client, cid, role, apply_defaults=True):
    r = client.put(f"/api/v1/cameras/{cid}/role", json={"role": role, "apply_defaults": apply_defaults})
    assert r.status_code == 200, r.text
    return r.json()


def _items(client, cid):
    r = client.get(f"/api/v1/cameras/{cid}/setup")
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


# --------------------------------------------------------------------------- presets

def test_presets_are_complete_and_valid(api):
    assert set(cr.ROLE_PRESETS) == CONTRACT_ROLES
    for rid, p in cr.ROLE_PRESETS.items():
        assert p.id == rid
        assert p.label and p.description and p.mounting_tip and p.analytics
        assert set(p.features) == set(cr.FEATURE_KEYS) and all(isinstance(v, bool) for v in p.features.values())
        assert cr.SENSITIVITY_MIN <= p.theft_sensitivity <= cr.SENSITIVITY_MAX
        assert p.person_max_frame_fraction is None or 0.05 <= p.person_max_frame_fraction <= 1.0
        assert p.required_setup, rid
        assert set(p.required_setup) | set(p.optional_setup) <= set(cr.SETUP_ITEM_IDS)
        assert not set(p.required_setup) & set(p.optional_setup)
        # every default passes the real schema
        cfg = CameraFeatureConfig.model_validate(cr.default_features(rid))
        assert cfg.person_max_frame_fraction == p.person_max_frame_fraction
        # a camera with every analysis flag off is not tracked at all
        assert any(p.features.values())
    for item in cr.SETUP_ITEM_IDS:
        assert item in cr.ITEM_LABELS and item in cr.ITEM_ACTIONS
        assert cr.ITEM_ACTIONS[item]["type"] in ("studio", "calibrate", "config")
    assert cr.ROLE_PRESETS["stockroom"].counts_footfall is False
    assert cr.ROLE_PRESETS["stockroom"].features["people_counting"] is True   # restricted areas need tracks
    assert set(cr.PRIMARY_FOOTFALL_ROLES) == {"entrance", "exit", "entrance_exit"}
    assert cr.ROLE_PRESETS["high_value"].theft_sensitivity > 1.0

    listed = api.get("/api/v1/camera-roles").json()
    assert [r["id"] for r in listed] == list(cr.ROLE_PRESETS)
    assert listed[0]["required_setup"] == ["entrance_tripwire"]


# --------------------------------------------------------------------------- apply_defaults

def test_apply_defaults_semantics(api):
    _add(api, "cr_aisle")
    out = _role(api, "cr_aisle", "aisle")
    assert out["role"] == "aisle" and out["applied_defaults"]["person_max_frame_fraction"] == 0.6
    assert out["features"] == {"people_counting": True, "shelf_interaction": True, "theft_detection": True,
                               "person_max_frame_fraction": 0.6, "night_watch": None}
    assert api.get("/api/v1/cameras/cr_aisle").json()["role"] == "aisle"

    # The operator switches theft off afterwards...
    r = api.put("/api/v1/cameras/cr_aisle/features", json={"people_counting": True, "shelf_interaction": True,
                                                          "theft_detection": False})
    assert r.status_code == 200
    # ...a role change without apply_defaults keeps every toggle and the size gate.
    out = _role(api, "cr_aisle", "checkout", apply_defaults=False)
    assert out["applied_defaults"] == {}
    assert out["features"]["theft_detection"] is False and out["features"]["person_max_frame_fraction"] == 0.6
    assert feature_manager.is_enabled("cr_aisle", "theft_detection") is False
    # Applying defaults again restores the preset.
    out = _role(api, "cr_aisle", "stockroom", apply_defaults=True)
    assert out["features"] == {"people_counting": True, "shelf_interaction": False, "theft_detection": False,
                               "person_max_frame_fraction": None, "night_watch": None}
    assert feature_manager.is_enabled("cr_aisle", "shelf_interaction") is False
    # Clearing the role leaves the toggles alone.
    out = _role(api, "cr_aisle", None)
    assert out["role"] is None and out["features"]["shelf_interaction"] is False
    assert api.get("/api/v1/cameras/cr_aisle/setup").json()["message"]

    assert api.put("/api/v1/cameras/cr_aisle/role", json={"role": "bakery"}).status_code == 422
    assert api.put("/api/v1/cameras/cr_missing/role", json={"role": "aisle"}).status_code == 404


def test_create_camera_with_role_applies_defaults_under_explicit_values(api):
    body = {"id": "cr_new", "name": "Till 1", "rtsp_url": "rtsp://10.9.9.9/stream", "role": "checkout",
            "features": {"theft_detection": False}}
    r = api.post("/api/v1/cameras", json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["role"] == "checkout"
    assert got["features"]["theft_detection"] is False          # sent explicitly: kept
    assert got["features"]["shelf_interaction"] is True         # from the preset
    assert got["features"]["person_max_frame_fraction"] == 0.6  # from the preset
    assert api.post("/api/v1/cameras", json={**body, "id": "cr_bad", "role": "roof"}).status_code == 422
    r = api.post("/api/v1/cameras/test-connection", json={"url": "rtsp://10.9.9.9/x", "role": "roof"})
    assert r.status_code == 422 and "Unknown camera role" in r.text


# --------------------------------------------------------------------------- setup status

def test_setup_items_flip_as_real_config_is_added(api):
    _add(api, "cr_checkout", "Lane 3")
    _role(api, "cr_checkout", "checkout")
    items = _items(api, "cr_checkout")
    assert [i for i in items if items[i]["required"]] == ["checkout_zone"]
    assert not any(i["done"] for i in items.values())
    assert items["checkout_zone"]["action"] == {"type": "studio", "tool": "checkout", "kind": "checkout"}
    assert items["pos_register_link"]["action"] == {"type": "config", "field": "pos_register_id"}

    lane = {"camera_id": "cr_checkout", "name": "Lane 3", "kind": "checkout",
            "points": [{"x": 0.1, "y": 0.5}, {"x": 0.6, "y": 0.5}, {"x": 0.6, "y": 0.95}, {"x": 0.1, "y": 0.95}]}
    r = api.post("/api/zones/queue", json=lane)
    assert r.status_code == 200, r.text
    assert api.post("/api/zones/queue", json={**lane, "kind": "belt"}).status_code == 422
    assert api.post("/api/zones/queue", json={**lane, "points": lane["points"][:2]}).status_code == 422
    items = _items(api, "cr_checkout")
    assert items["checkout_zone"]["done"] and not items["queue_zone"]["done"]
    r = api.post("/api/zones/queue", json={**lane, "name": "Line 3", "kind": "queue"})
    assert _items(api, "cr_checkout")["queue_zone"]["done"]
    assert any(z["zone_type"] == "QUEUE_AREA" for z in api.get("/api/v1/cameras/cr_checkout/zones").json())

    # POS link: linked is not enough, rows with that register must have arrived.
    r = api.put("/api/v1/cameras/cr_checkout/pos-register", json={"register_id": "CR-LANE-3"})
    assert r.status_code == 200 and r.json()["pos_rows_seen"] == 0
    item = _items(api, "cr_checkout")["pos_register_link"]
    assert not item["done"] and "no POS rows" in item["hint"]
    r = api.post("/api/v1/analytics/pos/ingest", json={"transactions": [
        {"transaction_id": "cr_tx1", "register_id": "CR-LANE-3", "sku_id": "S1", "amount": 4.5}]})
    assert r.status_code == 200
    assert _items(api, "cr_checkout")["pos_register_link"]["done"]
    regs = {x["register_id"]: x for x in api.get("/api/v1/store/pos-registers").json()["registers"]}
    assert regs["CR-LANE-3"]["linked_camera_ids"] == ["cr_checkout"] and regs["CR-LANE-3"]["rows"] == 1

    # Calibration through the existing API.
    img = [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}]
    flr = [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 5}, {"x": 0, "y": 5}]
    r = api.post("/api/v1/layout/cameras/cr_checkout/calibrate", json={"image_points": img, "floor_points": flr})
    assert r.status_code == 200, r.text
    assert _items(api, "cr_checkout")["calibrate"]["done"]

    # Product areas (impulse racks).
    r = api.post("/api/v1/analytics/products/zones", json={
        "id": "cr_pz1", "camera_id": "cr_checkout", "name": "Razors", "sku_id": "RZ", "category": "HEALTH",
        "points": [{"x": 0.7, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.4}, {"x": 0.7, "y": 0.4}]})
    assert r.status_code == 200, r.text
    setup = api.get("/api/v1/cameras/cr_checkout/setup").json()
    assert all(i["done"] for i in setup["items"] if i["id"] != "privacy_mask")
    assert setup["complete"] and setup["required_done"] == setup["required_total"] == 1

    # A non-checkout camera cannot hold a register link, and changing the role drops it.
    assert _role(api, "cr_checkout", "aisle")["pos_register_id"] is None
    assert api.put("/api/v1/cameras/cr_checkout/pos-register",
                   json={"register_id": "CR-LANE-3"}).status_code == 409


def test_setup_tripwire_restricted_and_mask_items(api):
    _add(api, "cr_entrance")
    _role(api, "cr_entrance", "entrance")
    assert not _items(api, "cr_entrance")["entrance_tripwire"]["done"]
    line = {"name": "Front door", "camera_id": "cr_entrance", "x1": 0.1, "y1": 0.6, "x2": 0.9, "y2": 0.6,
            "counts_footfall": False}
    tw = api.post("/api/zones/tripwire", json=line).json()["tripwire"]
    item = _items(api, "cr_entrance")["entrance_tripwire"]
    assert not item["done"] and "not set to count footfall" in item["hint"]
    api.patch(f"/api/zones/tripwire/{tw['id']}", json={"counts_footfall": True})
    assert _items(api, "cr_entrance")["entrance_tripwire"]["done"]

    _add(api, "cr_stock")
    _role(api, "cr_stock", "stockroom")
    area = api.post("/api/zones/intrusion", json={
        "name": "Stockroom", "camera_id": "cr_stock",
        "points": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}]}).json()["intrusion_zone"]
    item = _items(api, "cr_stock")["restricted_area"]
    assert not item["done"] and "no schedule" in item["hint"]
    r = api.patch(f"/api/zones/intrusion/{area['id']}", json={
        "schedule": [{"days": ["mon", "tue"], "start": "22:00", "end": "06:00"}]})
    assert r.status_code == 200, r.text
    assert _items(api, "cr_stock")["restricted_area"]["done"]
    assert not _items(api, "cr_stock")["privacy_mask"]["done"]
    api.post("/api/zones/exclusion", json={"camera_id": "cr_stock", "name": "Safe keypad",
                                           "points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.1}, {"x": 0.2, "y": 0.2}]})
    assert _items(api, "cr_stock")["privacy_mask"]["done"]


# --------------------------------------------------------------------------- store setup

def test_store_setup_coverage_messages(api):
    def analytics():
        data = api.get("/api/v1/store/setup").json()
        return data, {a["id"]: a for a in data["analytics"]}

    _add(api, "cr_aisle")
    _role(api, "cr_aisle", "aisle")
    data, a = analytics()
    assert a["footfall"]["status"] == "limited" and "Entrance camera" in a["footfall"]["message"]
    assert a["queue_times"]["status"] == "blocked"
    assert a["queue_times"]["message"] == "Queue times: needs a Checkout camera with a checkout or queue area."
    assert a["exit_without_checkout"]["status"] == "blocked"
    assert "cross-camera" in a["exit_without_checkout"]["message"]
    assert a["sweethearting"]["status"] == "unsupported"
    assert a["occupancy"]["status"] == "blocked"
    assert "entrance" in data["roles_missing"] and "checkout" in data["roles_missing"]
    mine = {c["camera_id"]: c for c in data["cameras"]}["cr_aisle"]
    assert mine["missing"] == ["Product shelf areas"] and mine["required_done"] == 0

    _add(api, "cr_entrance")
    _role(api, "cr_entrance", "entrance_exit")
    api.post("/api/zones/tripwire", json={"name": "Door", "camera_id": "cr_entrance",
                                          "x1": 0.1, "y1": 0.6, "x2": 0.9, "y2": 0.6})
    _add(api, "cr_checkout")
    _role(api, "cr_checkout", "checkout")
    api.post("/api/zones/queue", json={"camera_id": "cr_checkout", "kind": "queue", "name": "Line",
                                       "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]})
    data, a = analytics()
    assert a["footfall"]["status"] == "available"
    assert a["occupancy"]["status"] == "available"
    assert a["queue_times"]["status"] == "available"
    assert a["lane_pos"]["status"] == "blocked"
    mine = {c["camera_id"]: c for c in data["cameras"]}
    assert mine["cr_entrance"]["complete"] and not mine["cr_checkout"]["complete"]
    # Score counts only these three role cameras here (other tests clean up theirs).
    ours = [c for c in data["cameras"] if c["camera_id"].startswith("cr_")]
    assert sum(c["required_done"] for c in ours) == 1 and sum(c["required_total"] for c in ours) == 3
    assert data["score"]["total"] >= 3


# --------------------------------------------------------------------------- role-aware footfall

def test_footfall_prefers_door_lines_and_excludes_stockroom(api):
    from app.services.retail_metrics_service import retail_metrics_service

    for cid in ("cr_entrance", "cr_aisle", "cr_stock"):
        _add(api, cid)
    day = datetime(2033, 3, 4, 10, 0)
    te.tripwire_engine.drain_events()

    def ev(cam, n, direction="in"):
        return [{"tripwire_id": f"tw_{cam}", "tripwire_name": cam, "camera_id": cam, "track_id": f"{cam}{i}",
                 "direction": direction, "ts": day + timedelta(minutes=i), "counts_footfall": True}
                for i in range(n)]
    te.tripwire_engine._pending_events.extend(ev("cr_entrance", 3) + ev("cr_entrance", 1, "out")
                                              + ev("cr_aisle", 5) + ev("cr_stock", 4))
    assert _run(te.flush_tripwire_events(async_session_factory)) == 13

    async def figures():
        async with async_session_factory() as db:
            return (await te.tripwire_entries(db, day, day + timedelta(hours=2)),
                    await te.tripwire_footfall(db, day, day + timedelta(hours=2), bucket="none"))

    # No roles: every counting line, exactly as before roles existed.
    n, ff = _run(figures())
    assert n == 12 and ff["totals"]["in"] == 12
    # Roles: the entrance line wins, the stockroom never counts.
    _role(api, "cr_entrance", "entrance")
    _role(api, "cr_aisle", "aisle")
    _role(api, "cr_stock", "stockroom")
    n, ff = _run(figures())
    assert n == 3
    assert ff["totals"] == {"in": 3, "out": 1, "net": 2} and ff["counted_tripwires"] == ["tw_cr_entrance"]
    lines = {r["tripwire_id"]: r for r in ff["tripwires"]}
    assert lines["tw_cr_stock"]["counts_footfall"] is False and lines["tw_cr_entrance"]["camera_role"] == "entrance"
    # Without a door role the aisle line still counts, the stockroom still does not.
    _role(api, "cr_entrance", None)
    assert _run(figures())[0] == 8

    # Zone-visit and track fallbacks exclude the stockroom camera too.
    later = datetime(2033, 3, 5, 10, 0)

    async def add_obs():
        async with async_session_factory() as db:
            for i, cam in enumerate(("cr_aisle", "cr_stock", "cr_stock")):
                db.add(ZoneVisitModel(id=f"cr_zv{i}", zone_id="z", track_id=f"t{i}", camera_id=cam,
                                      entered_at=later, exited_at=later + timedelta(seconds=30), dwell_seconds=30))
                db.add(CustomerTrackModel(id=f"cr_ct{i}", track_id=f"u{i}", camera_id=cam, start_time=later,
                                          end_time=later + timedelta(seconds=30), hits=20, trajectory_points=[]))
            await db.commit()
    _run(add_obs())

    async def ff_window(start):
        async with async_session_factory() as db:
            return (await retail_metrics_service.footfall(db, start, start + timedelta(hours=1)),
                    await retail_metrics_service.footfall_source(db, start, start + timedelta(hours=1)))
    assert _run(ff_window(later)) == (1, "zone_visits")

    async def drop_visits():
        async with async_session_factory() as db:
            await db.execute(delete(ZoneVisitModel).where(ZoneVisitModel.id.like("cr_zv%")))
            await db.commit()
    _run(drop_visits())
    assert _run(ff_window(later)) == (1, "tracks")
    _role(api, "cr_stock", None)      # no role: back to counting everyone
    assert _run(ff_window(later)) == (3, "tracks")


# --------------------------------------------------------------------------- theft sensitivity

def test_theft_sensitivity_scaling_and_role_gates(api):
    base = cr.scaled_theft_thresholds(1.0)
    assert base["sweep_min_reaches"] == settings.THEFT_SWEEP_MIN_REACHES
    assert base["min_confidence"] == pytest.approx(settings.THEFT_MIN_CONFIDENCE)
    hv = cr.scaled_theft_thresholds(cr.theft_sensitivity("high_value"))
    assert hv["min_confidence"] < base["min_confidence"]
    assert hv["loiter_min_dwell_sec"] < base["loiter_min_dwell_sec"]
    assert 2 <= hv["sweep_min_reaches"] < base["sweep_min_reaches"]
    assert cr.theft_sensitivity(None) == 1.0 and cr.theft_sensitivity("aisle") == 1.0
    assert cr.scaled_theft_thresholds(99)["sensitivity"] == cr.SENSITIVITY_MAX

    # The same three reaches are sweeping on a high-value camera, not on an aisle.
    reaches = [{"timestamp": 100.0 + i, "zone_id": "z", "vis": 0.9} for i in range(hv["sweep_min_reaches"])]
    assert not detect_shelf_sweeping(reaches, window_sec=settings.THEFT_SWEEP_WINDOW_SEC,
                                      min_reaches=base["sweep_min_reaches"])["detected"]
    assert detect_shelf_sweeping(reaches, window_sec=settings.THEFT_SWEEP_WINDOW_SEC,
                                 min_reaches=hv["sweep_min_reaches"])["detected"]

    # pose_analytics picks the role up per camera.
    from app.services.pose_analytics import CameraState, PoseAnalytics

    pa = PoseAnalytics()
    shelf_interaction_service.save_zone(ProductShelfZone(
        id="cr_pz_hv", camera_id="cr_hv", name="Bread", sku_id="B", category="BAKERY",
        points=[{"x": 0.1, "y": 0.1}, {"x": 0.3, "y": 0.1}, {"x": 0.3, "y": 0.3}]))
    cr.role_cache.set_override({"cr_hv": "high_value", "cr_aisle": "aisle", "cr_checkout": "checkout"})
    cam = CameraState(camera_id="cr_hv")
    pa._refresh_role(cam)
    pa._refresh_product_zones(cam)
    assert cam.thresholds["sweep_min_reaches"] == hv["sweep_min_reaches"]
    assert [z.high_value for z in cam.zones] == [True]      # bakery is high value on a high-value camera
    assert pa._severity(cam, 0.35) == "HIGH"                 # alerts are raised HIGH
    assert cam.exit_rule_on is False                         # product-area camera, checkouts elsewhere

    cr.role_cache.set_override({"cr_hv": "aisle"})
    pa._refresh_role(cam)
    pa._refresh_product_zones(cam)
    assert [z.high_value for z in cam.zones] == [False] and pa._severity(cam, 0.35) == "LOW"
    assert cam.exit_rule_on is True                          # no checkout camera: no information
    assert cam.thresholds["sweep_min_reaches"] == base["sweep_min_reaches"]

    cr.role_cache.set_override({})
    pa._refresh_role(cam)
    assert cam.role is None and cam.exit_rule_on is True     # no role: unchanged behaviour

    stores = ["checkout", "aisle"]
    assert cr.exit_rule_allowed(None, stores) and cr.exit_rule_allowed("exit", stores)
    assert cr.exit_rule_allowed("checkout", stores) and not cr.exit_rule_allowed("stockroom", [])
    assert not cr.exit_rule_allowed("aisle", stores) and cr.exit_rule_allowed("aisle", ["aisle"])


# --------------------------------------------------------------------------- queue areas

def test_queue_area_visits_feed_lane_metrics_with_pos(api):
    from app.services.retail_metrics_service import retail_metrics_service
    from app.services.store_layout_service import store_layout_service

    _add(api, "cr_checkout")
    _role(api, "cr_checkout", "checkout")
    api.put("/api/v1/cameras/cr_checkout/pos-register", json={"register_id": "CR-L1"})
    qa = api.post("/api/zones/queue", json={"camera_id": "cr_checkout", "name": "Lane 1", "kind": "checkout",
                                            "points": [{"x": 0.0, "y": 0.5}, {"x": 0.5, "y": 0.5},
                                                       {"x": 0.5, "y": 1.0}, {"x": 0.0, "y": 1.0}]}).json()["queue_area"]
    te.tripwire_engine.drain_queue_visits()
    te.tripwire_engine.reset_camera("cr_checkout")
    inside = te.TrackView("t1", (100, 300, 200, 460))      # foot (150, 460) of 640x480 -> inside
    outside = te.TrackView("t1", (500, 100, 600, 200))     # foot (550, 200) -> outside
    passer = te.TrackView("t2", (100, 300, 200, 460))
    t0 = 1_900_000_000.0
    for i in range(11):                                     # t1 stands 10 s; t2 walks through in 1 s
        tracks = [inside] + ([passer] if i < 2 else [])
        te.tripwire_engine.evaluate("cr_checkout", tracks, 640, 480, t0 + i)
    assert te.tripwire_engine.queue_occupancy(now=t0 + 10)[qa["id"]] == 1
    # Counting off: no queue state at all.
    assert te.tripwire_engine.evaluate("cr_other", [inside], 640, 480, t0, counting=False)["crossings"] == []
    for i in range(11, 15):
        te.tripwire_engine.evaluate("cr_checkout", [outside], 640, 480, t0 + i)
    assert te.tripwire_engine.queue_occupancy(now=t0 + 14)[qa["id"]] == 0
    pending = list(te.tripwire_engine._pending_queue_visits)
    assert len(pending) == 1                                # the 1 s pass-through is not a visit
    assert pending[0]["dwell_seconds"] == pytest.approx(10.0) and pending[0]["area_kind"] == "checkout"
    assert _run(te.flush_tripwire_events(async_session_factory)) == 1

    day = pending[0]["entered_at"]

    async def add_pos_and_read():
        async with async_session_factory() as db:
            db.add(POSTransactionModel(id="cr_pos1", transaction_id="cr_T1", register_id="CR-L1", sku_id="A",
                                       quantity=1, amount=12.5, timestamp=day))
            await db.commit()
            layout = await store_layout_service.get_active_layout(db)
            return await retail_metrics_service.checkout_queues(db, layout.id, day - timedelta(hours=1),
                                                                 day + timedelta(hours=1))
    lanes = {l["zone_id"]: l for l in _run(add_pos_and_read())}
    lane = lanes[qa["id"]]
    assert lane["source"] == "camera_area" and lane["kind"] == "checkout" and lane["camera_id"] == "cr_checkout"
    assert lane["served_today"] == 1 and lane["avg_wait_seconds"] == pytest.approx(10.0)
    assert lane["register_ids"] == ["CR-L1"] and lane["pos_transactions"] == 1 and lane["pos_revenue"] == 12.5
    assert lane["conversion_pct"] == 100.0
    assert lane["status"] in ("IDLE", "NOT RUNNING")        # nobody in the lane now


# --------------------------------------------------------------------------- migration

def test_migration_m0012_on_legacy_fixture(tmp_path):
    from app.migrations import head_version, run_migrations, status
    from tests.test_migrations import _make_legacy

    db = tmp_path / "legacy.db"
    _make_legacy(db)
    report = run_migrations(db, backups_dir=tmp_path / "backups")
    assert report["from_version"] == 0 and report["to_version"] == head_version()
    c = sqlite3.connect(str(db))
    cols = {r[1]: r for r in c.execute("PRAGMA table_info(cameras)")}
    assert "role" in cols and "pos_register_id" in cols
    assert c.execute("SELECT name, role, pos_register_id FROM cameras WHERE id='cam1'").fetchone() == \
        ("Aisle 3", None, None)
    qcols = [r[1] for r in c.execute("PRAGMA table_info(queue_visits)")]
    assert qcols[:3] == ["id", "area_id", "area_kind"] and "dwell_seconds" in qcols
    assert "ix_queue_visits_area_entered" in {r[1] for r in c.execute("PRAGMA index_list(queue_visits)")}
    versions = [r[0] for r in c.execute("SELECT version FROM schema_migrations")]
    assert 12 in versions
    c.close()
    st = status(db)
    assert st["pending"] == [] and st["drift"]["missing"] == [] and st["drift"]["type_mismatches"] == []
    # Running again changes nothing.
    assert run_migrations(db, backups_dir=tmp_path / "b2")["applied"] == []
