"""Installed web app alerts: RFC 8291/8292 wire format, roster, escalation,
the notification's Acknowledge button, and the store-offline watchdog upload.

Push services are replaced with httpx.MockTransport; every captured body is
decrypted here with the phone's private key (the receiver side of RFC 8291),
so the tests check what a phone would really show.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings
from app.database import async_session_factory
from app.models.db_models import TheftIncidentModel
from app.services import auth_service as auth_mod
from app.services import offline_watchdog as ow
from app.services import push_alerts as pa
from app.services import web_push as wp
from app.services.alert_dispatcher import alert_dispatcher
from app.services.auth_service import intrusion_detector
from app.services.token_revocation import token_revocations
from app.routes import setup as setup_routes

from tests.test_auth_setup import _create_admin, _issued_code, client  # noqa: F401  (fixture)

OWNER_PW = "correct horse 1"
PW = "staff password 1"


# --------------------------------------------------------------------------- helpers

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(settings.DATABASE_PATH), timeout=15)


class Phone:
    """A browser's push subscription with its private key, to read what it receives."""

    def __init__(self, name: str, host: str = "fcm.googleapis.com"):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.pub = wp._public_bytes(self.priv.public_key())
        self.auth = uuid.uuid4().bytes
        self.endpoint = f"https://{host}/fcm/send/{name}-{uuid.uuid4().hex[:8]}"

    def subscription(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": wp.b64url(self.pub), "auth": wp.b64url(self.auth)}}

    def decrypt(self, body: bytes) -> dict:
        salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        as_pub = body[21:21 + idlen]
        assert rs == 4096 and idlen == 65
        mac = lambda k, d: hmac.new(k, d, hashlib.sha256).digest()  # noqa: E731
        secret = self.priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        ikm = mac(mac(self.auth, secret), b"WebPush: info\x00" + self.pub + as_pub + b"\x01")
        prk = mac(salt, ikm)
        cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class PushServer:
    """Stands in for every push service; answers 201, or 410 for endpoints marked gone."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.gone: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) in self.gone:
            return httpx.Response(410)
        return httpx.Response(201)

    def to(self, phone: Phone) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == phone.endpoint]


@pytest.fixture()
def push_server(monkeypatch):
    srv = PushServer()
    monkeypatch.setattr(wp, "http_transport", httpx.MockTransport(srv.handler))
    monkeypatch.setattr(settings, "CAMERA_ALERT_COOLDOWN_SEC", 0)
    pa.push_alerts.reset()
    with _db() as conn:
        for table in ("web_push_subscriptions", "push_alerts"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM system_setup WHERE key = ?", (pa.ROSTER_KEY,))
    yield srv
    with _db() as conn:
        for table in ("web_push_subscriptions", "push_alerts"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM system_setup WHERE key = ?", (pa.ROSTER_KEY,))


@pytest.fixture(autouse=True)
def _clean_auth_state():
    token_revocations.clear_cache()
    auth_mod.invalidate_account_cache()
    setup_routes._last_locked_login.clear()
    intrusion_detector.failed_attempts.clear()
    yield
    auth_mod.invalidate_account_cache()


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _staff(client):
    """Owner 'manager' + operators amy, ben, cat. Returns {username: (token, id)}."""
    owner = _create_admin(client, _issued_code(client), password=OWNER_PW).json()["access_token"]
    out = {}
    for name in ("amy", "ben", "cat"):
        r = client.post("/api/v1/users", headers=_h(owner),
                        json={"username": name, "display_name": name.title(), "role": "operator", "password": PW})
        assert r.status_code in (200, 201), r.text
    for name, pw in (("manager", OWNER_PW), ("amy", PW), ("ben", PW), ("cat", PW)):
        r = client.post("/api/v1/auth/login", json={"username": name, "password": pw})
        assert r.status_code == 200, r.text
        tok = r.json()["access_token"]
        out[name] = (tok, jwt.decode(tok, options={"verify_signature": False})["sub"])
    return out


def _subscribe(client, tok, phone: Phone, label=None):
    body = dict(phone.subscription())
    if label:
        body["label"] = label
    r = client.post("/api/v1/web-push/subscriptions", headers=_h(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_incident(conf=0.82) -> str:
    iid = f"theft_wp_{uuid.uuid4().hex[:8]}"

    async def _write():
        async with async_session_factory() as s:
            s.add(TheftIncidentModel(id=iid, theft_type="CONCEALMENT", severity="HIGH", status="ACTIVE",
                                     department="Test", camera_id="cam_wp", timestamp=datetime.utcnow(),
                                     confidence=conf, items_involved=[], rule="CONCEALMENT"))
            await s.commit()

    _run(_write())
    return iid


def _theft_alert(incident_id: str, conf: float) -> dict:
    return _run(alert_dispatcher.dispatch(
        "CONCEALMENT", "HIGH" if conf >= 0.75 else "WARNING", "Review: Concealment - Aisle 3",
        "Suspicious behaviour for staff review. Hand moved to pocket.",
        {"camera_id": "cam_wp", "incident_id": incident_id, "confidence": conf, "rule": "CONCEALMENT"}))


# --------------------------------------------------------------------------- wire format

def test_encryption_matches_rfc8291_example():
    d = wp.b64url_decode
    as_priv = ec.derive_private_key(int.from_bytes(d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                    ec.SECP256R1())
    out = wp.encrypt(d("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                     "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                     "BTBZMqHH6r4Tts7J_aSIgg", salt=d("DGv6ra1nlYgDCS1FRnbzlw"), as_private=as_priv)
    # RFC 8291 Appendix A: the 86-octet header, then the ciphertext (base64url each).
    assert out == (d("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
                   + d("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"))


def test_request_is_signed_with_the_boxs_vapid_key_and_readable_by_the_phone():
    phone = Phone("a")
    sub = wp.validate_subscription(phone.endpoint, wp.b64url(phone.pub), wp.b64url(phone.auth))
    req = wp.build_request(sub, {"title": "Hello", "body": "x"}, ttl=60, urgency="high", topic="store-offline!")
    assert req["headers"]["Content-Encoding"] == "aes128gcm"
    assert req["headers"]["Topic"] == "store-offline"
    assert phone.decrypt(req["body"]) == {"title": "Hello", "body": "x"}
    scheme, rest = req["headers"]["Authorization"].split(" ", 1)
    parts = dict(p.strip().split("=", 1) for p in rest.split(","))
    assert scheme == "vapid" and parts["k"] == wp.vapid_public_key()
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.b64url_decode(parts["k"]))
    claims = jwt.decode(parts["t"], pub, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["exp"] == req["not_after"]
    assert 0 < claims["exp"] - time.time() <= wp.MAX_VAPID_TTL_S
    assert claims["sub"].startswith(("https://", "mailto:"))


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",            # not https
    "https://evil.example.com/push",                   # not a push service
    "https://fcm.googleapis.com.evil.com/x",           # suffix trick
    "https://user:pw@fcm.googleapis.com/x",            # credentials
])
def test_subscription_endpoint_must_be_a_real_push_service(endpoint):
    phone = Phone("x")
    with pytest.raises(wp.WebPushError):
        wp.validate_subscription(endpoint, wp.b64url(phone.pub), wp.b64url(phone.auth))


def test_apple_and_mozilla_endpoints_are_accepted_and_bad_keys_refused():
    phone = Phone("x")
    for ep in ("https://web.push.apple.com/QHx", "https://updates.push.services.mozilla.com/wpush/v2/x"):
        assert wp.validate_subscription(ep, wp.b64url(phone.pub), wp.b64url(phone.auth))["endpoint"] == ep
    with pytest.raises(wp.WebPushError):
        wp.validate_subscription(phone.endpoint, wp.b64url(b"\x04" + b"\x01" * 64), wp.b64url(phone.auth))
    with pytest.raises(wp.WebPushError):
        wp.validate_subscription(phone.endpoint, wp.b64url(phone.pub), wp.b64url(b"short"))


def test_long_body_is_trimmed_to_fit_one_push():
    raw = wp.encode_payload({"title": "t", "body": "é" * 5000})
    assert len(raw) <= wp.MAX_PLAINTEXT and json.loads(raw)["body"].endswith("…")


# --------------------------------------------------------------------------- roster

def test_roster_needs_two_first_priority_people(client, push_server):
    staff = _staff(client)
    owner = staff["manager"][0]
    ids = {n: staff[n][1] for n in staff}
    r = client.put("/api/v1/web-push/roster", headers=_h(owner), json={"first_priority": [ids["amy"]]})
    assert r.status_code == 422 and "at least 2" in r.json()["detail"]
    r = client.put("/api/v1/web-push/roster", headers=_h(owner),
                   json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["ben"]]})
    assert r.status_code == 422 and "not both" in r.json()["detail"]
    r = client.put("/api/v1/web-push/roster", headers=_h(owner),
                   json={"first_priority": [ids["amy"], ids["ben"]], "min_confidence": 1.5})
    assert r.status_code == 422
    # An operator may read but not change it.
    assert client.get("/api/v1/web-push/roster", headers=_h(staff["amy"][0])).status_code == 200
    r = client.put("/api/v1/web-push/roster", headers=_h(staff["amy"][0]),
                   json={"first_priority": [ids["amy"], ids["ben"]]})
    assert r.status_code == 403
    r = client.put("/api/v1/web-push/roster", headers=_h(owner),
                   json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["cat"]],
                         "escalate_after_min": 3, "watchdog_offline_min": 15})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] and body["min_confidence"] == 0.75 and body["escalate_after_min"] == 3
    people = {p["username"]: p for p in body["people"]}
    assert people["amy"]["tier"] == "first_priority" and people["cat"]["tier"] == "backup"
    assert people["manager"]["tier"] == "none"


def test_unconfigured_roster_alerts_owners_and_admins_only(client, push_server):
    staff = _staff(client)
    owner_phone, amy_phone = Phone("owner"), Phone("amy")
    _subscribe(client, staff["manager"][0], owner_phone)
    _subscribe(client, staff["amy"][0], amy_phone)
    report = _theft_alert(_seed_incident(), 0.9)
    assert report["web_push"]["mode"] == "fallback" and report["web_push"]["sent"] == 1
    assert len(push_server.to(owner_phone)) == 1 and not push_server.to(amy_phone)


# --------------------------------------------------------------------------- threshold, escalation, ack

def test_threshold_escalation_and_acknowledge_from_the_notification(client, push_server):
    staff = _staff(client)
    ids = {n: staff[n][1] for n in staff}
    phones = {n: Phone(n) for n in ("amy", "ben", "cat")}
    sids = {n: _subscribe(client, staff[n][0], phones[n], label=f"{n} phone") for n in phones}
    r = client.put("/api/v1/web-push/roster", headers=_h(staff["manager"][0]),
                   json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["cat"]], "escalate_after_min": 5})
    assert r.status_code == 200, r.text

    # Below the phone level: recorded and on the dashboard, nobody woken.
    low = _theft_alert(_seed_incident(0.6), 0.6)
    assert low["web_push"]["pushed"] is False and "below" in low["web_push"]["skipped"]
    assert not push_server.requests

    incident = _seed_incident(0.82)
    report = _theft_alert(incident, 0.82)
    web = report["web_push"]
    assert web["mode"] == "roster" and web["sent"] == 2 and web["escalate_at"]
    msg = phones["amy"].decrypt(push_server.to(phones["amy"])[0].content)
    assert msg["kind"] == "alert" and msg["incident_id"] == incident
    assert msg["url"] == f"/dashboard?incident={incident}" and msg["ack"]["sid"] == sids["amy"]
    assert push_server.to(phones["amy"])[0].headers["Urgency"] == "high"
    assert not push_server.to(phones["cat"])

    # Nothing happens before the delay; after it, the backup phone gets it.
    assert _run(pa.push_alerts.tick())["escalated"] == 0
    with _db() as conn:
        conn.execute("UPDATE push_alerts SET escalate_at = ? WHERE incident_id = ?",
                     ((datetime.utcnow() - timedelta(seconds=1)).isoformat(sep=" "), incident))
    assert _run(pa.push_alerts.tick())["escalated"] == 1
    esc = phones["cat"].decrypt(push_server.to(phones["cat"])[0].content)
    assert esc["kind"] == "escalation" and esc["title"].startswith("Not acknowledged")
    assert _run(pa.push_alerts.tick())["escalated"] == 0          # only once

    # A forged token is refused; the real one acknowledges the incident.
    bad = client.post("/api/v1/web-push/ack", json={"alert_id": msg["alert_id"], "sid": sids["amy"], "token": "0" * 32})
    assert bad.status_code == 403
    other = client.post("/api/v1/web-push/ack", json={"alert_id": msg["alert_id"], "sid": sids["ben"],
                                                  "token": msg["ack"]["token"]})
    assert other.status_code == 403                                  # token is per phone
    before = len(push_server.requests)
    ok = client.post("/api/v1/web-push/ack", json={"alert_id": msg["alert_id"], "sid": sids["amy"],
                                               "token": msg["ack"]["token"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["acknowledged_by"] == "Amy"
    with _db() as conn:
        status, guard = conn.execute("SELECT status, guard_id FROM theft_incidents WHERE id = ?", (incident,)).fetchone()
        closed = conn.execute("SELECT closed_at, acknowledged_by FROM push_alerts WHERE incident_id = ?",
                              (incident,)).fetchone()
    assert status == "ACKNOWLEDGED" and guard == "Amy (phone)"
    assert closed[0] is not None and closed[1] == "Amy (phone)"
    # Everyone who got it sees "Acknowledged by", silently, on the same tag.
    handled = [r for r in push_server.requests[before:]]
    assert {str(r.url) for r in handled} == {phones[n].endpoint for n in ("amy", "ben", "cat")}
    note = phones["ben"].decrypt(push_server.to(phones["ben"])[-1].content)
    assert note["kind"] == "handled" and note["tag"] == msg["tag"] and "ack" not in note


def test_dashboard_acknowledgement_stops_escalation(client, push_server):
    staff = _staff(client)
    ids = {n: staff[n][1] for n in staff}
    phones = {n: Phone(n) for n in ("amy", "ben", "cat")}
    for n in phones:
        _subscribe(client, staff[n][0], phones[n])
    client.put("/api/v1/web-push/roster", headers=_h(staff["manager"][0]),
               json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["cat"]]})
    incident = _seed_incident()
    _theft_alert(incident, 0.9)
    r = client.post(f"/api/v1/theft/incidents/{incident}/acknowledge", headers=_h(staff["ben"][0]))
    assert r.status_code == 200, r.text
    with _db() as conn:
        conn.execute("UPDATE push_alerts SET escalate_at = ? WHERE incident_id = ?",
                     ((datetime.utcnow() - timedelta(seconds=1)).isoformat(sep=" "), incident))
    counts = _run(pa.push_alerts.tick())
    assert counts["acknowledged"] == 1 and counts["escalated"] == 0
    assert all(phones["cat"].decrypt(r.content)["kind"] != "escalation" for r in push_server.to(phones["cat"]))


def test_no_reachable_first_priority_phone_escalates_at_once(client, push_server):
    staff = _staff(client)
    ids = {n: staff[n][1] for n in staff}
    cat = Phone("cat")
    _subscribe(client, staff["cat"][0], cat)                        # amy and ben have no phone
    client.put("/api/v1/web-push/roster", headers=_h(staff["manager"][0]),
               json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["cat"]]})
    web = _theft_alert(_seed_incident(), 0.9)["web_push"]
    assert web["pushed"] is False and web["phones"] == 0
    assert _run(pa.push_alerts.tick())["escalated"] == 1
    esc = cat.decrypt(push_server.to(cat)[0].content)
    assert esc["kind"] == "escalation" and "No first-priority phone" in esc["body"]


def test_gone_subscription_is_removed(client, push_server):
    staff = _staff(client)
    phone = Phone("owner")
    sid = _subscribe(client, staff["manager"][0], phone)
    push_server.gone.add(phone.endpoint)
    _theft_alert(_seed_incident(), 0.9)
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_push_subscriptions WHERE id = ?", (sid,)).fetchone()[0] == 0


def test_phone_management_permissions(client, push_server):
    staff = _staff(client)
    amy_phone, ben_phone = Phone("amy"), Phone("ben")
    amy_sid = _subscribe(client, staff["amy"][0], amy_phone, label="Amy iPhone")
    ben_sid = _subscribe(client, staff["ben"][0], ben_phone)
    # Operators see only their own phones, with their endpoint ("this device").
    mine = client.get("/api/v1/web-push/subscriptions", headers=_h(staff["amy"][0])).json()["subscriptions"]
    assert [d["id"] for d in mine] == [amy_sid] and mine[0]["endpoint"] == amy_phone.endpoint
    every = client.get("/api/v1/web-push/subscriptions", headers=_h(staff["manager"][0])).json()["subscriptions"]
    assert {d["id"] for d in every} == {amy_sid, ben_sid} and all(d["endpoint"] is None for d in every)
    assert client.delete(f"/api/v1/web-push/subscriptions/{ben_sid}", headers=_h(staff["amy"][0])).status_code == 403
    assert client.post(f"/api/v1/web-push/subscriptions/{amy_sid}/test", headers=_h(staff["amy"][0])).json()["status"] == "sent"
    assert amy_phone.decrypt(push_server.to(amy_phone)[-1].content)["kind"] == "test"
    # Re-syncing without a name keeps the name; the same browser signing in as Ben moves to Ben.
    again = client.post("/api/v1/web-push/subscriptions", headers=_h(staff["ben"][0]), json=amy_phone.subscription())
    assert again.json()["id"] == amy_sid and again.json()["label"] == "Amy iPhone"
    with _db() as conn:
        assert conn.execute("SELECT user_id FROM web_push_subscriptions WHERE id = ?", (amy_sid,)).fetchone()[0] \
            == staff["ben"][1]
    assert client.delete(f"/api/v1/web-push/subscriptions/{ben_sid}", headers=_h(staff["manager"][0])).status_code == 200
    # A subscription to anything but a push service is refused.
    evil = Phone("x", host="evil.example.com")
    r = client.post("/api/v1/web-push/subscriptions", headers=_h(staff["amy"][0]), json=evil.subscription())
    assert r.status_code == 422


# --------------------------------------------------------------------------- watchdog upload

def test_watchdog_bundle_follows_the_contract_and_says_back_online(client, push_server, monkeypatch):
    staff = _staff(client)
    ids = {n: staff[n][1] for n in staff}
    phones = {n: Phone(n) for n in ("amy", "ben", "cat")}
    for n in phones:
        _subscribe(client, staff[n][0], phones[n])
    client.put("/api/v1/web-push/roster", headers=_h(staff["manager"][0]),
               json={"first_priority": [ids["amy"], ids["ben"]], "backup": [ids["cat"]],
                     "watchdog_offline_min": 20})
    monkeypatch.setattr(ow.OfflineWatchdog, "target", staticmethod(lambda: {
        "url": "https://tunnel.example.com/watchdog/v1/bundle", "store_id": "teststore",
        "token": "store-token-xyz", "ca_file": None}))

    uploads: list[dict] = []
    replies = [
        {"ok": True, "stored": 3, "offline_alert": None},
        {"ok": True, "stored": 3, "offline_alert": {"id": "a1b2c3", "offline_since": 1790990000,
                                                     "sent_at": 1790991200, "back_online_at": 1790994000,
                                                     "sent": 3, "failed": 0}},
        {"ok": True, "stored": 3, "offline_alert": None},
    ]

    def vps(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer store-token-xyz"
        assert request.headers["X-Store-Id"] == "teststore"
        uploads.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(uploads) - 1])

    monkeypatch.setattr(ow, "http_transport", httpx.MockTransport(vps))
    dog = ow.OfflineWatchdog()

    st = _run(dog.upload_now())
    assert st["result"] == "stored" and st["error"] is None
    b = uploads[0]
    assert b["version"] == 1 and b["store_id"] == "teststore" and b["enabled"] is True
    assert b["offline_after_s"] == 1200 and b["ack_alert_id"] is None and len(b["messages"]) == 3
    for m in b["messages"]:
        assert set(m) == {"endpoint", "headers", "body_b64", "not_after"}
        assert set(m["headers"]) <= {"Authorization", "Content-Encoding", "Content-Type", "TTL", "Urgency", "Topic"}
        assert m["headers"]["Topic"] == "store-offline" and m["headers"]["Urgency"] == "high"
        assert time.time() + 23 * 3600 <= m["not_after"] <= time.time() + 24 * 3600
    by_endpoint = {m["endpoint"]: m for m in b["messages"]}
    offline = phones["cat"].decrypt(base64.b64decode(by_endpoint[phones["cat"].endpoint]["body_b64"]))
    assert offline["kind"] == "offline" and "20 minutes" in offline["body"]
    assert not push_server.requests                                   # the box itself sent nothing

    # The VPS reports an outage that ended: one "back online" to every phone, then ack.
    _run(dog.upload_now())
    online = [phones[n].decrypt(push_server.to(phones[n])[-1].content) for n in phones]
    assert all(m["kind"] == "online" and "back online" in m["title"] for m in online)
    _run(dog.upload_now())
    assert uploads[2]["ack_alert_id"] == "a1b2c3"
    assert len(push_server.requests) == 3                             # not repeated


def test_watchdog_off_uploads_an_empty_disabled_bundle(client, push_server, monkeypatch):
    staff = _staff(client)
    ids = {n: staff[n][1] for n in staff}
    _subscribe(client, staff["amy"][0], Phone("amy"))
    client.put("/api/v1/web-push/roster", headers=_h(staff["manager"][0]),
               json={"first_priority": [ids["amy"], ids["ben"]], "watchdog_enabled": False})
    monkeypatch.setattr(ow.OfflineWatchdog, "target", staticmethod(lambda: {
        "url": "https://tunnel.example.com/watchdog/v1/bundle", "store_id": "s", "token": "t", "ca_file": None}))
    seen = []
    monkeypatch.setattr(ow, "http_transport", httpx.MockTransport(
        lambda req: (seen.append(json.loads(req.content)), httpx.Response(200, json={"ok": True, "stored": 0}))[1]))
    st = _run(ow.OfflineWatchdog().upload_now())
    assert seen[0]["enabled"] is False and seen[0]["messages"] == [] and st["result"] == "watchdog off"


def test_watchdog_reports_why_it_cannot_run(monkeypatch):
    monkeypatch.setattr(ow.OfflineWatchdog, "target", staticmethod(lambda: {"problem": "Online access is off."}))
    st = _run(ow.OfflineWatchdog().upload_now())
    assert st["result"] == "not configured" and st["configured"] is False


# --------------------------------------------------------------------------- web app files

def test_web_push_routes_do_not_collide_with_the_phone_app_push_routes():
    """routes/pairing.py owns /api/v1/push/* (FCM, the Flutter app). The web app API
    lives under /api/v1/web-push; both /config routes must answer as themselves."""
    from fastapi.testclient import TestClient

    from app.main import app

    tc = TestClient(app)                      # suite default: AUTH_DISABLED
    web = tc.get("/api/v1/web-push/config")
    assert web.status_code == 200 and web.json()["vapid_public_key"] == wp.vapid_public_key()
    fcm = tc.get("/api/v1/push/config")
    assert fcm.status_code == 200 and "vapid_public_key" not in fcm.json()


def test_service_worker_and_manifest_are_served_at_the_root(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert "no-cache" in sw.headers["cache-control"] and "showNotification" in sw.text
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    data = m.json()
    assert data["display"] == "standalone" and data["scope"] == "/" and data["start_url"].startswith("/dashboard")
    assert {i["purpose"] for i in data["icons"]} == {"any", "maskable"}
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200
    page = client.get("/dashboard").text
    assert 'rel="manifest"' in page and "phone_alerts.js" in page and 'id="settings-phone-alerts"' in page


def test_app_and_messages_are_named_after_the_store(client, monkeypatch):
    monkeypatch.setattr(settings, "STORE_NAME", "IGA Pearcedale")
    assert pa.store_label() == "IGA Pearcedale"
    data = client.get("/manifest.webmanifest").json()
    assert data["name"] == "IGA Pearcedale CCTV" and data["short_name"] == "IGA Pearcedale"
    monkeypatch.setattr(settings, "STORE_NAME", "Store")          # the unset default: device name instead
    assert pa.store_label() not in ("Store", "")
