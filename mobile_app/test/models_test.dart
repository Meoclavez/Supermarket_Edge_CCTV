import 'package:flutter_test/flutter_test.dart';
import 'package:edge_ai_cctv/models/feature_config.dart';
import 'package:edge_ai_cctv/models/theft_incident.dart';
import 'package:edge_ai_cctv/models/zone_model.dart';

void main() {
  group('TheftIncident', () {
    test('parses the backend TheftIncident schema', () {
      final inc = TheftIncident.fromJson({
        'id': 'inc_1',
        'theft_type': 'CONCEALMENT',
        'severity': 'HIGH',
        'status': 'ACTIVE',
        'department': 'Health & Beauty',
        'camera_id': 'cam_3',
        'camera_name': 'Aisle 3',
        'confidence': 0.72,
        'evidence_summary': 'Wrist moved from shelf to waist',
        'evidence_snapshot_url': '/api/v1/theft/incidents/inc_1/evidence',
        'timestamp': '2026-09-23T10:15:00',
      });
      expect(inc.ruleLabel, 'Concealment');
      expect(inc.confidence, 0.72);
      expect(inc.canAcknowledge, isTrue);
      expect(inc.canResolve, isTrue);
      expect(inc.evidenceSnapshotUrl, '/api/v1/theft/incidents/inc_1/evidence');
      // Naive backend timestamps are UTC.
      expect(inc.timestamp.toUtc(), DateTime.utc(2026, 9, 23, 10, 15));
    });

    test('missing or empty snapshot URL becomes null', () {
      final a = TheftIncident.fromJson({'id': 'a', 'theft_type': 'SHELF_SWEEPING', 'camera_id': 'c'});
      final b = TheftIncident.fromJson({'id': 'b', 'theft_type': 'X', 'camera_id': 'c', 'evidence_snapshot_url': ''});
      expect(a.evidenceSnapshotUrl, isNull);
      expect(b.evidenceSnapshotUrl, isNull);
      expect(a.ruleLabel, 'Shelf sweeping');
    });

    test('resolved incidents cannot be acknowledged or resolved again', () {
      final inc = TheftIncident.fromJson({'id': 'r', 'theft_type': 'X', 'camera_id': 'c', 'status': 'RESOLVED'});
      expect(inc.canAcknowledge, isFalse);
      expect(inc.canResolve, isFalse);
    });
  });

  group('TheftIncident (TheftIncidentOut fields)', () {
    test('prefers server rule label, evidence list and snapshot_url', () {
      final inc = TheftIncident.fromJson({
        'id': 'inc_2',
        'theft_type': 'CONCEALMENT',
        'rule': 'CONCEALMENT',
        'rule_label': 'Possible concealment',
        'evidence': ['Wrist near waist for 1.4 s', ''],
        'snapshot_url': '/api/v1/theft/incidents/inc_2/evidence',
        'review_label': 'Suspicious behaviour for staff review',
        'camera_id': 'c',
        'severity': 'HIGH',
      });
      expect(inc.ruleLabel, 'Possible concealment');
      expect(inc.ruleCode, 'CONCEALMENT');
      expect(inc.evidence, ['Wrist near waist for 1.4 s']);
      expect(inc.evidenceSnapshotUrl, '/api/v1/theft/incidents/inc_2/evidence');
      expect(inc.reviewLabel, 'Suspicious behaviour for staff review');
    });
  });

  group('FeatureConfig', () {
    test('round-trips retail flags and preserves unknown keys', () {
      final cfg = FeatureConfig.fromJson({
        'theft_detection': false,
        'shelf_interaction': true,
        'people_counting': true,
        'sub_stream_fps': 5,
      });
      expect(cfg.theftDetection, isFalse);
      cfg.peopleCounting = false;
      expect(cfg.toJson(), {
        'sub_stream_fps': 5,
        'theft_detection': false,
        'shelf_interaction': true,
        'people_counting': false,
      });
    });
  });

  group('ZoneType', () {
    test('maps to and from backend wire values', () {
      expect(ZoneType.tripwire.wireValue, 'TRIPWIRE');
      expect(ZoneType.restrictedArea.wireValue, 'RESTRICTED_ZONE');
      expect(ZoneType.fromWire('INTRUSION'), ZoneType.restrictedArea);
      expect(ZoneType.fromWire('EXCLUSION'), ZoneType.privacyMask);
      expect(ZoneType.fromWire('DOOR'), isNull);
      final zone = ZoneConfig(id: 'z', cameraId: 'c', name: 'Stockroom', zoneType: ZoneType.restrictedArea);
      expect(zone.toJson()['zone_type'], 'RESTRICTED_ZONE');
      expect(zone.allowedClasses, ['person']);
    });
  });
}
