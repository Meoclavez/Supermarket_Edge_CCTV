"""Wrong camera credentials and non-camera devices, against real local sockets.

Bug 1: a camera answering RTSP ``401`` was indistinguishable from an
unreachable one (FFmpeg prints the 401 to stderr only), so the worker retried
every 30 s forever and kept Dahua accounts locked. The worker now does an RTSP
handshake first, reports AUTH_FAILED and backs off hard.

Bug 2: discovery offered a Brother printer as a camera. Only devices that
answer RTSP (or ONVIF NVT WS-Discovery) are offered now, and adoption checks
the RTSP handshake before creating a camera.

The "camera" here is a small threaded RTSP server that implements Digest auth
the way Dahua firmware does (realm "Login to <serial>").
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import sqlite3
import threading
import time

import pytest

from app.services import live_analytics_engine as lae
from app.services import rtsp_probe
from app.services.camera_discovery import (
    classify_mdns_records,
    is_printer_mdns,
    parse_ws_discovery_reply,
    rtsp_options,
)
from app.services.camera_drivers import driver_for_hint

REALM = "Login to 3f2a9c0e71d4b8a6"
USER, GOOD_PW = "admin", "Corr3ct:p@ss"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeRtspCamera:
    """Answers OPTIONS/DESCRIBE with Dahua-style Digest auth; counts failed logins."""

    def __init__(self, password: str = GOOD_PW, http: bool = False,
                 per_connection_nonce: bool = False, close_after_challenge: bool = False):
        self.password = password
        self.http = http
        # Real Dahua firmware ties the nonce to the TCP connection, so a
        # client must answer the challenge on the connection that received it.
        self.per_connection_nonce = per_connection_nonce
        self.close_after_challenge = close_after_challenge
        self.failed_logins = 0
        self.good_logins = 0
        self.requests = 0
        self.nonce = "a1b2c3d4e5f6"
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(3)
        buf = b""
        nonce = os.urandom(8).hex() if self.per_connection_nonce else self.nonce
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                head, buf = buf.split(b"\r\n\r\n", 1)
                self.requests += 1
                reply = self._reply(head.decode(), nonce)
                conn.sendall(reply)
                if self.close_after_challenge and reply.startswith(b"RTSP/1.0 401"):
                    return
        except OSError:
            pass
        finally:
            conn.close()

    def _reply(self, head: str, nonce: str) -> bytes:
        if self.http:  # a printer's web server
            return b"HTTP/1.1 400 Bad Request\r\nServer: debut/1.30\r\nContent-Length: 0\r\n\r\n"
        lines = head.split("\r\n")
        method, uri, _ = lines[0].split(" ", 2)
        cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "0")
        auth = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("authorization:")), None)
        challenge = (f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n"
                     f'WWW-Authenticate: Digest realm="{REALM}", nonce="{nonce}"\r\n'
                     "Content-Length: 12\r\n\r\nUnauthorized").encode()
        if auth is None:
            return challenge
        p = dict(re.findall(r'(\w+)="([^"]*)"', auth))
        expected = _md5(f"{_md5(f'{p.get('username')}:{REALM}:{self.password}')}:{nonce}:{_md5(f'{method}:{p.get('uri')}')}")
        if p.get("username") == USER and p.get("response") == expected and p.get("uri") == uri:
            self.good_logins += 1
            return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nServer: Rtsp Server/3.0\r\nContent-Length: 0\r\n\r\n".encode()
        self.failed_logins += 1
        return challenge

    def url(self, user=USER, pw=GOOD_PW, path="/cam/realmonitor?channel=1&subtype=1"):
        from urllib.parse import quote

        auth = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
        return f"rtsp://{auth}127.0.0.1:{self.port}{path}"

    def close(self):
        self._stop.set()
        self.srv.close()


@pytest.fixture
def camera():
    cam = FakeRtspCamera()
    yield cam
    cam.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------- RTSP handshake

def test_probe_accepts_the_right_password(camera):
    res = rtsp_probe.probe_rtsp(camera.url(), 3)
    assert res.outcome == rtsp_probe.OK and res.status_code == 200
    assert res.realm == REALM
    assert camera.good_logins == 1 and camera.failed_logins == 0


def test_probe_reports_a_rejected_password_with_one_failed_login(camera):
    res = rtsp_probe.probe_rtsp(camera.url(pw="wrong"), 3)
    assert res.outcome == rtsp_probe.AUTH_FAILED and res.auth_failed
    assert camera.failed_logins == 1            # exactly one login attempt per probe
    assert "wrong" not in repr(res.to_dict())   # never echoes the credentials


def test_probe_without_credentials_is_auth_required(camera):
    res = rtsp_probe.probe_rtsp(camera.url(user=None), 3)
    assert res.outcome == rtsp_probe.AUTH_REQUIRED and camera.failed_logins == 0


def test_probe_distinguishes_unreachable_and_non_rtsp():
    assert rtsp_probe.probe_rtsp(f"rtsp://127.0.0.1:{_closed_port()}/x", 2).outcome == rtsp_probe.UNREACHABLE
    printer = FakeRtspCamera(http=True)
    try:
        res = rtsp_probe.probe_rtsp(f"rtsp://127.0.0.1:{printer.port}/onvif1", 2)
        assert res.outcome == rtsp_probe.NOT_RTSP and not res.speaks_rtsp
    finally:
        printer.close()


def test_digest_with_qop_matches_rfc_2617_example():
    # RFC 2617 section 3.5 example, with the RTSP method substituted by GET.
    params = {"realm": "testrealm@host.com", "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
              "qop": "auth,auth-int", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
    header = rtsp_probe.digest_authorization("GET", "/dir/index.html", "Mufasa", "Circle Of Life", params,
                                             cnonce="0a4f113b", nc="00000001")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header


# ------------------------------------------------------------ camera worker

def _drive(worker, max_sleeps):
    """Run the worker loop synchronously, recording each retry wait."""
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        return len(waits) >= max_sleeps

    worker._sleep = fake_sleep
    worker.run()
    return waits


def test_probe_answers_on_the_same_connection_for_per_connection_nonces():
    """Dahua ties the nonce to the connection: the right password must pass."""
    cam = FakeRtspCamera(per_connection_nonce=True)
    try:
        res = rtsp_probe.probe_rtsp(cam.url(), 3)
        assert res.outcome == rtsp_probe.OK, res
        assert cam.good_logins == 1 and cam.failed_logins == 0
        assert rtsp_probe.probe_rtsp(cam.url(pw="wrong"), 3).outcome == rtsp_probe.AUTH_FAILED
    finally:
        cam.close()


def test_probe_re_challenges_when_the_server_closes_after_401():
    cam = FakeRtspCamera(close_after_challenge=True)
    try:
        res = rtsp_probe.probe_rtsp(cam.url(), 3)
        assert res.outcome == rtsp_probe.OK, res
        assert cam.good_logins == 1 and cam.failed_logins == 0
    finally:
        cam.close()


def test_stream_that_opens_overrides_a_probe_auth_failure(camera, monkeypatch):
    """If FFmpeg opens the stream with the same credentials, the camera streams."""
    w = _worker(camera.url(pw="wrong"))

    class _Cap:
        reads = 0

        def isOpened(self):
            return True

        def read(self):
            _Cap.reads += 1
            w._stop.set()
            return False, None

        def set(self, *a):
            pass

        def release(self):
            pass

    monkeypatch.setattr(w, "_open", lambda *a, **k: _Cap())
    w._sleep = lambda s: True
    w.run()
    assert _Cap.reads == 1                       # it streamed instead of pausing
    assert w.rt.status != "AUTH_FAILED"


def _worker(url, cam_id="cam_auth_unit"):
    return lae.CameraWorker(lae.CameraRuntime(camera_id=cam_id, name="t", source=url), lae.LiveAnalyticsEngine())


def test_wrong_password_sets_auth_failed_and_stops_rapid_retries(camera, monkeypatch):
    opened = []
    w = _worker(camera.url(pw="wrong"))
    monkeypatch.setattr(w, "_open", lambda *a, **k: opened.append(a))
    states = []
    real_sleep_calls = []

    def fake_sleep(seconds):
        real_sleep_calls.append(seconds)
        states.append((w.rt.status, w.rt.last_error, w.rt.to_dict()["next_retry_in_s"]))
        return len(real_sleep_calls) >= 4

    w._sleep = fake_sleep
    w.run()

    # One quick retry, then the long pause; never the 1..30 s network backoff.
    assert real_sleep_calls == [lae.AUTH_QUICK_RETRY_SEC, lae.AUTH_RETRY_SEC, lae.AUTH_RETRY_SEC, lae.AUTH_RETRY_SEC]
    assert lae.AUTH_RETRY_SEC >= 1800
    status, err, retry_in = states[-1]
    assert status == "AUTH_FAILED"
    assert "rejected the username/password" in err and "wrong" not in err
    assert retry_in >= 1790
    # FFmpeg gets one confirming try per attempt (so a probe quirk can never
    # block a working camera); with the probe that is at most two failed
    # logins per attempt, spread over > 1.5 h (well under a 5-try lockout).
    assert len(opened) == 4
    assert camera.failed_logins == 4     # the probe's; the stubbed _open never reaches the camera


def test_unreachable_camera_uses_capped_exponential_backoff(monkeypatch):
    w = _worker(f"rtsp://user:pw@127.0.0.1:{_closed_port()}/x")
    monkeypatch.setattr(w, "_open", lambda *a, **k: pytest.fail("OpenCV must not be tried when nothing listens"))
    waits = _drive(w, 8)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert w.rt.last_error and "pw" not in w.rt.last_error


def test_auth_failed_worker_resumes_on_reconnect(camera, monkeypatch):
    """Paused for 30 min after a rejected login; Reconnect retries at once."""
    monkeypatch.setattr(lae, "AUTH_QUICK_RETRIES", 0)       # go straight to the long pause
    monkeypatch.setattr(lae.settings, "CAMERA_OPEN_TIMEOUT_SEC", 2.0)
    engine = lae.LiveAnalyticsEngine()
    opened = threading.Event()

    class _Cap:
        def isOpened(self):
            opened.set()
            return False

        def release(self):
            pass

    monkeypatch.setattr(lae.CameraWorker, "_open", lambda self, src=None: _Cap())
    engine.start_camera("cam_auth_resume", "t", camera.url())
    camera.password = "changed-on-the-camera"     # the operator's saved password is now wrong
    try:
        # First attempt may already have passed; force the wrong-password path.
        engine.reconnect_camera("cam_auth_resume")
        deadline = time.monotonic() + 10
        while engine.runtimes["cam_auth_resume"].status != "AUTH_FAILED" and time.monotonic() < deadline:
            time.sleep(0.05)
        rt = engine.runtimes["cam_auth_resume"]
        assert rt.status == "AUTH_FAILED"
        fails = camera.failed_logins
        time.sleep(1.5)
        assert camera.failed_logins == fails          # paused: no further login attempts

        # The camera's password is set back (or the operator fixes it) and presses Reconnect.
        camera.password = GOOD_PW
        opened.clear()
        assert engine.reconnect_camera("cam_auth_resume") is True
        assert opened.wait(5), "worker did not retry after Reconnect"
        assert camera.good_logins >= 1
    finally:
        engine.stop_all(3)


def test_new_credentials_restart_the_worker_immediately(camera, monkeypatch):
    """The supervisor starts a new worker when the injected source changes."""
    monkeypatch.setattr(lae.settings, "CAMERA_OPEN_TIMEOUT_SEC", 2.0)
    engine = lae.LiveAnalyticsEngine()
    opened = threading.Event()

    class _Cap:
        def isOpened(self):
            opened.set()
            return False

        def release(self):
            pass

    monkeypatch.setattr(lae.CameraWorker, "_open", lambda self, src=None: _Cap())
    try:
        engine.start_camera("cam_auth_new", "t", camera.url(pw="wrong"))
        deadline = time.monotonic() + 10
        while engine.runtimes["cam_auth_new"].status != "AUTH_FAILED" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine.runtimes["cam_auth_new"].status == "AUTH_FAILED"
        opened.clear()                  # FFmpeg's one confirming try on the wrong password
        engine.start_camera("cam_auth_new", "t", camera.url())      # what reconcile does on a source change
        assert opened.wait(5)
        assert camera.good_logins == 1
    finally:
        engine.stop_all(3)


# ------------------------------------------------------------------- health

def test_health_maps_auth_failed_to_failed_with_reason():
    from app.routes.health import _camera_services

    pipeline = {"cameras": [{"camera_id": "c1", "status": "AUTH_FAILED",
                             "last_error": "camera rejected the username/password (RTSP 401)",
                             "seconds_since_frame": None}]}
    entry = _camera_services([("c1", "Door")], pipeline)["rtsp_cam_c1"]
    assert entry["status"] == "FAILED"
    assert "rejected the username/password" in str(entry)


# ---------------------------------------------------------------- discovery

def test_rtsp_options_rejects_a_web_server_and_reads_the_dahua_realm(camera):
    printer = FakeRtspCamera(http=True)
    try:
        assert asyncio.run(rtsp_options("127.0.0.1", printer.port)) is None
    finally:
        printer.close()
    banner = asyncio.run(rtsp_options("127.0.0.1", camera.port))
    assert banner is not None and driver_for_hint(banner) == "dahua"


BROTHER = [
    {"host": "192.168.20.109", "type": "_http._tcp.local.", "name": "Brother MFC-L2750DW series._http._tcp.local.",
     "server": "BRWCC5EF87A8733.local.", "port": 80},
    {"host": "192.168.20.109", "type": "_ipp._tcp.local.", "name": "Brother MFC-L2750DW series._ipp._tcp.local.",
     "server": "BRWCC5EF87A8733.local.", "port": 631},
    {"host": "192.168.20.109", "type": "_pdl-datastream._tcp.local.", "name": "Brother._pdl-datastream._tcp.local.",
     "server": "BRWCC5EF87A8733.local.", "port": 9100},
]


def test_printer_mdns_record_is_never_offered_nor_probed():
    probed = []

    async def rtsp_check(host, port):
        probed.append((host, port))
        return "RTSP"

    assert asyncio.run(classify_mdns_records(BROTHER, rtsp_check)) == []
    # Even the bare _http._tcp advert (what was adopted before) is recognised by its hostname.
    assert asyncio.run(classify_mdns_records(BROTHER[:1], rtsp_check)) == []
    assert probed == []
    assert is_printer_mdns(["_http._tcp.local."], ["BRNB42200A1B2C3.local."])


def test_mdns_http_device_that_does_not_speak_rtsp_is_not_offered():
    async def rtsp_check(host, port):
        return None

    rec = [{"host": "192.168.20.50", "type": "_http._tcp.local.", "name": "NAS._http._tcp.local.",
            "server": "nas.local.", "port": 80}]
    assert asyncio.run(classify_mdns_records(rec, rtsp_check)) == []


def test_mdns_device_answering_rtsp_is_offered_on_the_rtsp_port():
    async def rtsp_check(host, port):
        return "Rtsp Server/3.0 (Dahua)" if port == 554 else None

    rec = [{"host": "192.168.20.42", "type": "_http._tcp.local.", "name": "IPC._http._tcp.local.",
            "server": "ipc-42.local.", "port": 80}]
    devs = asyncio.run(classify_mdns_records(rec, rtsp_check))
    assert len(devs) == 1
    d = devs[0]
    assert d.port == 554 and d.driver == "dahua"
    assert all(":554/" in s.url for s in d.streams) and not any(":80/" in s.url for s in d.streams)


NVT_MATCH = """<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<s:Body><d:ProbeMatches><d:ProbeMatch><d:Types>dn:NetworkVideoTransmitter</d:Types>
<d:Scopes>onvif://www.onvif.org/name/Dahua onvif://www.onvif.org/hardware/IPC-HDW2431T</d:Scopes>
<d:XAddrs>http://192.168.20.42/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""

