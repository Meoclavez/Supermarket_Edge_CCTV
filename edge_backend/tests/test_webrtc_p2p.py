"""Direct peer-to-peer live video (docs/REMOTE_VIDEO_CONTRACT.md).

go2rtc is replaced by an in-process fake that behaves like its API (one
stream per session, consumers attach at negotiation, "codecs not matched" for
H.265 to a browser without it). The real binary is exercised end to end
outside the suite (see the report of the change); here every rule of the
contract is checked: STUN only, sessions end on every trigger, the stream is
deleted when its session ends, limits, roles and diagnostics.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.config import settings
from app.database import async_session_factory, engine
from app.main import app
from app.models.db_models import Base, CameraModel
from app.services import go2rtc_manager as g2
from app.services import stun_probe
from app.services.auth_service import auth_service, general_rate_limiter, intrusion_detector
from app.services.remote_access_service import (RemoteAccessError, normalise_stun_server, normalise_stun_servers,
                                                remote_access_service)
from app.services.webrtc_sessions import (UNCONNECTED_CONSUMER_MAX_S, SessionError, WebRtcSessions, mismatch_source_codec, offer_video_codecs,
                                          producer_codec, producer_encoder)

CAM = "cam_p2p_1"
CAM2 = "cam_p2p_2"
OFFER_H264 = ("v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
              "m=video 9 UDP/TLS/RTP/SAVPF 96 98\r\na=rtpmap:96 VP8/90000\r\na=rtpmap:98 H264/90000\r\n"
              "a=recvonly\r\n")
OFFER_H265 = OFFER_H264.replace("a=recvonly", "a=rtpmap:100 H265/90000\r\na=recvonly")


def run(coro):
    return asyncio.run(coro)


class FakeGo2Rtc:
    """Just enough of go2rtc's API, with its real semantics."""

    def __init__(self, source_codec="H264", installed=True):
        self.source_codec = source_codec
        self.installed = installed
        self.is_running = False
        self.streams_: dict[str, dict] = {}
        self.sources: dict[str, str] = {}
        self.deleted: list[str] = []
        self.starts = 0
        self.stops = 0
        self.fail_answer: str | None = None
        self.on_exit = None

    def running(self):
        return self.is_running

    async def ensure_running(self):
        if not self.installed:
            raise g2.Go2RtcUnavailable("go2rtc is not installed")
        if not self.is_running:
            self.is_running = True
            self.starts += 1

    async def stop(self):
        if self.is_running:
            self.stops += 1
        self.is_running = False
        self.streams_.clear()

    def status(self):
        return {"installed": self.installed, "binary": "/fake/go2rtc" if self.installed else None,
                "version": "1.9.14", "state": "running" if self.is_running else "stopped",
                "running": self.is_running, "pid": 1 if self.is_running else None, "last_error": None,
                "restarts": 0, "api": "127.0.0.1:1984", "listen": ""}

    async def api_info(self):
        return {"version": "1.9.14"}

    async def add_stream(self, name, source):
        self.sources[name] = source
        self.streams_.setdefault(name, {"producers": [{"url": source}], "consumers": None})
        self.streams_[name]["producers"] = [{"url": source}]

    async def delete_stream(self, name):
        self.deleted.append(name)
        self.streams_.pop(name, None)

    async def streams(self):
        return {k: dict(v) for k, v in self.streams_.items()}

    async def stream(self, name):
        return self.streams_.get(name)

    async def webrtc_answer(self, name, offer_sdp, timeout=25.0):
        if self.fail_answer:
            raise g2.Go2RtcError(self.fail_answer, 500)
        src = self.sources[name]
        offered = offer_video_codecs(offer_sdp)
        produced = "H264" if src.startswith("ffmpeg:") else self.source_codec
        if produced not in offered:
            raise g2.Go2RtcError(f"streams: codecs not matched: video:{produced} => video:"
                                 + ", video:".join(sorted(offered)), 500)
        prod = {"url": src, "medias": [f"video, recvonly, {produced}"], "bytes_recv": 1000}
        if src.startswith("ffmpeg:"):
            prod["source"] = "exec:ffmpeg -hide_banner -i rtsp://x -c:v h264_vaapi -g 50 -f rtsp {output}"
        self.streams_[name] = {"producers": [prod],
                               "consumers": [{"id": "c1", "format_name": "webrtc/json", "bytes_send": 0,
                                              "remote_addr": "198.51.100.7:50000 srflx"}]}
        return "v=0\r\nanswer-for-" + name

    # test helpers
    def drop_consumer(self, name):
        self.streams_[name]["consumers"] = None


