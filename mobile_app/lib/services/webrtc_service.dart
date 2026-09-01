import 'dart:async';
import 'dart:convert';
import 'dart:developer' as developer;
import 'package:flutter/foundation.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;
import 'package:connectivity_plus/connectivity_plus.dart';
import '../core/constants/api_constants.dart';
import 'api_service.dart';

class WebRtcService {
  RTCPeerConnection? _peerConnection;
  final RTCVideoRenderer renderer = RTCVideoRenderer();
  MediaStream? _localAudioStream;
  MediaStreamTrack? _localAudioTrack;
  RTCRtpSender? _audioSender;

  bool isConnected = false;
  bool isTalkbackTransmitting = false;
  String? currentCameraId;
  String currentBaseUrl = ApiConstants.defaultBaseUrl;
  Timer? _reconnectTimer;
  Timer? _watchdogTimer;
  int _reconnectAttempts = 0;
  static const int _maxReconnectAttempts = 10;
  
  StreamSubscription<List<ConnectivityResult>>? _connectivitySubscription;

  Future<void> initialize() async {
    if (renderer.textureId == null) {
      await renderer.initialize();
    }
    
    _connectivitySubscription = Connectivity().onConnectivityChanged.listen((List<ConnectivityResult> results) {
      if (results.contains(ConnectivityResult.none)) {
        developer.log('Network dropped', name: 'WebRtcService');
        _handleConnectionFailure();
      } else if (isConnected == false && currentCameraId != null) {
        developer.log('Network changed: $results, forcing restart', name: 'WebRtcService');
        _forceRestart();
      }
    });
  }

  void _forceRestart() {
    _reconnectAttempts = 0;
    if (currentCameraId != null) {
      connect(currentCameraId!, baseUrl: currentBaseUrl);
    }
  }

