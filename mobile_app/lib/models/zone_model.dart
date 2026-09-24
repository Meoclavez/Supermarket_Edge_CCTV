/// Per-camera zone types supported by `POST /api/v1/cameras/{id}/zones`.
///
/// Shelf and department zones for heatmaps live on the store floor plan
/// (blueprint editor), not here.
enum ZoneType {
  /// Entrance / exit counting line.
  tripwire('TRIPWIRE', {'TRIPWIRE'}),

  /// Staff-only or restricted area such as a stockroom or cash office.
  restrictedArea('RESTRICTED_ZONE', {'RESTRICTED_ZONE', 'INTRUSION'}),

  /// Area blurred on the edge server and ignored by analytics.
  privacyMask('PRIVACY_MASK', {'PRIVACY_MASK', 'EXCLUSION'});

  const ZoneType(this.wireValue, this.acceptedWireValues);

  final String wireValue;
  final Set<String> acceptedWireValues;

  String get label {
    switch (this) {
      case ZoneType.tripwire:
        return 'Entry / exit line';
      case ZoneType.restrictedArea:
        return 'Staff-only area';
      case ZoneType.privacyMask:
        return 'Privacy mask';
    }
  }

  static ZoneType? fromWire(String? value) {
    final v = (value ?? '').toUpperCase();
    for (final t in ZoneType.values) {
      if (t.acceptedWireValues.contains(v)) return t;
    }
    return null;
  }
}
enum MaskMode { blackout, blur, mosaic, color }
enum TripwireDirection { aToB, bToA, bidirectional }

class Point2D {
  double x;
  double y;
  Point2D({required this.x, required this.y});

  Map<String, dynamic> toJson() => {'x': x, 'y': y};
  factory Point2D.fromJson(Map<String, dynamic> json) => Point2D(
        x: (json['x'] as num).toDouble(),
        y: (json['y'] as num).toDouble(),
      );

  Point2D clone() => Point2D(x: x, y: y);
}

class ZoneConfig {
  String id;
  String cameraId;
  String name;
  ZoneType zoneType;
  bool enabled;
  List<Point2D> polygonPoints;
  Point2D? lineStart;
  Point2D? lineEnd;
  TripwireDirection direction;
  MaskMode maskMode;
  double dwellTimeSeconds;
  List<String> allowedClasses;

  ZoneConfig({
    required this.id,
    required this.cameraId,
    required this.name,
    required this.zoneType,
    this.enabled = true,
    List<Point2D>? polygonPoints,
    this.lineStart,
    this.lineEnd,
    this.direction = TripwireDirection.bidirectional,
    this.maskMode = MaskMode.blackout,
    this.dwellTimeSeconds = 0.0,
    List<String>? allowedClasses,
  })  : polygonPoints = polygonPoints ?? [],
        allowedClasses = allowedClasses ?? ['person'];

  Map<String, dynamic> toJson() => {
        'id': id,
        'camera_id': cameraId,
        'name': name,
        'zone_type': zoneType.wireValue,
        'enabled': enabled,
        'polygon_points': polygonPoints.map((p) => p.toJson()).toList(),
        'line_start': lineStart?.toJson(),
        'line_end': lineEnd?.toJson(),
        'direction': direction.name,
        'mask_mode': maskMode.name,
        'dwell_time_seconds': dwellTimeSeconds,
        'allowed_classes': allowedClasses,
      };

  factory ZoneConfig.fromJson(Map<String, dynamic> json) {
    TripwireDirection parseDirection(String? val) {
      return TripwireDirection.values.firstWhere(
        (e) => e.name.toLowerCase() == (val ?? '').toLowerCase(),
        orElse: () => TripwireDirection.bidirectional,
      );
    }

    MaskMode parseMaskMode(String? val) {
      return MaskMode.values.firstWhere(
        (e) => e.name.toLowerCase() == (val ?? '').toLowerCase(),
        orElse: () => MaskMode.blackout,
      );
    }

    return ZoneConfig(
      id: json['id'] ?? '',
      cameraId: json['camera_id'] ?? '',
      name: json['name'] ?? '',
      zoneType: ZoneType.fromWire(json['zone_type']) ?? ZoneType.restrictedArea,
      enabled: json['enabled'] ?? true,
      polygonPoints: (json['polygon_points'] as List<dynamic>?)
              ?.map((p) => Point2D.fromJson(p as Map<String, dynamic>))
              .toList() ??
          [],
      lineStart: json['line_start'] != null
          ? Point2D.fromJson(json['line_start'] as Map<String, dynamic>)
          : null,
      lineEnd: json['line_end'] != null
          ? Point2D.fromJson(json['line_end'] as Map<String, dynamic>)
          : null,
      direction: parseDirection(json['direction']),
      maskMode: parseMaskMode(json['mask_mode']),
      dwellTimeSeconds: (json['dwell_time_seconds'] as num?)?.toDouble() ?? 0.0,
      allowedClasses: (json['allowed_classes'] as List<dynamic>?)
              ?.map((c) => c.toString())
              .toList() ??
          ['person'],
    );
  }
}
