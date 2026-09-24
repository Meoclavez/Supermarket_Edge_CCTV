import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/core/error_recovery.dart' as recovery;
import 'package:edge_ai_cctv/core/theme/app_theme.dart';
import 'package:edge_ai_cctv/core/theme/theme_controller.dart';
import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/models/theft_incident.dart';
import 'package:edge_ai_cctv/screens/app_shell.dart';
import 'package:edge_ai_cctv/screens/clip_archives_screen.dart';
import 'package:edge_ai_cctv/screens/dashboard_screen.dart';
import 'package:edge_ai_cctv/screens/dvr_playback_screen.dart';
import 'package:edge_ai_cctv/screens/events_center_screen.dart';
import 'package:edge_ai_cctv/screens/loss_prevention_alert_screen.dart';
import 'package:edge_ai_cctv/screens/loss_prevention_screen.dart';
import 'package:edge_ai_cctv/screens/multi_cam_grid_screen.dart';
import 'package:edge_ai_cctv/screens/pairing_screen.dart';
import 'package:edge_ai_cctv/screens/server_settings_screen.dart';
import 'package:edge_ai_cctv/screens/settings_screen.dart';
import 'package:edge_ai_cctv/screens/setup_wizard_screen.dart';
import 'package:edge_ai_cctv/screens/storage_health_screen.dart';
import 'package:edge_ai_cctv/screens/zone_editor_screen.dart';
import 'package:edge_ai_cctv/services/push_service.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';
import 'package:edge_ai_cctv/widgets/server_switcher.dart';

import 'support/mock_edge_server.dart';

