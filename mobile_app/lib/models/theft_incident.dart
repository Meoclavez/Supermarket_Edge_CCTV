import '../core/server_time.dart';

/// A loss-prevention incident as returned by `GET /api/v1/theft/incidents`.
///
/// Field names mirror `TheftIncidentOut` in edge_backend/app/routes/theft.py
/// (`TheftIncident` from schemas.py plus rule, rule_label, evidence,
/// snapshot_url and review_label).
/// An incident describes suspicious behaviour for staff review; it is not an
/// accusation, and the UI must present it that way.
class TheftIncident {
  final String id;
  final String theftType;
  final String? rule;
  final String? serverRuleLabel;
  final List<String> evidence;
  final String? reviewLabel;
  final String severity;
  final String status;
  final String department;
  final String cameraId;
  final String cameraName;
  final String? shelfZoneId;
  final String? zoneId;
  final double confidence;
  final String evidenceSummary;
  final String? evidenceSnapshotUrl;
  final String? evidenceClipUrl;
  final String? personTrackId;
  final String? guardId;
  final String? resolution;
  final String? notes;
  final DateTime timestamp;
  final DateTime? resolvedAt;

  TheftIncident({
    required this.id,
    required this.theftType,
    required this.severity,
    required this.status,
    required this.department,
    required this.cameraId,
    required this.cameraName,
    required this.confidence,
    required this.evidenceSummary,
    required this.timestamp,
    this.rule,
    this.serverRuleLabel,
    this.evidence = const [],
    this.reviewLabel,
    this.shelfZoneId,
    this.zoneId,
    this.evidenceSnapshotUrl,
    this.evidenceClipUrl,
    this.personTrackId,
    this.guardId,
    this.resolution,
    this.notes,
    this.resolvedAt,
  });

  static String? _nonEmpty(dynamic v) {
    if (v == null) return null;
    final s = v.toString().trim();
    return s.isEmpty ? null : s;
  }

  /// Backend timestamps are naive UTC (`datetime.utcnow`), so treat a value
  /// without an offset as UTC before converting to local time for display.
  static DateTime? _parseTime(dynamic v) {
    return parseServerTime(v);
  }

  factory TheftIncident.fromJson(Map<String, dynamic> json) {
    return TheftIncident(
      id: json['id']?.toString() ?? '',
      theftType: json['theft_type']?.toString() ?? 'UNKNOWN',
      rule: _nonEmpty(json['rule']),
      serverRuleLabel: _nonEmpty(json['rule_label']),
      evidence: (json['evidence'] as List<dynamic>? ?? const [])
          .map((e) => e.toString())
          .where((e) => e.trim().isNotEmpty)
          .toList(),
      reviewLabel: _nonEmpty(json['review_label']),
      severity: json['severity']?.toString() ?? 'HIGH',
      status: json['status']?.toString() ?? 'ACTIVE',
      department: json['department']?.toString() ?? '',
      cameraId: json['camera_id']?.toString() ?? '',
      cameraName: json['camera_name']?.toString() ?? json['camera_id']?.toString() ?? 'Camera',
      shelfZoneId: _nonEmpty(json['shelf_zone_id']),
      zoneId: _nonEmpty(json['zone_id']),
      confidence: (json['confidence'] as num?)?.toDouble() ?? 0.0,
      evidenceSummary: json['evidence_summary']?.toString() ?? '',
      evidenceSnapshotUrl: _nonEmpty(json['evidence_snapshot_url']) ?? _nonEmpty(json['snapshot_url']),
      evidenceClipUrl: _nonEmpty(json['evidence_clip_url']),
      personTrackId: _nonEmpty(json['person_track_id']),
      guardId: _nonEmpty(json['guard_id']),
      resolution: _nonEmpty(json['resolution']),
      notes: _nonEmpty(json['notes']),
      timestamp: _parseTime(json['timestamp']) ?? _parseTime(json['created_at']) ?? DateTime.now(),
      resolvedAt: _parseTime(json['resolved_at']),
    );
  }

  /// Rule identifier that fired (`rule`, falling back to `theft_type`).
  String get ruleCode => rule ?? theftType;

  /// The server's rule label when present, otherwise the rule code in words,
  /// e.g. `SHELF_SWEEPING` -> `Shelf sweeping`.
  String get ruleLabel {
    if (serverRuleLabel != null) return serverRuleLabel!;
    final words = ruleCode.toLowerCase().split('_').where((w) => w.isNotEmpty).toList();
    if (words.isEmpty) return 'Unknown rule';
    final text = words.join(' ');
    return text[0].toUpperCase() + text.substring(1);
  }

  bool get isOpen => status == 'ACTIVE' || status == 'ACKNOWLEDGED' || status == 'DISPATCHED';
  bool get canAcknowledge => status == 'ACTIVE';
  bool get canResolve => isOpen;
}
