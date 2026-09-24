import 'package:flutter_test/flutter_test.dart';

import 'package:edge_ai_cctv/core/server_time.dart';
import 'package:edge_ai_cctv/models/security_event.dart';
import 'package:edge_ai_cctv/models/theft_incident.dart';

void main() {
  final utc = DateTime.utc(2026, 9, 23, 14, 5, 30);

  test('offset-less server timestamps are UTC, returned as local time', () {
    final t = parseServerTime('2026-09-23T14:05:30')!;
    expect(t.isUtc, isFalse, reason: 'converted for display');
    expect(t.toUtc(), utc);
    expect(parseServerTime('2026-09-23T14:05:30.123456')!.toUtc(), DateTime.utc(2026, 9, 23, 14, 5, 30, 123, 456));
    expect(parseServerTime('2026-09-23 14:05:30')!.toUtc(), utc);
  });

  test('explicit offsets are respected', () {
    expect(parseServerTime('2026-09-23T14:05:30Z')!.toUtc(), utc);
    expect(parseServerTime('2026-09-23T16:05:30+02:00')!.toUtc(), utc);
    expect(parseServerTime('2026-09-23T09:05:30-0500')!.toUtc(), utc);
    expect(parseServerTime('2026-09-24T00:05:30+10')!.toUtc(), utc);
  });

  test('date-only values are UTC midnight, not mistaken for an offset', () {
    expect(parseServerTime('2026-09-23')!.toUtc(), DateTime.utc(2026, 9, 23));
  });

  test('null, empty and garbage give null', () {
    expect(parseServerTime(null), isNull);
    expect(parseServerTime('  '), isNull);
    expect(parseServerTime('yesterday'), isNull);
  });

  test('models use it: the same instant regardless of the phone time zone', () {
    final ev = SecurityEvent.fromJson({
      'id': 'e1', 'camera_id': 'c1', 'event_type': 'LOITERING', 'severity': 'WARNING',
      'timestamp': '2026-09-23T14:05:30',
    });
    expect(ev.timestamp.toUtc(), utc);
    final inc = TheftIncident.fromJson({'id': 'i1', 'theft_type': 'CONCEALMENT', 'timestamp': '2026-09-23T14:05:30'});
    expect(inc.timestamp.toUtc(), utc);
  });
}
