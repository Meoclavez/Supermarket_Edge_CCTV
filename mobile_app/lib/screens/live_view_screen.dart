import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/security_event.dart';
import '../services/api_service.dart';
import '../services/live_stream_transport.dart';
import '../services/server_registry.dart';
import '../services/webrtc_service.dart';
import '../widgets/mjpeg_view.dart';
import '../widgets/biometric_gate.dart';
import '../widgets/talkback_button.dart';
import '../widgets/timeline_models.dart';
import '../widgets/timeline_scrubber_widget.dart';
import 'camera_settings_screen.dart';
import 'clip_player_screen.dart';
import '../core/server_time.dart';

class LiveViewScreen extends StatefulWidget {
  final CameraFeed camera;

  const LiveViewScreen({Key? key, required this.camera}) : super(key: key);

  @override
  State<LiveViewScreen> createState() => _LiveViewScreenState();
}

class _LiveViewScreenState extends State<LiveViewScreen> {
  final WebRtcService _webrtcService = WebRtcService();
  final ApiService _apiService = ApiService();
  bool _isConnecting = true;
  String? _error;
  LiveTransport _transport = LiveTransport.webrtc;
  Uri? _mjpegUri;
  String? _fallbackReason;
  Timer? _webrtcTimer;
  bool _isMuted = false;

  late CameraFeed _currentCamera;
  List<CameraFeed> _cameras = [];

  List<TimelineRecordingSegment> _recordingSegments = [];
  List<TimelineEventPin> _eventPins = [];

  @override
  void initState() {
    super.initState();
    _currentCamera = widget.camera;
    _loadTimeline();
    _initAndConnect();
    _loadCameras();
  }

  Future<void> _loadCameras() async {
    try {
      final list = await _apiService.getCameras();
      setState(() {
        _cameras = list;
      });
    } catch (e) {
      debugPrint('Error loading cameras: $e');
    }
  }

  void _switchCamera(CameraFeed newCam) {
    if (newCam.id == _currentCamera.id) return;
    setState(() => _currentCamera = newCam);
    _loadTimeline();
    _initAndConnect();
  }

  /// Loads today's recorded segments and event markers from the edge server.
  /// On failure the timeline stays empty rather than showing sample data.
  Future<void> _loadTimeline() async {
    final cameraId = _currentCamera.id;
    setState(() {
      _recordingSegments = [];
      _eventPins = [];
    });
    try {
      final data = await _apiService.getCameraTimeline(cameraId, DateFormat('yyyy-MM-dd').format(DateTime.now()));
      if (!mounted || cameraId != _currentCamera.id) return;
      DateTime? parse(dynamic v) => parseServerTime(v);
      final segments = <TimelineRecordingSegment>[];
      for (final seg in (data['segments'] as List<dynamic>? ?? [])) {
        final start = parse(seg['start_time']);
        final end = parse(seg['end_time']);
        if (start != null && end != null) segments.add(TimelineRecordingSegment(start: start, end: end));
      }
      final pins = <TimelineEventPin>[];
      for (final ev in (data['events'] as List<dynamic>? ?? [])) {
        pins.add(TimelineEventPin(
          event: SecurityEvent.fromJson({
            ...Map<String, dynamic>.from(ev as Map),
            'camera_id': cameraId,
            'camera_name': _currentCamera.name,
            'location': _currentCamera.location,
          }),
        ));
      }
      setState(() {
        _recordingSegments = segments;
        _eventPins = pins;
      });
    } catch (e) {
      debugPrint('Timeline unavailable for $cameraId: $e');
    }
  }

