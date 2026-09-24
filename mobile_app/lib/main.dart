import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'core/error_recovery.dart';
import 'core/theme/app_theme.dart';
import 'core/theme/theme_controller.dart';
import 'screens/app_shell.dart';
import 'screens/pairing_screen.dart';
import 'screens/setup_wizard_screen.dart';
import 'services/api_service.dart';
import 'services/live_alert_channel.dart';
import 'services/notification_service.dart';
import 'services/pairing_service.dart';
import 'services/push_service.dart';
import 'services/server_registry.dart';

final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // 1. Paired servers (keyed by device id), migrating the old single-server settings.
  final registry = ServerRegistry();
  await registry.load();
  try {
    await registry.migrateLegacy();
  } catch (e) {
    debugPrint('Legacy settings migration deferred: $e');
  }

  // A server that rejects this phone's refresh token has revoked it (or the
  // phone was paired again): go back to pairing for that store.
  registry.onSessionLost = (deviceId) {
    if (deviceId != registry.activeId) return;
    navigatorKey.currentState?.pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => PairingScreen(reSignInDeviceId: deviceId)),
      (_) => false,
    );
  };

  // Light / Dark / System appearance chosen in Settings.
  await ThemeController().load();

  // 2. Keep the health monitor pointed at the active server's proven URL.
  ConnectionMonitor().setBaseUrl(ApiService().baseUrl);
  registry.addListener(() => ConnectionMonitor().setBaseUrl(ApiService().baseUrl));
  ConnectionMonitor().startMonitoring();
  registry.startNetworkWatch();

  // 3. Local notifications and (when this build has a Firebase config) push.
  await NotificationService().initialize(navigatorKey);

  // 4. Websocket live alerts while the app is open.
  LiveAlertChannel().start();

  runApp(const EdgeAiCctvApp());
}

class EdgeAiCctvApp extends StatefulWidget {
  const EdgeAiCctvApp({Key? key}) : super(key: key);

  @override
  State<EdgeAiCctvApp> createState() => _EdgeAiCctvAppState();
}

class _EdgeAiCctvAppState extends State<EdgeAiCctvApp> with WidgetsBindingObserver {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      // The phone may have moved between the store Wi-Fi and mobile data.
      unawaited(onServerResumed());
      LiveAlertChannel().start();
    } else if (state == AppLifecycleState.paused) {
      LiveAlertChannel().stop();
    }
  }

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<ThemeMode>(
      valueListenable: ThemeController(),
      builder: (context, mode, _) => MaterialApp(
        title: 'Store Edge CCTV',
        debugShowCheckedModeBanner: false,
        theme: AppTheme.lightTheme,
        darkTheme: AppTheme.darkTheme,
        themeMode: mode,
        navigatorKey: navigatorKey,
        home: const InitialRouter(),
      ),
    );
  }
}

/// Re-resolves the active server, refreshes its token if it is about to
/// expire, and sends it a newer push token if it has not seen one.
Future<void> onServerResumed() async {
  final registry = ServerRegistry();
  final result = await registry.resolveActive();
  final entry = registry.active;
  if (result == null || !result.resolved || entry == null) return;
  await registry.refreshIfExpiring(entry.deviceId);
  final token = PushService().token;
  if (token != null && entry.pushTokenSent != token) {
    await PairingService().sendPushToken(entry, token);
  }
}

class InitialRouter extends StatefulWidget {
  const InitialRouter({Key? key}) : super(key: key);

  @override
  State<InitialRouter> createState() => _InitialRouterState();
}

class _InitialRouterState extends State<InitialRouter> {
  @override
  void initState() {
    super.initState();
    _determineInitialRoute();
  }

  Future<void> _determineInitialRoute() async {
    final registry = ServerRegistry();
    final active = registry.active;
    if (active == null) {
      _navigate(const PairingScreen());
      return;
    }
    try {
      // Only an address that answers with this server's device id is used.
      final result = await registry.resolve(active.deviceId);
      if (result.resolved) {
        await onServerResumed();
        final setupRes = await http.get(Uri.parse('${result.url}/api/v1/setup/status')).timeout(const Duration(seconds: 3));
        if (setupRes.statusCode == 200 && (jsonDecode(setupRes.body) as Map)['is_completed'] == false) {
          _navigate(const SetupWizardScreen());
          return;
        }
      }
    } catch (e) {
      debugPrint('Startup check notice: $e');
    }
    if (await registry.hasSession(active.deviceId)) {
      _navigate(const AppShell());
    } else {
      _navigate(PairingScreen(reSignInDeviceId: active.deviceId));
    }
  }

  void _navigate(Widget screen) {
    if (mounted) {
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => screen));
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Scaffold(
      backgroundColor: p.background,
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.shield_rounded, size: 80, color: p.accent),
            const SizedBox(height: 24),
            CircularProgressIndicator(color: p.accent),
          ],
        ),
      ),
    );
  }
}