PRINTER_MATCH = NVT_MATCH.replace("dn:NetworkVideoTransmitter", "wprt:PrintDeviceType wsdp:Device").replace(
    "name/Dahua", "name/Brother")


def test_ws_discovery_offers_only_network_video_transmitters():
    dev = parse_ws_discovery_reply("192.168.20.42", NVT_MATCH)
    assert dev is not None and dev.driver == "dahua" and dev.model_name == "IPC-HDW2431T"
    assert parse_ws_discovery_reply("192.168.20.109", PRINTER_MATCH) is None
    # Our own Probe echoed back by multicast loopback is not a device.
    from app.services.camera_discovery import _WS_PROBE

    assert parse_ws_discovery_reply("192.168.20.5", _WS_PROBE.format(msg_id="x")) is None


# ------------------------------------------------------------ API: adoption / test

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _insert_device(dev_id, host, port, driver="onvif"):
    from app.config import settings

    with sqlite3.connect(settings.DATABASE_PATH) as db:
        db.execute("DELETE FROM discovered_devices WHERE id=?", (dev_id,))
        db.execute("INSERT INTO discovered_devices (id, transport, driver, host, port, stream_urls, channels, "
                   "requires_credentials, reachable, first_seen, last_seen) VALUES "
                   "(?, 'rtsp', ?, ?, ?, '[]', 1, 1, 1, '2026-09-01 10:00:00', '2026-09-01 10:00:00')",
                   (dev_id, driver, host, port))