@pytest.fixture(scope="module", autouse=True)
def _schema():
    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    run(go())


async def _add_cameras():
    async with async_session_factory() as db:
        await db.execute(delete(CameraModel).where(CameraModel.id.in_([CAM, CAM2])))
        db.add(CameraModel(id=CAM, name="P2P one", location="Test", rtsp_url="rtsp://10.9.8.7:554/cam/realmonitor?channel=1&subtype=1"))
        db.add(CameraModel(id=CAM2, name="P2P two", location="Test", rtsp_url="rtsp://10.9.8.7:554/cam/realmonitor?channel=2&subtype=1"))
        await db.commit()


async def _set_enabled(cam, enabled):
    async with async_session_factory() as db:
        await db.execute(update(CameraModel).where(CameraModel.id == cam).values(is_ai_enabled=enabled))
        await db.commit()


async def _delete_camera(cam):
    async with async_session_factory() as db:
        await db.execute(delete(CameraModel).where(CameraModel.id == cam))
        await db.commit()


@pytest.fixture
def svc(monkeypatch):
    run(_add_cameras())
    fake = FakeGo2Rtc()
    sessions = WebRtcSessions(manager=fake)
    sources = {CAM: "rtsp://admin:pw@10.9.8.7:554/cam/realmonitor?channel=1&subtype=1",
               CAM2: "rtsp://admin:pw@10.9.8.7:554/cam/realmonitor?channel=2&subtype=1"}
    monkeypatch.setattr(WebRtcSessions, "_source_for", staticmethod(lambda cam: sources[cam.id]))
    monkeypatch.setattr(sessions, "_ensure_reaper", lambda: None)
    monkeypatch.setitem(remote_access_service._settings, "max_video_sessions", 8)
    monkeypatch.setitem(remote_access_service._settings, "stun_servers", ["stun:stun.example.net:3478"])
    sessions.test_sources = sources
    sessions.fake = fake
    return sessions


def _create(svc, cam=CAM, sdp=OFFER_H264, **kw):
    return run(svc.create(camera_id=cam, offer_sdp=sdp, purpose="tile", viewer_ip="203.0.113.9", remote=True,
                          user="op", **kw))


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

def test_passthrough_session_one_stream_per_session(svc):
    sess, answer = _create(svc)
    assert answer.startswith("v=0") and sess.codec == "H264" and not sess.transcoded
    assert svc.fake.starts == 1, "go2rtc starts on the first session"
    # The source is the analysis resolver's URL (credentials included, in memory only).
    assert svc.fake.sources[sess.stream].startswith("rtsp://admin:pw@")
    sess2, _ = _create(svc)
    assert sess2.stream != sess.stream and len(svc.fake.streams_) == 2


def test_h265_camera_is_transcoded_for_a_browser_without_h265(svc):
    svc.fake.source_codec = "H265"
    sess, _ = _create(svc, sdp=OFFER_H264)
    assert sess.transcoded and sess.codec == "H264" and sess.source_codec == "H265"
    assert svc.fake.sources[sess.stream].startswith("ffmpeg:rtsp://")
    assert "#video=h264#hardware" in svc.fake.sources[sess.stream]
    assert sess.encoder == "h264_vaapi"
    # Learned: the next session goes straight to the transcode.
    calls = []
    orig = svc.fake.webrtc_answer

    async def spy(name, sdp, timeout=25.0):
        calls.append(svc.fake.sources[name])
        return await orig(name, sdp, timeout)
    svc.fake.webrtc_answer = spy
    sess2, _ = _create(svc, sdp=OFFER_H264)
    assert len(calls) == 1 and calls[0].startswith("ffmpeg:") and sess2.transcoded
    # A browser that plays H.265 gets it passed through.
    calls.clear()
    sess3, _ = _create(svc, sdp=OFFER_H265)
    assert not sess3.transcoded and sess3.codec == "H265"


