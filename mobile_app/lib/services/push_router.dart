import '../models/server_entry.dart';

/// Where a notification should take the user.
enum PushDestination {
  /// One loss-prevention incident (`incident_id` present).
  incident,

  /// The loss-prevention incident list.
  incidentList,

  /// The general event centre (camera offline, queue, tripwire, ...).
  events,
}

class PushRoute {
  final String deviceId;
  final PushDestination destination;
  final String? incidentId;
  final String? eventType;

  const PushRoute(this.deviceId, this.destination, {this.incidentId, this.eventType});

  @override
  String toString() => 'PushRoute($deviceId, $destination, incident=$incidentId)';
}

/// Legacy push `type` / iOS category for loss-prevention alerts.
const String kCategoryLossPrevention = 'LOSS_PREVENTION_ALERT';

/// Chooses the server and screen for a push or websocket payload.
///
/// Every push carries the `device_id` of the server that sent it. A payload
/// without one, or for a server this phone is not paired with, returns null
/// and must be ignored: it may come from a store this phone was removed from.
PushRoute? routeForPush(Map<String, dynamic> data, bool Function(String deviceId) isKnownServer) {
  final rawId = data['device_id'];
  if (rawId is! String || !isValidDeviceId(rawId)) return null;
  final deviceId = normaliseDeviceId(rawId);
  if (!isKnownServer(deviceId)) return null;

  final eventType = data['event_type']?.toString().toUpperCase();
  final incident = data['incident_id']?.toString().trim();
  if (incident != null && incident.isNotEmpty) {
    return PushRoute(deviceId, PushDestination.incident, incidentId: incident, eventType: eventType);
  }
  if (data['type'] == kCategoryLossPrevention || kLossPreventionEventTypes.contains(eventType)) {
    return PushRoute(deviceId, PushDestination.incidentList, eventType: eventType);
  }
  return PushRoute(deviceId, PushDestination.events, eventType: eventType);
}
