import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import 'timeline_models.dart';

class TimelinePainter extends CustomPainter {
  final DateTime windowStart;
  final DateTime windowEnd;
  final DateTime selectedTime;
  final List<TimelineRecordingSegment> recordingSegments;
  final List<TimelineEventPin> eventPins;
  final double pixelsPerHour;

  TimelinePainter({
    required this.windowStart,
    required this.windowEnd,
    required this.selectedTime,
    required this.recordingSegments,
    required this.eventPins,
    required this.pixelsPerHour,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final double totalSeconds = windowEnd.difference(windowStart).inSeconds.toDouble();
    if (totalSeconds <= 0) return;

    double timeToX(DateTime time) {
      final double elapsed = time.difference(windowStart).inSeconds.toDouble();
      return (elapsed / totalSeconds) * size.width;
    }

    // 1. Background Track
    final bgPaint = Paint()..color = const Color(0xFF161922);
    canvas.drawRect(Rect.fromLTWH(0, 0, size.width, size.height), bgPaint);

    // 2. Green Continuous Recording Bands
    final recPaint = Paint()
      ..color = AppTheme.liveGreen.withOpacity(0.4)
      ..style = PaintingStyle.fill;

    for (var seg in recordingSegments) {
      final double startX = timeToX(seg.start.isBefore(windowStart) ? windowStart : seg.start);
      final double endX = timeToX(seg.end.isAfter(windowEnd) ? windowEnd : seg.end);
      if (endX > startX) {
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTRB(startX, size.height * 0.45, endX, size.height * 0.85),
            const Radius.circular(3),
          ),
          recPaint,
        );
      }
    }

    // 3. Time Ruler Ticks & Labels
    final tickPaint = Paint()..strokeWidth = 1.0;
    final textPainter = TextPainter(textDirection: TextDirection.ltr);

    DateTime currentHour = DateTime(windowStart.year, windowStart.month, windowStart.day, windowStart.hour);
    while (currentHour.isBefore(windowEnd.add(const Duration(hours: 1)))) {
      final double x = timeToX(currentHour);
      if (x >= 0 && x <= size.width) {
        tickPaint.color = Colors.white38;
        canvas.drawLine(Offset(x, 0), Offset(x, size.height * 0.35), tickPaint);

        final timeStr = DateFormat('HH:mm').format(currentHour);
        textPainter.text = TextSpan(
          text: timeStr,
          style: const TextStyle(color: Colors.white60, fontSize: 9, fontWeight: FontWeight.bold),
        );
        textPainter.layout();
        textPainter.paint(canvas, Offset(x - (textPainter.width / 2), size.height * 0.05));
      }

      for (int m = 15; m < 60; m += 15) {
        final subTime = currentHour.add(Duration(minutes: m));
        final double subX = timeToX(subTime);
        if (subX >= 0 && subX <= size.width) {
          tickPaint.color = Colors.white12;
          canvas.drawLine(Offset(subX, size.height * 0.2), Offset(subX, size.height * 0.35), tickPaint);
        }
      }

      currentHour = currentHour.add(const Duration(hours: 1));
    }

    // 4. Red AI Event Pins
    for (var pin in eventPins) {
      if (pin.timestamp.isAfter(windowStart) && pin.timestamp.isBefore(windowEnd)) {
        final double pinX = timeToX(pin.timestamp);

        final linePaint = Paint()
          ..color = pin.color.withOpacity(0.8)
          ..strokeWidth = 2.0;
        canvas.drawLine(Offset(pinX, size.height * 0.3), Offset(pinX, size.height), linePaint);

        final diamondPath = Path()
          ..moveTo(pinX, size.height * 0.3)
          ..lineTo(pinX - 5, (size.height * 0.3) - 7)
          ..lineTo(pinX, (size.height * 0.3) - 14)
          ..lineTo(pinX + 5, (size.height * 0.3) - 7)
          ..close();

        final diamondPaint = Paint()
          ..color = pin.color
          ..style = PaintingStyle.fill;
        canvas.drawPath(diamondPath, diamondPaint);
      }
    }

    // 5. Center Playhead Needle
    final double playheadX = timeToX(selectedTime);
    if (playheadX >= 0 && playheadX <= size.width) {
      final needlePaint = Paint()
        ..color = AppTheme.cyberBlue
        ..strokeWidth = 2.5;
      canvas.drawLine(Offset(playheadX, 0), Offset(playheadX, size.height), needlePaint);

      final pointerPath = Path()
        ..moveTo(playheadX - 6, 0)
        ..lineTo(playheadX + 6, 0)
        ..lineTo(playheadX, 8)
        ..close();
      canvas.drawPath(pointerPath, Paint()..color = AppTheme.cyberBlue);
    }
  }

  @override
  bool shouldRepaint(covariant TimelinePainter oldDelegate) {
    return oldDelegate.selectedTime != selectedTime ||
        oldDelegate.windowStart != windowStart ||
        oldDelegate.windowEnd != windowEnd ||
        oldDelegate.pixelsPerHour != pixelsPerHour;
  }
}
