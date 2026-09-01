import 'package:flutter/material.dart';
import '../models/security_event.dart';

class TimelineRecordingSegment {
  final DateTime start;
  final DateTime end;
  final String quality;

  TimelineRecordingSegment({
    required this.start,
    required this.end,
    this.quality = '1080p',
  });

  bool contains(DateTime time) => time.isAfter(start) && time.isBefore(end);
}

class TimelineEventPin {
  final SecurityEvent event;
  final Color color;

  TimelineEventPin({
    required this.event,
    this.color = const Color(0xFFFF1744),
  });

  DateTime get timestamp => event.timestamp;
}