def test_errors_have_contract_codes(svc, monkeypatch):
    with pytest.raises(SessionError) as e:
        _create(svc, cam="nope")
    assert e.value.status == 404
    run(_set_enabled(CAM2, False))
    with pytest.raises(SessionError) as e:
        _create(svc, cam=CAM2)
    assert (e.value.status, e.value.code) == (409, "camera_off")
    svc.fake.fail_answer = "streams: dial tcp 10.9.8.7:554: i/o timeout"
    with pytest.raises(SessionError) as e:
        _create(svc)
    assert (e.value.status, e.value.code) == (502, "negotiation_failed")
    assert "could not be opened" in e.value.reason
    assert not svc.sessions, "a failed negotiation frees its slot"
    assert svc.fake.deleted, "and deletes its stream"
    svc.fake.fail_answer = None
    svc.fake.installed = False
    with pytest.raises(SessionError) as e:
        _create(svc)
    assert (e.value.status, e.value.code) == (503, "webrtc_unavailable")
    assert "go2rtc not running" in e.value.reason
    svc.fake.installed = True
    svc.test_sources[CAM] = "/dev/video0"
    with pytest.raises(SessionError) as e:
        _create(svc)
    assert e.value.status == 503 and "no stream source" in e.value.reason


def test_session_limit(svc, monkeypatch):
    monkeypatch.setitem(remote_access_service._settings, "max_video_sessions", 2)
    _create(svc)
    _create(svc)
    with pytest.raises(SessionError) as e:
        _create(svc)
    assert (e.value.status, e.value.code, e.value.extra) == (429, "video_session_limit", {"max_sessions": 2})


def test_close_deletes_the_stream_once_the_consumer_leaves(svc):
    sess, _ = _create(svc)
    assert run(svc.close(sess.id))
    assert svc.heartbeat(sess.id) is None, "heartbeat of an ended session answers 404"
    assert not run(svc.close(sess.id)), "idempotent"
    # The page closed its RTCPeerConnection: go2rtc drops the consumer.
    svc.fake.drop_consumer(sess.stream)
    run(svc.reap_once())
    assert sess.stream not in svc.fake.streams_ and sess.stream in svc.fake.deleted
    assert svc.outcomes()[-1]["end_reason"] == "closed_by_viewer"


def test_missed_heartbeat_and_absolute_cap(svc, monkeypatch):
    sess, _ = _create(svc)
    sess_b, _ = _create(svc)
    assert svc.heartbeat(sess.id)
    sess.last_heartbeat_mono -= settings.WEBRTC_IDLE_TIMEOUT_S + 1
    sess_b.started_mono -= settings.WEBRTC_MAX_SESSION_S + 1
    run(svc.reap_once())
    reasons = {o["session_id"]: o["end_reason"] for o in svc.outcomes()}
    assert reasons == {sess.id: "heartbeat_missed", sess_b.id: "max_session"}
    assert not svc.sessions


def test_phone_compat_session_needs_no_heartbeat(svc):
    sess, _ = _create(svc, heartbeat_required=False)
    sess.last_heartbeat_mono -= settings.WEBRTC_IDLE_TIMEOUT_S * 10
    run(svc.reap_once())
    assert sess.id in svc.sessions
    svc.fake.drop_consumer(sess.stream)
    run(svc.reap_once())
    assert sess.id not in svc.sessions and svc.outcomes()[-1]["end_reason"] == "connection_closed"
    assert sess.stream not in svc.fake.streams_


def test_consumer_gone_ends_the_session_and_deletes_its_stream(svc):
    sess, _ = _create(svc)
    other, _ = _create(svc, cam=CAM2)
    svc.fake.drop_consumer(sess.stream)
    run(svc.reap_once())
    assert sess.id not in svc.sessions and other.id in svc.sessions
    assert sess.stream not in svc.fake.streams_ and other.stream in svc.fake.streams_