/// Pumps the key screens under the light and the dark theme and checks every
/// visible piece of text against the colour actually painted behind it.
void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;
  const lanA = 'http://192.168.1.50:8000';

  final incidentJson = <String, dynamic>{
    'id': 'inc-1',
    'theft_type': 'CONCEALMENT',
    'rule': 'CONCEAL_ITEM',
    'rule_label': 'Item concealment',
    'evidence': ['Hand moved from shelf to jacket pocket', 'Left the aisle without a basket'],
    'review_label': 'Possible concealment',
    'severity': 'HIGH',
    'status': 'ACTIVE',
    'department': 'Health & beauty',
    'camera_id': 'cam-1',
    'camera_name': 'Aisle 4',
    'shelf_zone_id': 'shelf-4a',
    'confidence': 0.82,
    'evidence_summary': 'Concealment gesture near shelf 4A',
    'timestamp': '2026-09-24T10:15:00Z',
  };

  final client = MockClient((req) async {
    final path = req.url.path;
    if (path == '/api/v1/theft/incidents') {
      return http.Response(
          jsonEncode({
            'incidents': [
              incidentJson,
              {...incidentJson, 'id': 'inc-2', 'severity': 'MEDIUM', 'status': 'ACKNOWLEDGED', 'rule_label': 'Shelf sweep'},
              {...incidentJson, 'id': 'inc-3', 'severity': 'LOW', 'status': 'RESOLVED', 'rule_label': 'Checkout bypass'},
            ]
          }),
          200,
          headers: {'content-type': 'application/json'});
    }
    if (path == '/api/v1/events') {
      return http.Response(
          jsonEncode({
            'events': [
              {'id': 'e1', 'camera_id': 'cam-1', 'camera_name': 'Aisle 4', 'location': 'Aisle 4', 'event_type': 'SHELF_SWEEP', 'severity': 'HIGH', 'confidence': 0.9, 'timestamp': '2026-09-24T10:15:00Z'},
              {'id': 'e2', 'camera_id': 'cam-2', 'camera_name': 'Entrance', 'location': 'Entrance', 'event_type': 'LOITERING', 'severity': 'WARNING', 'confidence': 0.7, 'timestamp': '2026-09-24T10:16:00Z'},
              {'id': 'e3', 'camera_id': 'cam-2', 'camera_name': 'Entrance', 'location': 'Entrance', 'event_type': 'CROWDING', 'severity': 'INFO', 'confidence': 0.6, 'timestamp': '2026-09-24T10:17:00Z'},
            ]
          }),
          200,
          headers: {'content-type': 'application/json'});
    }
    return http.Response('{}', 200, headers: {'content-type': 'application/json'});
  });

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    ThemeController.resetForTest();
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    await registry.upsert(
        ServerEntry(deviceId: a, name: 'Store A', urls: [lanA], lastGoodUrl: lanA, pairedDeviceId: 'pd-aaaa'),
        makeActive: true);
    await registry.saveTokens(a, accessToken: 'ACCESS-aaaa');
    await registry.upsert(ServerEntry(deviceId: b, name: 'Store B', urls: [lanA]));
    PushService().debugSetStatus(PushStatus.notConfigured);
  });

  Future<void> pumpScreen(WidgetTester tester, ThemeMode mode, Widget home) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 2.5;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
      theme: AppTheme.lightTheme,
      darkTheme: AppTheme.darkTheme,
      themeMode: mode,
      home: home,
    ));
    await tester.pumpAndSettle();
  }

  // Text each screen must show, proving the mocked data actually rendered.
  const expectedText = <String, String>{
    'settings': 'Appearance',
    'server switcher': 'Store A',
    'incident list': 'Shelf sweep',
    'AI incident center': 'LOITERING',
    'loss-prevention alert': 'Hand moved from shelf to jacket pocket',
  };

  final screens = <String, Widget Function()>{
    'pairing (scan)': () => const PairingScreen(),
    'pairing (code)': () => const PairingScreen(initialMethod: PairingMethod.code),
    'pairing (sign in)': () => const PairingScreen(initialMethod: PairingMethod.signIn),
    'settings': () => const SettingsScreen(),
    'server switcher': () => const Scaffold(body: Column(children: [ServerSwitcherBar()])),
    'incident list': () => const LossPreventionScreen(),
    'AI incident center': () => const EventsCenterScreen(),
    'loss-prevention alert': () => LossPreventionAlertScreen.fromIncident(TheftIncident.fromJson(incidentJson)),
    'server settings': () => const ServerSettingsScreen(deviceId: MockEdgeServer.storeA),
    'setup wizard': () => const SetupWizardScreen(),
    'dashboard': () => const DashboardScreen(),
    'camera grid': () => const MultiCamGridScreen(),
    'clip archives': () => const ClipArchivesScreen(),
    'storage health': () => const StorageHealthScreen(),
    'app shell': () => const AppShell(),
    'DVR playback': () => const DVRPlaybackScreen(),
    'zone editor': () => const ZoneEditorScreen(),
  };

  for (final mode in [ThemeMode.light, ThemeMode.dark]) {
    for (final entry in screens.entries) {
      testWidgets('${entry.key} is readable in ${mode.name} mode', (tester) async {
        // AppShell's ErrorBoundary installs its own ErrorWidget.builder.
        final errorBuilder = ErrorWidget.builder;
        addTearDown(() => ErrorWidget.builder = errorBuilder);
        await http.runWithClient(() async {
          await pumpScreen(tester, mode, entry.value());
          expect(tester.takeException(), isNull, reason: 'layout overflow or build error');
          final shown = expectedText[entry.key];
          if (shown != null) expect(find.textContaining(shown), findsWidgets);
          expect(Theme.of(tester.element(find.byType(Scaffold).first)).brightness,
              mode == ThemeMode.light ? Brightness.light : Brightness.dark);
          expectReadableText(tester);
        }, () => client);
        ErrorWidget.builder = errorBuilder;
      });
    }

    testWidgets('open resolve form and store menu are readable in ${mode.name} mode', (tester) async {
      await http.runWithClient(() async {
        await pumpScreen(tester, mode, LossPreventionAlertScreen.fromIncident(TheftIncident.fromJson(incidentJson)));
        final resolve = find.widgetWithText(OutlinedButton, 'Resolve');
        await tester.ensureVisible(resolve);
        await tester.tap(resolve);
        await tester.pumpAndSettle();
        await tester.dragUntilVisible(find.text('Submit'), find.byType(ListView), const Offset(0, -200));
        await tester.pumpAndSettle();
        await tester.tap(find.text('False alarm'));
        await tester.pumpAndSettle();
        final submit = tester.widget<ElevatedButton>(find.widgetWithText(ElevatedButton, 'Submit'));
        expect(submit.enabled, isTrue, reason: 'choosing an outcome enables Submit');
        expect(tester.takeException(), isNull);
        expectReadableText(tester);

        await pumpScreen(tester, mode, const Scaffold(body: Column(children: [ServerSwitcherBar()])));
        await tester.tap(find.byType(ServerSwitcherBar));
        await tester.pumpAndSettle();
        expect(find.text('Add a store'), findsOneWidget);
        expect(tester.takeException(), isNull);
        expectReadableText(tester);
      }, () => client);
    });
  }

  testWidgets('the Settings appearance choice switches the app theme and is persisted', (tester) async {
    tester.view.physicalSize = const Size(1080, 3000);
    tester.view.devicePixelRatio = 2.5;
    addTearDown(tester.view.reset);
    await ThemeController().load();
    expect(ThemeController().value, ThemeMode.system);

    await http.runWithClient(() async {
      await tester.pumpWidget(ValueListenableBuilder<ThemeMode>(
        valueListenable: ThemeController(),
        builder: (_, mode, __) => MaterialApp(
          theme: AppTheme.lightTheme,
          darkTheme: AppTheme.darkTheme,
          themeMode: mode,
          home: const SettingsScreen(),
        ),
      ));
      await tester.pumpAndSettle();
      Brightness brightness() => Theme.of(tester.element(find.byType(ListView))).brightness;

      Future<void> choose(String label) async {
        final f = find.descendant(of: find.byKey(const ValueKey('theme-mode')), matching: find.text(label));
        await tester.ensureVisible(f);
        await tester.tap(f);
        await tester.pumpAndSettle();
      }

      await choose('Dark');
      expect(brightness(), Brightness.dark);
      expect((await SharedPreferences.getInstance()).getString(ThemeController.prefsKey), 'dark');

      await choose('Light');
      expect(brightness(), Brightness.light);
      expect((await SharedPreferences.getInstance()).getString(ThemeController.prefsKey), 'light');
      expectReadableText(tester);

      await choose('System');
      expect((await SharedPreferences.getInstance()).getString(ThemeController.prefsKey), 'system');
    }, () => client);

    // A fresh start reads the saved choice back.
    SharedPreferences.setMockInitialValues({ThemeController.prefsKey: 'light'});
    final reloaded = ThemeController.resetForTest();
    await reloaded.load();
    expect(reloaded.value, ThemeMode.light);
  });

  testWidgets('the contrast check catches light-on-light and dark-on-dark text', (tester) async {
    for (final (mode, color) in [(ThemeMode.light, Colors.white70), (ThemeMode.dark, Colors.black87)]) {
      await pumpScreen(tester, mode, Scaffold(body: Center(child: Text('Unreadable', style: TextStyle(color: color)))));
      expect(() => expectReadableText(tester), throwsA(isA<TestFailure>()), reason: mode.name);
    }
    await pumpScreen(tester, ThemeMode.light,
        const Scaffold(body: Card(child: Padding(padding: EdgeInsets.all(8), child: Text('Readable')))));
    expectReadableText(tester);
  });

  for (final mode in [ThemeMode.light, ThemeMode.dark]) {
    testWidgets('the offline banner is readable in every state in ${mode.name} mode', (tester) async {
      for (final state in [
        recovery.ConnectionState.RECONNECTING,
        recovery.ConnectionState.DISCONNECTED,
        recovery.ConnectionState.SERVER_UNREACHABLE,
      ]) {
        await pumpScreen(tester, mode, Scaffold(body: Column(children: [recovery.OfflineIndicatorBanner(state: state)])));
        expect(tester.takeException(), isNull);
        expectReadableText(tester);
      }
    });
  }

  test('the dark theme keeps the original OLED palette', () {
    final t = AppTheme.darkTheme;
    expect(t.brightness, Brightness.dark);
    expect(t.scaffoldBackgroundColor, const Color(0xFF080A0E));
    expect(t.colorScheme.primary, const Color(0xFF00E5FF));
    expect(t.colorScheme.onPrimary, const Color(0xFF000000));
    expect(t.colorScheme.secondary, const Color(0xFF00E676));
    expect(t.colorScheme.error, const Color(0xFFFF1744));
    expect(t.colorScheme.surface, const Color(0xFF12161F));
    expect(t.colorScheme.onSurface, Colors.white);
    expect(t.cardTheme.color, const Color(0xFF12161F));
    expect(t.appBarTheme.backgroundColor, const Color(0xFF080A0E));
    expect(t.inputDecorationTheme.fillColor, const Color(0xFF12161F));
    expect(t.extension<AppPalette>(), same(AppPalette.dark));
    expect(AppTheme.lightTheme.brightness, Brightness.light);
    expect(AppTheme.lightTheme.extension<AppPalette>(), same(AppPalette.light));
  });

  test('severity colours keep 4.5:1 on light surfaces, including their own 15% tint', () {
    const p = AppPalette.light;
    for (final c in [p.accent, p.live, p.alert, p.warning, p.caution, p.purple, p.muted, p.text]) {
      for (final bg in [p.background, p.card, p.elevated]) {
        expect(contrastRatio(c, bg), greaterThanOrEqualTo(4.5), reason: '$c on $bg');
        expect(contrastRatio(c, Color.alphaBlend(c.withValues(alpha: 0.15), bg)), greaterThanOrEqualTo(4.5),
            reason: '$c on its tint over $bg');
      }
      expect(contrastRatio(p.onAccent, c), greaterThanOrEqualTo(4.5), reason: 'white label on filled $c');
    }
  });
}

