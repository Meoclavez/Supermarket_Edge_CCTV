"""iKorex fleet control, box side (docs/FLEET_CONTROL_CONTRACT.md).

Signed policy / command verification (Ed25519, pinned on first use), replay
and expiry rules, licence dates, the 423 paused gate, 403 feature gates, the
camera limit, the telemetry allow-list and the check-in client. The VPS is an
httpx.MockTransport; nothing leaves this machine.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.main import app
from app.models.db_models import Base
from app.services import entitlements as ent
from app.services import fleet_control as fc

STORE = "teststore"
TARGET = {"url": "https://tunnel.example.com/control/v1/checkin", "store_id": STORE,
          "token": "store-token-abc", "ca_file": None}


# --------------------------------------------------------------------------- helpers

def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


def _key():
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import base64

    return priv, raw, base64.b64encode(raw).decode()


def _sign(obj, priv) -> dict:
    payload = fc.canonical_json(obj)
    return {"payload": fc.b64url_encode(payload), "sig": fc.b64url_encode(priv.sign(payload))}


def _policy(version=1, **kw) -> dict:
    p = {"type": "policy", "store_id": STORE, "version": version, "issued_at": fc.iso(), "state": "active",
         "message": "", "licence": {"valid_until": None, "grace_days": 14},
         "features": {k: True for k in ent.FEATURES}, "limits": {"max_cameras": None}}
    p.update(kw)
    return p


def _command(action="checkin_now", cid="c_abc123", minutes=15, **kw) -> dict:
    now = datetime.now(timezone.utc)
    c = {"type": "command", "id": cid, "store_id": STORE, "action": action, "issued_at": fc.iso(now),
         "not_after": fc.iso(now + timedelta(minutes=minutes))}
    c.update(kw)
    return c


def _store_policy(policy: dict) -> None:
    state = fc.load_state()
    state["policy"] = ent.normalise_policy(policy)
    fc.save_state(state)


class FakeVPS:
    def __init__(self):
        self.requests = []
        self.answer = {"control_public_key": None, "policy": None, "commands": [], "next_checkin_s": 300}
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(200, json=dict(self.answer, server_time=fc.iso()))

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def _checkin():
    return asyncio.run(fc.fleet_control.checkin_now())


@pytest.fixture(autouse=True)
def _clean_fleet(monkeypatch):
    engine = create_engine(f"sqlite:///{settings.DATABASE_PATH}")
    Base.metadata.create_all(engine)
    engine.dispose()

    def wipe():
        with _db() as conn:
            conn.execute("DELETE FROM system_setup WHERE key = ?", (ent.STATE_KEY,))
        ent.invalidate()

    wipe()
    ctl = fc.fleet_control
    ctl._failures, ctl._next_s, ctl.boot, ctl._started, ctl._poke = 0, None, None, False, False
    restarts = []
    monkeypatch.setattr(fc, "restart_hook", lambda: restarts.append(True))
    monkeypatch.setattr(settings, "FLEET_CONTROL_PUBLIC_KEY", "")
    yield restarts
    wipe()
    ctl.boot, ctl._started = None, False


@pytest.fixture()
def vps(monkeypatch):
    srv = FakeVPS()
    monkeypatch.setattr(fc, "http_transport", httpx.MockTransport(srv.handler))
    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: dict(TARGET)))
    return srv


# --------------------------------------------------------------------------- signatures

def test_signature_verifies_and_tampering_is_refused():
    priv, raw, _ = _key()
    env = _sign(_policy(version=3), priv)
    assert fc.verify_envelope(env, raw)["version"] == 3

    tampered = dict(env, payload=fc.b64url_encode(fc.canonical_json(_policy(version=3, state="suspended"))))
    with pytest.raises(fc.Rejected) as exc:
        fc.verify_envelope(tampered, raw)
    assert exc.value.code == "bad_signature"

    _, other_raw, _ = _key()
    with pytest.raises(fc.Rejected):
        fc.verify_envelope(env, other_raw)                     # signed by someone else
    with pytest.raises(fc.Rejected):
        fc.verify_envelope({"payload": env["payload"], "sig": "AAAA"}, raw)
    assert fc.fingerprint(raw).startswith("sha256:") and len(fc.fingerprint(raw)) == len("sha256:") + 16


# Produced by the VPS's own module (Server/cctv-tunnel/control/cctv_control/signing.py,
# ControlKey(Ed25519PrivateKey.from_private_bytes(bytes(range(32)))).sign(policy)).
VPS_VECTOR = {
    "key": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
    "fingerprint": "sha256:56475aa75463474c",
    "envelope": {
        "payload": ("eyJmZWF0dXJlcyI6eyJoZWF0bWFwcyI6ZmFsc2UsInRoZWZ0X2RldGVjdGlvbiI6dHJ1ZX0sImlzc3VlZF9hdCI6IjIwMjYt"
                    "MTAtMTZUMTA6MDA6MDBaIiwibGljZW5jZSI6eyJncmFjZV9kYXlzIjoxNCwidmFsaWRfdW50aWwiOiIyMDI3LTEwLTMxIn0s"
                    "ImxpbWl0cyI6eyJtYXhfY2FtZXJhcyI6bnVsbH0sIm1lc3NhZ2UiOiJBY2NvdW50IG9uIGhvbGQgXHUyMDEzIGNhbGwgaUtv"
                    "cmV4Iiwic3RhdGUiOiJzdXNwZW5kZWQiLCJzdG9yZV9pZCI6InBlYXJjZWRhbGUiLCJ0eXBlIjoicG9saWN5IiwidmVyc2lv"
                    "biI6N30"),
        "sig": "dqj2FDXy7wwczrAwPdaaFk3Ly61hRtINK6MUJSYJoy_hk2K2Z9R0dmoYn_Y0lwyqWTGLGvktWIa7_NzMTc0bCA",
    },
}


def test_envelope_signed_by_the_vps_module_verifies():
    raw = fc.decode_public_key(VPS_VECTOR["key"])               # standard base64 with padding
    assert raw is not None and fc.fingerprint(raw) == VPS_VECTOR["fingerprint"]
    policy = fc.verify_envelope(VPS_VECTOR["envelope"], raw)
    assert policy["version"] == 7 and policy["state"] == "suspended" and policy["store_id"] == "pearcedale"
    clean = ent.normalise_policy(policy, "pearcedale")
    assert clean["message"] == "Account on hold \u2013 call iKorex"
    assert clean["features"]["heatmaps"] is False and clean["features"]["night_watch"] is True
    bad = dict(VPS_VECTOR["envelope"], sig=VPS_VECTOR["envelope"]["sig"][:-2] + "AA")
    with pytest.raises(fc.Rejected):
        fc.verify_envelope(bad, raw)


def test_first_checkin_pins_the_key_and_a_changed_key_is_refused(vps):
    priv_a, raw_a, pub_a = _key()
    vps.answer.update(control_public_key=pub_a, policy=_sign(_policy(version=1), priv_a))
    st = _checkin()
    assert st["control_key_fingerprint"] == fc.fingerprint(raw_a) and st["control_key_source"] == "pinned"
    assert st["policy_version"] == 1 and st["managed"] is True and st["key_changed"] is False

    priv_b, raw_b, pub_b = _key()                              # the VPS now presents another key
    vps.answer.update(control_public_key=pub_b,
                      policy=_sign(_policy(version=2, state="suspended"), priv_b),
                      commands=[_sign(_command(), priv_b)])
    st = _checkin()
    assert st["key_changed"] is True and st["offered_key_fingerprint"] == fc.fingerprint(raw_b)
    assert st["policy_version"] == 1 and st["paused"] is False  # nothing applied
    assert fc.load_state()["pending_results"] == []            # commands not run
    _checkin()
    assert "control_key_changed" in vps.body()["health"]["errors"]

    # A local administrator resets the pin: the next check-in trusts the new key.
    st = asyncio.run(fc.fleet_control.reset_key("local admin manager"))
    assert st["control_key_fingerprint"] is None and st["key_changed"] is False
    assert fc.load_state()["audit"][-1]["actor"] == "local admin manager"
    st = _checkin()
    assert st["control_key_fingerprint"] == fc.fingerprint(raw_b) and st["policy_version"] == 2
    assert st["paused"] is True


def test_env_key_overrides_trust_on_first_use(vps, monkeypatch):
    priv_a, raw_a, pub_a = _key()
    priv_b, _, pub_b = _key()
    monkeypatch.setattr(settings, "FLEET_CONTROL_PUBLIC_KEY", pub_a)
    vps.answer.update(control_public_key=pub_b, policy=_sign(_policy(version=1), priv_b))
    st = _checkin()
    assert st["policy_version"] == 0 and st["control_key_source"] == "env"
    assert fc.load_state()["last_policy_error"] == "bad_signature"
    assert fc.load_state()["control_public_key"] is None       # never pinned while the env key is set
    vps.answer.update(control_public_key=pub_a, policy=_sign(_policy(version=1), priv_a))
    assert _checkin()["policy_version"] == 1


# --------------------------------------------------------------------------- policy rules

def test_policy_versions_must_increase_and_name_this_store(vps):
    priv, _, pub = _key()
    vps.answer.update(control_public_key=pub, policy=_sign(_policy(version=5), priv))
    assert _checkin()["policy_version"] == 5

    vps.answer["policy"] = _sign(_policy(version=4, state="suspended"), priv)       # older: ignored
    assert _checkin()["paused"] is False
    vps.answer["policy"] = _sign(_policy(version=5, state="suspended"), priv)       # same: ignored
    assert _checkin()["paused"] is False
    vps.answer["policy"] = _sign(_policy(version=6, store_id="otherstore", state="suspended"), priv)
    st = _checkin()
    assert st["paused"] is False and st["policy_version"] == 5
    assert fc.load_state()["last_policy_error"] == "wrong_store"
    vps.answer["policy"] = _sign(_policy(version=6, features={"heatmaps": "yes"}), priv)
    _checkin()
    assert fc.load_state()["last_policy_error"] == "bad_features"
    # Missing feature keys are on (forward compatible), unknown ones ignored.
    vps.answer["policy"] = _sign(_policy(version=7, features={"heatmaps": False, "teleport": False}), priv)
    st = _checkin()
    assert st["policy_version"] == 7 and st["features"]["heatmaps"] is False
    assert all(v for k, v in st["features"].items() if k != "heatmaps")


def test_no_policy_means_everything_on_and_nothing_paused():
    assert ent.stored_policy() is None
    assert ent.is_paused() is False
    assert all(ent.feature_enabled(k) for k in ent.FEATURES)
    assert ent.max_cameras() is None
    ent.ensure_camera_room(1000)                               # no limit, no error


def test_licence_status_by_date():
    p = _policy(licence={"valid_until": "2027-10-31", "grace_days": 14})
    assert ent.licence_info(p, date(2027, 10, 31))["status"] == "ok"
    assert ent.licence_info(p, date(2027, 11, 1))["status"] == "warning"
    assert ent.licence_info(p, date(2027, 11, 14))["status"] == "warning"
    info = ent.licence_info(p, date(2027, 11, 15))
    assert info["status"] == "expired" and info["grace_until"] == "2027-11-14"
    assert ent.licence_info(_policy(), date(2099, 1, 1))["status"] == "ok"     # no end date
    with pytest.raises(ent.PolicyError):
        ent.normalise_policy(_policy(licence={"valid_until": "31/10/2027"}))

    _store_policy(_policy(licence={"valid_until": "2027-10-31", "grace_days": 0}))
    assert ent.effective_state(date(2027, 11, 1))["paused"] is True
    assert ent.effective_state(date(2027, 11, 1))["paused_reason"] == "licence_expired"
    assert ent.effective_state(date(2027, 10, 1))["paused"] is False


# --------------------------------------------------------------------------- commands

def test_commands_are_verified_answered_once_and_expire(vps):
    priv, _, pub = _key()
    vps.answer.update(control_public_key=pub, commands=[
        _sign(_command("checkin_now", "c_one"), priv),
        _sign(_command("checkin_now", "c_old", minutes=-1), priv),            # expired
        _sign(_command("reboot", "c_shell"), priv),                           # not on the allow-list
        _sign(_command("checkin_now", "c_far", store_id="otherstore"), priv),  # another store
        _sign(_command("run_health_check", "c_hc"), priv),
        {"payload": _sign(_command("checkin_now", "c_forged"), priv)["payload"], "sig": "AAAA"},
    ])
    _checkin()
    results = {r["id"]: r for r in fc.load_state()["pending_results"]}
    assert results["c_one"]["status"] == "done"
    assert results["c_old"]["status"] == "rejected" and results["c_old"]["detail"] == "expired"
    assert results["c_shell"]["detail"] == "action_not_allowed"
    assert results["c_far"]["detail"] == "wrong_store"
    assert results["c_hc"]["status"] == "done"
    assert "c_forged" not in results                           # bad signature: not trusted enough to answer

    # The same commands again (replay): nothing is run or answered twice; the
    # earlier results go out with this check-in and are then cleared.
    _checkin()
    sent = {r["id"] for r in vps.body()["command_results"]}
    assert {"c_one", "c_old", "c_shell", "c_far", "c_hc"} <= sent
    assert fc.load_state()["pending_results"] == []
    assert set(fc.load_state()["seen_command_ids"]) >= {"c_one", "c_hc"}


def test_restart_command_persists_its_result_then_exits(vps, monkeypatch, _clean_fleet):
    monkeypatch.setattr(fc, "RESTART_COMMAND_DELAY_S", 0.0)
    priv, _, pub = _key()
    vps.answer.update(control_public_key=pub, commands=[_sign(_command("restart_app", "c_rst"), priv)])

    async def run():
        fc.fleet_control._started = True
        await fc.fleet_control.checkin_now()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert _clean_fleet == [True]                              # the process would exit now
    pending = fc.load_state()["pending_results"]
    assert [(r["id"], r["status"]) for r in pending] == [("c_rst", "done")]  # sent after the restart


def test_plan_change_restarts_only_for_boot_time_services(vps, monkeypatch, _clean_fleet):
    monkeypatch.setattr(fc, "RESTART_DEBOUNCE_S", 0.0)
    priv, _, pub = _key()

    async def run(policy):
        fc.fleet_control._started = True
        vps.answer.update(control_public_key=pub, policy=_sign(policy, priv))
        await fc.fleet_control.checkin_now()
        await asyncio.sleep(0.05)
        fc.fleet_control._restart_task = None

    fc.fleet_control.record_boot()
    asyncio.run(run(_policy(version=1, features={"theft_detection": False})))   # live gate: no restart
    assert _clean_fleet == []
    asyncio.run(run(_policy(version=2, state="suspended", message="Account on hold")))
    assert _clean_fleet == [True]


# --------------------------------------------------------------------------- API gates

def test_suspended_box_answers_423_except_auth_health_and_fleet_status():
    _store_policy(_policy(state="suspended", message="Account on hold - call iKorex"))
    c = TestClient(app)
    r = c.get("/api/v1/theft/incidents")
    assert r.status_code == 423
    assert r.json()["code"] == "service_paused" and r.json()["message"] == "Account on hold - call iKorex"
    assert c.get("/api/v1/cameras").status_code == 423
    assert c.get("/stream").status_code == 423
    assert c.get("/api/v1/auth/status").status_code == 200
    assert c.get("/api/v1/health").status_code != 423
    st = c.get("/api/v1/fleet/status")
    assert st.status_code == 200 and st.json()["paused"] is True
    assert "token" not in json.dumps(st.json()).lower().replace("control_key", "")
    assert c.get("/sw.js").status_code == 200
    assert c.get("/dashboard").status_code == 200              # the page loads and shows "paused"


def test_expired_licence_pauses_by_date():
    _store_policy(_policy(licence={"valid_until": "2020-01-31", "grace_days": 14}))
    r = TestClient(app).get("/api/v1/theft/incidents")
    assert r.status_code == 423 and "licence" in r.json()["message"].lower()
    assert ent.feature_enabled("online_access") is True        # the tunnel stays up while paused
    assert ent.feature_enabled("remote_live_video") is False


def test_features_not_in_the_plan_return_403_and_stop_their_alerts(monkeypatch):
    _store_policy(_policy(features={"theft_detection": False, "heatmaps": False, "zone_alerts": False,
                                    "online_access": False, "pos_integration": False}))
    c = TestClient(app)
    r = c.get("/api/v1/theft/incidents")
    assert r.status_code == 403 and r.json() == {**r.json(), "code": "feature_disabled",
                                                 "feature": "theft_detection"}
    r = c.get("/api/v1/analytics/heatmaps")
    assert r.status_code == 403 and r.json()["feature"] == "heatmaps"
    assert c.get("/api/v1/analytics/pos/till-key").json()["feature"] == "pos_integration"
    assert c.post("/api/v1/remote-access/verify").json()["feature"] == "online_access"
    assert c.get("/api/v1/remote-access").status_code == 200   # check-in credentials stay editable
    assert c.get("/api/v1/fleet/status").status_code == 200

    from app.services.alert_dispatcher import alert_dispatcher
    from app.services.feature_manager import feature_manager
    from app.services.remote_access_service import remote_access_service

    assert feature_manager.is_enabled("cam_any", "theft_detection") is False
    assert feature_manager.is_enabled("cam_any", "people_counting") is True
    report = asyncio.run(alert_dispatcher.dispatch("TRIPWIRE_ALERT", "WARNING", "Line crossed", "x",
                                                   {"camera_id": "cam_any"}))
    assert report["logged"] is False and "zone_alerts" in report["skipped"]
    monkeypatch.setattr(remote_access_service, "_settings", {**remote_access_service.settings, "enabled": True})
    run, reason = remote_access_service.should_run()
    assert run is False and "plan" in reason


def test_remote_live_video_off_refuses_webrtc_through_the_public_address(monkeypatch):
    _store_policy(_policy(features={"remote_live_video": False}))
    from app.services import public_exposure

    monkeypatch.setattr(settings, "AUTH_DISABLED", False)          # remote access needs real auth
    monkeypatch.setattr(public_exposure, "is_remote_request", lambda conn: True)
    r = TestClient(app).post("/api/v1/webrtc/sessions", json={"camera_id": "cam_x"})
    assert r.status_code == 403 and r.json()["feature"] == "remote_live_video", r.text
    monkeypatch.setattr(public_exposure, "is_remote_request", lambda conn: False)
    r = TestClient(app).post("/api/v1/webrtc/sessions", json={"camera_id": "cam_x"})
    assert r.status_code != 403 or r.json().get("feature") != "remote_live_video"


def test_camera_limit_refuses_more_cameras(monkeypatch):
    _store_policy(_policy(limits={"max_cameras": 2}))
    monkeypatch.setattr(ent, "enabled_camera_count", lambda: 2)
    with pytest.raises(ent.GateError) as exc:
        ent.ensure_camera_room(1)
    assert exc.value.status_code == 409 and exc.value.body["code"] == "camera_limit"
    r = TestClient(app).post("/api/v1/layout/devices/adopt", json={})
    assert r.status_code == 409 and r.json()["code"] == "camera_limit" and r.json()["limit"] == 2
    r = TestClient(app).put("/api/v1/cameras/enabled", json={"camera_ids": ["nope"], "enabled": True})
    assert r.status_code == 404                                # unknown camera: checked before the limit
    monkeypatch.setattr(ent, "enabled_camera_count", lambda: 1)
    ent.ensure_camera_room(1)


# --------------------------------------------------------------------------- telemetry + client

def _all_keys(obj, schema, path=""):
    """Every key path in obj, asserting each is allowed by schema."""
    if isinstance(obj, dict):
        assert isinstance(schema, dict), path
        for k, v in obj.items():
            assert k in schema, f"{path}.{k} is not an allowed telemetry field"
            _all_keys(v, schema[k], f"{path}.{k}")
    elif isinstance(obj, list):
        assert isinstance(schema, list), path
        for v in obj:
            _all_keys(v, schema[0], path + "[]")
    else:
        assert schema is None and (obj is None or isinstance(obj, (str, int, float, bool))), path


def test_telemetry_carries_only_allowed_fields_and_no_names(vps, monkeypatch):
    from app.services.live_analytics_engine import live_engine

    monkeypatch.setattr(live_engine, "status", lambda: {
        "running": True, "cameras_online": 1, "cameras_total": 2,
        "detector": {"available": True, "provider": "MIGraphXExecutionProvider", "model": "/models/rtmo.onnx"},
        "cameras": [{"camera_id": "cam_1", "name": "Secret Aisle Cam", "rtsp_url": "rtsp://10.0.0.9/x",
                     "status": "ONLINE"}],
    })
    _checkin()
    body = vps.body()
    _all_keys(body, fc.ALLOWED_BODY)
    text = json.dumps(body)
    for leak in ("Secret Aisle", "rtsp", "10.0.0.9", "store-token", "rtmo", "/models"):
        assert leak not in text
    assert body["version"] == 1 and body["store_id"] == STORE
    assert body["health"]["inference_provider"] == "MIGraphXExecutionProvider"
    assert set(body["usage"]) == {"alerts_24h", "incidents_24h", "users_total", "phones_registered"}
    assert set(body["health"]["errors"]) <= fc.ERROR_CODES
    # Formats the VPS accepts (anything else it drops silently).
    import re

    box = body["box"]
    assert re.fullmatch(r"sha256-[0-9a-f]{12}", box["hostname_hash"])
    assert box["os"] is None or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z .()_+-]{0,47}", box["os"])
    assert box["git_commit"] is None or re.fullmatch(r"[0-9a-f]{7,40}", box["git_commit"])
    assert all(re.fullmatch(r"[a-z0-9_]{1,48}", c) for c in body["health"]["errors"])
    assert body["health"]["services"]["pipeline"] in ("ok", "off", "error", "degraded", "paused")
    assert len(vps.requests[-1].content) <= fc.MAX_BODY_BYTES
    # Anything a future change adds outside the contract is dropped on the way out.
    assert fc.filter_allowed({"health": {"cpu_pct": 1, "camera_names": ["A"]}, "x": 1}, fc.ALLOWED_BODY) == \
        {"health": {"cpu_pct": 1}}


def test_checkin_client_headers_backoff_and_idle(vps, monkeypatch):
    st = _checkin()
    req = vps.requests[-1]
    assert req.method == "POST" and str(req.url) == TARGET["url"]
    assert req.headers["authorization"] == "Bearer store-token-abc"
    assert req.headers["x-store-id"] == STORE and req.headers["content-type"] == "application/json"
    assert st["last_result"] == "ok" and st["last_ok_at"] and fc.fleet_control._failures == 0
    assert fc.fleet_control._next_s == 300.0

    vps.status = 503
    st = _checkin()
    assert st["last_result"] == "failed" and "503" in st["last_error"] and fc.fleet_control._failures == 1
    _checkin()
    assert fc.fleet_control._failures == 2                     # loop backs off 60 s -> 120 s ... 15 min

    monkeypatch.setattr(fc.FleetControl, "target", staticmethod(lambda: {"problem": "No tunnel server set."}))
    n = len(vps.requests)
    st = _checkin()
    assert st["configured"] is False and st["last_result"] == "not configured" and len(vps.requests) == n


def test_target_needs_wss_server_store_id_and_token(monkeypatch):
    from app.services import remote_access_service as ras

    monkeypatch.setattr(ras, "load_settings", lambda: {"server_url": "", "store_id": ""})
    assert "problem" in fc.FleetControl.target()
    monkeypatch.setattr(ras, "load_settings", lambda: {"server_url": "wss://tunnel.example.com",
                                                       "store_id": STORE, "enabled": False})
    monkeypatch.setattr(ras, "token_configured", lambda: True)
    monkeypatch.setattr(ras, "_read_token", lambda: "tok")
    t = fc.FleetControl.target()
    assert t["url"] == "https://tunnel.example.com/control/v1/checkin"   # works with online access off
    assert t["store_id"] == STORE and t["token"] == "tok"


def test_dev_checkin_url_is_loopback_only(monkeypatch):
    # FLEET_CONTROL_DEV_URL (developer / QA runs against a local cctv-control)
    # is honoured only for a loopback host; anything else is ignored.
    monkeypatch.delenv(fc.DEV_URL_ENV, raising=False)
    assert fc.dev_checkin_url() is None
    for good, want in (("http://127.0.0.1:9277", "http://127.0.0.1:9277/control/v1/checkin"),
                       ("http://localhost:9200/", "http://localhost:9200/control/v1/checkin"),
                       ("http://[::1]:9200", "http://[::1]:9200/control/v1/checkin")):
        monkeypatch.setenv(fc.DEV_URL_ENV, good)
        assert fc.dev_checkin_url() == want
    for bad in ("http://10.0.0.5:9200", "http://tunnel.example.com", "ftp://127.0.0.1", "http://127.0.0.1.evil.com",
                "http://user@evil.com#@127.0.0.1", "not a url"):
        monkeypatch.setenv(fc.DEV_URL_ENV, bad)
        assert fc.dev_checkin_url() is None, bad