def test_camera_off_deleted_or_credentials_changed_end_sessions(svc):
    a, _ = _create(svc)
    b, _ = _create(svc, cam=CAM2)
    run(_set_enabled(CAM, False))
    run(svc.reap_once())
    assert a.id not in svc.sessions and b.id in svc.sessions
    svc.test_sources[CAM2] = "rtsp://admin:NEWPASS@10.9.8.7:554/cam/realmonitor?channel=2&subtype=1"
    run(svc.reap_once())
    assert b.id not in svc.sessions
    c, _ = _create(svc, cam=CAM2)
    run(_delete_camera(CAM2))
    run(svc.reap_once())
    assert c.id not in svc.sessions
    reasons = [o["end_reason"] for o in svc.outcomes()]
    assert reasons == ["camera_off", "camera_source_changed", "camera_deleted"]


def test_a_viewer_that_ignores_the_end_is_cut_by_restarting_go2rtc(svc, monkeypatch):
    stuck, _ = _create(svc)
    other, _ = _create(svc, cam=CAM2)
    run(svc.close(stuck.id, reason="max_session"))
    run(svc.reap_once())
    assert svc.fake.stops == 0, "the page gets one heartbeat to close first"
    svc._ending[stuck.stream] -= svc.force_grace_s() + 1
    run(svc.reap_once())
    assert svc.fake.stops == 1 and not svc.fake.is_running
    assert other.id not in svc.sessions
    assert svc.outcomes()[-1]["end_reason"] == "gateway_restart"


def test_a_consumer_that_never_connected_does_not_restart_go2rtc(svc):
    """Closed while its offer was being answered: go2rtc drops it when ICE gives up. Restarting
    go2rtc for it cut every other viewer 25 s after each such close (integration run 2026-09-29)."""
    early, _ = _create(svc)
    other, _ = _create(svc, cam=CAM2)
    # go2rtc 1.9.14's view of it: no peer address, protocol "http", sender bytes growing anyway.
    svc.fake.streams_[early.stream]["consumers"] = [{"id": "c9", "format_name": "webrtc/json", "protocol": "http",
                                                     "senders": [{"id": 4, "bytes": 515006, "packets": 482}]}]
    run(svc.close(early.id))
    svc._ending[early.stream] -= svc.force_grace_s() + 1
    run(svc.reap_once())
    assert svc.fake.stops == 0 and other.id in svc.sessions
    svc.fake.drop_consumer(early.stream)             # ICE gave up
    run(svc.reap_once())
    assert early.stream not in svc._ending and early.stream not in svc.fake.streams_
    # ... but not forever: a bounded wait, then the restart.
    late, _ = _create(svc, cam=CAM2)
    svc.fake.streams_[late.stream]["consumers"] = [{"id": "c8", "format_name": "webrtc/json"}]
    run(svc.close(late.id))
    svc._ending[late.stream] -= UNCONNECTED_CONSUMER_MAX_S + 1
    run(svc.reap_once())
    assert svc.fake.stops == 1


def test_orphan_streams_are_deleted(svc):
    sess, _ = _create(svc)
    svc.fake.streams_["ws_leftover"] = {"producers": [{"url": "rtsp://x"}], "consumers": None}
    run(svc.reap_once())
    assert "ws_leftover" not in svc.fake.streams_ and sess.stream in svc.fake.streams_


def test_gateway_gone_ends_sessions(svc):
    sess, _ = _create(svc)
    svc.fake.is_running = False
    run(svc.reap_once())
    assert not svc.sessions and svc.outcomes()[-1]["end_reason"] == "gateway_stopped"


def test_relay_report_is_a_violation(svc, caplog):
    sess, _ = _create(svc)
    assert svc.report(sess.id, {"state": "connected", "pair": {"local": "host", "remote": "srflx",
                                                              "protocol": "udp"}, "frames_decoded": 30})
    assert sess.pair["remote"] == "srflx" and svc.violations == 0
    svc.report(sess.id, {"state": "connected", "pair": {"local": "relay", "remote": "srflx", "protocol": "udp"}})
    assert svc.violations == 1 and "violation" in svc.outcomes()[-1]
    assert "RELAY" in caplog.text


