import 'dart:ui';
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
  static const Color alertRed = Color(0xFFFF1744);
  static const Color warningOrange = Color(0xFFFF9100);
  static const Color hailoPurple = Color(0xFFB388FF);
  static const Color textMuted = Color(0xFF8A99AD);

  // --- Light ("white") surface palette ---
  static const Color lightBackground = Color(0xFFF4F6FA);
  static const Color lightCard = Color(0xFFFFFFFF);
  static const Color lightElevated = Color(0xFFE9EDF3);
  static const Color lightBorder = Color(0xFFCBD3DE);
  static const Color lightInk = Color(0xFF0E1726);

  static ThemeData get darkTheme => _build(AppPalette.dark, Brightness.dark);

  static ThemeData get lightTheme => _build(AppPalette.light, Brightness.light);

  /// Dark theme kept for full-screen video viewers regardless of the app theme.
  static final ThemeData viewerTheme = darkTheme;

  static ThemeData _build(AppPalette p, Brightness brightness) {
    final isDark = brightness == Brightness.dark;
    final ColorScheme scheme = isDark
        ? const ColorScheme.dark(
            primary: cyberBlue,
            onPrimary: oledBlack,
            secondary: liveGreen,
            onSecondary: oledBlack,
            error: alertRed,
            onError: Colors.white,
            surface: cardSurface,
            onSurface: Colors.white,
          )
        : ColorScheme.fromSeed(seedColor: AppPalette.light.accent, brightness: Brightness.light).copyWith(
            primary: AppPalette.light.accent,
            onPrimary: Colors.white,
            secondary: AppPalette.light.live,
            onSecondary: Colors.white,
            error: AppPalette.light.alert,
            onError: Colors.white,
            surface: lightCard,
            onSurface: lightInk,
            onSurfaceVariant: AppPalette.light.muted,
            outline: lightBorder,
            outlineVariant: lightBorder,
          );
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      scaffoldBackgroundColor: p.background,
      colorScheme: scheme,
      extensions: <ThemeExtension<dynamic>>[p],
      appBarTheme: AppBarTheme(
        backgroundColor: p.background,
        foregroundColor: isDark ? null : p.text,
        elevation: 0,
        centerTitle: false,
        scrolledUnderElevation: 0,
        titleTextStyle: TextStyle(
          color: p.text,
          fontSize: 16,
          fontWeight: FontWeight.bold,
          letterSpacing: 1.1,
        ),
      ),
      cardTheme: CardThemeData(
        color: p.card,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: p.border, width: 1),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: p.card,
        hintStyle: TextStyle(color: p.muted, fontSize: 13),
        labelStyle: TextStyle(color: p.accent, fontSize: 13),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide(color: p.border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide(color: p.border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide(color: p.accent, width: 1.5),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: p.accent,
          foregroundColor: p.onAccent,
          elevation: 2,
          textStyle: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13, letterSpacing: 0.5),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: p.text,
          side: BorderSide(color: p.border),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        ),
      ),
    );
  }
}

/// Theme-dependent colours for app surfaces and severity accents.
///
/// The dark values are the original OLED palette unchanged. The light values
/// are darker shades of the same hues so that text and icons in these colours,
/// including on a 15% tint of themselves, stay at or above 4.5:1 contrast on
/// white and light-grey surfaces.
///
/// Colours drawn on top of video (overlays, zone and skeleton painters, the
/// MJPEG view) keep using the constant [AppTheme] colours instead.
@immutable
class AppPalette extends ThemeExtension<AppPalette> {
  final Color background;
  final Color card;
  final Color glass;
  final Color elevated;
  final Color border;
  final Color text;
  final Color muted;
  final Color accent;
  final Color onAccent;
  final Color live;
  final Color alert;
  final Color warning;
  final Color caution;
  final Color purple;
  final bool isDark;

  const AppPalette({
    required this.background,
    required this.card,
    required this.glass,
    required this.elevated,
    required this.border,
    required this.text,
    required this.muted,
    required this.accent,
    required this.onAccent,
    required this.live,
    required this.alert,
    required this.warning,
    required this.caution,
    required this.purple,
    required this.isDark,
  });

