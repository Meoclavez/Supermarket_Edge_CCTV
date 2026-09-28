"""Recorder sub-stream upgrade (CIF -> D1) and automatic per-camera stream selection.

A fake Dahua recorder speaks the CGI API over HTTP/1.1 with Digest
authentication whose nonce is bound to the TCP connection (as on the real
recorder: an answer on another connection is a failed login). A fake RTSP
"switcher" reports the size the recorder's sub-stream currently delivers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.services import dahua_config as dc
from app.services.recorder_substreams import (
    ALREADY, CAPS_UNKNOWN, NOT_ATTEMPTED, REFUSED, RESTORE_OK, RESTORED, SWITCHED, UNSUPPORTED,
    RecorderSubstreams,
)
from app.services.stream_selection import StreamSelection, parse_dahua, with_subtype

USER, PASSWORD = "admin", "s3cret-pass"
HOST = "127.0.0.1"


# --------------------------------------------------------------- fake recorder

class FakeDahua:
    """channels: {ch: dict(w, h, bitrate, codec, caps=[...], sub2=(w, h) | None, key=('wh'|'res'|'both'))}."""

    def __init__(self, channels: dict, *, standard: str = "PAL", caps_mode: str = "encode_cgi",
                 close_every_response: bool = False):
        self.channels = {int(k): dict(v) for k, v in channels.items()}
        self.standard = standard
        self.caps_mode = caps_mode                # encode_cgi | encodecaps | none
        self.close_every_response = close_every_response
        self.refuse_set = False
        self.no_stick: set[int] = set()
        self.refuse_restore = False
        self.auth_failures = 0
        self.set_calls: list[dict] = []
        self.requests: list[str] = []
        self.connections = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.nonce = uuid.uuid4().hex          # bound to this TCP connection
                with fake._lock:
                    fake.connections += 1

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: str, extra: dict | None = None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                if fake.close_every_response and code != 401:
                    # Ends the connection after each answered request (never
                    # after a challenge, whose nonce lives on this connection).
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

            def _challenge(self):
                self._send(401, "Unauthorized", {
                    "WWW-Authenticate": f'Digest realm="Login to FAKE", qop="auth", nonce="{self.nonce}", opaque="op"'})

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization")
                if not header:
                    return False
                params = {k.lower(): (a or b) for k, a, b in
                          re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', header.partition(" ")[2])}
                h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
                ha1 = h(f"{USER}:{params.get('realm')}:{PASSWORD}")
                ha2 = h(f"GET:{params.get('uri')}")
                want = h(f"{ha1}:{params.get('nonce')}:{params.get('nc')}:{params.get('cnonce')}:auth:{ha2}")
                ok = (params.get("username") == USER and params.get("nonce") == self.nonce
                      and params.get("response") == want and params.get("uri") == self.path)
                if not ok:
                    with fake._lock:
                        fake.auth_failures += 1
                return ok

            def do_GET(self):
                if not self.headers.get("Authorization"):
                    return self._challenge()
                if not self._authorised():
                    return self._challenge()
                fake.requests.append(self.path)
                code, body = fake.handle(self.path)
                self._send(code, body)

        self.server = ThreadingHTTPServer((HOST, 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # -- CGI -------------------------------------------------------------------
    def _encode_lines(self) -> list[str]:
        lines = []
        for ch, c in sorted(self.channels.items()):
            p = f"table.Encode[{ch - 1}]"
            lines += [f"{p}.MainFormat[0].Video.Width=2560", f"{p}.MainFormat[0].Video.Height=1440"]
            e = f"{p}.ExtraFormat[0]."
            key = c.get("key", "both")
            if key in ("wh", "both"):
                lines += [f"{e}Video.Width={c['w']}", f"{e}Video.Height={c['h']}"]
            if key in ("res", "both"):
                lines.append(f"{e}Video.resolution={c['w']}x{c['h']}")
            lines += [f"{e}Video.BitRate={c.get('bitrate', 256)}", f"{e}Video.BitRateControl=CBR",
                      f"{e}Video.Compression={c.get('codec', 'H.264')}", f"{e}Video.FPS=15",
                      f"{e}Video.GOP=30", f"{e}VideoEnable=true"]
        return lines

    def handle(self, path: str) -> tuple[int, str]:
        parts = urlsplit(path)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        if parts.path == "/cgi-bin/configManager.cgi" and q.get("action") == "getConfig":
            name = q.get("name")
            if name == "Encode":
                return 200, "\r\n".join(self._encode_lines()) + "\r\n"
            if name == "VideoStandard":
                return 200, f"table.VideoStandard={self.standard}\r\n"
            if name == "EncodeCaps" and self.caps_mode == "encodecaps":
                return 200, "\r\n".join(
                    f"table.EncodeCaps[{ch - 1}].ExtraFormat[0].Video.ResolutionTypes={','.join(c['caps'])}"
                    for ch, c in sorted(self.channels.items())) + "\r\n"
            return 400, "Error\r\nBad Request!\r\n"
        if parts.path == "/cgi-bin/encode.cgi" and q.get("action") == "getConfigCaps":
            if self.caps_mode != "encode_cgi":
                return 400, "Error\r\nBad Request!\r\n"
            c = self.channels.get(int(q.get("channel", 0)))
            if c is None:
                return 400, "Error\r\n"
            return 200, (f"caps.ExtraFormat[0].Video.ResolutionTypes={','.join(c['caps'])}\r\n"
                         "caps.ExtraFormat[0].Video.BitRateOptions=32,2048\r\n")
        if parts.path == "/cgi-bin/configManager.cgi" and q.get("action") == "setConfig":
            sets = {k: v for k, v in q.items() if k != "action"}
            self.set_calls.append(sets)
            restoring = any(v in ("352", "352x288") for v in sets.values())
            if self.refuse_set or (restoring and self.refuse_restore):
                return 200, "Error\r\n"
            for k, v in sets.items():
                m = re.match(r"^Encode\[(\d+)\]\.ExtraFormat\[0\]\.Video\.(\w+)$", k)
                if not m:
                    return 200, "Error\r\n"
                ch = int(m.group(1)) + 1
                if ch in self.no_stick and not restoring:
                    continue
                c = self.channels[ch]
                field = m.group(2)
                if field == "Width":
                    c["w"] = int(v)
                elif field == "Height":
                    c["h"] = int(v)
                elif field.lower() == "resolution":
                    size = dc.parse_size(v, self.standard)
                    c["w"], c["h"] = size
                elif field == "BitRate":
                    c["bitrate"] = int(v)
            return 200, "OK\r\n"
        return 404, "Error\r\n"


class FakeRtspSwitcher:
    """Answers stream probes with the size the recorder delivers now."""

    def __init__(self, fake: FakeDahua):
        self.fake = fake
        self.override: dict[int, tuple[int, int]] = {}
        self.down: set[int] = set()
        self.targets: list[str] = []

    async def __call__(self, target: str) -> dict:
        self.targets.append(target)
        ch, sub = parse_dahua(target)
        if ch in self.down:
            return {"success": False, "error_code": "CONNECT_FAILED", "error": "no stream"}
        c = self.fake.channels.get(ch)
        if c is None:
            return {"success": False, "error_code": "CONNECT_FAILED"}
        if sub == 1:
            w, h = self.override.get(ch, (c["w"], c["h"]))
        elif sub == 2:
            if not c.get("sub2"):
                return {"success": False, "error_code": "CONNECT_FAILED", "error": "no third stream"}
            w, h = c["sub2"]
        else:
            w, h = 2560, 1440
        return {"success": True, "width": w, "height": h}


@pytest.fixture
def make_fake():
    made = []

    def _make(channels, **kw):
        f = FakeDahua(channels, **kw)
        made.append(f)
        return f

    yield _make
    for f in made:
        f.stop()


def _cams(channels, subtype=1):
    return [{"id": f"cam{ch}", "name": f"Cam {ch}", "channel": ch, "subtype": subtype, "rtsp_port": 554,
             "rtsp_url": f"rtsp://{HOST}:554/cam/realmonitor?channel={ch}&subtype={subtype}"}
            for ch in channels]


def _service(tmp_path, fake, switcher=None):
    svc = RecorderSubstreams(storage_dir=tmp_path)
    svc.channel_delay_s = 0
    svc.read_delay_s = 0
    svc.verify_initial_wait_s = 0
    svc.verify_gap_s = 0
    svc.verify_attempts = 2
    svc.probe_fn = switcher or FakeRtspSwitcher(fake)
    svc.changed = []

    async def on_changed(cams, verified):
        svc.changed.append(([c["id"] for c in cams], verified))

    svc.on_channel_changed = on_changed
    return svc


def _run(svc, fake, channels=None, *, password=PASSWORD, kind="upgrade", cams=None):
    cams = cams or _cams(sorted(fake.channels))
    fn = svc.run_upgrade if kind == "upgrade" else svc.run_restore
    return asyncio.run(fn(HOST, cams, channels, "tester", username=USER, password=password, http_port=fake.port))


def _entry(run, ch):
    return next(e for e in run["channels"] if e["channel"] == ch)


CIF_D1 = {"w": 352, "h": 288, "bitrate": 256, "caps": ["D1", "CIF", "QCIF"]}


# ---------------------------------------------------------------- D1 upgrade

def test_d1_supported_is_switched_and_verified(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1, 2: {**CIF_D1, "key": "res", "codec": "H.265"}})
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    assert run["state"] == "done", run
    for ch in (1, 2):
        e = _entry(run, ch)
        assert e["outcome"] == SWITCHED, e
        assert e["text"].startswith("switched to D1")
        assert (fake.channels[ch]["w"], fake.channels[ch]["h"]) == (704, 576)
    # CIF-level bit rate raised (H.264 1024, H.265 768); codec/FPS/GOP never written.
    assert fake.channels[1]["bitrate"] == 1024 and fake.channels[2]["bitrate"] == 768
    for call in fake.set_calls:
        assert not any(k.endswith(("Compression", "FPS", "GOP")) for k in call)
    # Backup of the full previous sub-stream config, private.
    path = svc.state_path(HOST)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    saved = json.loads(path.read_text())["backups"]["1"]["config"]
    assert saved["Video.Width"] == "352" and saved["Video.GOP"] == "30"
    # Audited, and each channel's workers restarted once with the verified size.
    audit = [json.loads(x) for x in (tmp_path / "recorder_audit.jsonl").read_text().splitlines()]
    assert {a["channel"] for a in audit} == {1, 2} and audit[0]["by"] == "tester"
    assert audit[0]["before"]["Video.Width"] == "352" and audit[0]["after"]["Video.Width"] == "704"
    assert svc.changed == [(["cam1"], (704, 576)), (["cam2"], (704, 576))]
    assert fake.auth_failures == 0


def test_already_d1_is_left_alone(tmp_path, make_fake):
    fake = make_fake({1: {"w": 704, "h": 576, "bitrate": 1024, "caps": ["D1", "CIF"]},
                      2: {"w": 1280, "h": 720, "bitrate": 2048, "caps": ["720P", "D1"]}})
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, [1, 2])
    assert [_entry(run, c)["outcome"] for c in (1, 2)] == [ALREADY, ALREADY]
    assert _entry(run, 1)["text"].startswith("already D1 or higher")
    assert fake.set_calls == []


def test_d1_unsupported_keeps_cif(tmp_path, make_fake):
    fake = make_fake({3: {"w": 352, "h": 288, "caps": ["CIF", "QCIF"]}})
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    e = _entry(run, 3)
    assert e["outcome"] == UNSUPPORTED and e["text"].startswith("kept CIF: D1 not supported by this camera")
    assert fake.set_calls == [] and fake.channels[3]["w"] == 352


def test_caps_unknown_is_skipped(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1}, caps_mode="none")
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    assert _entry(run, 1)["outcome"] == CAPS_UNKNOWN and fake.set_calls == []


def test_encodecaps_firmware_variant(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1, 2: {"w": 352, "h": 288, "caps": ["CIF"]}}, caps_mode="encodecaps")
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    assert _entry(run, 1)["outcome"] == SWITCHED
    assert _entry(run, 2)["outcome"] == UNSUPPORTED


def test_ntsc_recorder_uses_704x480(tmp_path, make_fake):
    fake = make_fake({1: {"w": 352, "h": 240, "bitrate": 256, "caps": ["D1", "CIF"]}}, standard="NTSC")
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    assert _entry(run, 1)["outcome"] == SWITCHED
    assert (fake.channels[1]["w"], fake.channels[1]["h"]) == (704, 480)


def test_setconfig_refused_keeps_cif(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1})
    fake.refuse_set = True
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    e = _entry(run, 1)
    assert e["outcome"] == REFUSED and e["text"].startswith("kept CIF: NVR refused")
    assert fake.channels[1]["w"] == 352 and len(fake.set_calls) == 1
    assert svc.changed == []


def test_value_not_sticking_is_restored(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1})
    fake.no_stick.add(1)
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None)
    e = _entry(run, 1)
    assert e["outcome"] == RESTORED and "value did not stick" in e["text"]
    assert (fake.channels[1]["w"], fake.channels[1]["h"], fake.channels[1]["bitrate"]) == (352, 288, 256)
    assert len(fake.set_calls) == 2           # the change, then the restore


def test_stream_not_d1_is_restored(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1})
    switcher = FakeRtspSwitcher(fake)
    switcher.override[1] = (352, 288)          # config says D1 but the stream stays CIF
    svc = _service(tmp_path, fake, switcher)
    run = _run(svc, fake, None)
    e = _entry(run, 1)
    assert e["outcome"] == RESTORED and "not D1" in e["text"], e
    assert (fake.channels[1]["w"], fake.channels[1]["bitrate"]) == (352, 256)
    assert svc.changed == [(["cam1"], None)]  # worker restarted once, on the restored stream
    assert all("subtype=1" in t and f"{USER}:" in t for t in switcher.targets)


def test_stream_down_is_restored(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1})
    switcher = FakeRtspSwitcher(fake)
    switcher.down.add(1)
    svc = _service(tmp_path, fake, switcher)
    run = _run(svc, fake, None)
    assert _entry(run, 1)["outcome"] == RESTORED
    assert "did not come up" in _entry(run, 1)["text"]
    assert fake.channels[1]["w"] == 352


def test_restore_failure_is_reported(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1})
    switcher = FakeRtspSwitcher(fake)
    switcher.override[1] = (352, 288)
    fake.refuse_restore = True
    svc = _service(tmp_path, fake, switcher)
    run = _run(svc, fake, None)
    e = _entry(run, 1)
    assert e["outcome"] == "restore_failed" and "restore failed" in e["text"]


def test_wrong_password_stops_without_retry(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1, 2: CIF_D1, 3: CIF_D1})
    svc = _service(tmp_path, fake)
    run = _run(svc, fake, None, password="wrong")
    assert run["state"] == "stopped" and run.get("auth_failed") is True
    assert "rejected" in run["message"]
    assert fake.auth_failures == 1            # exactly one failed login, then stop
    assert fake.set_calls == []
    assert {e["outcome"] for e in run["channels"]} == {NOT_ATTEMPTED}


def test_restore_writes_saved_settings_back(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1, 2: CIF_D1})
    svc = _service(tmp_path, fake)
    _run(svc, fake, None)
    assert fake.channels[1]["w"] == 704
    run = _run(svc, fake, [1], kind="restore")
    assert run["state"] == "done"
    e = _entry(run, 1)
    assert e["outcome"] == RESTORE_OK and e["text"].startswith("restored to 352")
    assert (fake.channels[1]["w"], fake.channels[1]["bitrate"]) == (352, 256)
    assert fake.channels[2]["w"] == 704                 # not asked for
    # Restore all saved channels.
    run = _run(svc, fake, None, kind="restore")
    assert fake.channels[2]["w"] == 352
    audit = [json.loads(x) for x in (tmp_path / "recorder_audit.jsonl").read_text().splitlines()]
    assert [a["action"] for a in audit].count("restore_saved") == 3


def test_digest_nonce_is_bound_to_the_connection(tmp_path, make_fake):
    """Many requests on one connection reuse its nonce; after the recorder
    closes the connection the client asks for a new challenge instead of
    replaying the old nonce (which would count as a failed login)."""
    for close in (False, True):
        fake = make_fake({1: CIF_D1, 2: CIF_D1}, close_every_response=close)
        svc = _service(tmp_path / str(close), fake)
        run = _run(svc, fake, None)
        assert {e["outcome"] for e in run["channels"]} == {SWITCHED}
        assert fake.auth_failures == 0
        if not close:
            assert fake.connections == 1


def test_preview_is_read_only(tmp_path, make_fake):
    fake = make_fake({1: CIF_D1, 2: {"w": 352, "h": 288, "caps": ["CIF"]}, 3: {**CIF_D1, "w": 704, "h": 576}})
    svc = _service(tmp_path, fake)
    out = asyncio.run(svc.preview(HOST, _cams([1, 2, 3]), username=USER, password=PASSWORD, http_port=fake.port))
    plans = {r["channel"]: r["plan"] for r in out["channels"]}
    assert plans == {1: "upgrade", 2: "unsupported", 3: "already"}
    assert fake.set_calls == []


def test_client_parsers():
    assert dc.parse_size("D1") == (704, 576) and dc.parse_size("D1", "NTSC") == (704, 480)
    caps = {"caps.ExtraFormat[0].Video.ResolutionTypes[0]": "704x576",
            "caps.ExtraFormat[0].Video.ResolutionTypes[1]": "CIF"}
    assert dc.caps_resolutions(caps, 1) == [(704, 576), (352, 288)]
    tbl = {"table.EncodeCaps[4].ExtraFormat[0].Video.ResolutionTypes": "D1,CIF",
           "table.EncodeCaps[0].ExtraFormat[0].Video.ResolutionTypes": "CIF"}
    assert dc.caps_resolutions(tbl, 5) == [(704, 576), (352, 288)]
    assert dc.caps_resolutions(tbl, 1) == [(352, 288)]
    sub = {"Video.resolution": "CIF", "Video.BitRate": "600"}
    assert dc.d1_assignments(3, sub, (704, 576)) == {"Encode[2].ExtraFormat[0].Video.resolution": "D1"}


# ---------------------------------------------------------- stream selection

URL = f"rtsp://{HOST}:554/cam/realmonitor?channel=7&subtype=1"


def _selector(tmp_path, sizes: dict[int, tuple[int, int] | None]):
    sel = StreamSelection(path=tmp_path / "sel.json")
    sel.gap_s = 0
    sel.decoder_cache = False
    calls = []

    async def probe(target):
        calls.append(target)
        _, st = parse_dahua(target)
        size = sizes.get(st)
        if size is None:
            return {"success": False, "error_code": "CONNECT_FAILED"}
        return {"success": True, "width": size[0], "height": size[1]}

    sel.probe_fn = probe
    sel.calls = calls
    return sel


def _measure(sel, cam="c7", url=URL):
    return asyncio.run(sel.measure_camera(cam, url, open_target_for=lambda u: u))


def test_auto_picks_subtype_2_when_it_is_d1(tmp_path):
    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    assert sel.decide("c7", URL, None).state == "pending"
    _measure(sel)
    d = sel.decide("c7", URL, None)
    assert d.subtype == 2 and d.state == "chosen"
    assert d.url.endswith("channel=7&subtype=2")
    assert d.label == "Sub-stream 2 704×576 (D1)"
    assert "352×288" in d.reason


def test_auto_prefers_smallest_d1_stream(tmp_path):
    sel = _selector(tmp_path, {1: (1280, 720), 2: (704, 576)})
    _measure(sel)
    assert sel.decide("c7", URL, "auto").subtype == 2
    sel2 = _selector(tmp_path / "b", {1: (704, 576), 2: (1280, 720)})
    _measure(sel2)
    d = sel2.decide("c7", URL, "auto")
    assert d.subtype == 1 and d.label == "Sub-stream 704×576 (D1)"


def test_auto_never_picks_main(tmp_path):
    sel = _selector(tmp_path, {1: (352, 288), 2: None, 0: (2560, 1440)})
    _measure(sel)
    d = sel.decide("c7", URL, None)
    assert d.subtype == 1 and "subtype=1" in d.url
    assert d.label == "Sub-stream 352×288 — D1 not available"
    assert all("subtype=0" not in c for c in sel.calls)   # main never even probed
    # A camera added on its main stream stays there under Auto.
    main_url = with_subtype(URL, 0)
    assert sel.decide("c8", main_url, None).subtype == 0


def test_per_camera_override(tmp_path):
    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    _measure(sel)
    assert sel.decide("c7", URL, "main").subtype == 0
    assert sel.decide("c7", URL, "sub").subtype == 1
    assert sel.decide("c7", URL, "auto").subtype == 2
    # Non-Dahua sources are opened exactly as configured.
    d = sel.decide("c9", "rtsp://10.0.0.5/stream1", "main")
    assert d.url == "rtsp://10.0.0.5/stream1" and d.state == "not_applicable"


def test_url_change_forgets_measurements_and_persists(tmp_path):
    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    _measure(sel)
    other = URL.replace("channel=7", "channel=8")
    assert sel.decide("c7", other, None).state == "pending"
    # Measurements survive a restart (new instance reading the same file).
    again = StreamSelection(path=tmp_path / "sel.json")
    assert again.decide("c7", URL, None).subtype == 2


def test_verified_d1_upgrade_updates_choice(tmp_path):
    sel = _selector(tmp_path, {1: (352, 288), 2: (1280, 720)})
    _measure(sel)
    assert sel.decide("c7", URL, None).subtype == 2
    sel.record("c7", URL, 1, width=704, height=576, source="verified")
    assert sel.decide("c7", URL, None).subtype == 1


def test_alternate_stream_for_subtype_2_is_the_sub_stream():
    from app.services.camera_drivers import alternate_stream_url

    assert alternate_stream_url(with_subtype(URL, 2)).endswith("subtype=1")
    assert alternate_stream_url(URL).endswith("subtype=0")


def test_supervisor_opens_the_chosen_stream(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services import pipeline_supervisor as ps
    from app.services import stream_selection as ss_mod

    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    _measure(sel)
    monkeypatch.setattr(ss_mod, "stream_selection", sel)
    cam = SimpleNamespace(id="c7", rtsp_url=URL, features={})
    assert ps._camera_source(cam).endswith("subtype=2")
    cam.features = {"stream_quality": "sub"}
    assert ps._camera_source(cam).endswith("subtype=1")


# ------------------------------------------------------------------------- API

def test_recorder_api_upgrade_restore_and_selection(tmp_path, make_fake, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import async_session_factory, engine
    from app.models.db_models import Base, CameraModel
    from app.routes import recorders as rr

    fake = make_fake({1: CIF_D1, 2: {"w": 352, "h": 288, "caps": ["CIF"]}})
    svc = _service(tmp_path, fake)
    monkeypatch.setattr(rr, "recorder_substreams", svc)
    monkeypatch.setattr(rr, "recorder_credentials", lambda host, cams: (USER, PASSWORD, "test"))
    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    monkeypatch.setattr(rr, "stream_selection", sel)

    app = FastAPI()
    app.include_router(rr.router)
    app.include_router(rr.camera_stream_router)
    tag = uuid.uuid4().hex[:6]
    ids = [f"rs_{tag}_{ch}" for ch in (1, 2)]

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as db:
            for ch, cid in zip((1, 2), ids):
                db.add(CameraModel(id=cid, name=f"Ch {ch}", location="t", status="ONLINE",
                                   rtsp_url=f"rtsp://{HOST}:554/cam/realmonitor?channel={ch}&subtype=1",
                                   features={"stream_quality": "auto"}))
            await db.commit()

    async def cleanup():
        async with async_session_factory() as db:
            for cid in ids:
                row = await db.get(CameraModel, cid)
                if row is not None:
                    await db.delete(row)
            await db.commit()

    with TestClient(app) as client:
        client.portal.call(setup)
        try:
            recs = client.get("/api/v1/recorders").json()["recorders"]
            rec = next(r for r in recs if r["id"] == HOST)
            assert {1, 2} <= set(rec["channels"])
            assert client.post(f"/api/v1/recorders/{HOST}/substreams/d1", json={}).status_code == 422
            assert client.post(f"/api/v1/recorders/{HOST}/substreams/d1",
                               json={"channels": [63]}).status_code == 400
            assert client.post("/api/v1/recorders/10.9.9.9/substreams/d1",
                               json={"all_cif": True}).status_code == 404

            prev = client.get(f"/api/v1/recorders/{HOST}/substreams", params={"http_port": fake.port}).json()
            assert {r["channel"]: r["plan"] for r in prev["channels"]} == {1: "upgrade", 2: "unsupported"}

            r = client.post(f"/api/v1/recorders/{HOST}/substreams/d1",
                            json={"all_cif": True, "http_port": fake.port})
            assert r.status_code == 202 and r.json()["state"] == "running"
            for _ in range(200):
                res = client.get(f"/api/v1/recorders/{HOST}/substreams/d1").json()
                if not res["running"]:
                    break
                client.portal.call(asyncio.sleep, 0.02)
            run = res["run"]
            assert run["state"] == "done", run
            assert {e["channel"]: e["outcome"] for e in run["channels"]} == {1: SWITCHED, 2: UNSUPPORTED}
            assert res["saved_channels"] == [1] and res["audit"][0]["channel"] == 1

            r = client.post(f"/api/v1/recorders/{HOST}/substreams/restore",
                            json={"all": True, "http_port": fake.port})
            assert r.status_code == 202
            for _ in range(200):
                res = client.get(f"/api/v1/recorders/{HOST}/substreams/d1").json()
                if not res["running"]:
                    break
                client.portal.call(asyncio.sleep, 0.02)
            assert res["run"]["kind"] == "restore" and _entry(res["run"], 1)["outcome"] == RESTORE_OK
            assert fake.channels[1]["w"] == 352

            # Per-camera stream selection: measure (read-only) then report the choice.
            monkeypatch.setattr(sel, "measure_camera",
                                lambda cid, url, **kw: sel.__class__.measure_camera(
                                    sel, cid, url, open_target_for=lambda u: u))
            got = client.post(f"/api/v1/cameras/{ids[0]}/stream-selection/measure").json()
            assert got["subtype"] == 2 and got["label"] == "Sub-stream 2 704×576 (D1)"
            got = client.get(f"/api/v1/cameras/{ids[0]}/stream-selection").json()
            assert got["subtype"] == 2 and got["measured"]["1"]["w"] == 352
        finally:
            client.portal.call(cleanup)


def test_background_measuring_runs_once_and_reconciles(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "STREAM_AUTO_SELECT", True)
    sel = _selector(tmp_path, {1: (352, 288), 2: (704, 576)})
    sel.initial_delay_s = 0
    main_url = with_subtype(URL, 0)
    reconciles = []

    async def on_change():
        reconciles.append(1)

    async def go():
        # Stub the per-camera open target (no stored credentials in this test).
        orig = sel.measure_camera

        async def measure(cam, url, **kw):
            return await orig(cam, url, open_target_for=lambda u: u, **kw)

        sel.measure_camera = measure
        cams = [("c7", URL, None), ("cmain", main_url, None), ("cfix", URL.replace("=7", "=9"), "sub"),
                ("other", "rtsp://10.0.0.5/stream1", None)]
        assert sel.schedule(cams, on_change) == 1          # only the Auto sub-stream camera
        await sel.wait_idle(10)
        assert sel.schedule(cams, on_change) == 0          # measured once, not again
    asyncio.run(go())
    assert reconciles == [1]
    assert sel.decide("c7", URL, None).subtype == 2
    assert all("subtype=0" not in c for c in sel.calls)
    monkeypatch.setattr(settings, "STREAM_AUTO_SELECT", False)
    assert sel.schedule([("c8", URL.replace("=7", "=8"), None)]) == 0


def test_stream_quality_setting_round_trip(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import async_session_factory, engine
    from app.models.db_models import Base, CameraModel
    from app.routes import cameras as cam_routes

    app = FastAPI()
    app.include_router(cam_routes.router)
    cid = f"sq_{uuid.uuid4().hex[:6]}"

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session_factory() as db:
            db.add(CameraModel(id=cid, name="SQ", location="t", status="ONLINE", rtsp_url=URL,
                               features={"person_max_frame_fraction": 0.5}))
            await db.commit()

    async def cleanup():
        async with async_session_factory() as db:
            row = await db.get(CameraModel, cid)
            if row is not None:
                await db.delete(row)
                await db.commit()

    with TestClient(app) as client:
        client.portal.call(setup)
        try:
            r = client.put(f"/api/v1/cameras/{cid}/features", json={"stream_quality": "main"})
            assert r.status_code == 200 and r.json()["stream_quality"] == "main"
            assert r.json()["person_max_frame_fraction"] == 0.5
            # A client that does not send it (older dashboard / phone app) keeps it.
            r = client.put(f"/api/v1/cameras/{cid}/features", json={"people_counting": False})
            assert r.json()["stream_quality"] == "main"
            assert client.put(f"/api/v1/cameras/{cid}/features",
                              json={"stream_quality": "best"}).status_code == 422
        finally:
            client.portal.call(cleanup)
