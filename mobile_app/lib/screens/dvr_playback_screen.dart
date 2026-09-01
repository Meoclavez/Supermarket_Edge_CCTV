import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../widgets/timeline_models.dart';
import '../widgets/timeline_scrubber_widget.dart';

class DVRPlaybackScreen extends StatefulWidget {
  const DVRPlaybackScreen({Key? key}) : super(key: key);

  @override
  State<DVRPlaybackScreen> createState() => _DVRPlaybackScreenState();
}

class _DVRPlaybackScreenState extends State<DVRPlaybackScreen> {
  String _selectedCamera = 'Camera 01 - Main Gate';
  DateTime _selectedDate = DateTime.now();
  DateTime _playheadTime = DateTime.now().subtract(const Duration(hours: 2));

  late List<TimelineRecordingSegment> _recordingSegments;
  late List<TimelineEventPin> _eventPins;

  @override
  void initState() {
    super.initState();
    _loadTimelineData();
  }

  void _loadTimelineData() {
    final now = DateTime.now();
    _recordingSegments = [
      TimelineRecordingSegment(start: now.subtract(const Duration(hours: 18)), end: now.subtract(const Duration(hours: 8))),
      TimelineRecordingSegment(start: now.subtract(const Duration(hours: 7)), end: now),
    ];
    _eventPins = [];
  }

  @override
  Widget build(BuildContext context) {
    final dateStr = DateFormat('yyyy-MM-dd').format(_selectedDate);

    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('24/7 CONTINUOUS DVR PLAYBACK', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            icon: const Icon(Icons.calendar_today_rounded, color: AppTheme.cyberBlue, size: 20),
            onPressed: () async {
              final picked = await showDatePicker(
                context: context,
                initialDate: _selectedDate,
                firstDate: DateTime.now().subtract(const Duration(days: 30)),
                lastDate: DateTime.now(),
              );
              if (picked != null) {
                setState(() => _selectedDate = picked);
              }
            },
          ),
        ],
      ),
      body: Column(
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            color: AppTheme.cardSurface,
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text('Date: $dateStr', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13)),
                Text(_selectedCamera, style: const TextStyle(color: AppTheme.cyberBlue, fontSize: 13)),
              ],
            ),
          ),
          Expanded(
            flex: 6,
            child: Container(
              color: Colors.black,
              child: Center(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(Icons.play_circle_fill_rounded, color: AppTheme.cyberBlue, size: 64),
                    const SizedBox(height: 12),
                    Text(
                      'DVR Playhead: ${DateFormat('HH:mm:ss').format(_playheadTime)}',
                      style: const TextStyle(color: Colors.white70, fontSize: 14, fontWeight: FontWeight.bold),
                    ),
                    const SizedBox(height: 6),
                    const Text('Zero-Copy 1-Min Segment HLS Stream', style: TextStyle(color: Colors.white38, fontSize: 11)),
                  ],
                ),
              ),
            ),
          ),
          Expanded(
            flex: 4,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: TimelineScrubberWidget(
                initialTime: _playheadTime,
                recordingSegments: _recordingSegments,
                eventPins: _eventPins,
                onSeek: (time) {
                  setState(() => _playheadTime = time);
                },
              ),
            ),
          ),
        ],
      ),
    );
  }
}