def test_closing_report_after_the_end_keeps_the_camera(svc):
    """The page sends report(closed) and DELETE at once; the report often arrives second."""
    sess, _ = _create(svc)
    run(svc.close(sess.id))
    assert svc.report(sess.id, {"state": "closed", "pair": {"local": "prflx", "remote": "host"}}) is False
    last = svc.outcomes()[-1]
    assert last["report"] == "closed" and last["camera_id"] == CAM and last["viewer_ip"] == "203.0.113.9"


def test_ring_buffer_keeps_last_50(svc):
    for i in range(60):
        svc.report(f"x{i}", {"state": "failed", "error": f"e{i}"})
    out = svc.outcomes()
    assert len(out) == 50 and out[0]["session_id"] == "x10"


def test_idle_stop_after_settings_change(svc):
    _create(svc)
    svc._restart_when_idle = True
    run(svc._apply_idle_restart())
    assert svc.fake.is_running, "not while a session is open"
    for s in list(svc.sessions.values()):
        run(svc.close(s.id))
    svc._ending.clear()
    run(svc._apply_idle_restart())
    assert not svc.fake.is_running


# --------------------------------------------------------------------------- #
# SDP / go2rtc parsing helpers
# --------------------------------------------------------------------------- #

def test_offer_and_producer_parsing():
    assert offer_video_codecs(OFFER_H264) == {"VP8", "H264"}
    assert "H265" in offer_video_codecs(OFFER_H265)
    audio_only = "v=0\r\nm=audio 9 UDP 111\r\na=rtpmap:111 opus/48000/2\r\n"
    assert offer_video_codecs(audio_only) == set()
    assert mismatch_source_codec("streams: codecs not matched: video:H265, audio:AAC => video:H264, video:VP8") \
        == "H265"
    assert producer_codec({"producers": [{"medias": ["audio, recvonly, PCMA", "video, recvonly, H264"]}]}) == "H264"
    assert producer_codec({"producers": [{"url": "rtsp://x"}]}) is None
    assert producer_encoder({"producers": [{"source": "exec:ffmpeg -i x -c:v h264_nvenc -g 50"}]}) == "h264_nvenc"


def test_go2rtc_config_by_mode_never_has_a_relay():
    auto = g2.build_config(mode="auto", webrtc_port=8555, stun_servers=["stun:stun.example.net:3478",
                                                                          "turn:evil.example:3478"],
                           ffmpeg_bin="/usr/bin/ffmpeg", api_listen_port=1984, rtsp_listen_port=18554)
    assert auto["webrtc"]["listen"] == "" and "candidates" not in auto["webrtc"]
    assert auto["webrtc"]["ice_servers"] == [{"urls": ["stun:stun.example.net:3478"]}]
    assert auto["api"]["listen"] == "127.0.0.1:1984" and auto["api"]["local_auth"] is True
    assert auto["api"]["password"] == "${EDGE_GO2RTC_API_PASSWORD}", "the password never goes into the file"
    assert set(auto["api"]["allow_paths"]) == {"/api", "/api/streams", "/api/webrtc"}
    assert auto["rtsp"]["listen"].startswith("127.0.0.1:")
    assert "rtmp" not in auto["app"]["modules"] and "srtp" not in auto["app"]["modules"]
    assert "hls" not in auto["app"]["modules"] and "streams" not in auto
    fixed = g2.build_config(mode="fixed_port", webrtc_port=8555, stun_servers=["stun:stun.example.net:3478"],
                            ffmpeg_bin=None)
    assert fixed["webrtc"]["listen"] == ":8555" and fixed["webrtc"]["candidates"] == ["stun:8555"]
    assert "turn" not in str(auto).lower().replace("return", "")


def test_config_file_is_private(tmp_path):
    path = g2.write_config(g2.build_config(mode="auto", webrtc_port=8555, stun_servers=[], ffmpeg_bin=None),
                           tmp_path / "go2rtc" / "go2rtc.yaml")
    assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700


