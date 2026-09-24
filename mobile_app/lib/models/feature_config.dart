/// Per-camera retail analytics flags (`CameraFeatureConfig` on the edge server).
///
/// Only the three retail flags are editable in the app. Any other keys the
/// server returns (stream FPS, recording options, ...) are kept verbatim and
/// sent back unchanged, so saving a toggle never resets settings the app does
/// not know about.
class FeatureConfig {
  bool theftDetection;
  bool shelfInteraction;
  bool peopleCounting;
  final Map<String, dynamic> _passthrough;

  FeatureConfig({
    this.theftDetection = true,
    this.shelfInteraction = true,
    this.peopleCounting = true,
    Map<String, dynamic>? passthrough,
  }) : _passthrough = passthrough ?? {};

  static const _keys = {'theft_detection', 'shelf_interaction', 'people_counting'};

  factory FeatureConfig.fromJson(Map<String, dynamic> json) {
    return FeatureConfig(
      theftDetection: json['theft_detection'] ?? true,
      shelfInteraction: json['shelf_interaction'] ?? true,
      peopleCounting: json['people_counting'] ?? true,
      passthrough: Map<String, dynamic>.from(json)..removeWhere((k, _) => _keys.contains(k)),
    );
  }

  Map<String, dynamic> toJson() {
    return {
      ..._passthrough,
      'theft_detection': theftDetection,
      'shelf_interaction': shelfInteraction,
      'people_counting': peopleCounting,
    };
  }
}
