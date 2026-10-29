"""Queue length in floor metres (services/queue_length.py) and where it is reported.

* measured only from calibrated floor positions inside the CHECKOUT zone, along
  the zone's long side; never from a count alone;
* null with a plain reason when no calibrated camera sees (enough of) the zone;
* 0 m for an empty zone in view; spacing only from 2 people up;
* zone metrics, checkout lanes and /analytics/space carry it; sales per m2 per
  zone is explicitly not given (the tills send SKU only).
"""
from __future__ import annotations

import math

import pytest

from app.services import queue_length as ql
from app.services.retail_metrics_service import ZoneMetrics

LANE = [(30, 20), (36, 20), (36, 22), (30, 22)]          # 6 m x 2 m, long side along x


def test_long_axis_of_a_rectangle_and_a_rotated_one():
    ax = ql.long_axis(LANE)
    assert ax["length_m"] == pytest.approx(6.0) and ax["width_m"] == pytest.approx(2.0)
    ux, uy = ax["direction"]
    assert abs(ux) == pytest.approx(1.0) and uy == pytest.approx(0.0, abs=1e-9)
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    rot = [(x * c - y * s, x * s + y * c) for x, y in [(0, 0), (8, 0), (8, 1.5), (0, 1.5)]]
    ax = ql.long_axis(rot)
    assert ax["length_m"] == pytest.approx(8.0, abs=1e-6) and ax["width_m"] == pytest.approx(1.5, abs=1e-6)
    assert abs(ax["direction"][0] * c + ax["direction"][1] * s) == pytest.approx(1.0, abs=1e-6)
    assert ql.long_axis([(0, 0), (1, 1)]) is None


def test_spread_along_the_long_side_and_spacing():
    people = [{"x_m": 31.0, "y_m": 21.0}, {"x_m": 32.2, "y_m": 20.5}, {"x_m": 33.4, "y_m": 21.6},
              {"x_m": 10.0, "y_m": 10.0}]                                   # last one is outside the lane
    r = ql.measure(people, LANE, covered_fraction=0.9)
    assert r["people"] == 3 and r["length_m"] == pytest.approx(2.4) and r["spacing_m"] == pytest.approx(1.2)
    assert r["length_estimate_m"] == pytest.approx(3.6) and r["reason"] is None and r["zone_length_m"] == 6.0


def test_empty_and_single_person():
    r = ql.measure([], LANE, 1.0)
    assert r["length_m"] == 0.0 and r["people"] == 0 and r["spacing_m"] is None
    r = ql.measure([(33, 21)], LANE, 1.0)
    assert r["length_m"] == 0.0 and r["people"] == 1 and r["spacing_m"] is None and r["length_estimate_m"] is None


@pytest.mark.parametrize("covered", [None, 0.0])
def test_no_calibrated_camera_means_no_number(covered):
    r = ql.measure([(31, 21), (34, 21)], LANE, covered)
    assert r["length_m"] is None and r["people"] is None and r["spacing_m"] is None
    assert "No calibrated camera sees this queue area" in r["reason"]


def test_too_little_in_view_means_no_number():
    r = ql.measure([(31, 21), (34, 21)], LANE, 0.3)
    assert r["length_m"] is None and "only 30%" in r["reason"]


def test_zone_metrics_dict_carries_the_queue():
    m = ZoneMetrics(zone_id="z", name="Checkout 1", category="CHECKOUT", queue=ql.measure([(31, 21), (33, 21)], LANE, 1.0))
    d = m.to_dict()
    assert d["queue_length_m"] == 2.0 and d["queue"]["people"] == 2
    assert ZoneMetrics(zone_id="a", name="Aisle", category="AISLE").to_dict()["queue_length_m"] is None


def test_queue_lengths_use_only_calibrated_cameras(monkeypatch):
    from app.services import restricted_areas
    from app.services.live_analytics_engine import live_engine
    from app.services.retail_metrics_service import retail_metrics_service
    from app.services.tracking_service import floor_projector

    class Z:  # a store zone row
        id, category, polygon = "zq", "CHECKOUT", [{"x": x, "y": y} for x, y in LANE]

    monkeypatch.setattr(live_engine, "live_snapshot", lambda: {"persons": [{"x_m": 31, "y_m": 21}, {"x_m": 35, "y_m": 21}]})
    monkeypatch.setattr(live_engine, "runtimes", {"cal": object(), "uncal": object()})
    monkeypatch.setattr(floor_projector, "has_homography", lambda cid: cid == "cal")
    seen = []
    monkeypatch.setattr(restricted_areas, "floor_coverage", lambda cid, poly, size: (seen.append(cid) or 0.8, 0.0, 10))
    out = retail_metrics_service.queue_lengths([Z])
    assert seen == ["cal"] and out["zq"]["length_m"] == 4.0 and out["zq"]["covered_fraction"] == 0.8
    monkeypatch.setattr(floor_projector, "has_homography", lambda cid: False)
    out = retail_metrics_service.queue_lengths([Z])
    assert out["zq"]["length_m"] is None and out["zq"]["reason"]
