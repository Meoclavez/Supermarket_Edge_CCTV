import 'dart:ui';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

class AppTheme {
  // --- OLED True Black & Cyber Surface Palette ---
  static const Color oledBlack = Color(0xFF000000);
  static const Color darkBackground = Color(0xFF080A0E);
  static const Color cardSurface = Color(0xFF12161F);
  static const Color glassSurface = Color(0xCC121722);
  static const Color elevatedSurface = Color(0xFF1B222E);
  static const Color borderHighlight = Color(0xFF222B3A);
  static const Color borderGlow = Color(0x4D00E5FF);

  // --- High-Contrast Surveillance Accents ---
  static const Color cyberBlue = Color(0xFF00E5FF);
  static const Color cyberCyan = Color(0xFF00E5FF);
  static const Color liveGreen = Color(0xFF00E676);
  static const Color emergencyRed = Color(0xFFFF1744);
  static const Color warningOrange = Color(0xFFFF9100);
  static const Color hailoPurple = Color(0xFFB388FF);
  static const Color textMuted = Color(0xFF8A99AD);

  static ThemeData get darkTheme {
    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      scaffoldBackgroundColor: darkBackground,
      colorScheme: const ColorScheme.dark(
        primary: cyberBlue,
        onPrimary: oledBlack,
        secondary: liveGreen,
        onSecondary: oledBlack,
        error: emergencyRed,
        onError: Colors.white,
        surface: cardSurface,
        onSurface: Colors.white,
      ),
      appBarTheme: const AppBarTheme(
        backgroundColor: darkBackground,
        elevation: 0,
        centerTitle: false,
        scrolledUnderElevation: 0,
        titleTextStyle: TextStyle(
          color: Colors.white,
          fontSize: 16,
          fontWeight: FontWeight.bold,
          letterSpacing: 1.1,
        ),
      ),
      cardTheme: CardTheme(
        color: cardSurface,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: const BorderSide(color: borderHighlight, width: 1),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: cardSurface,
        hintStyle: const TextStyle(color: textMuted, fontSize: 13),
        labelStyle: const TextStyle(color: cyberBlue, fontSize: 13),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: borderHighlight),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: borderHighlight),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: const BorderSide(color: cyberBlue, width: 1.5),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: cyberBlue,
          foregroundColor: oledBlack,
          elevation: 2,
          textStyle: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13, letterSpacing: 0.5),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: Colors.white,
          side: const BorderSide(color: borderHighlight),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        ),
      ),
    );
  }
}

/// Reusable Modern Frosted Glass Card
class GlassCard extends StatelessWidget {
  final Widget child;
  final EdgeInsetsGeometry padding;
  final EdgeInsetsGeometry? margin;
  final double borderRadius;
  final Color? borderColor;
  final double blurSigma;
  final VoidCallback? onTap;

  const GlassCard({
    Key? key,
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.margin,
    this.borderRadius = 14,
    this.borderColor,
    this.blurSigma = 16.0,
    this.onTap,
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    Widget content = ClipRRect(
      borderRadius: BorderRadius.circular(borderRadius),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: blurSigma, sigmaY: blurSigma),
        child: Container(
          padding: padding,
          decoration: BoxDecoration(
            color: AppTheme.glassSurface,
            borderRadius: BorderRadius.circular(borderRadius),
            border: Border.all(
              color: borderColor ?? AppTheme.borderHighlight.withOpacity(0.8),
              width: 1,
            ),
          ),
          child: child,
        ),
      ),
    );

    if (margin != null) {
      content = Padding(padding: margin!, child: content);
    }
    if (onTap != null) {
      return GestureDetector(onTap: onTap, child: content);
    }
    return content;
  }
}

/// Status Badge with Glow
class CyberBadge extends StatelessWidget {
  final String label;
  final Color color;
  final bool isGlowing;
  final IconData? icon;

  const CyberBadge({
    Key? key,
    required this.label,
    required this.color,
    this.isGlowing = false,
    this.icon,
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withOpacity(0.6), width: 1),
        boxShadow: isGlowing
            ? [BoxShadow(color: color.withOpacity(0.35), blurRadius: 8, spreadRadius: 1)]
            : [],
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 11, color: color),
            const SizedBox(width: 4),
          ] else ...[
            Container(width: 6, height: 6, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
            const SizedBox(width: 5),
          ],
          Text(
            label,
            style: TextStyle(
              color: color,
              fontSize: 10,
              fontWeight: FontWeight.bold,
              letterSpacing: 0.8,
            ),
          ),
        ],
      ),
    );
  }
}

/// Monospace Telemetry Chip for Latency/FPS/Inference
class TelemetryChip extends StatelessWidget {
  final String label;
  final String value;
  final Color valueColor;

  const TelemetryChip({
    Key? key,
    required this.label,
    required this.value,
    this.valueColor = AppTheme.cyberBlue,
  }) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
      decoration: BoxDecoration(
        color: Colors.black.withOpacity(0.4),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: AppTheme.borderHighlight.withOpacity(0.5)),
      ),
      child: RichText(
        text: TextSpan(
          style: const TextStyle(fontSize: 10, fontFamily: 'monospace'),
          children: [
            TextSpan(text: '$label: ', style: const TextStyle(color: AppTheme.textMuted)),
            TextSpan(text: value, style: TextStyle(color: valueColor, fontWeight: FontWeight.bold)),
          ],
        ),
      ),
    );
  }
}
