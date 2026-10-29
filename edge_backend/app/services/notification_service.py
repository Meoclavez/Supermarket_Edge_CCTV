"""Loss-prevention alert entry points and the dashboard websocket hub.

An alert is a prompt for a staff member to look at something -- suspicious
behaviour for review, or a camera that stopped delivering video. It is never
an accusation, so pushes are high-priority and time-sensitive with the
platform's normal alert sound. There is deliberately no critical-alert
entitlement, no siren sound and no emergency-services wording.

The fan-out itself (alert log, websocket broadcast, per-phone push with
prefs, mute and cooldown) lives in :mod:`app.services.alert_dispatcher`.
This module keeps the websocket hub the dashboard subscribes to and the
historical ``notify_loss_prevention`` signature that pose analytics calls.

Phones are reached only through paired devices (``/api/v1/pairing``) and
Firebase Cloud Messaging HTTP v1; APNs is reached through FCM. The retired
legacy FCM server-key API and the unsigned direct-APNs path were removed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import WebSocket

from app.models.schemas import EventSeverity, EventType, SecurityEvent

logger = logging.getLogger("NotificationService")

APNS_CATEGORY = "LOSS_PREVENTION_ALERT"
FCM_CHANNEL_ID = "loss_prevention_alerts"


class AlertHub:
    """Connected dashboard and phone websockets that receive every alert as JSON.

    Each connection keeps the credential it was opened with. Before every
    message, and when :meth:`prune_revoked` runs after a sign-out, account
    change or phone revocation, that credential is checked again: a socket
    whose session ended is closed (1008) instead of streaming on. Evidence
    links in a message get a fresh clip token for that connection's session
    (services/evidence_urls.py); nothing stored carries a token.
    """

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._creds: Dict[int, Tuple[Optional[str], Optional[Dict[str, Any]]]] = {}

    async def connect(self, websocket: WebSocket, raw_token: Optional[str] = None,
                      payload: Optional[Dict[str, Any]] = None) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        self._creds[id(websocket)] = (raw_token, payload)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._creds.pop(id(websocket), None)

    def still_authorised(self, websocket: WebSocket) -> bool:
        raw, payload = self._creds.get(id(websocket), (None, None))
        if raw is None:
            return True  # AUTH_DISABLED, or opened without a credential by a test
        from app.services.auth_service import auth_service

        return auth_service.verify_session_token(raw) is not None

    async def prune_revoked(self) -> int:
        """Close every socket whose session no longer verifies. Returns how many."""
        closed = 0
        for connection in list(self.active_connections):
            if not self.still_authorised(connection):
                self.disconnect(connection)
                closed += 1
                try:
                    await connection.close(code=1008)
                except Exception:
                    pass
        return closed

    def _for_connection(self, websocket: WebSocket, event_data: dict) -> dict:
        from app.services.evidence_urls import signed_path

        payload = self._creds.get(id(websocket), (None, None))[1]
        out = dict(event_data)
        for key in ("snapshot_url", "clip_url", "evidence_url"):
            if out.get(key):
                out[key] = signed_path(out[key], payload)
        return out

    async def broadcast_event(self, event_data: dict) -> int:
        """Send to every still-authorised client; returns how many received it."""
        await self.prune_revoked()
        delivered = 0
        dead: List[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(self._for_connection(connection, event_data))
                delivered += 1
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)
        return delivered


alert_hub = AlertHub()


class NotificationService:
    async def notify_loss_prevention(self, title: str, body: str, data: Dict[str, Any]) -> dict:
        """Alert staff about a loss-prevention event.

        ``data`` should carry ``camera_id`` and ``event_type`` (an
        :class:`EventType` value; ``THEFT_SUSPECTED`` if omitted), and may
        carry ``severity``, ``confidence`` (only if actually measured),
        ``incident_id``, ``snapshot_url``, ``clip_url``, ``zone_name``.

        Returns what really happened (see ``alert_dispatcher.dispatch``).
        """
        from app.services.alert_dispatcher import alert_dispatcher

        data = dict(data or {})
        try:
            event_type = EventType(str(data.get("event_type") or EventType.THEFT_SUSPECTED.value))
        except ValueError:
            event_type = EventType.THEFT_SUSPECTED
        try:
            severity = EventSeverity(str(data.get("severity") or EventSeverity.HIGH.value))
        except ValueError:
            severity = EventSeverity.HIGH
        return await alert_dispatcher.dispatch(event_type.value, severity.value, title, body, data)

    async def dispatch_event_notification(self, event: SecurityEvent) -> dict:
        """Push an alert that the caller already logged and broadcast (``/events/trigger``)."""
        from app.services.alert_dispatcher import alert_dispatcher
        from app.services.evidence_urls import evidence_path

        title = f"{_label(event.event_type)} - {event.camera_name}"
        body = event.description or f"Review {event.location} ({event.camera_name})."
        data = {
            "alert_id": event.id,
            "camera_id": event.camera_id,
            "camera_name": event.camera_name,
            "location": event.location,
            # Same-origin paths (services/evidence_urls.py), never EDGE_BASE_URL links.
            "snapshot_url": evidence_path(event.snapshot_url) or "",
            "clip_url": evidence_path(event.clip_url) or "",
            "timestamp": event.timestamp.isoformat(),
        }
        if event.metadata:
            data["zone_name"] = event.metadata.get("zone_name", "")
            if event.metadata.get("incident_id"):
                data["incident_id"] = event.metadata["incident_id"]
        report = await alert_dispatcher.dispatch(
            event.event_type.value, event.severity.value, title, body, data,
            persist=False, broadcast=False,
        )
        return report["push"]


def _label(event_type: EventType) -> str:
    return event_type.value.replace("_", " ").capitalize()


notification_service = NotificationService()