// --------------------------------------------------------------- contrast

double _channel(double c) => c <= 0.03928 ? c / 12.92 : math.pow((c + 0.055) / 1.055, 2.4).toDouble();

double relativeLuminance(Color c) => 0.2126 * _channel(c.r) + 0.7152 * _channel(c.g) + 0.0722 * _channel(c.b);

/// WCAG 2.x contrast ratio of two opaque colours.
double contrastRatio(Color a, Color b) {
  final la = relativeLuminance(a), lb = relativeLuminance(b);
  return (math.max(la, lb) + 0.05) / (math.min(la, lb) + 0.05);
}

/// The colour painted behind [element]: the nearest opaque fill among its
/// ancestors, with translucent fills above it composited on top.
Color resolvedBackground(Element element) {
  final layers = <Color>[];
  final theme = Theme.of(element);
  element.visitAncestorElements((ancestor) {
    final w = ancestor.widget;
    Color? fill;
    if (w is Material) {
      if (w.type != MaterialType.transparency) {
        fill = w.color ?? (w.type == MaterialType.card ? theme.cardColor : theme.canvasColor);
      }
    } else if (w is ColoredBox) {
      fill = w.color;
    } else if (w is DecoratedBox && w.position == DecorationPosition.background) {
      fill = _decorationColor(w.decoration);
    } else if (w is Ink) {
      fill = _decorationColor(w.decoration);
    }
    if (fill != null && fill.a > 0) layers.add(fill);
    return fill == null || fill.a < 1.0;
  });
  var bg = layers.isNotEmpty && layers.last.a >= 1.0 ? layers.removeLast() : theme.scaffoldBackgroundColor;
  for (final layer in layers.reversed) {
    bg = Color.alphaBlend(layer, bg);
  }
  return bg;
}