def test_transcode_source_is_hardware_adaptive(monkeypatch):
    url = "rtsp://u:p@10.0.0.2/x"
    assert g2.transcode_source(url, "auto") == f"ffmpeg:{url}#video=h264#hardware"
    assert g2.transcode_source(url, "nvenc") == f"ffmpeg:{url}#video=h264#hardware=cuda"
    assert g2.transcode_source(url, "qsv") == f"ffmpeg:{url}#video=h264#hardware=vaapi"
    assert g2.transcode_source(url, "libx264") == f"ffmpeg:{url}#video=h264"


@pytest.mark.skipif(not hasattr(__import__("os"), "killpg"), reason="POSIX process groups")
def test_leftover_child_that_ignores_sigterm_is_killed():
    """A GPU ffmpeg outlived its go2rtc after SIGTERM (integration run 2026-09-29)."""
    import os
    import subprocess

    proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 30 & wait"], start_new_session=True)
    try:
        assert g2.reap_group(proc.pid, grace=0.3) is True
        proc.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
        assert g2.reap_group(proc.pid) is False     # nothing left: a no-op
    finally:
        if proc.poll() is None:
            proc.kill()


def test_go2rtc_log_lines_are_redacted():
    line = g2.redact("WRN [streams] error=\"dial\" url=rtsp://admin:hunter2@10.0.0.5:554/x")
    assert "hunter2" not in line and "10.0.0.5" in line


def test_missing_binary_is_reported_honestly(monkeypatch):
    from app.services.resilience import ServiceHealthTracker

    m = g2.Go2RtcManager()
    m.binary_finder = lambda: None
    with pytest.raises(g2.Go2RtcUnavailable) as e:
        run(m.ensure_running())
    assert "not installed" in str(e.value)
    entry = ServiceHealthTracker().services["go2rtc"]
    assert entry["status"] == "NOT_PRESENT" and "not installed" in entry["last_error"]
    ServiceHealthTracker().reset()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def test_stun_settings_refuse_relays():
    assert normalise_stun_server("stun.example.net") == "stun:stun.example.net:3478"
    assert normalise_stun_server("stun:Stun.Example.net:19302") == "stun:stun.example.net:19302"
    for bad in ("turn:relay.example:3478", "turns:relay.example:5349", "stun:user@x:1", "stun:x:99999", "stun:"):
        with pytest.raises(RemoteAccessError):
            normalise_stun_server(bad)
    assert normalise_stun_servers("stun:a.example:3478, b.example") == ["stun:a.example:3478",
                                                                        "stun:b.example:3478"]
    with pytest.raises(RemoteAccessError):
        normalise_stun_servers([f"s{i}.example" for i in range(5)])


def test_settings_defaults_and_update(monkeypatch):
    saved = {}
    monkeypatch.setattr("app.services.remote_access_service.save_settings", lambda d: saved.update(d))
    monkeypatch.setattr(remote_access_service, "apply", lambda restart=False: None)
    monkeypatch.setattr(remote_access_service, "_settings", dict(remote_access_service._settings))
    s = remote_access_service.webrtc_settings()
    assert set(s) == {"stun_servers", "webrtc_mode", "webrtc_port", "max_video_sessions", "live_transport_on_lan"}
    status = remote_access_service.update(stun_servers=["stun:stun.ikorex.com.au:3478", "stun.l.google.com:19302"],
                                          webrtc_mode="fixed_port", webrtc_port=8555, max_video_sessions=4,
                                          live_transport_on_lan="webrtc")
    assert status["webrtc_mode"] == "fixed_port" and status["max_video_sessions"] == 4
    assert saved["stun_servers"] == ["stun:stun.ikorex.com.au:3478", "stun:stun.l.google.com:19302"]
    with pytest.raises(RemoteAccessError):
        remote_access_service.update(stun_servers=["turn:x.example:3478"])
    with pytest.raises(RemoteAccessError):
        remote_access_service.update(webrtc_port=80)


# --------------------------------------------------------------------------- #
# STUN NAT probe (no network: parsing and classification)
# --------------------------------------------------------------------------- #

