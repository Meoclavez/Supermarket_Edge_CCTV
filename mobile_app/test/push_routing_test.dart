import 'package:flutter_test/flutter_test.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/services/live_alert_channel.dart';
import 'package:edge_ai_cctv/services/push_router.dart';

void main() {
  const a = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  const b = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';
  const stranger = 'cccccccc-3333-4333-8333-cccccccccccc';
  bool known(String id) => id == a || id == b;

  test('a loss-prevention push opens that server\'s incident', () {
    final r = routeForPush({
      'device_id': b,
      'device_name': 'Store B',
      'event_type': 'THEFT_SUSPECTED',
      'severity': 'HIGH',
      'incident_id': 'inc-42',
      'camera_id': 'cam1',
      'type': kCategoryLossPrevention,
    }, known)!;
    expect(r.deviceId, b);
    expect(r.destination, PushDestination.incident);
    expect(r.incidentId, 'inc-42');
  });

  test('loss-prevention push without an incident opens the incident list of that server', () {
    final r = routeForPush({'device_id': a.toUpperCase(), 'event_type': 'CONCEALMENT', 'alert_id': 'evt_1'}, known)!;
    expect(r.deviceId, a);
    expect(r.destination, PushDestination.incidentList);
  });

  test('other alerts open the event centre of that server', () {
    final r = routeForPush({'device_id': a, 'event_type': 'CAMERA_OFFLINE', 'camera_id': 'cam3'}, known)!;
    expect(r.destination, PushDestination.events);
  });

  test('pushes from unknown servers or without a device id are ignored', () {
    expect(routeForPush({'device_id': stranger, 'incident_id': 'inc-1'}, known), isNull);
    expect(routeForPush({'incident_id': 'inc-1', 'type': kCategoryLossPrevention}, known), isNull);
    expect(routeForPush({'device_id': 'garbage', 'incident_id': 'inc-1'}, known), isNull);
  });

  test('websocket alerts are tagged with the connected server and foreign ids dropped', () {
    final entry = ServerEntry(deviceId: a, name: 'Store A', urls: ['http://192.168.1.50:8000']);
    final tagged = alertFromLiveMessage({'id': 'evt_9', 'event_type': 'LOITERING', 'severity': 'WARNING'}, entry)!;
    expect(tagged['device_id'], a);
    expect(tagged['alert_id'], 'evt_9');
    expect(alertFromLiveMessage({'event_type': 'LOITERING', 'device_id': b}, entry), isNull);
    expect(alertFromLiveMessage({'type': 'ping'}, entry), isNull);
  });

  test('alert prefs filter like the server: type, severity, camera, quiet hours', () {
    const p = AlertPrefs(eventTypes: ['THEFT_SUSPECTED'], minSeverity: 'WARNING', cameraIds: ['cam1'], quietStart: '22:00', quietEnd: '07:00');
    final noon = DateTime(2026, 1, 1, 12);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'HIGH', cameraId: 'cam1', at: noon), isTrue);
    expect(p.matches(eventType: 'LOITERING', severity: 'HIGH', cameraId: 'cam1', at: noon), isFalse);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'INFO', cameraId: 'cam1', at: noon), isFalse);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'HIGH', cameraId: 'cam2', at: noon), isFalse);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'HIGH', cameraId: 'cam1', at: DateTime(2026, 1, 1, 23, 30)), isFalse);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'HIGH', cameraId: 'cam1', at: DateTime(2026, 1, 2, 6, 59)), isFalse);
    expect(p.matches(eventType: 'THEFT_SUSPECTED', severity: 'HIGH', cameraId: 'cam1', at: DateTime(2026, 1, 2, 7, 0)), isTrue);
  });
}