Color? _decorationColor(Decoration? d) => switch (d) {
      BoxDecoration(:final color) => color,
      ShapeDecoration(:final color) => color,
      _ => null,
    };

bool _insideDisabledControl(Element e) {
  var disabled = false;
  e.visitAncestorElements((ancestor) {
    final w = ancestor.widget;
    if (w is ButtonStyleButton && !w.enabled) disabled = true;
    return !disabled;
  });
  return disabled;
}

/// Every visible text run must reach 4.5:1 against what is painted behind it
/// (3:1 for WCAG "large" text: 18 px, or 14 px bold). Text in disabled
/// controls is exempt, as WCAG exempts inactive components.
void expectReadableText(WidgetTester tester) {
  final failures = <String>[];
  var checked = 0;
  for (final element in find.byType(RichText).evaluate()) {
    final rich = element.widget as RichText;
    if (_insideDisabledControl(element)) continue;
    final bg = resolvedBackground(element);
    void visit(InlineSpan span, TextStyle? inherited) {
      final style = inherited?.merge(span.style) ?? span.style;
      if (span is TextSpan) {
        final text = span.text ?? '';
        if (text.trim().isNotEmpty && style?.color != null) {
          // Icon glyphs are drawn with RichText too; they are not text.
          final isIcon = style!.fontFamily == 'MaterialIcons';
          if (!isIcon) {
            final fg = Color.alphaBlend(style.color!, bg);
            final size = style.fontSize ?? 14;
            final bold = (style.fontWeight ?? FontWeight.normal).value >= FontWeight.bold.value;
            final large = size >= 18 || (size >= 14 && bold);
            final ratio = contrastRatio(fg, bg);
            checked++;
            if (ratio < (large ? 3.0 : 4.5)) {
              failures.add('"${text.length > 40 ? '${text.substring(0, 40)}...' : text}" '
                  '${style.color} on $bg = ${ratio.toStringAsFixed(2)}:1');
            }
          }
        }
        span.children?.forEach((c) => visit(c, style));
      }
    }

    visit(rich.text, null);
  }
  expect(checked, greaterThan(0), reason: 'no text was checked');
  expect(failures, isEmpty, reason: 'low-contrast text:\n${failures.join('\n')}');
}