def test_stun_binding_response_parsing():
    import os
    import struct

    txid = os.urandom(12)
    port, ip = 40123, (203 << 24) | (0 << 16) | (113 << 8) | 7
    xport = port ^ (stun_probe.MAGIC_COOKIE >> 16)
    xip = ip ^ stun_probe.MAGIC_COOKIE
    attr = struct.pack("!HHBBHI", 0x0020, 8, 0, 1, xport, xip)
    msg = struct.pack("!HHI", 0x0101, len(attr), stun_probe.MAGIC_COOKIE) + txid + attr
    assert stun_probe.parse_binding_response(msg, txid) == ("203.0.113.7", 40123)
    assert stun_probe.parse_binding_response(msg, os.urandom(12)) is None
    assert stun_probe.build_binding_request(txid)[:2] == b"\x00\x01"


def test_nat_classification():
    same = [{"server_ip": "1.1.1.1", "mapped": "5.5.5.5:40000"}, {"server_ip": "2.2.2.2", "mapped": "5.5.5.5:40000"}]
    diff = [{"server_ip": "1.1.1.1", "mapped": "5.5.5.5:40000"}, {"server_ip": "2.2.2.2", "mapped": "5.5.5.5:40007"}]
    one = [{"server_ip": "1.1.1.1", "mapped": "5.5.5.5:40000"}, {"server_ip": "2.2.2.2", "mapped": None}]
    assert stun_probe.classify(same) == "endpoint_independent"
    assert stun_probe.classify(diff) == "endpoint_dependent"
    assert stun_probe.classify(one) == "unknown"
    assert stun_probe.parse_stun_url("stun:stun.example.net:19302") == ("stun.example.net", 19302)


def test_diagnostics_advice(svc, monkeypatch):
    fake_nat = {"local_port": 40000, "servers": [
        {"server": "stun:stun.example.net:3478", "server_ip": "1.1.1.1", "mapped": "5.5.5.5:40000",
         "configured": True},
        {"server": stun_probe.FALLBACK_SECOND_SERVER, "server_ip": "2.2.2.2", "mapped": "5.5.5.5:40000",
         "configured": False}], "mapping": "endpoint_independent", "public_address": "5.5.5.5",
        "port_preserved": True, "fallback_server": stun_probe.FALLBACK_SECOND_SERVER}
    called = []
    monkeypatch.setattr(stun_probe, "probe", lambda servers: called.append(servers) or fake_nat)
    d = run(svc.diagnostics())
    assert called == [["stun:stun.example.net:3478"]]
    assert d["nat_mapping"] == "endpoint_independent" and d["public_address"] == "5.5.5.5"
    assert any("without any router change" in a for a in d["advice"])
    monkeypatch.setitem(remote_access_service._settings, "webrtc_mode", "fixed_port")
    d = run(svc.diagnostics())
    assert any("ufw allow 8555/udp" in a for a in d["advice"])
    d = run(svc.diagnostics(probe_nat=False))
    assert d["nat"] is None and len(called) == 2


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #

def _headers(role="owner", **extra):
    tok = auth_service.create_access_token({"sub": "u1", "role": role, "type": "user_session", **extra})
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def http(svc, monkeypatch):
    import app.routes.webrtc as route

    monkeypatch.setattr(route, "webrtc_sessions", svc)
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    intrusion_detector.failed_attempts.clear()
    general_rate_limiter.history.clear()
    route.webrtc_open_limiter.history.clear()
    return TestClient(app, client=("192.168.1.40", 40000), raise_server_exceptions=False)


def test_session_lifecycle_calls_are_not_rate_limited(http):
    """A rotating grid alone sent > 100 webrtc calls/min per viewer (integration run 2026-09-29):
    heartbeat/report/DELETE must never be refused (a refused DELETE leaks the session), and they
    must not eat the dashboard's general per-address budget."""
    h = _headers()
    for _ in range(general_rate_limiter.requests + 20):
        assert http.post("/api/v1/webrtc/sessions/nope/heartbeat", headers=h).status_code == 404
        assert http.delete("/api/v1/webrtc/sessions/nope", headers=h).status_code == 204
        assert http.post("/api/v1/webrtc/sessions/nope/report", headers=h, json={"state": "closed"}).status_code == 200
    assert http.get("/api/v1/webrtc/config", headers=h).status_code == 200   # general budget untouched