  /// WebRTC on the store LAN; the MJPEG stream over HTTPS when the phone
  /// uses the remote URL, or when WebRTC does not connect in time.
  Future<void> _initAndConnect() async {
    _webrtcTimer?.cancel();
    final cameraId = _currentCamera.id;
    setState(() {
      _isConnecting = true;
      _error = null;
      _fallbackReason = null;
      _transport = LiveTransport.webrtc;
      _mjpegUri = null;
    });
    final baseUrl = _apiService.baseUrl;
    final transport = initialLiveTransport(
      activeUrl: baseUrl,
      remoteUrl: ServerRegistry().active?.remoteUrl,
      iceInfo: await _fetchIceInfo(baseUrl),
    );
    if (!mounted || cameraId != _currentCamera.id) return;
    if (transport == LiveTransport.mjpeg) {
      await _useMjpeg('Remote connection: WebRTC works only on the store network, so this is the HTTPS video stream.');
      return;
    }
    try {
      await _webrtcService.initialize();
      await _webrtcService.connect(cameraId, enableBackchannel: true);
      if (!mounted) return;
      setState(() => _isConnecting = false);
      _webrtcTimer = Timer(kWebRtcConnectTimeout, () {
        if (mounted && _transport == LiveTransport.webrtc && !_webrtcService.isConnected) {
          _useMjpeg('WebRTC did not connect within ${kWebRtcConnectTimeout.inSeconds} s, so this is the HTTPS video stream.');
        }
      });
    } catch (e) {
      await _useMjpeg('WebRTC could not start, so this is the HTTPS video stream.');
    }
  }

  Future<Map<String, dynamic>?> _fetchIceInfo(String baseUrl) async {
    try {
      final res = await http
          .get(Uri.parse('$baseUrl/api/v1/webrtc/ice-servers'), headers: await _apiService.authHeaders())
          .timeout(const Duration(seconds: 3));
      if (res.statusCode == 200) {
        final data = jsonDecode(res.body);
        if (data is Map<String, dynamic>) return data;
      }
    } catch (_) {}
    return null;
  }

  Future<void> _useMjpeg(String reason) async {
    _webrtcTimer?.cancel();
    try {
      await _webrtcService.disconnect();
    } catch (_) {}
    final registry = ServerRegistry();
    final token = registry.activeId == null ? null : await registry.accessToken(registry.activeId!);
    if (!mounted) return;
    setState(() {
      _transport = LiveTransport.mjpeg;
      _mjpegUri = mjpegStreamUri(_apiService.baseUrl, _currentCamera.id, token: token);
      _fallbackReason = reason;
      _isConnecting = false;
      _error = null;
    });
  }

  @override
  void dispose() {
    _webrtcTimer?.cancel();
    _webrtcService.dispose();
    super.dispose();
  }

  /// Video viewers stay dark in every app theme: the controls sit on and
  /// around black video, and the overlays use constant colours.
  @override
  Widget build(BuildContext context) =>
      Theme(data: AppTheme.viewerTheme, child: Builder(builder: _buildViewer));

