"""Product shelf zones: CRUD, shelf level / value tier attribution, DB-backed stats."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models.db_models import Base, ShelfInteractionModel
from app.services.shelf_interaction_service import (
    PointCoord,
    ProductShelfZone,
    ShelfInteractionService,
    StudyMetricsConfig,
    derive_attributes,
    shelf_interaction_service,
)
from app.services.timeutil import utcnow


def _zone(zid, cam="cam_si", y0=0.1, y1=0.3, x0=0.1, x1=0.4, **kw):
    kw.setdefault("sku_id", f"SKU-{zid}")
    kw.setdefault("category", "Snacks")
    return ProductShelfZone(
        id=zid, camera_id=cam, name=f"Product {zid}",
        points=[PointCoord(x=x0, y=y0), PointCoord(x=x1, y=y0), PointCoord(x=x1, y=y1), PointCoord(x=x0, y=y1)],
        **kw,
    )


def test_product_shelf_zone_crud_keeps_new_fields():
    z = _zone("test_shelf_zone_99", price=2.5, facing_count=5, shelf_level="top", value_tier="premium",
              study_metrics=StudyMetricsConfig(track_hand_reach=True))
    saved = shelf_interaction_service.save_zone(z)
    try:
        fetched = shelf_interaction_service.get_zone("test_shelf_zone_99")
        assert fetched.shelf_level == "TOP" and fetched.value_tier == "PREMIUM"   # normalised
        d = shelf_interaction_service.zone_dict(saved)
        assert d["effective_shelf_level"] == "TOP" and d["shelf_level_source"] == "operator"
        assert d["effective_value_tier"] == "PREMIUM" and d["value_tier_source"] == "operator"
        # Re-saving (Studio edit) keeps the creation time and bumps the version.
        v = shelf_interaction_service.version
        again = shelf_interaction_service.save_zone(z.model_copy(update={"shelf_level": None, "created_at": "x"}))
        assert again.created_at == saved.created_at and shelf_interaction_service.version > v
        assert shelf_interaction_service.get_zone("test_shelf_zone_99").shelf_level is None
    finally:
        assert shelf_interaction_service.delete_zone("test_shelf_zone_99") is True
    assert shelf_interaction_service.get_zone("test_shelf_zone_99") is None


def test_auto_level_and_invalid_level():
    assert _zone("a", shelf_level="auto").shelf_level is None
    assert _zone("a", shelf_level="").shelf_level is None
    with pytest.raises(Exception):
        _zone("a", shelf_level="SKY")


def test_shelf_level_derivation():
    zones = [
        _zone("top", y0=0.05, y1=0.25),
        _zone("mid", y0=0.30, y1=0.55),
        _zone("bot", y0=0.60, y1=0.90),
        _zone("lone", x0=0.7, x1=0.9, y0=0.3, y1=0.5),                      # nothing above/below it
        _zone("legacy", cam="cam_other", shelf_tier="EYE_LEVEL"),            # lone, old tier set
        _zone("endcap", cam="cam_other2", shelf_tier="ENDCAP"),
        _zone("op", x0=0.12, x1=0.38, y0=0.30, y1=0.55, cam="cam_si", shelf_level="BOTTOM"),
    ]
    a = derive_attributes(zones)
    assert (a["top"]["shelf_level"], a["top"]["shelf_level_source"]) == ("TOP", "derived")
    assert (a["mid"]["shelf_level"], a["mid"]["shelf_level_source"]) == ("MIDDLE", "derived")
    assert (a["bot"]["shelf_level"], a["bot"]["shelf_level_source"]) == ("BOTTOM", "derived")
    assert a["lone"]["shelf_level"] is None and a["lone"]["shelf_level_source"] is None   # unknown, not guessed
    assert (a["legacy"]["shelf_level"], a["legacy"]["shelf_level_source"]) == ("MIDDLE", "legacy_tier")
    assert a["endcap"]["shelf_level"] is None
    assert (a["op"]["shelf_level"], a["op"]["shelf_level_source"]) == ("BOTTOM", "operator")


def test_value_tier_derivation(monkeypatch):
    monkeypatch.setattr(settings, "THEFT_HIGH_VALUE_CATEGORIES", "SPIRITS")
    monkeypatch.setattr(settings, "THEFT_HIGH_VALUE_MIN_PRICE", 0.0)
    few = derive_attributes([_zone("p1", price=2.0), _zone("p2", price=9.0)])
    assert few["p1"]["value_tier"] is None            # < 3 priced zones: no tertiles
    zones = [_zone("c", price=1.0), _zone("m", price=5.0), _zone("e", price=20.0),
             _zone("s", price=0.0, category="Spirits"), _zone("u", price=0.0),
             _zone("o", price=1.0, value_tier="STANDARD")]
    a = derive_attributes(zones)
    assert a["c"]["value_tier"] == "LOW" and a["e"]["value_tier"] == "PREMIUM"
    assert a["s"]["value_tier"] == "PREMIUM" and a["s"]["high_value"] is True
    assert a["u"]["value_tier"] is None
    assert (a["o"]["value_tier"], a["o"]["value_tier_source"]) == ("STANDARD", "operator")


def test_manual_pose_endpoint_picks_one_zone_and_records_nothing():
    a = _zone("man_a", cam="cam_man", x0=0.3, x1=0.6, y0=0.3, y1=0.7)
    b = _zone("man_b", cam="cam_man", x0=0.55, x1=0.9, y0=0.3, y1=0.7)     # overlaps a at x 0.55..0.6
    shelf_interaction_service.save_zone(a)
    shelf_interaction_service.save_zone(b)
    try:
        out = [(0.1, 0.1, 0.9)] * 17
        inside = list(out)
        inside[10] = (0.58, 0.5, 0.95)          # in both; deeper in man_b (0.03 vs 0.02)
        ev = shelf_interaction_service.process_person_pose("cam_man", 7, out, (0, 0, 1, 1), now_ts=100.0)
        assert ev == []
        ev = shelf_interaction_service.process_person_pose("cam_man", 7, inside, (0, 0, 1, 1), now_ts=100.5)
        assert [(e.action_type, e.zone_id) for e in ev] == [("REACH_IN", "man_b")]
        ev = shelf_interaction_service.process_person_pose("cam_man", 7, inside, (0, 0, 1, 1), now_ts=102.0)
        assert [e.action_type for e in ev] == ["INSPECT_DWELL"]
        ev = shelf_interaction_service.process_person_pose("cam_man", 7, out, (0, 0, 1, 1), now_ts=103.5)
        assert [(e.action_type, e.dwell_duration_sec) for e in ev] == [("REACH_END", 3.0)]
    finally:
        shelf_interaction_service.delete_zone("man_a")
        shelf_interaction_service.delete_zone("man_b")


@pytest.fixture
def db_rows():
    eng = create_engine(f"sqlite:///{Path(settings.DATABASE_PATH).resolve()}")
    Base.metadata.create_all(eng)
    ids = []

    def add(zone_id, track, hand="right", cam="cam_si_db", dur=1.0, **kw):
        rid = f"si_test_{len(ids)}_{zone_id}"
        now = utcnow() - timedelta(seconds=30)
        with Session(eng) as s:
            s.add(ShelfInteractionModel(id=rid, camera_id=cam, shelf_zone_id=zone_id, timestamp=now,
                                        person_track_id=track, action_type="REACH", duration_sec=dur,
                                        hand=hand, started_at=now, ended_at=now, confidence=0.9, **kw))
            s.commit()
        ids.append(rid)

    yield add
    with Session(eng) as s:
        for rid in ids:
            r = s.get(ShelfInteractionModel, rid)
            if r is not None:
                s.delete(r)
        s.commit()
    eng.dispose()


def test_stats_come_from_the_database_and_survive_a_restart(db_rows):
    z = _zone("si_db_zone", cam="cam_si_db", shelf_level="MIDDLE")
    shelf_interaction_service.save_zone(z)
    try:
        db_rows("si_db_zone", "t1", dur=1.0)
        db_rows("si_db_zone", "t1", hand="left", dur=3.0)
        db_rows("si_db_zone", "t2", dur=2.0)
        # A reach recorded for a zone that was later deleted keeps its stored attribution.
        db_rows("si_gone", "t3", zone_name="Old shelf", sku_id="SKU-OLD", shelf_level="TOP")

        # A fresh service instance (what a restart gives you) has no counters,
        # yet the numbers are the same: they are read from shelf_interactions.
        fresh = ShelfInteractionService(config_path=shelf_interaction_service.config_path)
        assert fresh.get_zone("si_db_zone") is not None
        with TestClient(app) as client:
            st = client.get("/api/v1/analytics/products/si_db_zone/stats").json()
            assert (st["reaches"], st["touches"], st["shoppers"]) == (3, 3, 2)
            assert (st["left_hand"], st["right_hand"]) == (1, 2)
            assert st["avg_duration_sec"] == 2.0 and st["total_duration_sec"] == 6.0
            assert st["shelf_level"] == "MIDDLE"
            assert st["picks"] is None and st["put_backs"] is None and st["impressions"] is None
            assert client.get("/api/v1/analytics/products/nope/stats").status_code == 404

            summ = client.get("/api/v1/analytics/products/summary?camera_id=cam_si_db").json()
            per = {p["zone_id"]: p for p in summ["products"]}
            assert per["si_gone"]["configured"] is False and per["si_gone"]["sku_id"] == "SKU-OLD"
            assert per["si_gone"]["shelf_level"] == "TOP" and per["si_gone"]["name"] == "Old shelf"
            assert summ["totals"]["reaches"] == 4 and summ["totals"]["shoppers_reaching"] == 3
            assert summ["window"]["start"].endswith("Z")
            assert client.get("/api/v1/analytics/products/summary?date=2020-13-01").status_code == 422
            # A day with nothing recorded says so.
            empty = client.get("/api/v1/analytics/products/summary?camera_id=cam_si_db&date=2020-01-01").json()
            assert empty["totals"]["reaches"] == 0 and empty["observed"] is False and empty["message"]
    finally:
        shelf_interaction_service.delete_zone("si_db_zone")
