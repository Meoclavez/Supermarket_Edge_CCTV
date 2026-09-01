class SecurityEvent {
  final String id;
  final String cameraId;
  final String cameraName;
  final String location;
  final String eventType;
  final String severity;
  final double confidence;
  final DateTime timestamp;
  final String? clipUrl;
  final String? snapshotUrl;
  final bool acknowledged;

  SecurityEvent({
    required this.id,
    required this.cameraId,
    required this.cameraName,
    required this.location,
    required this.eventType,
    required this.severity,
    required this.confidence,
    required this.timestamp,
    this.clipUrl,
    this.snapshotUrl,
    required this.acknowledged,
  });

  factory SecurityEvent.fromJson(Map<String, dynamic> json) {
    return SecurityEvent(
      id: json['id'] ?? '',
      cameraId: json['camera_id'] ?? '',
      cameraName: json['camera_name'] ?? json['camera_id'] ?? 'Camera',
      location: json['location'] ?? 'Zone',
      eventType: json['event_type'] ?? 'MOTION_ALERT',
      severity: json['severity'] ?? 'WARNING',
      confidence: (json['confidence'] as num?)?.toDouble() ?? 0.0,
      timestamp: json['timestamp'] != null ? DateTime.parse(json['timestamp']) : DateTime.now(),
      clipUrl: json['clip_url'],
      snapshotUrl: json['snapshot_url'],
      acknowledged: json['acknowledged'] ?? false,
    );
  }

  bool get isCritical => severity.toUpperCase() == 'CRITICAL';
}
