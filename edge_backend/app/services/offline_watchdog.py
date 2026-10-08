"""Store-offline alert: a dead man's switch held by the VPS.

A box that has lost power or internet cannot tell anyone. So, while it is
up, it hands the VPS a ready-made "store offline" Web Push request for every
alert phone, already encrypted to each phone and signed with this box's VAPID
key (``web_push.build_request``). The VPS keeps them, watches the store's tunnel
heartbeat, and POSTs them unchanged when the store has been silent for
``watchdog_offline_min`` minutes (default 15), once per outage. It never holds
a key and never sees the text. Contract: ``docs/OFFLINE_WATCHDOG_CONTRACT.md``.

* Upload: every 30 min (VAPID tokens are good for 24 h, so the stored
  requests stay valid through a long outage), shortly after a phone or roster
  change, and at start-up. Only ``wss://`` tunnel servers are supported; the
  upload authenticates with the store id and token frpc already uses.
* When the VPS reports that it sent an offline alert and the store is back,
  this box sends "back online (offline from HH:MM to HH:MM)" to the same
  phones, then acknowledges the alert on the next upload.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger("edge.offline_watchdog")

UPLOAD_INTERVAL_S = 30 * 60
POKE_DELAY_S = 5.0
START_DELAY_S = 20.0
RETRY_S = 120.0
HTTP_TIMEOUT_S = 15.0
VAPID_TTL_S = 23 * 3600 + 30 * 60
MAX_MESSAGES = 50

# Tests replace this with httpx.MockTransport; None = real network.
http_transport: Optional[httpx.AsyncBaseTransport] = None


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _local_hhmm(ts: Optional[int]) -> str:
    from app.services.timeutil import to_local, utc_from_ts

    if not ts:
        return "?"
    return to_local(utc_from_ts(ts)).strftime("%H:%M")


def _duration(seconds: int) -> str:
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


class OfflineWatchdog:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._wake_loop = None
        self._next_delay: Optional[float] = None
        self._pending_ack: Optional[str] = None
        self._handled_alerts: set = set()
        self.state: Dict[str, Any] = {"last_upload_at": None, "last_ok_at": None, "result": "not run yet",
                                      "error": None, "phones": 0, "stored": None, "offline_alert": None}

    # ---- configuration
    @staticmethod
    def target() -> Dict[str, Any]:
        """Where and how to upload, or {"problem": ...}."""
        from app.services import remote_access_service as ras

        s = ras.remote_access_service.settings
        if not s.get("enabled"):
            return {"problem": "Online access is off, so the VPS cannot watch this store."}
        try:
            server = ras.parse_server_url(s.get("server_url"))
        except Exception as exc:
            return {"problem": f"Tunnel server address is not valid: {exc}"}
        if not server:
            return {"problem": "No tunnel server configured (Settings -> Online access)."}
        if server.get("protocol") != "wss":
            return {"problem": "The watchdog needs a wss:// tunnel server."}
        store_id = s.get("store_id") or ""
        token = ras._read_token() if ras.token_configured() else None
        if not store_id or not token:
            return {"problem": "Store ID or store token missing (Settings -> Online access)."}
        port = "" if server["port"] == 443 else f":{server['port']}"
        return {"url": f"https://{server['host']}{port}/watchdog/v1/bundle", "store_id": store_id,
                "token": token, "ca_file": ras.find_ca_bundle()}

    def status(self) -> Dict[str, Any]:
        from app.services.push_alerts import load_roster

        roster = load_roster()
        tgt = self.target()
        return {
            "enabled": bool(roster.get("watchdog_enabled")),
            "offline_min": roster.get("watchdog_offline_min"),
            "configured": "problem" not in tgt,
            "problem": tgt.get("problem"),
            **{k: v for k, v in self.state.items()},
        }

    # ---- bundle
    async def build_bundle(self, roster: Dict[str, Any], store_id: str) -> Dict[str, Any]:
        from app.services import device_identity, web_push
        from app.services.push_alerts import recipient_subscriptions

        enabled = bool(roster.get("watchdog_enabled"))
        minutes = int(roster.get("watchdog_offline_min") or 15)
        messages: List[Dict[str, Any]] = []
        subs = await recipient_subscriptions() if enabled else []
        store = device_identity.get_identity().get("device_name") or "The store"
        for sub in subs[:MAX_MESSAGES]:
            payload = {
                "v": 1, "kind": "offline", "title": f"{store}: CCTV offline",
                "body": (f"The CCTV box at {store} has not been in contact for {minutes} minutes. "
                         "Check its power and the store's internet. Theft alerts are not working until it is back."),
                "tag": "store-offline", "url": "/dashboard", "alert_id": "store-offline",
                "ts": int(time.time()),
            }
            try:
                req = web_push.build_request({"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth},
                                             payload, ttl=86400, urgency="high", topic="store-offline",
                                             vapid_ttl_s=VAPID_TTL_S)
            except web_push.WebPushError as exc:
                logger.warning("Watchdog message for %s not built: %s", sub.id, exc)
                continue
            messages.append({"endpoint": req["endpoint"], "headers": req["headers"],
                             "body_b64": base64.b64encode(req["body"]).decode("ascii"),
                             "not_after": req["not_after"]})
        self.state["phones"] = len(messages)
        return {"version": 1, "store_id": store_id, "enabled": enabled and bool(messages),
                "offline_after_s": minutes * 60, "messages": messages, "ack_alert_id": self._pending_ack}

    # ---- upload
    async def upload_now(self) -> Dict[str, Any]:
        from app.services.push_alerts import load_roster

        tgt = self.target()
        self.state["last_upload_at"] = _iso(time.time())
        if "problem" in tgt:
            self.state.update(result="not configured", error=tgt["problem"])
            return self.status()
        bundle = await self.build_bundle(load_roster(), tgt["store_id"])
        headers = {"Authorization": f"Bearer {tgt['token']}", "X-Store-Id": tgt["store_id"]}
        try:
            async with httpx.AsyncClient(transport=http_transport, timeout=HTTP_TIMEOUT_S,
                                         verify=tgt["ca_file"] or True) as client:
                resp = await client.post(tgt["url"], json=bundle, headers=headers)
        except httpx.HTTPError as exc:
            self.state.update(result="failed", error=f"VPS not reachable: {type(exc).__name__}")
            logger.warning("Watchdog upload failed: %s", self.state["error"])
            self._next_delay = RETRY_S
            return self.status()
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            reason = body.get("error") or (resp.text or "")[:120]
            self.state.update(result="refused", error=f"HTTP {resp.status_code}: {reason}")
            logger.warning("Watchdog upload refused: %s", self.state["error"])
            if resp.status_code >= 500 or resp.status_code == 404:
                self._next_delay = RETRY_S
            return self.status()
        if bundle["ack_alert_id"] and self._pending_ack == bundle["ack_alert_id"]:
            self._pending_ack = None
        self.state.update(result="stored" if bundle["enabled"] else "watchdog off", error=None,
                          last_ok_at=self.state["last_upload_at"], stored=body.get("stored"),
                          offline_alert=body.get("offline_alert"))
        logger.info("Watchdog bundle uploaded: %s message(s), offline after %d min",
                    body.get("stored"), bundle["offline_after_s"] // 60)
        await self._handle_alert(body.get("offline_alert"))
        return self.status()

    async def _handle_alert(self, alert: Optional[Dict[str, Any]]) -> None:
        """The VPS sent an offline alert: say "back online" once, then acknowledge it."""
        from app.services import device_identity
        from app.services.push_alerts import push_alerts, recipient_subscriptions

        if not isinstance(alert, dict) or not alert.get("id"):
            return
        alert_id = str(alert["id"])
        if not alert.get("back_online_at"):
            self._next_delay = RETRY_S  # the VPS has not seen us back yet
            return
        if alert_id not in self._handled_alerts:
            store = device_identity.get_identity().get("device_name") or "The store"
            since, back = int(alert.get("offline_since") or 0), int(alert["back_online_at"])
            body = (f"Offline from {_local_hhmm(since)} to {_local_hhmm(back)}"
                    f"{f' ({_duration(back - since)})' if since else ''}. Alerts are working again.")
            result = await push_alerts.send_notice("online", f"{store}: CCTV back online", body,
                                                   await recipient_subscriptions(), tag="store-offline")
            logger.info("Back-online notice for watchdog alert %s: %s", alert_id, result)
            self._handled_alerts.add(alert_id)
        self._pending_ack = alert_id
        self._next_delay = POKE_DELAY_S

    # ---- loop
    def _event(self) -> Optional[asyncio.Event]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._wake
        if self._wake_loop is not loop:
            self._wake = asyncio.Event()
            self._wake_loop = loop
        return self._wake

    def poke(self) -> None:
        """Upload soon (a phone or the roster changed)."""
        self._next_delay = POKE_DELAY_S
        ev = self._event()
        if ev is not None and self._wake_loop is not None:
            self._wake_loop.call_soon_threadsafe(ev.set)

    async def _loop(self) -> None:
        delay = START_DELAY_S
        while True:
            ev = self._event()
            try:
                await asyncio.wait_for(ev.wait(), timeout=delay)
                ev.clear()
                if self._next_delay:
                    await asyncio.sleep(self._next_delay)  # debounce a burst of changes
            except asyncio.TimeoutError:
                pass
            self._next_delay = None
            try:
                await self.upload_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watchdog upload crashed")
            delay = self._next_delay or UPLOAD_INTERVAL_S
            self._next_delay = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._event()
            self._task = asyncio.create_task(self._loop(), name="offline-watchdog-upload")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


offline_watchdog = OfflineWatchdog()