  Widget _buildViewer(BuildContext context) {
    return BiometricGate(
      promptReason: 'Authenticate to view camera ${_currentCamera.name}',
      child: Scaffold(
        backgroundColor: Colors.black,
        appBar: AppBar(
          backgroundColor: Colors.black,
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(_currentCamera.name, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
              Text('${_currentCamera.location} • ${_transport == LiveTransport.mjpeg ? 'HTTPS stream' : 'WebRTC live'}', style: const TextStyle(fontSize: 11, color: AppTheme.liveGreen)),
            ],
          ),
          actions: [
            IconButton(
              icon: Icon(_isMuted ? Icons.volume_off : Icons.volume_up),
              onPressed: () => setState(() => _isMuted = !_isMuted),
            ),
            IconButton(
              icon: const Icon(Icons.refresh),
              onPressed: _initAndConnect,
            ),
            IconButton(
              icon: const Icon(Icons.tune),
              tooltip: 'Camera analytics',
              onPressed: () => Navigator.push(
                context,
                MaterialPageRoute(
                  builder: (context) => CameraSettingsScreen(camera: _currentCamera, onUpdated: _loadCameras),
                ),
              ),
            ),
          ],
        ),
        body: SafeArea(
          child: Column(
            children: [
              if (_cameras.isNotEmpty)
                Container(
                  height: 40,
                  margin: const EdgeInsets.symmetric(vertical: 8),
                  child: ListView.builder(
                    scrollDirection: Axis.horizontal,
                    itemCount: _cameras.length,
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    itemBuilder: (context, index) {
                      final cam = _cameras[index];
                      final isSelected = cam.id == _currentCamera.id;
                      return Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: ChoiceChip(
                          label: Text(cam.name, style: TextStyle(fontSize: 12, color: isSelected ? Colors.black : Colors.white)),
                          selected: isSelected,
                          selectedColor: AppTheme.cyberBlue,
                          backgroundColor: AppTheme.cardSurface,
                          onSelected: (val) {
                            if (val) _switchCamera(cam);
                          },
                        ),
                      );
                    },
                  ),
                ),
              // 1. Live WebRTC Video Viewport
              Expanded(
                flex: 5,
                child: Stack(
                  children: [
                    Center(
                      child: _transport == LiveTransport.mjpeg && _mjpegUri != null
                          ? MjpegView(uri: _mjpegUri!)
                          : _isConnecting
                          ? const Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                CircularProgressIndicator(color: AppTheme.cyberBlue),
                                SizedBox(height: 12),
                                Text('Connecting WebRTC Ultra-Low Latency Feed...', style: TextStyle(color: Colors.white60, fontSize: 12)),
                              ],
                            )
                          : _error != null
                              ? Column(
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  children: [
                                    const Icon(Icons.error_outline, color: AppTheme.alertRed, size: 48),
                                    const SizedBox(height: 8),
                                    Text('WebRTC Failed: $_error', style: const TextStyle(color: Colors.white70, fontSize: 12), textAlign: TextAlign.center),
                                    const SizedBox(height: 12),
                                    ElevatedButton(onPressed: _initAndConnect, child: const Text('Retry')),
                                  ],
                                )
                              : RTCVideoView(
                                  _webrtcService.renderer,
                                  objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover,
                                ),
                    ),
                    if (_fallbackReason != null)
                      Positioned(
                        left: 12,
                        right: 12,
                        bottom: 8,
                        child: Text(
                          _fallbackReason!,
                          key: const ValueKey('live-fallback-reason'),
                          style: const TextStyle(color: Colors.white60, fontSize: 11),
                        ),
                      ),
                    Positioned(
                      top: 12,
                      left: 12,
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                        decoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.6),
                          borderRadius: BorderRadius.circular(4),
                          border: Border.all(color: AppTheme.liveGreen),
                        ),
                        child: Row(
                          children: [
                            const Icon(Icons.fiber_manual_record, color: AppTheme.liveGreen, size: 10),
                            const SizedBox(width: 4),
                            Text(_transport == LiveTransport.mjpeg ? 'LIVE (MJPEG / HTTPS)' : 'LIVE (WebRTC / LAN)',
                                style: const TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.bold)),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),

              // 2. Control bar (store PA push-to-talk, privacy masking info)
              Container(
                padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 24),
                color: AppTheme.cardSurface,
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    if (_transport == LiveTransport.webrtc)
                      TalkbackButton(webrtcService: _webrtcService)
                    else
                      const Tooltip(
                        message: 'Store PA needs WebRTC on the store network',
                        child: Icon(Icons.mic_off, color: Colors.white38),
                      ),
                    IconButton(
                      icon: const Icon(Icons.security, color: AppTheme.cyberBlue),
                      tooltip: 'Privacy Masking Active',
                      onPressed: () {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Privacy masks are applied on the edge server before video leaves it.')),
                        );
                      },
                    ),
                  ],
                ),
              ),

              // 3. 24-Hour Interactive Timeline Scrubber
              Expanded(
                flex: 4,
                child: Padding(
                  padding: const EdgeInsets.all(12.0),
                  child: TimelineScrubberWidget(
                    initialTime: DateTime.now(),
                    recordingSegments: _recordingSegments,
                    eventPins: _eventPins,
                    onSeek: (selectedTime) {
                      debugPrint('Seeked timeline to: $selectedTime');
                    },
                    onEventSelected: (event) {
                      Navigator.push(
                        context,
                        MaterialPageRoute(builder: (context) => ClipPlayerScreen(event: event)),
                      );
                    },
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
