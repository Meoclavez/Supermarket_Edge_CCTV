"""Adding a camera by address: validation, the real connection test, and credential masking.

The connection test must open the stream for real and return the frame it
got; these tests feed it an OpenCV-written video file and, when ffmpeg is
installed, a live MJPEG stream served by ffmpeg over HTTP.
"""

from __future__ import annotations

import base64
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import camera_source
from app.services.auth_service import auth_service
from app.services.camera_source import SourceError, normalize_source

SECRET = "S3cr3t!pw@x"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "test_admin", "role": "admin", "type": "user_session"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def video_file(tmp_path_factory) -> Path:
    """A 64x48 (4:3) MJPG AVI that OpenCV can read without ffmpeg on PATH."""
    path = tmp_path_factory.mktemp("vid") / "test.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (64, 48))
    assert w.isOpened()
    for i in range(20):
        frame = np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8)
        cv2.rectangle(frame, (8, 8), (40, 30), (0, 255, 0), -1)
        w.write(frame)
    w.release()
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _decode_preview(data_url: str) -> np.ndarray:
    assert data_url.startswith("data:image/jpeg;base64,")
    img = cv2.imdecode(np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


# ------------------------------------------------------------------ validation

def test_rtsp_credentials_are_lifted_out_of_the_url():
    src = normalize_source("rtsp", f"rtsp://admin:{SECRET.replace('@', '%40')}@192.168.1.64:554/stream1")
    assert src.url == "rtsp://192.168.1.64:554/stream1"
    assert src.username == "admin" and src.password == SECRET
    # Explicit fields win over embedded ones.
    src = normalize_source("rtsp", "rtsp://a:b@cam.local/live", username="op", password="pw")
    assert (src.username, src.password, src.url) == ("op", "pw", "rtsp://cam.local/live")


@pytest.mark.parametrize("stype,url,fragment", [
    ("rtsp", "http://192.168.1.2/x", "must start with rtsp://"),
    ("rtsp", "rtsp:///nohost", "no valid host"),
    ("rtsp", "rtsp://192.168.1.2:99999/x", "malformed"),
    ("http", "ftp://x/y", "must start with http://"),
    ("rtsp", "", "Enter the stream URL"),
    ("rtsp", "rtsp://host/pa th", "spaces"),
    ("usb", "abc", "device index"),
    ("usb", "99", "between 0 and 63"),
    ("file", "relative/video.mp4", "absolute"),
    ("file", "/definitely/not/here.mp4", "does not exist"),
    ("smoke", "rtsp://h/x", "Unknown source type"),
])
def test_invalid_sources_are_rejected(stype, url, fragment):
    with pytest.raises(SourceError) as exc:
        normalize_source(stype, url)
    assert fragment.lower() in str(exc.value).lower()


def test_type_inference_and_usb_index():
    assert normalize_source(None, "rtsp://h/x").source_type == "rtsp"
    assert normalize_source(None, "https://h/v.mjpg").source_type == "http"
    assert normalize_source("mjpeg", "http://h/v.mjpg").source_type == "http"
    onvif = normalize_source("onvif", "192.168.1.64:8080")
    assert (onvif.onvif_host, onvif.onvif_port) == ("192.168.1.64", 8080)
    if sys.platform.startswith("linux"):
        assert normalize_source("usb", "2", check_local=False).url == "/dev/video2"


def test_create_rejects_bad_input_inline(client):
    r = client.post("/api/v1/cameras", json={"name": "X", "source_type": "rtsp", "rtsp_url": "http://nope/x"})
    assert r.status_code == 422 and "rtsp://" in r.json()["detail"]
    r = client.post("/api/v1/cameras", json={"name": "  ", "source_type": "rtsp", "rtsp_url": "rtsp://h/x"})
    assert r.status_code == 422 and "name" in r.json()["detail"].lower()
    r = client.post("/api/v1/cameras/test-connection", json={"source_type": "file", "url": "/no/such.mp4"})
    assert r.status_code == 422


# ------------------------------------------------------------ connection test

def test_connection_test_reads_a_real_frame_from_a_file(client, video_file):
    r = client.post("/api/v1/cameras/test-connection", json={"source_type": "file", "url": str(video_file)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True, data
    assert (data["width"], data["height"]) == (64, 48)
    assert data["fps"] == pytest.approx(10.0, abs=0.5)  # container rate for a file
    img = _decode_preview(data["preview_jpeg"])
    assert img.shape[1] / img.shape[0] == pytest.approx(64 / 48, rel=0.05)


def test_connection_test_is_not_blocked_by_a_hanging_camera_open(client, video_file):
    """OpenCV serialises FFmpeg opens process-wide; the probe runs in a child
    process so a camera worker stuck opening a dead stream cannot stall it."""
    import threading

    def hang():
        cap = cv2.VideoCapture("rtsp://10.255.255.1:554/x", cv2.CAP_FFMPEG,
                               [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 6000])
        cap.release()

    t = threading.Thread(target=hang, daemon=True)
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    r = client.post("/api/v1/cameras/test-connection", json={"source_type": "file", "url": str(video_file)})
    elapsed = time.monotonic() - started
    assert r.json()["success"] is True
    assert elapsed < 4.0, f"probe waited {elapsed:.1f}s behind another open"
    t.join(timeout=10)


def test_connection_test_reports_failure_without_fabricating(client):
    port = _free_port()  # nothing listens here
    r = client.post("/api/v1/cameras/test-connection", json={
        "source_type": "rtsp", "url": f"rtsp://127.0.0.1:{port}/x",
        "username": "admin", "password": SECRET, "timeout_s": 3,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is False and data["error"]
    assert "preview_jpeg" not in data and "width" not in data
    assert SECRET not in r.text and "S3cr3t" not in r.text


class _FfmpegMjpegServer:
    """Serves ``multipart/x-mixed-replace`` MJPEG produced live by ffmpeg.

    One ffmpeg per client (ffmpeg's own ``-listen 1`` HTTP server accepts a
    single connection, and a readiness probe would consume it).
    """

    def __init__(self, width=320, height=180, rate=10):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                "-i", f"testsrc=size={width}x{height}:rate={rate}", "-t", "30",
                "-f", "mpjpeg", "-q:v", "5", "pipe:1"]
        procs = self.procs = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                procs.append(proc)
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=ffmpeg")
                self.end_headers()
                try:
                    while True:
                        chunk = proc.stdout.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.kill()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_port}/cam.mjpg"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self.procs:
            p.kill()
            p.wait(timeout=5)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_connection_test_against_live_mjpeg_served_by_ffmpeg(client):
    server = _FfmpegMjpegServer(320, 180, 10)
    try:
        r = client.post("/api/v1/cameras/test-connection", json={
            "source_type": "http", "url": server.url, "timeout_s": 8,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["success"] is True, data
        assert (data["width"], data["height"]) == (320, 180)
        # Measured over consecutive live frames when enough arrive in the window.
        assert data["fps"] is None or 3 <= data["fps"] <= 40
        img = _decode_preview(data["preview_jpeg"])
        assert img.shape[1] / img.shape[0] == pytest.approx(320 / 180, rel=0.05)
    finally:
        server.close()


# ----------------------------------------------------------- credential masking

def test_credentials_never_appear_in_responses_or_the_url_column(client):
    import sqlite3

    body = {
        "name": "Masking test", "department": "aisle", "location": "Aisle 9",
        "source_type": "rtsp", "rtsp_url": "rtsp://127.0.0.1:9/cam/realmonitor?channel=1&subtype=1",
        "username": "admin", "password": SECRET,
    }
    r = client.post("/api/v1/cameras", json=body)
    assert r.status_code == 200, r.text
    cam = r.json()
    cam_id = cam["id"]
    try:
        assert SECRET not in r.text and "S3cr3t" not in r.text
        assert cam["department"] == "AISLE" and cam["status"] == "STARTING" and cam["fps"] == 0
        for resp in (client.get("/api/v1/cameras"), client.get(f"/api/v1/cameras/{cam_id}")):
            assert resp.status_code == 200 and SECRET not in resp.text and "S3cr3t" not in resp.text

        with sqlite3.connect(settings.DATABASE_PATH) as db:
            stored = db.execute("SELECT rtsp_url FROM cameras WHERE id=?", (cam_id,)).fetchone()[0]
        assert stored == body["rtsp_url"]  # credential-free

        # Encrypted at rest, injected only when the stream is opened.
        assert camera_source.has_credentials(cam_id)
        secret_files = list((Path(settings.STORAGE_DIR) / "secrets" / "named").glob("camcred-*.enc"))
        assert secret_files and all(SECRET.encode() not in f.read_bytes() for f in secret_files)
        opened = camera_source.stream_source_for(cam_id, stored)
        assert opened.startswith("rtsp://admin:S3cr3t%21pw%40x@127.0.0.1:9/")
        assert camera_source.normalize_source(None, opened).password == SECRET

        # Echoing a redacted URL back through PUT keeps the stored one.
        cam["rtsp_url"] = "rtsp://***@127.0.0.1:9/cam/realmonitor?channel=1&subtype=1"
        assert client.put(f"/api/v1/cameras/{cam_id}", json=cam).status_code == 200
        with sqlite3.connect(settings.DATABASE_PATH) as db:
            assert db.execute("SELECT rtsp_url FROM cameras WHERE id=?", (cam_id,)).fetchone()[0] == stored
    finally:
        assert client.delete(f"/api/v1/cameras/{cam_id}").status_code == 200
    assert not camera_source.has_credentials(cam_id)


def test_inline_url_credentials_are_moved_and_legacy_rows_are_masked(client):
    import sqlite3

    r = client.post("/api/v1/cameras", json={
        "name": "Inline creds", "rtsp_url": f"rtsp://op:{SECRET.replace('@', '%40')}@127.0.0.1:9/live",
    })
    assert r.status_code == 200, r.text
    cam_id = r.json()["id"]
    try:
        assert "S3cr3t" not in r.text and r.json()["rtsp_url"] == "rtsp://127.0.0.1:9/live"
        # A row written by an older flow with the password inline is masked on the way out.
        with sqlite3.connect(settings.DATABASE_PATH) as db:
            db.execute("UPDATE cameras SET rtsp_url=? WHERE id=?", (f"rtsp://op:{SECRET}@127.0.0.1:9/live", cam_id))
        got = client.get(f"/api/v1/cameras/{cam_id}")
        assert "S3cr3t" not in got.text and got.json()["rtsp_url"] == "rtsp://***@127.0.0.1:9/live"
    finally:
        client.delete(f"/api/v1/cameras/{cam_id}")


# ------------------------------------------------------------------------ auth

def test_add_and_test_require_operator_auth(client, auth_headers, monkeypatch, video_file):
    monkeypatch.setattr(settings, "AUTH_DISABLED", False)
    r = client.post("/api/v1/cameras/test-connection", json={"source_type": "file", "url": str(video_file)})
    assert r.status_code == 401
    r = client.post("/api/v1/cameras", json={"name": "x", "rtsp_url": "rtsp://h/x"})
    assert r.status_code == 401
    r = client.post("/api/v1/cameras/test-connection", json={"source_type": "file", "url": str(video_file)},
                    headers=auth_headers)
    assert r.status_code == 200 and r.json()["success"] is True, r.json()


# ------------------------------------------------------------------------ ONVIF

def test_onvif_stream_uri_is_resolved_from_the_media_service():
    """A stub ONVIF device: GetCapabilities -> GetProfiles -> GetStreamUri."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen.append(body)
            host = f"127.0.0.1:{self.server.server_port}"
            if "GetCapabilities" in body:
                xml = f"<tt:Media xmlns:tt='x'><tt:XAddr>http://{host}/onvif/Media</tt:XAddr></tt:Media>"
            elif "GetProfiles" in body:
                xml = ("<trt:GetProfilesResponse xmlns:trt='t' xmlns:tt='x'>"
                       "<trt:Profiles token='main'><tt:Resolution><tt:Width>2560</tt:Width><tt:Height>1440</tt:Height></tt:Resolution></trt:Profiles>"
                       "<trt:Profiles token='sub'><tt:Resolution><tt:Width>704</tt:Width><tt:Height>576</tt:Height></tt:Resolution></trt:Profiles>"
                       "</trt:GetProfilesResponse>")
            else:
                assert "<trt:ProfileToken>sub</trt:ProfileToken>" in body
                xml = "<trt:MediaUri xmlns:trt='t' xmlns:tt='x'><tt:Uri>rtsp://u:p@10.0.0.5:554/sub</tt:Uri></trt:MediaUri>"
            data = f"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body>{xml}</s:Body></s:Envelope>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/soap+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        info = camera_source.resolve_onvif_stream("127.0.0.1", srv.server_port, "admin", SECRET, timeout=3)
    finally:
        srv.shutdown()
    assert info["url"] == "rtsp://10.0.0.5:554/sub" and info["profile"] == "sub"
    assert all("PasswordDigest" in b and SECRET not in b for b in seen)


# ------------------------------------------------------------------ preflight

def test_preflight_reports_a_missing_override_dir_as_will_be_created(tmp_path, monkeypatch):
    from app.services import preflight

    missing = tmp_path / "not" / "yet" / "snapshots"
    monkeypatch.setattr(settings, "SNAPSHOTS_DIR", missing)
    results, errors, _ = preflight.check_storage()
    entry = next(r for r in results if r["name"] == "SNAPSHOTS_DIR")
    assert entry["writable"] is True and entry["note"] == "will be created"
    assert not [e for e in errors if "SNAPSHOTS_DIR" in str(e)]
    assert not missing.exists()                 # preflight never writes

    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        monkeypatch.setattr(settings, "SNAPSHOTS_DIR", blocked / "snapshots")
        results, errors, _ = preflight.check_storage()
        entry = next(r for r in results if r["name"] == "SNAPSHOTS_DIR")
        import os
        if os.geteuid() != 0:
            assert entry["writable"] is False and "cannot be created" in entry["note"]
            assert any("SNAPSHOTS_DIR" in str(e) for e in errors)
    finally:
        blocked.chmod(0o700)