def _camera_count():
    from app.config import settings

    with sqlite3.connect(settings.DATABASE_PATH) as db:
        return db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]


def test_adopting_a_printer_is_refused_and_creates_no_camera(client):
    printer = FakeRtspCamera(http=True)
    dev_id = f"mdns:127.0.0.1:{printer.port}"
    try:
        _insert_device(dev_id, "127.0.0.1", printer.port)
        before = _camera_count()
        res = client.post("/api/v1/layout/devices/adopt", json={"device_id": dev_id, "name": "Printer"})
        assert res.status_code == 422, res.text
        assert "does not speak RTSP" in res.json()["detail"]
        assert _camera_count() == before
    finally:
        printer.close()


def test_adopting_with_a_wrong_password_is_refused(client, camera):
    dev_id = f"rtsp:127.0.0.1:{camera.port}"
    _insert_device(dev_id, "127.0.0.1", camera.port, driver="dahua")
    before = _camera_count()
    res = client.post("/api/v1/layout/devices/adopt", json={
        "device_id": dev_id, "name": "Door", "username": USER, "password": "wrong", "quality": "sub"})
    assert res.status_code == 422, res.text
    assert "Wrong username or password" in res.json()["detail"]
    assert "wrong" not in res.json()["detail"].replace("Wrong", "")
    assert _camera_count() == before and camera.failed_logins == 1

    res = client.post("/api/v1/layout/devices/adopt", json={
        "device_id": dev_id, "name": "Door", "username": USER, "password": GOOD_PW, "quality": "sub"})
    assert res.status_code == 201, res.text
    assert res.json()["stream_check"] == rtsp_probe.OK
    client.delete(f"/api/v1/layout/cameras/{res.json()['camera_id']}")