  static const AppPalette dark = AppPalette(
    background: AppTheme.darkBackground,
    card: AppTheme.cardSurface,
    glass: AppTheme.glassSurface,
    elevated: AppTheme.elevatedSurface,
    border: AppTheme.borderHighlight,
    text: Colors.white,
    muted: AppTheme.textMuted,
    accent: AppTheme.cyberBlue,
    onAccent: AppTheme.oledBlack,
    live: AppTheme.liveGreen,
    alert: AppTheme.alertRed,
    warning: AppTheme.warningOrange,
    caution: Colors.amber,
    purple: AppTheme.hailoPurple,
    isDark: true,
  );

  static const AppPalette light = AppPalette(
    background: AppTheme.lightBackground,
    card: AppTheme.lightCard,
    glass: Color(0xF2FFFFFF),
    elevated: AppTheme.lightElevated,
    border: AppTheme.lightBorder,
    text: AppTheme.lightInk,
    muted: Color(0xFF4F5E72),
    accent: Color(0xFF005F6E),
    onAccent: Colors.white,
    live: Color(0xFF146B32),
    alert: Color(0xFFB00E28),
    warning: Color(0xFF8F4000),
    caution: Color(0xFF735100),
    purple: Color(0xFF5E35A8),
    isDark: false,
  );

  /// Secondary text at the emphasis `Colors.white70`/`white54`/... gave in the
  /// dark theme. In the light theme the ink is kept dark enough to read.
  Color dim(double alpha) => isDark
      ? Colors.white.withValues(alpha: alpha)
      : text.withValues(alpha: (0.62 + 0.38 * alpha).clamp(0.0, 1.0));

  /// Hairlines and faint fills (the dark theme's `white10`/`white12`/`white24`).
  Color hairline(double alpha) => (isDark ? Colors.white : text).withValues(alpha: alpha);

  @override
  AppPalette copyWith({
    Color? background,
    Color? card,
    Color? glass,
    Color? elevated,
    Color? border,
    Color? text,
    Color? muted,
    Color? accent,
    Color? onAccent,
    Color? live,
    Color? alert,
    Color? warning,
    Color? caution,
    Color? purple,
    bool? isDark,
  }) =>
      AppPalette(
        background: background ?? this.background,
        card: card ?? this.card,
        glass: glass ?? this.glass,
        elevated: elevated ?? this.elevated,
        border: border ?? this.border,
        text: text ?? this.text,
        muted: muted ?? this.muted,
        accent: accent ?? this.accent,
        onAccent: onAccent ?? this.onAccent,
        live: live ?? this.live,
        alert: alert ?? this.alert,
        warning: warning ?? this.warning,
        caution: caution ?? this.caution,
        purple: purple ?? this.purple,
        isDark: isDark ?? this.isDark,
      );

  @override
  AppPalette lerp(ThemeExtension<AppPalette>? other, double t) {
    if (other is! AppPalette) return this;
    Color l(Color a, Color b) => Color.lerp(a, b, t)!;
    return AppPalette(
      background: l(background, other.background),
      card: l(card, other.card),
      glass: l(glass, other.glass),
      elevated: l(elevated, other.elevated),
      border: l(border, other.border),
      text: l(text, other.text),
      muted: l(muted, other.muted),
      accent: l(accent, other.accent),
      onAccent: l(onAccent, other.onAccent),
      live: l(live, other.live),
      alert: l(alert, other.alert),
      warning: l(warning, other.warning),
      caution: l(caution, other.caution),
      purple: l(purple, other.purple),
      isDark: t < 0.5 ? isDark : other.isDark,
    );
  }
}

extension AppPaletteContext on BuildContext {
  /// The active [AppPalette]; falls back to the palette matching the theme's
  /// brightness when a test pumps a bare `MaterialApp` without the extension.
  AppPalette get palette {
    final theme = Theme.of(this);
    return theme.extension<AppPalette>() ??
        (theme.brightness == Brightness.dark ? AppPalette.dark : AppPalette.light);
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
            color: context.palette.glass,
            borderRadius: BorderRadius.circular(borderRadius),
            border: Border.all(
              color: borderColor ?? context.palette.border.withValues(alpha: 0.8),
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
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withValues(alpha: 0.6), width: 1),
        boxShadow: isGlowing
            ? [BoxShadow(color: color.withValues(alpha: 0.35), blurRadius: 8, spreadRadius: 1)]
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
        color: Colors.black.withValues(alpha: 0.4),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: AppTheme.borderHighlight.withValues(alpha: 0.5)),
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
