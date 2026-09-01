import 'dart:math' as math;
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import '../models/zone_model.dart';

class ZoneCanvasPainter extends CustomPainter {
  final ui.Image? snapshotImage;
  final List<ZoneConfig> existingZones;
  final ZoneConfig? activeDraftZone;
  final int? selectedVertexIndex;
  final int? hoveredVertexIndex;
  final Offset? activeMousePosition;
  final double scale;

  ZoneCanvasPainter({
    required this.snapshotImage,
    required this.existingZones,
    required this.activeDraftZone,
    this.selectedVertexIndex,
    this.hoveredVertexIndex,
    this.activeMousePosition,
    this.scale = 1.0,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final bgPaint = Paint()..color = const Color(0xFF0A0D11);
    canvas.drawRect(Rect.fromLTWH(0, 0, size.width, size.height), bgPaint);

    Rect renderRect = Rect.fromLTWH(0, 0, size.width, size.height);
    if (snapshotImage != null) {
      final double imageAspect = snapshotImage!.width / snapshotImage!.height;
      final double screenAspect = size.width / size.height;

      double rw, rh, rx, ry;
      if (screenAspect > imageAspect) {
        rh = size.height;
        rw = size.height * imageAspect;
        rx = (size.width - rw) / 2;
        ry = 0;
      } else {
        rw = size.width;
        rh = size.width / imageAspect;
        rx = 0;
        ry = (size.height - rh) / 2;
      }
      renderRect = Rect.fromLTWH(rx, ry, rw, rh);

      paintImage(
        canvas: canvas,
        rect: renderRect,
        image: snapshotImage!,
        fit: BoxFit.contain,
      );
    } else {
      _drawGridPattern(canvas, renderRect);
    }

    final borderPaint = Paint()
      ..color = AppTheme.borderHighlight
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5;
    canvas.drawRect(renderRect, borderPaint);

    Offset normalizedToCanvas(Point2D pt) {
      return Offset(
        renderRect.left + (pt.x * renderRect.width),
        renderRect.top + (pt.y * renderRect.height),
      );
    }

    for (var zone in existingZones) {
      if (!zone.enabled) continue;
      _paintZone(canvas, zone, renderRect, normalizedToCanvas, isDraft: false);
    }

    if (activeDraftZone != null) {
      _paintZone(canvas, activeDraftZone!, renderRect, normalizedToCanvas, isDraft: true);
    }
  }

  void _paintZone(
    Canvas canvas,
    ZoneConfig zone,
    Rect renderRect,
    Offset Function(Point2D) toCanvas, {
    required bool isDraft,
  }) {
    Color baseColor = _getZoneColor(zone.zoneType);

    if (zone.zoneType == ZoneType.tripwire) {
      if (zone.lineStart == null || zone.lineEnd == null) return;
      final p1 = toCanvas(zone.lineStart!);
      final p2 = toCanvas(zone.lineEnd!);

      final linePaint = Paint()
        ..color = isDraft ? AppTheme.cyberBlue : baseColor
        ..strokeWidth = 3.0
        ..style = PaintingStyle.stroke;
      canvas.drawLine(p1, p2, linePaint);

      _drawTripwireArrows(canvas, p1, p2, zone.direction, isDraft ? AppTheme.cyberBlue : baseColor);

      _drawVertexHandle(canvas, p1, isHovered: hoveredVertexIndex == 0, isSelected: selectedVertexIndex == 0, label: 'A');
      _drawVertexHandle(canvas, p2, isHovered: hoveredVertexIndex == 1, isSelected: selectedVertexIndex == 1, label: 'B');
    } else {
      if (zone.polygonPoints.isEmpty) return;

      final path = Path();
      for (int i = 0; i < zone.polygonPoints.length; i++) {
        final pt = toCanvas(zone.polygonPoints[i]);
        if (i == 0) {
          path.moveTo(pt.dx, pt.dy);
        } else {
          path.lineTo(pt.dx, pt.dy);
        }
      }
      if (zone.polygonPoints.length > 2) {
        path.close();
      }

      if (zone.zoneType == ZoneType.privacyMask) {
        final maskPaint = Paint()
          ..color = Colors.purple.withOpacity(0.3)
          ..style = PaintingStyle.fill;
        canvas.drawPath(path, maskPaint);
        
        final strokePaint = Paint()
          ..color = Colors.cyanAccent
          ..strokeWidth = isDraft ? 2.5 : 2.0
          ..style = PaintingStyle.stroke;
        canvas.drawPath(path, strokePaint);

        _drawHatchPattern(canvas, path, Colors.white24);

        // Draw badge
        final bounds = path.getBounds();
        final badgeCenter = Offset(bounds.center.dx, bounds.center.dy);
        
        final textSpan = TextSpan(
          text: '🌫️ EXCLUSION / PRIVACY MASK\n${zone.maskMode.name.toUpperCase()}',
          style: const TextStyle(
            color: Colors.white,
            fontSize: 10,
            fontWeight: FontWeight.bold,
            backgroundColor: Colors.black54,
          ),
        );
        final textPainter = TextPainter(
          text: textSpan,
          textAlign: TextAlign.center,
          textDirection: TextDirection.ltr,
        );
        textPainter.layout();
        textPainter.paint(
            canvas, 
            Offset(badgeCenter.dx - textPainter.width / 2, badgeCenter.dy - textPainter.height / 2));

      } else {
        final fillPaint = Paint()
          ..color = (isDraft ? AppTheme.cyberBlue : baseColor).withOpacity(0.22)
          ..style = PaintingStyle.fill;
        canvas.drawPath(path, fillPaint);

        final strokePaint = Paint()
          ..color = isDraft ? AppTheme.cyberBlue : baseColor
          ..strokeWidth = isDraft ? 2.5 : 2.0
          ..style = PaintingStyle.stroke;
        canvas.drawPath(path, strokePaint);
      }

      for (int i = 0; i < zone.polygonPoints.length; i++) {
        final pt = toCanvas(zone.polygonPoints[i]);
        _drawVertexHandle(
          canvas,
          pt,
          isHovered: isDraft && hoveredVertexIndex == i,
          isSelected: isDraft && selectedVertexIndex == i,
          label: '${i + 1}',
        );
      }

      if (isDraft && zone.polygonPoints.length >= 2 && activeMousePosition != null) {
        final guidePaint = Paint()
          ..color = Colors.white38
          ..strokeWidth = 1.0
          ..style = PaintingStyle.stroke;
        final lastPt = toCanvas(zone.polygonPoints.last);
        canvas.drawLine(lastPt, activeMousePosition!, guidePaint);
      }
    }
  }

  void _drawVertexHandle(Canvas canvas, Offset position, {bool isHovered = false, bool isSelected = false, String? label}) {
    final double radius = isSelected ? 8.0 : (isHovered ? 7.0 : 5.5);

    if (isSelected || isHovered) {
      final glowPaint = Paint()
        ..color = AppTheme.cyberBlue.withOpacity(0.4)
        ..style = PaintingStyle.fill;
      canvas.drawCircle(position, radius + 4, glowPaint);
    }

    final handlePaint = Paint()
      ..color = isSelected ? AppTheme.cyberBlue : (isHovered ? Colors.white : const Color(0xFF00E676))
      ..style = PaintingStyle.fill;
    canvas.drawCircle(position, radius, handlePaint);

    final corePaint = Paint()
      ..color = Colors.black
      ..style = PaintingStyle.fill;
    canvas.drawCircle(position, radius * 0.45, corePaint);
  }

  void _drawTripwireArrows(Canvas canvas, Offset p1, Offset p2, TripwireDirection direction, Color color) {
    final double dx = p2.dx - p1.dx;
    final double dy = p2.dy - p1.dy;
    final double length = math.sqrt(dx * dx + dy * dy);
    if (length < 10) return;

    final Offset mid = Offset((p1.dx + p2.dx) / 2, (p1.dy + p2.dy) / 2);
    final double angle = math.atan2(dy, dx);
    final double normAngle = angle + math.pi / 2;

    const double arrowSize = 12.0;
    final arrowPaint = Paint()
      ..color = color
      ..style = PaintingStyle.fill;

    void drawArrowAt(Offset pos, double dirAngle) {
      final path = Path()
        ..moveTo(pos.dx + arrowSize * math.cos(dirAngle), pos.dy + arrowSize * math.sin(dirAngle))
        ..lineTo(pos.dx + arrowSize * math.cos(dirAngle + 2.5), pos.dy + arrowSize * math.sin(dirAngle + 2.5))
        ..lineTo(pos.dx, pos.dy)
        ..lineTo(pos.dx + arrowSize * math.cos(dirAngle - 2.5), pos.dy + arrowSize * math.sin(dirAngle - 2.5))
        ..close();
      canvas.drawPath(path, arrowPaint);
    }

    if (direction == TripwireDirection.aToB || direction == TripwireDirection.bidirectional) {
      drawArrowAt(Offset(mid.dx + 12 * math.cos(normAngle), mid.dy + 12 * math.sin(normAngle)), normAngle);
    }
    if (direction == TripwireDirection.bToA || direction == TripwireDirection.bidirectional) {
      drawArrowAt(Offset(mid.dx - 12 * math.cos(normAngle), mid.dy - 12 * math.sin(normAngle)), normAngle + math.pi);
    }
  }

  void _drawHatchPattern(Canvas canvas, Path clipPath, Color color) {
    canvas.save();
    canvas.clipPath(clipPath);
    final hatchPaint = Paint()
      ..color = color
      ..strokeWidth = 1.2;
    for (double i = -1000; i < 2000; i += 16) {
      canvas.drawLine(Offset(i, 0), Offset(i + 1000, 1000), hatchPaint);
    }
    canvas.restore();
  }

  void _drawGridPattern(Canvas canvas, Rect rect) {
    final gridPaint = Paint()
      ..color = Colors.white.withOpacity(0.04)
      ..strokeWidth = 1;
    for (double x = rect.left; x <= rect.right; x += 40) {
      canvas.drawLine(Offset(x, rect.top), Offset(x, rect.bottom), gridPaint);
    }
    for (double y = rect.top; y <= rect.bottom; y += 40) {
      canvas.drawLine(Offset(rect.left, y), Offset(rect.right, y), gridPaint);
    }
  }

  Color _getZoneColor(ZoneType type) {
    switch (type) {
      case ZoneType.intrusion:
        return AppTheme.emergencyRed;
      case ZoneType.tripwire:
        return AppTheme.warningOrange;
      case ZoneType.privacyMask:
        return Colors.white70;
      case ZoneType.door:
        return AppTheme.cyberBlue;
      case ZoneType.package:
        return AppTheme.liveGreen;
    }
  }

  @override
  bool shouldRepaint(covariant ZoneCanvasPainter oldDelegate) => true;
}
