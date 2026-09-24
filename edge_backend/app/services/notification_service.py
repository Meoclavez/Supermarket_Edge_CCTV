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
from typing import Any, Dict, List

from fastapi import WebSocket

from app.models.schemas import EventSeverity, EventType, SecurityEvent

logger = logging.getLogger("NotificationService")

APNS_CATEGORY = "LOSS_PREVENTION_ALERT"
FCM_CHANNEL_ID = "loss_prevention_alerts"


class AlertHub:
    """Connected dashboard websockets that receive every alert as JSON."""

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_event(self, event_data: dict) -> int:
        """Send to every client; returns how many received it."""
        delivered = 0
        dead: List[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(event_data)
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

        title = f"{_label(event.event_type)} - {event.camera_name}"
        body = event.description or f"Review {event.location} ({event.camera_name})."
        data = {
            "alert_id": event.id,
            "camera_id": event.camera_id,
            "camera_name": event.camera_name,
            "location": event.location,
            "snapshot_url": event.snapshot_url or "",
            "clip_url": event.clip_url or "",
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