def test_config_contract_and_lan_transport(http, monkeypatch):
    body = http.get("/api/v1/webrtc/config", headers=_headers()).json()
    for key in ("enabled", "available", "reason", "remote", "transport", "ice_servers", "ice_transport_policy",
                "relay", "heartbeat_s", "idle_timeout_s", "max_session_s", "max_sessions", "active_sessions", "mode"):
        assert key in body, key
    assert body["remote"] is False and body["transport"] == "local" and body["relay"] is False
    assert body["ice_servers"] == [{"urls": ["stun:stun.example.net:3478"]}]
    assert body["ice_transport_policy"] == "all" and body["heartbeat_s"] == settings.WEBRTC_HEARTBEAT_S
    assert http.get("/api/v1/webrtc/config", headers={**_headers(), "X-Live-Transport": "webrtc"}
                    ).json()["transport"] == "webrtc"
    monkeypatch.setitem(remote_access_service._settings, "live_transport_on_lan", "webrtc")
    assert http.get("/api/v1/webrtc/config", headers=_headers()).json()["transport"] == "webrtc"
    assert http.get("/api/v1/webrtc/config").status_code == 401


def test_session_http_flow_viewer_may_watch(http, svc):
    viewer = _headers(role="viewer")
    r = http.post("/api/v1/webrtc/sessions", json={"camera_id": CAM, "sdp": OFFER_H264, "purpose": "focus"},
                  headers=viewer)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"session_id", "sdp", "heartbeat_s", "expires_at", "codec", "transcoded"}
    sid = body["session_id"]
    assert http.post(f"/api/v1/webrtc/sessions/{sid}/heartbeat", headers=viewer).json()["expires_at"]
    rep = http.post(f"/api/v1/webrtc/sessions/{sid}/report", headers=viewer,
                    json={"state": "connected", "pair": {"local": "host", "remote": "srflx", "protocol": "udp"},
                          "bytes_received": 1000, "frames_decoded": 25})
    assert rep.status_code == 200
    # Admin-only views.
    assert http.get("/api/v1/webrtc/sessions", headers=viewer).status_code == 403
    assert http.get("/api/v1/webrtc/diagnostics?nat=false", headers=viewer).status_code == 403
    assert http.get("/api/v1/webrtc/sessions", headers=_headers(pd="phone1")).status_code in (401, 403)
    listed = http.get("/api/v1/webrtc/sessions", headers=_headers()).json()["sessions"]
    assert listed[0]["camera_id"] == CAM and listed[0]["pair"]["remote"] == "srflx"
    assert {"viewer_ip", "remote", "started_at", "last_heartbeat", "bytes_sent", "purpose"} <= set(listed[0])
    assert http.delete(f"/api/v1/webrtc/sessions/{sid}", headers=viewer).status_code == 204
    assert http.delete(f"/api/v1/webrtc/sessions/{sid}", headers=viewer).status_code == 204
    assert http.post(f"/api/v1/webrtc/sessions/{sid}/heartbeat", headers=viewer).status_code == 404


def test_session_http_errors(http, svc, monkeypatch):
    h = _headers()
    assert http.post("/api/v1/webrtc/sessions", json={"camera_id": "nope", "sdp": OFFER_H264},
                     headers=h).status_code == 404
    run(_set_enabled(CAM2, False))
    r = http.post("/api/v1/webrtc/sessions", json={"camera_id": CAM2, "sdp": OFFER_H264}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "camera_off"
    monkeypatch.setitem(remote_access_service._settings, "max_video_sessions", 1)
    assert http.post("/api/v1/webrtc/sessions", json={"camera_id": CAM, "sdp": OFFER_H264},
                     headers=h).status_code == 201
    r = http.post("/api/v1/webrtc/sessions", json={"camera_id": CAM, "sdp": OFFER_H264}, headers=h)
    assert r.status_code == 429 and r.json() == {**r.json(), "code": "video_session_limit", "max_sessions": 1}
    stream_tok = auth_service.generate_stream_token(CAM2)
    r = http.post(f"/api/v1/webrtc/sessions?token={stream_tok}", json={"camera_id": CAM, "sdp": OFFER_H264})
    assert r.status_code == 403, "a stream token only opens its own camera"
