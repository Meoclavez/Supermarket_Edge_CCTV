"""Recorder channel names, adopt with names/purpose, and honest per-area lost sales.

* Channel names are read read-only from the Dahua recorder
  (``configManager.cgi?action=getConfig&name=ChannelTitle``) over HTTP Digest;
  a rejected sign-in stops at the first 401 (no retry, no lockout).
* Adopted channels take the operator's name and optional purpose; nothing is
  assigned automatically.
* A probed channel's frame rate is what the stream reported, never a default.
* "Lost sales" per area is never invented: null with the reason.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.dahua_probe_service import (
    ChannelProbeResult,
    DahuaProbeService,
    _measured_fps,
    parse_channel_titles,
)
from tests.test_recorder_substreams import HOST, PASSWORD, USER, FakeDahua

TITLES = (
    "table.ChannelTitle[0].Name=Front door\r\n"
    "table.ChannelTitle[1].Name=\r\n"
    "table.ChannelTitle[2].Name=Liquor aisle\r\n"
)


class TitledDahua(FakeDahua):
    def handle(self, path):
        q = dict(parse_qsl(urlsplit(path).query))
        if q.get("action") == "getConfig" and q.get("name") == "ChannelTitle":
            return 200, TITLES
        return super().handle(path)


@pytest.fixture
def titled_nvr():
    made = []

    def _make(**kw):
        f = TitledDahua({1: {"w": 704, "h": 576}}, **kw)
        made.append(f)
        return f

    yield _make
    for f in made:
        f.stop()


# ------------------------------------------------------------ channel names

def test_parse_channel_titles_skips_empty_and_junk():
    kv = {
        "table.ChannelTitle[0].Name": "Front door",
        "table.ChannelTitle[1].Name": "  ",
        "ChannelTitle[3].Name": "Back\x07 dock",
        "table.ChannelTitle[2].Other": "x",
        "garbage": "1",
    }
    assert parse_channel_titles(kv) == {1: "Front door", 4: "Back dock"}
    assert parse_channel_titles({"table.ChannelTitle[0].Name": "x" * 300})[1] == "x" * 120


@pytest.mark.parametrize("close_after_challenge", [False, True])
def test_titles_read_over_digest(titled_nvr, close_after_challenge):
    fake = titled_nvr(close_after_challenge=close_after_challenge, nonce_per_connection=not close_after_challenge)
    titles, err = DahuaProbeService().read_channel_titles(HOST, USER, PASSWORD, http_port=fake.port)
    assert err is None
    assert titles == {1: "Front door", 3: "Liquor aisle"}
    assert fake.auth_failures == 0
    assert fake.requests == ["/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"]


def test_titles_wrong_password_stops_at_first_401(titled_nvr):
    fake = titled_nvr(close_after_challenge=True, nonce_per_connection=False)
    titles, err = DahuaProbeService().read_channel_titles(HOST, USER, "wrong", http_port=fake.port)
    assert titles == {}
    assert "401" in err and "not retried" in err and "wrong" not in err
    assert fake.auth_failures == 1          # exactly one failed login, no retry
    assert fake.requests == []


def test_titles_not_attempted_without_password():
    titles, err = DahuaProbeService().read_channel_titles(HOST, USER, "", http_port=1)
    assert titles == {} and "no recorder sign-in" in err


def _result(ch, active=True, fps=None, resolution=None):
    url = f"rtsp://{HOST}:554/cam/realmonitor?channel={ch}&subtype=1"
    return ChannelProbeResult(channel=ch, active=active, status="ACTIVE" if active else "NO_SIGNAL",
                              sub_url=url, main_url=url, preferred_url=url, preferred_subtype=1,
                              resolution=resolution, fps=fps)


def test_probe_attaches_recorder_titles_and_reports_title_errors():
    svc = DahuaProbeService()
    with patch.object(svc, "check_tcp", return_value=True), \
         patch.object(svc, "probe_rtsp_auth", return_value=(True, None)), \
         patch.object(svc, "_test_channel_worker", side_effect=lambda h, p, ch, u, pw: _result(ch)), \
         patch.object(svc, "read_channel_titles", return_value=({1: "Front door"}, None)) as rt:
        res = asyncio.run(svc.probe_nvr(HOST, username=USER, password=PASSWORD, max_channels=2, http_port=8080))
    rt.assert_called_once_with(HOST, USER, PASSWORD, 8080)
    d = res.to_dict()
    assert d["titles_read"] is True and d["titles_error"] is None
    assert [c["title"] for c in d["channels"]] == ["Front door", None]

    with patch.object(svc, "check_tcp", return_value=True), \
         patch.object(svc, "probe_rtsp_auth", return_value=(True, None)), \
         patch.object(svc, "_test_channel_worker", side_effect=lambda h, p, ch, u, pw: _result(ch)), \
         patch.object(svc, "read_channel_titles", return_value=({}, "refused (HTTP 401); not retried")):
        d = asyncio.run(svc.probe_nvr(HOST, username=USER, password=PASSWORD, max_channels=1)).to_dict()
    assert d["titles_read"] is False and "not retried" in d["titles_error"]
    assert d["authenticated"] is True and d["channels"][0]["title"] is None


def test_unreported_frame_rate_is_none_not_a_default():
    assert _measured_fps(0) is None
    assert _measured_fps(None) is None
    assert _measured_fps(float("nan")) is None
    assert _measured_fps(1000) is None
    assert _measured_fps(12.49) == 12.5


# ------------------------------------------------------------------ adopt

def _host():
    return f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250 + 1}"


@pytest.fixture
def client():
    # The context manager runs the app's startup (database schema).
    with patch("app.routes.dahua.pipeline_supervisor.reconcile_cameras", new=AsyncMock()), TestClient(app) as c:
        yield c


def test_adopt_uses_operator_names_and_optional_purpose(client):
    host = _host()
    res = client.post("/api/v1/dahua/adopt", json={
        "host": host, "port": 554, "username": "admin", "password": "fake-nvr-pw",
        "channels": [
            {"channel": 1, "name": "  Front   door ", "role": "entrance", "quality": "sub"},
            {"channel": 2, "name": "", "quality": "sub"},
        ],
    })
    assert res.status_code == 201, res.text
    cams = res.json()["cameras"]
    assert cams[0]["name"] == "Front door" and cams[0]["role"] == "entrance"
    assert cams[1]["name"] == "Recorder channel 2" and cams[1]["role"] is None
    # No department chosen: stored as GENERAL, the camera table's "none" value.
    assert cams[0]["department"] == "GENERAL" and cams[1]["department"] == "GENERAL"

    first = client.get(f"/api/v1/cameras/{cams[0]['camera_id']}").json()
    second = client.get(f"/api/v1/cameras/{cams[1]['camera_id']}").json()
    from app.services import camera_roles

    assert first["role"] == "entrance"
    for key, value in camera_roles.default_features("entrance").items():
        assert first["features"][key] == value
    assert second.get("role") in (None, "")          # nothing assigned automatically


def test_adopt_unknown_purpose_is_refused_before_anything_is_stored(client):
    host = _host()
    res = client.post("/api/v1/dahua/adopt", json={
        "host": host, "channels": [{"channel": 1, "name": "A"}, {"channel": 2, "name": "B", "role": "bedroom"}],
    })
    assert res.status_code == 422 and "Channel 2" in res.text
    cams = client.get("/api/v1/cameras").json()
    listed = cams["cameras"] if isinstance(cams, dict) else cams
    assert not [c for c in listed if host in (c.get("location") or "")]


# ------------------------------------------------------------- lost sales

def test_lost_sales_never_invented():
    from app.routes.analytics import (
        LOST_SALES_NEEDS_POS,
        LOST_SALES_NOT_ATTRIBUTABLE,
        _with_zone_lost_sales,
    )

    no_pos = _with_zone_lost_sales([{"zone_id": "z1", "visits": 12}], pos_connected=False)[0]
    assert no_pos["lost_sales"] is None
    assert no_pos["lost_sales_status"] == "needs_pos" and no_pos["lost_sales_reason"] == LOST_SALES_NEEDS_POS
    with_pos = _with_zone_lost_sales([{"zone_id": "z1", "visits": 12}], pos_connected=True)[0]
    assert with_pos["lost_sales"] is None
    assert with_pos["lost_sales_status"] == "not_attributable"
    assert with_pos["lost_sales_reason"] == LOST_SALES_NOT_ATTRIBUTABLE


def test_funnel_zones_carry_lost_sales_status(client):
    res = client.get("/api/v1/analytics/funnels")
    assert res.status_code == 200, res.text
    body = res.json()
    for z in body.get("zones") or []:
        assert z["lost_sales"] is None
        assert z["lost_sales_status"] == ("not_attributable" if body["pos_connected"] else "needs_pos")


def test_product_zone_category_suggestions_include_high_value_words(client):
    body = client.get("/api/v1/analytics/products/zones").json()
    assert "LIQUOR" in body["high_value_categories"]
    assert set(body["high_value_categories"]) <= set(body["category_suggestions"])
    assert all(c == c.upper() for c in body["category_suggestions"])
