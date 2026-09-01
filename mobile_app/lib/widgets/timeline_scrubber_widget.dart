import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/security_event.dart';
import 'timeline_models.dart';
import 'timeline_painter.dart';

class TimelineScrubberWidget extends StatefulWidget {
  final DateTime initialTime;
  final List<TimelineRecordingSegment> recordingSegments;
  final List<TimelineEventPin> eventPins;
  final ValueChanged<DateTime> onSeek;
  final ValueChanged<SecurityEvent>? onEventSelected;

  const TimelineScrubberWidget({
    Key? key,
    required this.initialTime,
    required this.recordingSegments,
    required this.eventPins,
    required this.onSeek,
    this.onEventSelected,
  }) : super(key: key);

  @override
  State<TimelineScrubberWidget> createState() => _TimelineScrubberWidgetState();
}

class _TimelineScrubberWidgetState extends State<TimelineScrubberWidget> {
  late DateTime _selectedTime;
  late DateTime _windowStart;
  late DateTime _windowEnd;

  double _visibleHours = 12.0;
  static const double _minHours = 1.0;
  static const double _maxHours = 24.0;
  static const Duration _snapThreshold = Duration(minutes: 2);

  @override
  void initState() {
    super.initState();
    _selectedTime = widget.initialTime;
    _recalculateWindow();
  }

  void _recalculateWindow() {
    final halfSpan = Duration(minutes: ((_visibleHours * 60) / 2).round());
    _windowStart = _selectedTime.subtract(halfSpan);
    _windowEnd = _selectedTime.add(halfSpan);
  }

  void _handleDrag(DragUpdateDetails details, double widgetWidth) {
    final double totalSeconds = _windowEnd.difference(_windowStart).inSeconds.toDouble();
    final double secondsPerPixel = totalSeconds / widgetWidth;
    final double deltaSeconds = -details.primaryDelta! * secondsPerPixel;

    DateTime newTime = _selectedTime.add(Duration(milliseconds: (deltaSeconds * 1000).round()));

    for (var pin in widget.eventPins) {
      if (newTime.difference(pin.timestamp).abs() < _snapThreshold) {
        newTime = pin.timestamp;
        HapticFeedback.selectionClick();
        if (widget.onEventSelected != null) {
          widget.onEventSelected!(pin.event);
        }
        break;
      }
    }

    setState(() {
      _selectedTime = newTime;
      _recalculateWindow();
    });
    widget.onSeek(_selectedTime);
  }

  void _handlePinchZoom(ScaleUpdateDetails details) {
    if (details.scale == 1.0) return;
    setState(() {
      _visibleHours = (_visibleHours / details.scale).clamp(_minHours, _maxHours);
      _recalculateWindow();
    });
  }

  @override
  Widget build(BuildContext context) {
    final timeStr = DateFormat('yyyy-MM-dd  HH:mm:ss').format(_selectedTime);

    return Container(
      decoration: BoxDecoration(
        color: AppTheme.cardSurface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppTheme.borderHighlight),
      ),
      padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Row(
                children: [
                  const Icon(Icons.history_toggle_off, color: AppTheme.cyberBlue, size: 18),
                  const SizedBox(width: 6),
                  Text(
                    timeStr,
                    style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13),
                  ),
                ],
              ),
              Row(
                children: [
                  _buildZoomChip('1h', 1.0),
                  _buildZoomChip('4h', 4.0),
                  _buildZoomChip('12h', 12.0),
                  _buildZoomChip('24h', 24.0),
                ],
              ),
            ],
          ),
          const SizedBox(height: 12),
          LayoutBuilder(
            builder: (context, constraints) {
              return GestureDetector(
                onHorizontalDragUpdate: (details) => _handleDrag(details, constraints.maxWidth),
                onScaleUpdate: _handlePinchZoom,
                child: SizedBox(
                  width: constraints.maxWidth,
                  height: 70,
                  child: CustomPaint(
                    painter: TimelinePainter(
                      windowStart: _windowStart,
                      windowEnd: _windowEnd,
                      selectedTime: _selectedTime,
                      recordingSegments: widget.recordingSegments,
                      eventPins: widget.eventPins,
                      pixelsPerHour: constraints.maxWidth / _visibleHours,
                    ),
                  ),
                ),
              );
            },
          ),
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              _buildLegendItem(AppTheme.liveGreen, 'Continuous Recording'),
              const SizedBox(width: 20),
              _buildLegendItem(AppTheme.emergencyRed, 'AI Detection Event'),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildZoomChip(String label, double hours) {
    final bool isSelected = (_visibleHours - hours).abs() < 0.5;
    return GestureDetector(
      onTap: () {
        setState(() {
          _visibleHours = hours;
          _recalculateWindow();
        });
      },
      child: Container(
        margin: const EdgeInsets.only(left: 4),
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
        decoration: BoxDecoration(
          color: isSelected ? AppTheme.cyberBlue : Colors.white10,
          borderRadius: BorderRadius.circular(4),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: 10,
            fontWeight: FontWeight.bold,
            color: isSelected ? Colors.black : Colors.white70,
          ),
        ),
      ),
    );
  }

  Widget _buildLegendItem(Color color, String label) {
    return Row(
      children: [
        Container(width: 8, height: 8, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
        const SizedBox(width: 4),
        Text(label, style: const TextStyle(fontSize: 10, color: Colors.white54)),
      ],
    );
  }
}
