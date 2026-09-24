import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The operator's Light / Dark / System appearance choice, persisted in
/// SharedPreferences alongside the app's other settings.
class ThemeController extends ValueNotifier<ThemeMode> {
  static const prefsKey = 'app_theme_mode';

  static ThemeController _instance = ThemeController._();
  factory ThemeController() => _instance;
  ThemeController._() : super(ThemeMode.system);

  /// A fresh controller for widget tests.
  @visibleForTesting
  static ThemeController resetForTest() => _instance = ThemeController._();

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    value = decode(prefs.getString(prefsKey));
  }

  Future<void> setMode(ThemeMode mode) async {
    value = mode;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(prefsKey, mode.name);
  }

  static ThemeMode decode(String? raw) =>
      ThemeMode.values.firstWhere((m) => m.name == raw, orElse: () => ThemeMode.system);
}