def test_connection_test_reports_wrong_password_distinctly(client, camera):
    r = client.post("/api/v1/cameras/test-connection", json={
        "source_type": "rtsp", "url": camera.url(user=None), "username": USER, "password": "wrong", "timeout_s": 4})
    data = r.json()
    assert data["success"] is False and data["error_code"] == "AUTH_FAILED"
    assert "Wrong username or password" in data["error"]
    assert camera.failed_logins == 1          # no second attempt through FFmpeg

    r = client.post("/api/v1/cameras/test-connection", json={
        "source_type": "rtsp", "url": f"rtsp://127.0.0.1:{_closed_port()}/x", "timeout_s": 3})
    assert r.json()["error_code"] == "UNREACHABLE"


def test_reconnect_and_credentials_endpoints(client):
    res = client.post("/api/v1/cameras", json={
        "id": "cam_auth_api", "name": "Auth API", "location": "", "source_type": "rtsp",
        "rtsp_url": f"rtsp://127.0.0.1:{_closed_port()}/x", "username": "a", "password": "b"})
    assert res.status_code == 200, res.text
    try:
        r = client.post("/api/v1/cameras/cam_auth_api/reconnect")
        assert r.status_code == 200 and r.json()["camera_id"] == "cam_auth_api"
        r = client.put("/api/v1/cameras/cam_auth_api/credentials", json={"username": "admin", "password": "N3w!"})
        assert r.status_code == 200 and "N3w!" not in r.text
        from app.services import camera_source

        assert camera_source.load_credentials("cam_auth_api") == ("admin", "N3w!")
        assert client.post("/api/v1/cameras/nope_missing/reconnect").status_code == 404
    finally:
        client.delete("/api/v1/layout/cameras/cam_auth_api")
