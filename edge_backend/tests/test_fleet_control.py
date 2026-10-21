"""iKorex fleet control, box side (docs/FLEET_CONTROL_CONTRACT.md).

Signed policy / command verification (Ed25519, pinned on first use), replay
and expiry rules, licence dates, the 423 paused gate, 403 feature gates, the
camera limit, the telemetry allow-list, the check-in client, and the offline
standby rule (signed check-in acks, grace, resume, clocks; owner decision
2026-10-21). The VPS is an httpx.MockTransport; nothing leaves this machine.
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
        # Set to an Ed25519 private key to answer with a signed ack (like cctv-control);
        # ack_edit(ack) may change the ack before it is signed; ack_envelope overrides it.
        self.signer = None
        self.ack_edit = None
        self.ack_envelope = None
        self.policy_version = None
        self.acks = []

    def _policy_version(self) -> int:
        if self.policy_version is not None:
            return self.policy_version
        env = self.answer.get("policy")
        if env:
            return json.loads(fc.b64url_decode(env["payload"]))["version"]
        return 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        answer = dict(self.answer, server_time=fc.iso())
        if self.ack_envelope is not None:
            answer["ack"] = self.ack_envelope
        elif self.signer is not None:
            ack = {"type": "ack", "store_id": STORE, "nonce": json.loads(request.content).get("nonce"),
                   "server_time": answer["server_time"], "policy_version": self._policy_version()}
            if self.ack_edit:
                self.ack_edit(ack)
            answer["ack"] = _sign(ack, self.signer)
            self.acks.append(answer["ack"])
        return httpx.Response(200, json=answer)

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


# --------------------------------------------------------------------------- version

def test_this_release_is_reported_everywhere(vps):
    assert settings.APP_VERSION == "2.7.0" and settings.VERSION == settings.APP_VERSION
    _checkin()
    assert vps.body()["box"]["software_version"] == "2.7.0"
    c = TestClient(app)
    assert c.get("/api/v1/health").json()["version"] == "2.7.0"
    st = c.get("/api/v1/fleet/status").json()
    assert st["software_version"] == "2.7.0"                    # Settings > iKorex service card


# --------------------------------------------------------------------------- offline standby

class Clocks:
    """The three clocks entitlements counts offline time with."""

    def __init__(self):
        self.wall = 1_900_000_000.0
        self.boot = 5_000.0
        self.boot_id = "11111111-1111-4111-8111-111111111111"

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.boot += seconds

    def reboot(self, uptime: float = 60.0, wall_delta: float = 60.0) -> None:
        self.boot_id = "22222222-2222-4222-8222-222222222222" if self.boot_id[0] == "1" else \
            "11111111-1111-4111-8111-111111111111"
        self.boot = uptime
        self.wall += wall_delta


@pytest.fixture()
def clocks(monkeypatch):
    c = Clocks()
    monkeypatch.setattr(ent, "wall_clock", lambda: c.wall)
    monkeypatch.setattr(ent, "boot_clock", lambda: c.boot)
    monkeypatch.setattr(ent, "boot_id", lambda: c.boot_id)
    return c


H = 3600.0


def _grace_box(vps, hours=2, version=1):
    """A box on a policy with an offline limit, with one verified contact."""
    priv, raw, pub = _key()
    vps.signer = priv
    vps.answer.update(control_public_key=pub,
                      policy=_sign(_policy(version=version, licence={"valid_until": None, "grace_days": 14,
                                                                     "offline_grace_hours": hours}), priv))
    st = _checkin()
    assert st["offline_grace_hours"] == hours and st["last_ack_error"] is None, st
    vps.answer["policy"] = None
    return priv, raw, pub


def test_offline_grace_hours_is_validated():
    for good in (None, 1, 72, 2160):
        p = ent.normalise_policy(_policy(licence={"valid_until": None, "grace_days": 14, "offline_grace_hours": good}))
        assert p["licence"]["offline_grace_hours"] == good
    assert ent.normalise_policy(_policy())["licence"]["offline_grace_hours"] is None   # missing = never
    for bad in (0, 2161, -1, "24", True, 1.5):
        with pytest.raises(ent.PolicyError) as exc:
            ent.normalise_policy(_policy(licence={"valid_until": None, "offline_grace_hours": bad}))
        assert exc.value.code == "bad_offline_grace_hours"


def test_ack_verification_rules():
    priv, raw, _ = _key()
    nonce = fc.new_nonce()
    assert len(nonce) == 22 and nonce != fc.new_nonce()
    now = datetime.now(timezone.utc)

    def ack(**kw):
        a = {"type": "ack", "store_id": STORE, "nonce": nonce, "server_time": fc.iso(now), "policy_version": 3}
        a.update(kw)
        return _sign(a, priv)

    ok = fc.verify_ack(ack(), raw, STORE, nonce, now)
    assert ok["policy_version"] == 3 and ok["skew_s"] is None

    def code(envelope, key=raw, store=STORE, n=nonce):
        with pytest.raises(fc.Rejected) as exc:
            fc.verify_ack(envelope, key, store, n, now)
        return exc.value.code

    assert code(None) == "ack_missing"
    assert code(ack(), key=None) == "no_control_key"
    assert code(ack(nonce=fc.new_nonce())) == "wrong_nonce"
    assert code(ack(), n=fc.new_nonce()) == "wrong_nonce"             # a replayed ack: last check-in's nonce
    assert code(ack(store_id="otherstore")) == "wrong_store"
    assert code(ack(type="policy")) == "not_an_ack"
    assert code(ack(server_time="yesterday")) == "bad_server_time"
    assert code(ack(policy_version=-1)) == "bad_policy_version"
    assert code(ack(policy_version=True)) == "bad_policy_version"
    other, _, _ = _key()
    assert code(_sign({"type": "ack", "store_id": STORE, "nonce": nonce, "server_time": fc.iso(now),
                       "policy_version": 3}, other)) == "bad_signature"   # a fake VPS with its own key
    good = ack()
    assert code({"payload": good["payload"], "sig": fc.b64url_encode(b"x" * 64)}) == "bad_signature"
    assert code({"payload": good["payload"]}) == "bad_encoding"           # unsigned
    # A clock more than 10 min off: still accepted (the nonce proves freshness), skew reported.
    stale = fc.verify_ack(ack(server_time=fc.iso(now - timedelta(hours=2))), raw, STORE, nonce, now)
    assert 7190 <= stale["skew_s"] <= 7210
    assert fc.verify_ack(ack(server_time=fc.iso(now - timedelta(minutes=9))), raw, STORE, nonce, now)["skew_s"] is None


def test_verified_ack_records_contact_and_bad_acks_never_count(vps, clocks):
    priv, raw, pub = _key()
    vps.answer.update(control_public_key=pub)
    st = _checkin()                                             # no ack (an older VPS)
    assert st["last_verified_contact_at"] is None and st["last_ack_error"] == "ack_missing"
    vps.signer = priv
    st = _checkin()
    assert st["last_verified_contact_at"] and st["last_ack_error"] is None
    state = fc.load_state()
    assert state["contact_anchor"]["source"] == "ack" and state["contact_anchor"]["boot_id"] == clocks.boot_id
    nonces = [json.loads(r.content)["nonce"] for r in vps.requests]
    assert all(len(n) == 22 for n in nonces) and len(set(nonces)) == len(nonces)   # fresh every check-in
    first = st["last_verified_contact_at"]

    clocks.advance(H)
    for edit, why in ((lambda a: a.update(nonce=fc.new_nonce()), "wrong_nonce"),
                      (lambda a: a.update(store_id="otherstore"), "wrong_store")):
        vps.ack_edit = edit
        st = _checkin()
        assert st["last_ack_error"] == why and fc.load_state()["contact_anchor"]["source"] == "ack"
        assert st["last_verified_contact_age_s"] == int(H)       # the clock did not restart
    vps.ack_edit = None
    vps.ack_envelope = vps.acks[0]                              # replay of an earlier, validly signed ack
    assert _checkin()["last_ack_error"] == "wrong_nonce"
    other, _, _ = _key()
    vps.ack_envelope = _sign({"type": "ack", "store_id": STORE, "nonce": "x" * 22, "server_time": fc.iso(),
                              "policy_version": 0}, other)
    assert _checkin()["last_ack_error"] == "bad_signature"
    vps.ack_envelope = {"payload": vps.acks[0]["payload"]}
    assert _checkin()["last_ack_error"] == "bad_encoding"
    assert fc.load_state()["last_verified_contact_at"] == first

    # Telemetry: standby flag and the age of the last verified contact (allowed fields only).
    body = vps.body()
    assert body["policy"]["standby"] is False and body["policy"]["last_verified_contact_age_s"] == int(H)
    assert len(body["nonce"]) == 22
    _all_keys(body, fc.ALLOWED_BODY)


def test_clock_skew_is_accepted_recorded_and_reported(vps, clocks):
    priv, _, pub = _key()
    vps.signer = priv
    vps.answer.update(control_public_key=pub)
    server = datetime.now(timezone.utc) - timedelta(hours=2)
    vps.ack_edit = lambda a: a.update(server_time=fc.iso(server))
    st = _checkin()
    assert st["last_ack_error"] is None and 7190 <= st["clock_skew_s"] <= 7210
    assert st["last_verified_contact_at"] == fc.iso(server)     # the server time is used for the record
    _checkin()
    assert "clock_skew" in vps.body()["health"]["errors"]
    vps.ack_edit = None
    st = _checkin()
    assert st["clock_skew_s"] is None
    _checkin()
    assert "clock_skew" not in vps.body()["health"]["errors"]


def test_no_standby_without_an_offline_limit(vps, clocks):
    priv, _, pub = _key()
    vps.signer = priv
    vps.answer.update(control_public_key=pub, policy=_sign(_policy(version=1), priv))
    _checkin()
    vps.status = 503
    clocks.advance(1000 * H)
    st = _checkin()
    assert st["standby"] is False and st["paused"] is False and st["standby_warning"] is False
    assert st["offline_grace_hours"] is None and st["standby_in_s"] is None
    assert st["last_verified_contact_age_s"] == int(1000 * H)
    assert TestClient(app).get("/api/v1/cameras").status_code != 423


def test_standby_after_grace_warning_first_then_resume_only_after_verified_ack(vps, clocks, monkeypatch,
                                                                              _clean_fleet):
    monkeypatch.setattr(fc, "RESTART_DEBOUNCE_S", 0.0)
    priv, _, pub = _grace_box(vps, hours=4)
    fc.fleet_control.record_boot()
    vps.status = 503                                            # the store's internet goes down

    clocks.advance(2.9 * H)
    st = _checkin()
    assert st["standby"] is False and st["standby_warning"] is False      # 1.1 h of 4 h left: > 25 %
    clocks.advance(0.2 * H)
    st = fc.fleet_control.status()
    assert st["standby_warning"] is True and st["paused"] is False        # 0.9 h left: banner
    assert 0.85 * H <= st["standby_in_s"] <= 0.95 * H
    assert abs(fc.fleet_control._cap_for_standby(10 * H) - (st["standby_in_s"] + 1)) <= 1  # wakes on time
    assert fc.fleet_control._cap_for_standby(60.0) == 60.0

    clocks.advance(1.0 * H)                                     # past the 4 h limit
    st = _checkin()
    assert st["standby"] is True and st["paused"] is True and st["paused_reason"] == "standby"
    assert st["standby_warning"] is False
    assert st["message"] == ent.STANDBY_MESSAGE
    assert ent.feature_enabled("theft_detection") is False and ent.feature_enabled("online_access") is True
    assert ent.is_paused() is True and fc.fleet_control.needs_restart() is True   # the existing pause path
    c = TestClient(app)
    r = c.get("/api/v1/cameras")
    assert r.status_code == 423 and r.json()["code"] == "service_standby"
    assert r.json()["message"] == ("Standby: this box can't reach iKorex. It will restart automatically once "
                                   "the connection is back and verified.")
    assert c.get("/api/v1/fleet/status").json()["standby"] is True
    assert c.get("/api/v1/auth/status").status_code == 200
    # Back online, but nothing verifiable: stays in standby.
    vps.status = 200
    vps.ack_envelope = {"payload": "e30", "sig": "AAAA"}
    assert _checkin()["standby"] is True
    assert vps.body()["policy"]["standby"] is True and vps.body()["health"]["pipeline_state"] == "paused"
    vps.ack_envelope = None
    vps.ack_edit = lambda a: a.update(nonce=fc.new_nonce())
    assert _checkin()["standby"] is True
    vps.ack_edit = None

    async def verified():
        fc.fleet_control._started = True
        fc.fleet_control.boot = {"paused": True, "features": {k: True for k in ent.FEATURES}}  # booted in standby
        st = await fc.fleet_control.checkin_now()               # e.g. "Check connection now"
        await asyncio.sleep(0.05)
        return st

    st = asyncio.run(verified())
    assert st["standby"] is False and st["paused"] is False and st["last_ack_error"] is None
    assert _clean_fleet == [True]                               # resumed through the debounced restart
    assert fc.load_state()["audit"][-1]["action"] == "standby lifted: verified contact"


def test_resume_waits_for_the_new_policy_to_apply(vps, clocks):
    priv, _, pub = _grace_box(vps, hours=1)
    clocks.advance(2 * H)
    assert fc.fleet_control.status()["standby"] is True
    # iKorex now holds v2 but sends it in a form this box refuses: not in step, not verified.
    vps.answer["policy"] = _sign(_policy(version=2, features={"heatmaps": "maybe"}), priv)
    st = _checkin()
    assert st["standby"] is True and st["last_ack_error"] == "policy_not_applied"
    vps.answer["policy"] = _sign(_policy(version=3, licence={"valid_until": None, "grace_days": 14,
                                                              "offline_grace_hours": 1}), priv)
    st = _checkin()
    assert st["standby"] is False and st["policy_version"] == 3


def test_standby_survives_a_restart_and_clocks_set_back(vps, clocks):
    _grace_box(vps, hours=2)
    vps.status = 503
    clocks.advance(1.5 * H)
    _checkin()                                                  # persists 1.5 h as the floor
    assert fc.load_state()["contact_anchor"]["floor_s"] == pytest.approx(1.5 * H, abs=1)

    # Same boot, wall clock set back a week: the boot clock still counts.
    clocks.wall -= 7 * 24 * H
    clocks.boot += 0.6 * H
    ent.invalidate()
    assert ent.effective_state()["standby"] is True

    # Rebooted with the clock set back before the contact: never less than the floor or this boot's uptime.
    clocks.reboot(uptime=120.0, wall_delta=-30 * 24 * H)
    ent.invalidate()
    st = ent.effective_state()
    assert st["standby"] is False and st["offline"]["elapsed_s"] == int(1.5 * H + 120)  # floor + uptime
    clocks.advance(0.6 * H)                                     # running time always counts
    ent.invalidate()
    assert ent.effective_state()["standby"] is True
    assert fc.fleet_control.record_boot()["paused"] is True     # a restart in standby starts paused

    # An ordinary reboot (wall clock right): the wall clock counts the time switched off.
    priv, _, pub = _key()
    vps.status = 200
    vps.signer = priv
    vps.answer.update(control_public_key=None)
    asyncio.run(fc.fleet_control.reset_key("local admin test"))
    assert fc.fleet_control.status()["standby"] is True         # resetting the key does not lift standby
    vps.answer.update(control_public_key=pub)
    assert _checkin()["standby"] is False                       # verified with the newly pinned key
    clocks.reboot(uptime=60.0, wall_delta=3 * H)
    ent.invalidate()
    assert ent.effective_state()["standby"] is True


def test_changed_control_key_never_lifts_standby(vps, clocks):
    _grace_box(vps, hours=1)
    clocks.advance(2 * H)
    priv_b, _, pub_b = _key()                                   # a fake VPS with its own key
    vps.signer = priv_b
    vps.answer.update(control_public_key=pub_b)
    st = _checkin()
    assert st["key_changed"] is True and st["standby"] is True and st["last_ack_error"] == "control_key_changed"


def test_grace_counts_from_the_first_offline_limit_without_any_verified_contact(vps, clocks):
    priv, _, pub = _key()                                       # an older VPS: policy, but no acks
    vps.answer.update(control_public_key=pub,
                      policy=_sign(_policy(version=1, licence={"valid_until": None, "offline_grace_hours": 3}),
                                   priv))
    st = _checkin()
    assert st["last_verified_contact_at"] is None and st["last_ack_error"] == "ack_missing"
    assert fc.load_state()["contact_anchor"]["source"] == "policy"
    clocks.advance(2 * H)
    assert fc.fleet_control.status()["standby"] is False
    clocks.advance(1.1 * H)
    assert fc.fleet_control.status()["standby"] is True


def test_banner_threshold():
    pol = _policy(licence={"valid_until": None, "offline_grace_hours": 100})
    anchor = {"box_at": 0.0, "boot_id": None, "boottime_s": None, "floor_s": 0.0}

    def at(hours):
        return ent.offline_info(pol, {"contact_anchor": dict(anchor, floor_s=hours * H)})

    import unittest.mock as um

    with um.patch.object(ent, "wall_clock", lambda: 0.0), um.patch.object(ent, "boot_id", lambda: None):
        assert at(74)["warning"] is False and at(74)["remaining_s"] == int(26 * H)
        assert at(76)["warning"] is True and at(76)["standby"] is False
        assert at(100)["standby"] is False and at(100.01)["standby"] is True and at(101)["warning"] is False
        assert ent.offline_info(_policy(), {"contact_anchor": dict(anchor, floor_s=500 * H)})["standby"] is False


# Produced by the VPS's own module (cctv_control/signing.py, same key as VPS_VECTOR):
# ControlKey(...).sign({"type": "ack", "store_id": "pearcedale", "nonce": ..., "server_time": ...,
# "policy_version": 7}), exactly as server.checkin_response builds it.
VPS_ACK_VECTOR = {
    "nonce": "q83vEjRWeJq8EjRWeJq83w",
    "envelope": {
        "payload": ("eyJub25jZSI6InE4M3ZFalJXZUpxOEVqUldlSnE4M3ciLCJwb2xpY3lfdmVyc2lvbiI6Nywic2VydmVyX3RpbWUiOiIy"
                    "MDI2LTEwLTIxVDEwOjAwOjAwWiIsInN0b3JlX2lkIjoicGVhcmNlZGFsZSIsInR5cGUiOiJhY2sifQ"),
        "sig": "FdZYddBHWHnZxgZxO9xvTGAnuzzW7umatIuydKnu6JX-2S47GUe2gtZ4gKq17xER0QtiTBgxBEMCQartTC0wDg",
    },
}


def test_ack_signed_by_the_vps_module_verifies():
    raw = fc.decode_public_key(VPS_VECTOR["key"])
    now = datetime(2026, 10, 21, 10, 3, tzinfo=timezone.utc)
    ack = fc.verify_ack(VPS_ACK_VECTOR["envelope"], raw, "pearcedale", VPS_ACK_VECTOR["nonce"], now)
    assert ack["policy_version"] == 7 and ack["skew_s"] is None
    assert ack["server_time"] == datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(fc.Rejected):
        fc.verify_ack(VPS_ACK_VECTOR["envelope"], raw, "pearcedale", fc.new_nonce(), now)
