import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/security_event.dart';
import '../services/api_service.dart';

class ClipPlayerScreen extends StatefulWidget {
  final SecurityEvent event;

  const ClipPlayerScreen({Key? key, required this.event}) : super(key: key);

  @override
  State<ClipPlayerScreen> createState() => _ClipPlayerScreenState();
}

class _ClipPlayerScreenState extends State<ClipPlayerScreen> {
  VideoPlayerController? _controller;
  bool _isInitialized = false;
  String? _error;
  double _playbackSpeed = 1.0;

  @override
  void initState() {
    super.initState();
    _initVideo();
  }

  void _initVideo() {
    final baseUrl = ApiService().baseUrl;
    final clipUrl = widget.event.clipUrl ?? '$baseUrl/api/v1/events/clips/${widget.event.id}.mp4';

    _controller = VideoPlayerController.networkUrl(Uri.parse(clipUrl))
      ..initialize().then((_) {
        setState(() {
          _isInitialized = true;
        });
        _controller?.play();
        _controller?.setLooping(true);
      }).catchError((err) {
        setState(() {
          _error = err.toString();
        });
      });

    _controller?.addListener(() {
      setState(() {});
    });
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  /// Video viewers stay dark in every app theme: the controls sit on and
  /// around black video, and the overlays use constant colours.
  @override
  Widget build(BuildContext context) =>
      Theme(data: AppTheme.viewerTheme, child: Builder(builder: _buildViewer));

  Widget _buildViewer(BuildContext context) {
    final timeStr = DateFormat('yyyy-MM-dd HH:mm:ss').format(widget.event.timestamp);

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.black,
        title: Text(widget.event.eventType.replaceAll('_', ' '), style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            icon: const Icon(Icons.download_rounded),
            onPressed: () {
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Downloading 15s MP4 clip...')),
              );
            },
          ),
          IconButton(
            icon: const Icon(Icons.share),
            onPressed: () {
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Exporting signed event clip link...')),
              );
            },
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Video Player
            AspectRatio(
              aspectRatio: _isInitialized ? _controller!.value.aspectRatio : 16 / 9,
              child: Stack(
                alignment: Alignment.bottomCenter,
                children: [
                  _isInitialized
                      ? VideoPlayer(_controller!)
                      : _error != null
                          ? Container(
                              color: Colors.black87,
                              child: Center(
                                child: Text('Failed to load clip: $_error', style: const TextStyle(color: Colors.white60)),
                              ),
                            )
                          : const Center(child: CircularProgressIndicator(color: AppTheme.cyberBlue)),
                  if (_isInitialized)
                    VideoProgressIndicator(
                      _controller!,
                      allowScrubbing: true,
                      colors: const VideoProgressColors(
                        playedColor: AppTheme.alertRed,
                        bufferedColor: Colors.white24,
                        backgroundColor: Colors.black54,
                      ),
                    ),
                ],
              ),
            ),

            // Video Controls
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  IconButton(
                    icon: Icon(_controller?.value.isPlaying ?? false ? Icons.pause : Icons.play_arrow),
                    iconSize: 32,
                    onPressed: () {
                      setState(() {
                        _controller!.value.isPlaying ? _controller!.pause() : _controller!.play();
                      });
                    },
                  ),
                  Row(
                    children: [
                      _buildSpeedButton(0.5),
                      _buildSpeedButton(1.0),
                      _buildSpeedButton(2.0),
                    ],
                  ),
                ],
              ),
            ),

            const Divider(color: AppTheme.borderHighlight),

            // Event Metadata Details
            Expanded(
              child: Padding(
                padding: const EdgeInsets.all(16.0),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Text('Event ID: ${widget.event.id}', style: const TextStyle(color: Colors.white54, fontSize: 12)),
                        Container(
                          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                          decoration: BoxDecoration(
                            color: widget.event.isHigh ? AppTheme.alertRed.withOpacity(0.2) : AppTheme.warningOrange.withOpacity(0.2),
                            borderRadius: BorderRadius.circular(4),
                          ),
                          child: Text(
                            widget.event.severity,
                            style: TextStyle(
                              color: widget.event.isHigh ? AppTheme.alertRed : AppTheme.warningOrange,
                              fontSize: 11,
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    Text('Location: ${widget.event.location} (${widget.event.cameraName})', style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                    const SizedBox(height: 4),
                    Text('Detected: $timeStr', style: const TextStyle(color: Colors.white70, fontSize: 13)),
                    const SizedBox(height: 4),
                    Text('AI Confidence: ${(widget.event.confidence * 100).toStringAsFixed(1)}%', style: const TextStyle(color: AppTheme.liveGreen, fontSize: 13)),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSpeedButton(double speed) {
    final isSelected = _playbackSpeed == speed;
    return GestureDetector(
      onTap: () {
        setState(() {
          _playbackSpeed = speed;
          _controller?.setPlaybackSpeed(speed);
        });
      },
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 4),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: isSelected ? AppTheme.cyberBlue : AppTheme.cardSurface,
          borderRadius: BorderRadius.circular(4),
          border: Border.all(color: AppTheme.borderHighlight),
        ),
        child: Text('${speed}x', style: TextStyle(color: isSelected ? Colors.black : Colors.white70, fontSize: 12, fontWeight: FontWeight.bold)),
      ),
    );
  }
}
