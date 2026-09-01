class FeatureConfig {
  bool motionTracking;
  bool fallDetection;
  bool doorMonitoring;
  bool packageTheftTracking;
  bool inactivityAlerts;
  bool continuousRecording;
  bool eventClipRecording;

  FeatureConfig({
    this.motionTracking = true,
    this.fallDetection = false,
    this.doorMonitoring = false,
    this.packageTheftTracking = false,
    this.inactivityAlerts = false,
    this.continuousRecording = true,
    this.eventClipRecording = true,
  });

  factory FeatureConfig.fromJson(Map<String, dynamic> json) {
    return FeatureConfig(
      motionTracking: json['motion_tracking'] ?? true,
      fallDetection: json['fall_detection'] ?? false,
      doorMonitoring: json['door_monitoring'] ?? false,
      packageTheftTracking: json['package_theft_tracking'] ?? false,
      inactivityAlerts: json['inactivity_alerts'] ?? false,
      continuousRecording: json['continuous_recording'] ?? true,
      eventClipRecording: json['event_clip_recording'] ?? true,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'motion_tracking': motionTracking,
      'fall_detection': fallDetection,
      'door_monitoring': doorMonitoring,
      'package_theft_tracking': packageTheftTracking,
      'inactivity_alerts': inactivityAlerts,
      'continuous_recording': continuousRecording,
      'event_clip_recording': eventClipRecording,
    };
  }
}
