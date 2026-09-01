class ApiConstants {
  // Edge Mini PC Base URL (Can be modified dynamically in settings for local Wi-Fi or WireGuard)
  static const String defaultBaseUrl = 'http://192.168.1.100:8000';
  static const String defaultGo2rtcUrl = 'http://192.168.1.100:1984';

  // Endpoints
  static const String camerasEndpoint = '/api/v1/cameras';
  static const String eventsEndpoint = '/api/v1/events';
  static const String triggerEventEndpoint = '/api/v1/events/trigger';
  static const String webrtcOfferEndpoint = '/api/v1/webrtc/offer';
  static const String webrtcIceEndpoint = '/api/v1/webrtc/ice-servers';
  static const String webrtcTokenEndpoint = '/api/v1/webrtc/token';
  static const String registerDeviceEndpoint = '/api/v1/cameras/register-device';

  // WebRTC ICE Servers
  static const Map<String, dynamic> rtcIceServers = {
    'iceServers': [
      {'urls': 'stun:stun.l.google.com:19302'},
      {'urls': 'stun:stun1.l.google.com:19302'},
    ],
    'sdpSemantics': 'unified-plan',
  };

  // WebRTC Media Constraints
  static const Map<String, dynamic> rtcMediaConstraints = {
    'mandatory': {
      'OfferToReceiveAudio': true,
      'OfferToReceiveVideo': true,
    },
    'optional': [],
  };
}
