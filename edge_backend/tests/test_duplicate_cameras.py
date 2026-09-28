"""Duplicate camera guard: identity, recorder device lists, 409 on add, store-total exclusion.

The live case this guards against: NVR 192.168.20.160 channel 1 added once
by typing its main-stream URL and again from the recorder's channel list on
its sub-stream, so every person was counted twice.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.services import dahua_config as dc
from app.services import duplicate_cameras as dcam
from app.services.duplicate_cameras import (
    DuplicateGuard, identity_for, map_channels, parse_remote_devices, read_recorder_devices,
)
from tests.test_recorder_substreams import HOST, PASSWORD, USER, FakeDahua

NVR = "192.168.20.160"
MAIN = f"rtsp://{NVR}:554/cam/realmonitor?channel=1&subtype=0"
SUB = f"rtsp://{NVR}:554/cam/realmonitor?channel=1&subtype=1"


def _cam(cid, url, name=None, created="2026-09-01", enabled=True, location=""):
    return {"id": cid, "name": name or cid, "rtsp_url": url, "created_at": created, "enabled": enabled,
            "location": location}


# -------------------------------------------------------------------- identity

def test_dahua_main_and_sub_are_the_same_camera():
    a, b = identity_for(MAIN), identity_for(SUB)
    assert a.key == b.key == f"net:{NVR}:554/ch1"
    assert (a.stream, b.stream) == ("main", "sub")
    # Port written or implied, credentials in the URL: same identity.
    c = identity_for(f"rtsp://admin:p%40ss@{NVR}/cam/realmonitor?channel=1&subtype=2")
    assert c.key == a.key and c.stream == "sub2"


def test_different_channels_hosts_and_ports_differ():
    ch2 = identity_for(f"rtsp://{NVR}:554/cam/realmonitor?channel=2&subtype=1")
    assert ch2.key != identity_for(SUB).key and not (ch2.keys & identity_for(SUB).keys)
    other = identity_for("rtsp://192.168.20.161/cam/realmonitor?channel=1&subtype=1")
    assert not (other.keys & identity_for(SUB).keys)
    # Port-forwarded cameras behind one address are different cameras.
    p1, p2 = identity_for("rtsp://203.0.113.5:5541/stream1"), identity_for("rtsp://203.0.113.5:5542/stream1")
    assert not (p1.keys & p2.keys)


def test_hikvision_onvif_and_generic_grammars():
    h1, h2 = identity_for("rtsp://10.0.0.9/Streaming/Channels/101"), identity_for("rtsp://u:p@10.0.0.9:554/Streaming/Channels/102")
    assert h1.key == h2.key and (h1.channel, h1.stream, h2.stream) == (1, "main", "sub")
    assert identity_for("rtsp://10.0.0.9/Streaming/Channels/201").key != h1.key
    assert identity_for("rtsp://10.0.0.9/h264/ch1/sub/av_stream").keys & h1.keys
    # ONVIF-resolved profile URIs and stream1/stream2 of one camera.
    o1 = identity_for("rtsp://10.0.0.20/profile1/media.smp")
    o2 = identity_for("rtsp://10.0.0.20:554/profile2/media.smp")
    assert o1.key == o2.key
    s1, s2 = identity_for("rtsp://10.0.0.21/stream1"), identity_for("rtsp://10.0.0.21/stream2")
    assert s1.key == s2.key and (s1.stream, s2.stream) == ("main", "sub")
    # An unknown path is its own identity (no guessing).
    g1, g2 = identity_for("rtsp://10.0.0.22/live/ch1"), identity_for("rtsp://10.0.0.22/live/ch2")
    assert not (g1.keys & g2.keys)
    # MJPEG, USB, files.
    assert identity_for("http://u:p@10.0.0.23:8080/video.mjpg").key == identity_for("http://10.0.0.23:8080/video.mjpg").key
    assert identity_for("0").key == identity_for("/dev/video0").key
    assert identity_for("/home/op/test.mp4") is None


def test_guard_groups_live_case_and_prefers_nvr_sub_stream(tmp_path):
    g = DuplicateGuard(storage_dir=tmp_path)
    g.set_cameras_override([
        _cam("cam_b4fbe4dbd6", MAIN, "Dahua NVR/Camera (RTSP)", created="2026-09-01"),
        _cam("cam_1c7b1fe3a2", SUB, "Dahua NVR Ch 1", created="2026-09-10", location=f"Dahua NVR {NVR} Ch 1"),
        _cam("cam_ch2", f"rtsp://{NVR}:554/cam/realmonitor?channel=2&subtype=1", "Dahua NVR Ch 2"),
    ])
    rep = g.report()
    assert rep["count"] == 1
    grp = rep["groups"][0]
    assert grp["primary_id"] == "cam_1c7b1fe3a2"            # sub-stream beats older main stream
    assert rep["excluded_camera_ids"] == ["cam_b4fbe4dbd6"]
    assert grp["message"] == ("Same camera added twice: Dahua NVR Ch 1 (NVR channel 1, sub-stream) and "
                              "Dahua NVR/Camera (RTSP) (NVR channel 1, main stream).")
    assert g.camera_fields("cam_b4fbe4dbd6") == {"duplicate_group": grp["id"], "duplicate_of": "cam_1c7b1fe3a2",
                                                 "duplicate_primary": False}
    assert g.camera_fields("cam_ch2")["duplicate_group"] is None
    # No device list read yet: said so, host+channel matching only.
    assert rep["recorders"][0]["host"] == NVR and "not read yet" in rep["recorders"][0]["note"]


def test_turned_off_copy_is_never_primary_and_operator_choice_wins(tmp_path):
    g = DuplicateGuard(storage_dir=tmp_path)
    g.set_cameras_override([_cam("a", SUB, enabled=False), _cam("b", MAIN)])
    assert g.report()["groups"][0]["primary_id"] == "b"
    g.set_primary("a", ["a", "b"])
    assert g.report()["groups"][0]["primary_id"] == "a"
    assert g.excluded_ids() == frozenset({"b"})


def test_dismiss_is_stored(tmp_path):
    g = DuplicateGuard(storage_dir=tmp_path)
    cams = [_cam("a", SUB), _cam("b", MAIN)]
    g.set_cameras_override(cams)
    g.dismiss(["a", "b"], actor="tester")
    assert g.report()["count"] == 0 and g.excluded_ids() == frozenset()
    # A new process (new guard on the same storage) remembers it.
    g2 = DuplicateGuard(storage_dir=tmp_path)
    g2.set_cameras_override(cams)
    assert g2.report()["count"] == 0
    assert g2.report()["dismissed"][0]["camera_ids"] == ["a", "b"]
    g2.undismiss(["a", "b"])
    assert g2.report()["count"] == 1


# ------------------------------------------------------ recorder device list

REMOTE_DEVICE = """table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=192.168.20.21
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Port=37777
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.UserName=admin
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Password=cam-secret-1
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.VideoInputChannels=1
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.VideoInputs[0].Name=Front door
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Address=192.168.20.22
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Password=cam-secret-2
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_1.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Address=0.0.0.0
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_2.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_3.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_3.VideoInputs[0].MainStreamUrl=rtsp://192.168.20.24:554/stream1
"""
REMOTE_CHANNEL = """table.RemoteChannel[0].Device=uuid:System_CONFIG_NETCAMERA_INFO_1
table.RemoteChannel[0].Channel=0
table.RemoteChannel[0].Enable=true
table.RemoteChannel[1].Device=uuid:System_CONFIG_NETCAMERA_INFO_0
table.RemoteChannel[1].Channel=0
table.RemoteChannel[1].Enable=true
"""


class RemoteDeviceDahua(FakeDahua):
    remote_channel = True

    def handle(self, path):
        q = dict(parse_qsl(urlsplit(path).query))
        if q.get("action") == "getConfig" and q.get("name") == "RemoteDevice":
            return 200, REMOTE_DEVICE.replace("\n", "\r\n")
        if q.get("action") == "getConfig" and q.get("name") == "RemoteChannel":
            if self.remote_channel:
                return 200, REMOTE_CHANNEL.replace("\n", "\r\n")
            return 400, "Error\r\nBad Request!\r\n"
        return super().handle(path)


@pytest.fixture
def fake_nvr():
    made = []

    def _make(**kw):
        f = RemoteDeviceDahua({1: {"w": 352, "h": 288}}, **kw)
        made.append(f)
        return f

    yield _make
    for f in made:
        f.stop()


def test_parse_remote_devices_is_defensive_and_keeps_no_passwords():
    kv = dc.parse_kv(REMOTE_DEVICE)
    devs = parse_remote_devices(kv)
    assert set(devs) == {"uuid:System_CONFIG_NETCAMERA_INFO_0", "uuid:System_CONFIG_NETCAMERA_INFO_1",
                         "uuid:System_CONFIG_NETCAMERA_INFO_3"}
    assert devs["uuid:System_CONFIG_NETCAMERA_INFO_3"]["address"] == "192.168.20.24"
    assert "secret" not in repr(devs)
    # Without RemoteChannel: INFO_<n> is channel n+1.
    chans, how = map_channels(devs, {})
    assert how == "device ids" and chans["1"]["address"] == "192.168.20.21" and chans["4"]["address"] == "192.168.20.24"
    # With RemoteChannel it decides.
    chans, how = map_channels(devs, dc.parse_kv(REMOTE_CHANNEL))
    assert how == "RemoteChannel" and chans == {
        "1": {"address": "192.168.20.22", "rtsp_port": None, "input": 1},
        "2": {"address": "192.168.20.21", "rtsp_port": None, "input": 1}}
    # Array form and junk.
    arr = parse_remote_devices({"table.RemoteDevice[0].Address": "10.1.1.1", "table.RemoteDevice[0].Port": "x",
                                "garbage": "1", "table.RemoteDevice": "?"})
    assert map_channels(arr)[0]["1"]["address"] == "10.1.1.1"


@pytest.mark.parametrize("close_after_challenge", [False, True])
def test_read_device_list_over_digest(fake_nvr, close_after_challenge):
    fake = fake_nvr(close_after_challenge=close_after_challenge, nonce_per_connection=not close_after_challenge)
    client = dc.DahuaHttpClient(HOST, USER, PASSWORD, port=fake.port)
    try:
        data = read_recorder_devices(client)
    finally:
        client.close()
    assert data["source"] == "RemoteChannel" and data["channels"]["2"]["address"] == "192.168.20.21"
    assert fake.auth_failures == 0
    assert [p.split("name=")[1] for p in fake.requests] == ["RemoteDevice", "RemoteChannel"]


def test_wrong_password_stops_at_first_401_and_falls_back(fake_nvr, tmp_path, monkeypatch):
    fake = fake_nvr(close_after_challenge=True, nonce_per_connection=False)
    g = DuplicateGuard(storage_dir=tmp_path)
    g.set_cameras_override([_cam("a", f"rtsp://{HOST}:554/cam/realmonitor?channel=2&subtype=1")])
    import app.services.recorder_substreams as rs

    monkeypatch.setattr(rs, "recorder_credentials", lambda host, cams: (USER, "wrong", "test"))
    out = asyncio.run(g.refresh_recorder(HOST, [{"id": "a"}], http_port=fake.port))
    assert out["ok"] is False and out["auth_failed"] and "not retried" in out["error"]
    assert fake.auth_failures == 1 and fake.requests == []
    entry = g.recorder_maps()[HOST]
    assert entry["channels"] == {} and "wrong" not in repr(entry)


def test_nvr_channel_matches_camera_added_by_its_own_ip(fake_nvr, tmp_path, monkeypatch):
    fake = fake_nvr()
    g = DuplicateGuard(storage_dir=tmp_path)
    nvr_ch2 = f"rtsp://{HOST}:554/cam/realmonitor?channel=2&subtype=1"
    direct = "rtsp://192.168.20.21:554/cam/realmonitor?channel=1&subtype=0"
    g.set_cameras_override([_cam("nvr2", nvr_ch2, "Front door (NVR)", created="2026-09-10"),
                            _cam("direct", direct, "Front door direct", created="2026-09-01"),
                            _cam("nvr1", f"rtsp://{HOST}:554/cam/realmonitor?channel=1&subtype=1")])
    assert g.report()["count"] == 0          # map unknown: host + channel only
    import app.services.recorder_substreams as rs

    monkeypatch.setattr(rs, "recorder_credentials", lambda host, cams: (USER, PASSWORD, "test"))
    out = asyncio.run(g.refresh_recorder(HOST, [], http_port=fake.port))
    assert out["ok"] and out["source"] == "RemoteChannel"
    rep = g.report()
    assert rep["count"] == 1
    grp = rep["groups"][0]
    assert grp["reason"] == "nvr_and_direct" and grp["primary_id"] == "nvr2"   # NVR channel preferred
    assert {c["camera_id"] for c in grp["cameras"]} == {"nvr2", "direct"}
    assert "192.168.20.21" in grp["message"]
    assert rep["recorders"][0]["ok"] and rep["recorders"][0]["channels_mapped"] == 2
    # Adding the direct camera again is refused with the NVR channel named.
    found = g.conflicts("rtsp://192.168.20.21/stream1")
    assert [f["camera_id"] for f in found] == ["nvr2", "direct"]
    detail = g.conflict_detail("rtsp://192.168.20.21/stream1", found)
    assert "Front door (NVR)" in detail["message"] and "NVR channels" in detail["message"]


# -------------------------------------------------------------------- the API

@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import engine
    from app.models.db_models import Base
    from app.routes import cameras, dahua, layout
    from app.services.pipeline_supervisor import pipeline_supervisor

    async def no_reconcile(*a, **kw):
        return None

    monkeypatch.setattr(pipeline_supervisor, "reconcile_cameras", no_reconcile)
    monkeypatch.setattr(dcam.duplicate_guard, "auto_refresh", False)
    app = FastAPI()
    for r in (cameras.router, layout.router, dahua.router):
        app.include_router(r)
    created: list[str] = []
    with TestClient(app) as client:
        async def setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        client.portal.call(setup)
        client.created = created
        yield client
        for cid in created:
            client.delete(f"/api/v1/cameras/{cid}")
    dcam.duplicate_guard.invalidate()


def _add(client, url, name, **extra):
    r = client.post("/api/v1/cameras", json={"name": name, "rtsp_url": url, "source_type": "rtsp",
                                             "is_ai_enabled": False, **extra})
    if r.status_code == 200:
        client.created.append(r.json()["id"])
    return r


def test_add_refused_with_409_then_allowed(api):
    host = f"10.77.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    sub = f"rtsp://{host}:554/cam/realmonitor?channel=1&subtype=1"
    first = _add(api, sub, "Dahua NVR Ch 1", location=f"Dahua NVR {host} Ch 1")
    assert first.status_code == 200, first.text
    r = _add(api, f"rtsp://admin:pw@{host}:554/cam/realmonitor?channel=1&subtype=0", "Typed URL")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "duplicate_camera" and d["existing"][0]["camera_id"] == first.json()["id"]
    assert "Dahua NVR Ch 1" in d["message"] and "NVR channels" in d["message"] and "pw" not in r.text
    ok = _add(api, f"rtsp://{host}:554/cam/realmonitor?channel=1&subtype=0", "Close-up", allow_duplicate=True)
    assert ok.status_code == 200 and ok.json()["duplicate_of"] == first.json()["id"]
    cams = {c["id"]: c for c in api.get("/api/v1/cameras").json()["cameras"]}
    assert cams[ok.json()["id"]]["duplicate_of"] == first.json()["id"]
    assert cams[first.json()["id"]]["duplicate_group"] == cams[ok.json()["id"]]["duplicate_group"]
    rep = api.get("/api/v1/cameras/duplicates").json()
    assert any(g["primary_id"] == first.json()["id"] for g in rep["groups"])
    # Another channel of the same recorder is fine; changing its URL onto channel 1 is not.
    other = _add(api, f"rtsp://{host}:554/cam/realmonitor?channel=2&subtype=1", "Ch 2")
    assert other.status_code == 200
    body = {**other.json(), "rtsp_url": sub}
    assert api.put(f"/api/v1/cameras/{other.json()['id']}", json=body).status_code == 409
    assert api.put(f"/api/v1/cameras/{other.json()['id']}", json={**body, "allow_duplicate": True}).status_code == 200


def test_scan_adopt_and_dahua_import_refuse_duplicates(api, monkeypatch):
    from app.database import async_session_factory
    from app.models.db_models import DiscoveredDeviceModel
    from app.routes import layout

    host = f"10.78.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    first = _add(api, f"rtsp://{host}:554/cam/realmonitor?channel=3&subtype=1", "Ch 3")
    assert first.status_code == 200
    dev_id = f"dev_{uuid.uuid4().hex[:8]}"

    async def add_device():
        async with async_session_factory() as db:
            db.add(DiscoveredDeviceModel(id=dev_id, transport="rtsp", driver="dahua", host=host, port=554))
            await db.commit()
    api.portal.call(add_device)

    async def stream_ok(*a, **kw):
        return "ok"
    monkeypatch.setattr(layout, "_adopt_stream_check", stream_ok)
    body = {"device_id": dev_id, "name": "Adopted", "channel": 3, "quality": "main"}
    r = api.post("/api/v1/layout/devices/adopt", json=body)
    assert r.status_code == 409 and r.json()["detail"]["existing"][0]["camera_id"] == first.json()["id"]
    r = api.post("/api/v1/layout/devices/adopt", json={**body, "allow_duplicate": True})
    assert r.status_code == 201
    api.created.append(r.json()["camera_id"])

    # Recorder import: all-or-nothing 409 naming the channel; channel 4 alone is fine.
    r = api.post("/api/v1/dahua/adopt", json={"host": host, "channels": [{"channel": 3}, {"channel": 4}]})
    assert r.status_code == 409
    assert [c["channel"] for c in r.json()["detail"]["channels"]] == [3]
    r = api.post("/api/v1/dahua/adopt", json={"host": host, "channels": [{"channel": 4}, {"channel": 4, "quality": "main"}]})
    assert r.status_code == 409 and [c["channel"] for c in r.json()["detail"]["channels"]] == [4]
    r = api.post("/api/v1/dahua/adopt", json={"host": host, "channels": [{"channel": 4}]})
    assert r.status_code == 201
    api.created.extend(c["camera_id"] for c in r.json()["cameras"])


def test_dismiss_and_primary_routes(api):
    host = f"10.79.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    a = _add(api, f"rtsp://{host}/Streaming/Channels/102", "Hik sub").json()["id"]
    b = _add(api, f"rtsp://{host}/Streaming/Channels/101", "Hik main", allow_duplicate=True).json()["id"]
    rep = api.post("/api/v1/cameras/duplicates/primary", json={"camera_id": b}).json()
    grp = next(g for g in rep["groups"] if a in g["excluded_camera_ids"] or b in g["excluded_camera_ids"])
    assert grp["primary_id"] == b and grp["excluded_camera_ids"] == [a]
    rep = api.post("/api/v1/cameras/duplicates/dismiss", json={"camera_ids": [a, b]}).json()
    assert not any(a in [c["camera_id"] for c in g["cameras"]] for g in rep["groups"])
    assert api.get(f"/api/v1/cameras/{a}").json()["duplicate_of"] is None
    assert api.post("/api/v1/cameras/duplicates/dismiss", json={"camera_ids": [a, "nope"]}).status_code == 404


# ------------------------------------------------------ store-total exclusion

def test_duplicate_does_not_double_store_totals(tmp_path, monkeypatch):
    from app.database import async_session_factory, engine
    from app.models.db_models import Base, TripwireEventModel, ZoneVisitModel
    from app.services.retail_metrics_service import retail_metrics_service
    from app.services.tripwire_engine import tripwire_entries, tripwire_footfall

    g = DuplicateGuard(storage_dir=tmp_path)
    monkeypatch.setattr(dcam, "duplicate_guard", g)
    tag = uuid.uuid4().hex[:6]
    prim, dup = f"p_{tag}", f"d_{tag}"
    host = f"10.80.{uuid.uuid4().int % 250}.1"
    g.set_cameras_override([_cam(prim, f"rtsp://{host}/cam/realmonitor?channel=1&subtype=1"),
                            _cam(dup, f"rtsp://{host}/cam/realmonitor?channel=1&subtype=0")])
    start = datetime(2001, 1, 1) + timedelta(days=uuid.uuid4().int % 3000)
    end = start + timedelta(hours=1)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as db:
            before_active = await retail_metrics_service.active_shoppers(db)
            for cam in (prim, dup):   # the same 3 people crossing, seen by both copies
                for i in range(3):
                    db.add(TripwireEventModel(tripwire_id=f"tw_{cam}", tripwire_name="Door", camera_id=cam,
                                              track_id=f"t{i}", direction="in", ts=start + timedelta(minutes=i),
                                              counts_footfall=True))
                    db.add(ZoneVisitModel(id=f"zv_{cam}_{i}", zone_id=f"z_{tag}", track_id=f"{cam}_t{i}",
                                          camera_id=cam, entered_at=datetime.utcnow() - timedelta(minutes=5)))
            await db.commit()
            try:
                entries = await tripwire_entries(db, start, end)
                foot = await tripwire_footfall(db, start, end)
                active = await retail_metrics_service.active_shoppers(db) - before_active
                g.dismiss([prim, dup])          # "different cameras": both count again
                entries_both = await tripwire_entries(db, start, end)
                active_both = await retail_metrics_service.active_shoppers(db) - before_active
            finally:
                from sqlalchemy import delete
                await db.execute(delete(TripwireEventModel).where(TripwireEventModel.camera_id.in_([prim, dup])))
                await db.execute(delete(ZoneVisitModel).where(ZoneVisitModel.camera_id.in_([prim, dup])))
                await db.commit()
            return entries, foot, active, entries_both, active_both

    entries, foot, active, entries_both, active_both = asyncio.run(run())
    assert entries == 3 and active == 3
    assert foot["totals"]["in"] == 3
    line = next(r for r in foot["tripwires"] if r.get("camera_id") == dup)
    assert line["counts_footfall"] is False and "duplicate camera" in line["excluded_reason"]
    assert entries_both == 6 and active_both == 6


def test_floor_heatmap_leaves_duplicate_out_but_keeps_its_image(tmp_path, monkeypatch):
    from app.database import async_session_factory, engine
    from app.models.db_models import Base, CustomerTrackModel
    from app.services import heatmap_history as hh

    g = DuplicateGuard(storage_dir=tmp_path)
    monkeypatch.setattr(dcam, "duplicate_guard", g)
    tag = uuid.uuid4().hex[:6]
    prim, dup = f"hp_{tag}", f"hd_{tag}"
    g.set_cameras_override([_cam(prim, "rtsp://10.81.0.1/cam/realmonitor?channel=1&subtype=1"),
                            _cam(dup, "rtsp://10.81.0.1/cam/realmonitor?channel=1&subtype=0")])
    start = datetime(1999, 1, 1) + timedelta(hours=uuid.uuid4().int % 5000)
    t0 = start.timestamp()

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as db:
            for cam in (prim, dup):
                pts = [{"x": 2.0, "y": 2.0, "u": 0.5, "v": 0.5, "t": t0 + 60 + k} for k in range(5)]
                db.add(CustomerTrackModel(id=f"ct_{cam}", track_id=f"t_{cam}", camera_id=cam, start_time=start + timedelta(minutes=1),
                                          end_time=start + timedelta(minutes=2), trajectory_points=pts))
            await db.commit()
            try:
                return await hh.compute_hour(db, start, start + timedelta(hours=1))
            finally:
                from sqlalchemy import delete
                await db.execute(delete(CustomerTrackModel).where(CustomerTrackModel.camera_id.in_([prim, dup])))
                await db.commit()

    out = asyncio.run(run())
    assert ("image", dup, "presence") in out and ("image", prim, "presence") in out
    floor = out.get(("floor", None, "presence"))
    if floor is not None:   # a layout exists in the test database
        assert floor["samples"] == out[("image", prim, "presence")]["samples"]


# -------------------------------------------------------------- delete impact

def test_delete_impact_lists_deleted_and_kept_and_removal_stays_excluded(api):
    from app.database import async_session_factory
    from app.models.db_models import TripwireEventModel

    host = f"10.82.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    a = _add(api, f"rtsp://{host}/cam/realmonitor?channel=1&subtype=1", "Sub").json()["id"]
    b = _add(api, f"rtsp://{host}/cam/realmonitor?channel=1&subtype=0", "Main", allow_duplicate=True).json()["id"]

    async def add_rows():
        async with async_session_factory() as db:
            for i in range(4):
                db.add(TripwireEventModel(tripwire_id="tw", camera_id=b, track_id=str(i), direction="in",
                                          ts=datetime(2002, 1, 1), counts_footfall=True))
            await db.commit()
    api.portal.call(add_rows)
    imp = api.get(f"/api/v1/cameras/{b}/delete-impact").json()
    kept = {k["what"]: k["count"] for k in imp["kept"]}
    deleted = {k["what"]: k["count"] for k in imp["deleted"]}
    assert kept["counting-line crossings (footfall history)"] == 4
    assert deleted["camera settings, role, analytics switches"] == 1
    assert imp["duplicate_of"] == a and "excluded from store totals" in imp["note"]

    assert api.delete(f"/api/v1/cameras/{b}").status_code == 200
    api.created.remove(b)

    async def count_rows():
        from sqlalchemy import func, select
        async with async_session_factory() as db:
            return await db.scalar(select(func.count()).select_from(TripwireEventModel).where(TripwireEventModel.camera_id == b))
    assert api.portal.call(count_rows) == 4              # history kept, as the impact said
    dcam.duplicate_guard.invalidate()
    rep = dcam.duplicate_guard.report()
    assert b in rep["excluded_camera_ids"] and not any(a in g["excluded_camera_ids"] + [g["primary_id"]] for g in rep["groups"])
    assert api.get("/api/v1/cameras/nope/delete-impact").status_code == 404
