import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/security_event.dart';
import '../services/webrtc_service.dart';
import '../widgets/biometric_gate.dart';
import '../widgets/talkback_button.dart';
import '../widgets/timeline_models.dart';
import '../widgets/timeline_scrubber_widget.dart';
import 'clip_player_screen.dart';

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
  bool _isMuted = false;

  late CameraFeed _currentCamera;
  List<CameraFeed> _cameras = [];

  late List<TimelineRecordingSegment> _recordingSegments;
  late List<TimelineEventPin> _eventPins;

  @override
  void initState() {
    super.initState();
    _currentCamera = widget.camera;
    _initTimelineMockData();
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
    setState(() {
      _currentCamera = newCam;
      _initTimelineMockData();
    });
    _initAndConnect();
  }

  void _initTimelineMockData() {
    final now = DateTime.now();
    _recordingSegments = [
      TimelineRecordingSegment(start: now.subtract(const Duration(hours: 12)), end: now.subtract(const Duration(hours: 5))),
      TimelineRecordingSegment(start: now.subtract(const Duration(hours: 4, minutes: 30)), end: now),
    ];
    _eventPins = [
      TimelineEventPin(
        event: SecurityEvent(
          id: 'ev_01',
          cameraId: _currentCamera.id,
          cameraName: _currentCamera.name,
          location: _currentCamera.location,
          eventType: 'FALL_DETECTED',
          severity: 'CRITICAL',
          confidence: 0.96,
          timestamp: now.subtract(const Duration(hours: 2, minutes: 15)),
          acknowledged: false,
        ),
      ),
    ];
  }

  Future<void> _initAndConnect() async {
    setState(() {
      _isConnecting = true;
      _error = null;
    });
    try {
      await _webrtcService.initialize();
      await _webrtcService.connect(_currentCamera.id, enableBackchannel: true);
      setState(() => _isConnecting = false);
    } catch (e) {
      setState(() {
        _error = e.toString();
        _isConnecting = false;
      });
    }
  }

  @override
  void dispose() {
    _webrtcService.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return BiometricGate(
      promptReason: 'Authenticate to view secure camera ${_currentCamera.name}',
      child: Scaffold(
        backgroundColor: Colors.black,
        appBar: AppBar(
          backgroundColor: Colors.black,
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(_currentCamera.name, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
              Text('${_currentCamera.location} • WebRTC <300ms + 2-Way Audio', style: const TextStyle(fontSize: 11, color: AppTheme.liveGreen)),
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
                      child: _isConnecting
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
                                    const Icon(Icons.error_outline, color: AppTheme.emergencyRed, size: 48),
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
                    Positioned(
                      top: 12,
                      left: 12,
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                        decoration: BoxDecoration(
                          color: Colors.black.withOpacity(0.6),
                          borderRadius: BorderRadius.circular(4),
                          border: Border.all(color: AppTheme.liveGreen),
                        ),
                        child: const Row(
                          children: [
                            Icon(Icons.fiber_manual_record, color: AppTheme.liveGreen, size: 10),
                            SizedBox(width: 4),
                            Text('LIVE (P2P / RELAY)', style: TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.bold)),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),

              // 2. Control Bar (Push-to-Talk 2-Way Audio & Privacy Zones)
              Container(
                padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 24),
                color: AppTheme.cardSurface,
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    TalkbackButton(webrtcService: _webrtcService),
                    IconButton(
                      icon: const Icon(Icons.security, color: AppTheme.cyberBlue),
                      tooltip: 'Privacy Masking Active',
                      onPressed: () {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Privacy Masking is enforced directly on Edge N100 hardware.')),
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