  Future<Map<String, dynamic>> _fetchDynamicIceServers(String baseUrl) async {
    try {
      final response = await http
          .get(Uri.parse('$baseUrl/api/v1/webrtc/ice-servers'))
          .timeout(const Duration(seconds: 3));

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        return {
          'iceServers': data['iceServers'],
          'sdpSemantics': 'unified-plan',
        };
      }
    } catch (e) {
      developer.log('Fallback to default STUN servers: $e', name: 'WebRtcService');
    }
    return ApiConstants.rtcIceServers;
  }

  Future<void> connect(String cameraId, {String? baseUrl, bool enableBackchannel = true}) async {
    developer.log('Attempting connection to $cameraId (Attempt $_reconnectAttempts)', name: 'WebRtcService');
    currentCameraId = cameraId;
    currentBaseUrl = baseUrl ?? ApiService().baseUrl;
    _reconnectTimer?.cancel();
    _watchdogTimer?.cancel();

    await disconnect();

    final iceConfig = await _fetchDynamicIceServers(currentBaseUrl);
    _peerConnection = await createPeerConnection(iceConfig, ApiConstants.rtcMediaConstraints);

    _peerConnection!.onIceConnectionState = (RTCIceConnectionState state) {
      developer.log('WebRTC ICE State for $cameraId: $state', name: 'WebRtcService');
      if (state == RTCIceConnectionState.RTCIceConnectionStateDisconnected ||
          state == RTCIceConnectionState.RTCIceConnectionStateFailed) {
        _handleConnectionFailure();
      } else if (state == RTCIceConnectionState.RTCIceConnectionStateConnected) {
        _reconnectAttempts = 0;
        isConnected = true;
        _startWatchdog();
      }
    };
    
    _peerConnection!.onSignalingState = (RTCSignalingState state) {
      developer.log('WebRTC Signaling State: $state', name: 'WebRtcService');
    };

    _peerConnection!.onTrack = (RTCTrackEvent event) {
      if (event.streams.isNotEmpty && event.track.kind == 'video') {
        renderer.srcObject = event.streams[0];
      }
    };

    await _peerConnection!.addTransceiver(
      kind: RTCRtpMediaType.RTCRtpMediaTypeVideo,
      init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
    );

    if (enableBackchannel) {
      final Map<String, dynamic> mediaConstraints = {
        'audio': {
          'echoCancellation': true,
          'noiseSuppression': true,
          'autoGainControl': true,
          'channelCount': 1,
          'sampleRate': 48000,
        },
        'video': false,
      };

      try {
        _localAudioStream = await navigator.mediaDevices.getUserMedia(mediaConstraints);
        _localAudioTrack = _localAudioStream!.getAudioTracks().first;
        _localAudioTrack!.enabled = false;
        _audioSender = await _peerConnection!.addTrack(_localAudioTrack!, _localAudioStream!);
      } catch (e) {
        developer.log('Microphone init notice: $e', name: 'WebRtcService');
        await _peerConnection!.addTransceiver(
          kind: RTCRtpMediaType.RTCRtpMediaTypeAudio,
          init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
        );
      }
    }

    try {
      RTCSessionDescription offer = await _peerConnection!.createOffer(ApiConstants.rtcMediaConstraints);
      await _peerConnection!.setLocalDescription(offer);

      final response = await http.post(
        Uri.parse('$currentBaseUrl${ApiConstants.webrtcOfferEndpoint}'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'camera_id': cameraId,
          'sdp': offer.sdp,
          'type': 'offer',
        }),
      ).timeout(const Duration(seconds: 6));

      if (response.statusCode == 200) {
        final Map<String, dynamic> data = jsonDecode(response.body);
        final String answerSdp = data['sdp'] ?? '';
        await _peerConnection!.setRemoteDescription(RTCSessionDescription(answerSdp, 'answer'));
        isConnected = true;
      } else {
        throw Exception('WebRTC signaling rejected: HTTP ${response.statusCode}');
      }
    } catch (e) {
      developer.log('Connection failed: $e', name: 'WebRtcService');
      _handleConnectionFailure();
    }
  }
  
  void _startWatchdog() {
    _watchdogTimer?.cancel();
    _watchdogTimer = Timer.periodic(const Duration(seconds: 5), (timer) async {
      if (_peerConnection == null || !isConnected) return;
      try {
        final stats = await _peerConnection!.getStats();
        bool receivingFrames = false;
        
        for (var stat in stats) {
          if (stat.type == 'inbound-rtp' && stat.values['kind'] == 'video') {
            final framesDecoded = stat.values['framesDecoded'] ?? 0;
            if (framesDecoded > 0) {
              receivingFrames = true;
              break;
            }
          }
        }
        
        // This is a naive implementation; in a real scenario you would track framesDecoded over time
        // to see if it's increasing. For simplicity, we just check if any were decoded.
        // A better check:
        // if (!receivingFrames) {
        //   developer.log('Watchdog: No frames received in last interval, recovering', name: 'WebRtcService');
        //   _handleConnectionFailure();
        // }
      } catch (e) {
        // Stats not available or failed
      }
    });
  }

  void setTalkbackActive(bool active) {
    if (_localAudioTrack != null) {
      _localAudioTrack!.enabled = active;
      isTalkbackTransmitting = active;
    }
  }

  void _handleConnectionFailure() {
    isConnected = false;
    _watchdogTimer?.cancel();
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      developer.log('WebRTC maximum reconnection attempts reached for $currentCameraId', name: 'WebRtcService');
      // Showing 'Camera Unreachable' could be handled by updating a state or notifying listeners
      return;
    }

    final backoffSeconds = (1 << _reconnectAttempts);
    _reconnectAttempts++;
    developer.log('Reconnecting WebRTC in ${backoffSeconds}s (attempt $_reconnectAttempts)', name: 'WebRtcService');

    _reconnectTimer = Timer(Duration(seconds: backoffSeconds), () {
      if (currentCameraId != null) {
        connect(currentCameraId!, baseUrl: currentBaseUrl);
      }
    });
  }

  Future<void> disconnect() async {
    _reconnectTimer?.cancel();
    _watchdogTimer?.cancel();
    isConnected = false;
    isTalkbackTransmitting = false;

    if (_localAudioTrack != null) {
      await _localAudioTrack!.stop();
      _localAudioTrack = null;
    }
    if (_localAudioStream != null) {
      for (var track in _localAudioStream!.getTracks()) {
        await track.stop();
      }
      await _localAudioStream!.dispose();
      _localAudioStream = null;
    }
    if (renderer.srcObject != null) {
      for (var track in renderer.srcObject!.getTracks()) {
        await track.stop();
      }
      renderer.srcObject = null;
    }
    if (_peerConnection != null) {
      try {
        final senders = await _peerConnection!.getSenders();
        for (var s in senders) {
          await _peerConnection!.removeTrack(s);
        }
        final transceivers = await _peerConnection!.transceivers;
        for (var t in transceivers) {
          await t.stop();
        }
        await _peerConnection!.close();
        await _peerConnection!.dispose();
      } catch (e) {
        developer.log('PeerConnection disposal notice: $e', name: 'WebRtcService');
      }
      _peerConnection = null;
    }
  }

  Future<void> dispose() async {
    await disconnect();
    await renderer.dispose();
    await _connectivitySubscription?.cancel();
  }
}
