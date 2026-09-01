class CameraFeed {
  final String id;
  final String name;
  final String location;
  final String rtspUrl;
  final String webrtcUrl;
  final String status;
  final int fps;
  final String resolution;
  final bool isAiEnabled;
  final List<String> aiModels;

  final String? diagnosticState;
  final String? errorMessage;
  final bool isAutoRecovering;

  CameraFeed({
    required this.id,
    required this.name,
    required this.location,
    required this.rtspUrl,
    required this.webrtcUrl,
    required this.status,
    required this.fps,
    required this.resolution,
    required this.isAiEnabled,
    required this.aiModels,
    this.diagnosticState,
    this.errorMessage,
    this.isAutoRecovering = false,
  });

  factory CameraFeed.fromJson(Map<String, dynamic> json) {
    return CameraFeed(
      id: json['id'] ?? '',
      name: json['name'] ?? '',
      location: json['location'] ?? '',
      rtspUrl: json['rtsp_url'] ?? '',
      webrtcUrl: json['webrtc_url'] ?? '',
      status: json['status'] ?? 'ONLINE',
      fps: json['fps'] ?? 30,
      resolution: json['resolution'] ?? '1920x1080',
      isAiEnabled: json['is_ai_enabled'] ?? true,
      aiModels: List<String>.from(json['ai_models'] ?? []),
      diagnosticState: json['diagnostic_state'],
      errorMessage: json['error_message'],
      isAutoRecovering: json['is_auto_recovering'] ?? false,
    );
  }

  bool get isOnline => status.toUpperCase() == 'ONLINE';
}
