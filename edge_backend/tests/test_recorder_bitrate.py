"""Recorder sub-stream bit rate: raise, verify on the stream, restore on failure.

Uses the fake Dahua recorder of tests/test_recorder_substreams.py (Digest with
a nonce bound to the connection, optional close-after-401).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.services.recorder_substreams import (
    ALREADY_RATE, CAPPED, NOT_ATTEMPTED, RAISED, REFUSED, RESTORE_OK, RESTORED, STOPPED,
)
from tests.test_recorder_substreams import (
    HOST, PASSWORD, USER, FakeDahua, FakeRtspSwitcher, _cams, _entry, _service,
)


class CappedDahua(FakeDahua):
    """Channels may carry ``maxrate`` (BitRateOptions upper bound) or ``nocaps``."""

    def handle(self, path: str):
        parts = urlsplit(path)
        q = dict(parse_qsl(parts.query))
        if parts.path == "/cgi-bin/encode.cgi" and q.get("action") == "getConfigCaps":
            c = self.channels.get(int(q.get("channel", 0)))
            if c is None or c.get("nocaps"):
                return 400, "Error\r\nBad Request!\r\n"
            return 200, (f"caps.ExtraFormat[0].Video.ResolutionTypes=D1,CIF\r\n"
                         f"caps.ExtraFormat[0].Video.BitRateOptions=32,{c.get('maxrate', 2048)}\r\n")
        return super().handle(path)


D1_512 = {"w": 704, "h": 576, "bitrate": 512, "caps": ["D1", "CIF"]}


@pytest.fixture
def make_fake():
    made = []

    def _make(channels, **kw):
        f = CappedDahua(channels, **kw)
        made.append(f)
        return f

    yield _make
    for f in made:
        f.stop()


def _bitrate(svc, fake, channels=None, *, kbps=768, password=PASSWORD, allow_lower=False):
    return asyncio.run(svc.run_bitrate(HOST, _cams(sorted(fake.channels)), channels, "tester", username=USER,
                                       password=password, http_port=fake.port, kbps=kbps, allow_lower=allow_lower))


def _sets_touch_only_bitrate(fake):
    for s in fake.set_calls:
        keys = [k for k in s if "ExtraFormat" in k]
        assert all(k.endswith("Video.BitRate") for k in keys), s


def test_raise_and_verify(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512), 25: {**D1_512, "bitrate": 1024}})
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake)
    assert run["state"] == "done", run
    e = _entry(run, 22)
    assert e["outcome"] == RAISED and e["text"] == "raised 512→768 kbps (stream verified)", e
    # "all below" leaves a channel already above the target out of the run entirely.
    assert [x["channel"] for x in run["channels"]] == [22]
    assert fake.channels[22]["bitrate"] == 768 and fake.channels[25]["bitrate"] == 1024
    assert (fake.channels[22]["w"], fake.channels[22]["h"]) == (704, 576)
    _sets_touch_only_bitrate(fake)
    assert svc.changed == [(["cam22"], (704, 576))]      # worker restarted once
    saved = json.loads(svc.state_path(HOST).read_text())
    assert saved["backups"]["22"]["config"]["Video.BitRate"] == "512"
    audit = svc.audit_entries(HOST)
    assert audit[0]["action"] == "set_substream_bitrate" and audit[0]["after"] == {"Video.BitRate": "768"}


def test_explicit_channel_already_higher_is_not_lowered(tmp_path, make_fake):
    fake = make_fake({25: {**D1_512, "bitrate": 1024}, 22: dict(D1_512)})
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake, [22, 25])
    assert _entry(run, 25)["outcome"] == ALREADY_RATE and _entry(run, 25)["text"] == "already ≥ 768 kbps (1024 kbps)"
    assert _entry(run, 22)["outcome"] == RAISED
    assert fake.channels[25]["bitrate"] == 1024
    # Lowering only with explicit channels and allow_lower.
    run = _bitrate(svc, fake, [25], allow_lower=True)
    assert _entry(run, 25)["outcome"] == RAISED and "1024→768" in _entry(run, 25)["text"]
    assert fake.channels[25]["bitrate"] == 768


def test_refused_is_kept(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)})
    fake.refuse_set = True
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake)
    e = _entry(run, 22)
    assert e["outcome"] == REFUSED and e["text"].startswith("kept 512 kbps: NVR refused ("), e
    assert fake.channels[22]["bitrate"] == 512 and svc.changed == []


def test_value_not_sticking_is_restored(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)})
    fake.no_stick.add(22)
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake)
    e = _entry(run, 22)
    assert e["outcome"] == RESTORED and "value did not stick" in e["text"], e
    assert fake.channels[22]["bitrate"] == 512
    assert svc.changed == [(["cam22"], None)]


def test_stream_down_after_change_is_restored(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)})
    sw = FakeRtspSwitcher(fake)
    sw.down.add(22)
    svc = _service(tmp_path, fake, sw)
    run = _bitrate(svc, fake)
    e = _entry(run, 22)
    assert e["outcome"] == RESTORED and "did not come up" in e["text"], e
    assert fake.channels[22]["bitrate"] == 512


def test_size_change_after_raise_is_restored(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)})
    sw = FakeRtspSwitcher(fake)
    sw.override[22] = (352, 288)
    svc = _service(tmp_path, fake, sw)
    e = _entry(_bitrate(svc, fake), 22)
    assert e["outcome"] == RESTORED and "not 704×576" in e["text"], e


def test_wrong_password_stops_at_first_401(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512), 23: dict(D1_512)}, close_after_challenge=True, nonce_per_connection=False)
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake, password="wrong")
    assert run["state"] == "stopped" and run["auth_failed"], run
    assert {e["outcome"] for e in run["channels"]} == {NOT_ATTEMPTED}
    assert fake.auth_failures == 1 and fake.set_calls == []


def test_close_after_challenge_recorder_works(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)}, close_after_challenge=True, nonce_per_connection=False)
    svc = _service(tmp_path, fake)
    assert _entry(_bitrate(svc, fake), 22)["outcome"] == RAISED
    assert fake.auth_failures == 0


def test_caps_clamp(tmp_path, make_fake):
    fake = make_fake({22: {**D1_512, "maxrate": 640}, 23: {**D1_512, "maxrate": 512}, 24: {**D1_512, "nocaps": 1}})
    svc = _service(tmp_path, fake)
    run = _bitrate(svc, fake)
    assert _entry(run, 22)["text"] == "raised 512→640 kbps (stream verified; capped at 640 kbps by the camera)"
    assert fake.channels[22]["bitrate"] == 640
    assert _entry(run, 23)["outcome"] == CAPPED and _entry(run, 23)["text"] == "kept 512 kbps: capped at 512 kbps by the camera"
    # Caps unreadable: proceeds (the change is still verified).
    assert _entry(run, 24)["outcome"] == RAISED and fake.channels[24]["bitrate"] == 768


def test_restore_brings_back_old_bitrate(tmp_path, make_fake):
    fake = make_fake({22: dict(D1_512)})
    svc = _service(tmp_path, fake)
    _bitrate(svc, fake)
    assert fake.channels[22]["bitrate"] == 768
    run = asyncio.run(svc.run_restore(HOST, _cams([22]), None, "tester", username=USER, password=PASSWORD,
                                      http_port=fake.port))
    assert _entry(run, 22)["outcome"] == RESTORE_OK
    assert fake.channels[22]["bitrate"] == 512 and (fake.channels[22]["w"], fake.channels[22]["h"]) == (704, 576)


def test_bitrate_api(tmp_path, make_fake, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import async_session_factory, engine
    from app.models.db_models import Base, CameraModel
    from app.routes import recorders as rr

    fake = make_fake({1: dict(D1_512), 2: {**D1_512, "bitrate": 1024}})
    svc = _service(tmp_path, fake)
    monkeypatch.setattr(rr, "recorder_substreams", svc)
    monkeypatch.setattr(rr, "recorder_credentials", lambda host, cams: (USER, PASSWORD, "test"))
    app = FastAPI()
    app.include_router(rr.router)
    tag = uuid.uuid4().hex[:6]
    ids = [f"br_{tag}_{ch}" for ch in (1, 2)]

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as db:
            for ch, cid in zip((1, 2), ids):
                db.add(CameraModel(id=cid, name=f"Ch {ch}", location="t", status="ONLINE",
                                   rtsp_url=f"rtsp://{HOST}:554/cam/realmonitor?channel={ch}&subtype=1"))
            await db.commit()

    async def cleanup():
        async with async_session_factory() as db:
            for cid in ids:
                row = await db.get(CameraModel, cid)
                if row is not None:
                    await db.delete(row)
            await db.commit()

    def wait(client):
        for _ in range(200):
            res = client.get(f"/api/v1/recorders/{HOST}/substreams/bitrate").json()
            if not res["running"]:
                return res
            client.portal.call(asyncio.sleep, 0.02)
        raise AssertionError("run did not finish")

    with TestClient(app) as client:
        client.portal.call(setup)
        try:
            base = f"/api/v1/recorders/{HOST}/substreams/bitrate"
            assert client.post(base, json={"kbps": 768}).status_code == 422
            assert client.post(base, json={"all_below": True, "kbps": 10}).status_code == 422
            assert client.post(base, json={"channels": [63]}).status_code == 400
            r = client.post(base, json={"all_below": True, "kbps": 768, "http_port": fake.port})
            assert r.status_code == 202 and r.json()["kind"] == "bitrate" and r.json()["kbps"] == 768
            res = wait(client)
            assert res["run"]["state"] == "done", res
            assert {e["channel"]: e["outcome"] for e in res["run"]["channels"]} == {1: RAISED}
            assert res["saved_channels"] == [1]
            prev = client.get(f"/api/v1/recorders/{HOST}/substreams", params={"http_port": fake.port}).json()
            assert {r["channel"]: r["bitrate_kbps"] for r in prev["channels"]} == {1: 768, 2: 1024}
            r = client.post(f"/api/v1/recorders/{HOST}/substreams/restore", json={"all": True, "http_port": fake.port})
            assert r.status_code == 202
            res = wait(client)
            assert _entry(res["run"], 1)["outcome"] == RESTORE_OK and fake.channels[1]["bitrate"] == 512
        finally:
            client.portal.call(cleanup)
