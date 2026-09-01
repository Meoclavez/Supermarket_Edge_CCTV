import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'core/theme/app_theme.dart';
import 'screens/app_shell.dart';
import 'services/notification_service.dart';
import 'services/api_service.dart';
import 'core/error_recovery.dart';
import 'screens/setup_wizard_screen.dart';
import 'screens/login_screen.dart';

final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

/// Top-level background message handler for FCM
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp();
  debugPrint('Handling background message: ${message.messageId}');
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // 1. Initialize API Client and persistent base URL settings
  await ApiService().init();
  ConnectionMonitor().setBaseUrl(ApiService().baseUrl);
  ConnectionMonitor().startMonitoring();

  // 2. Initialize Firebase Core & Background Messaging
  try {
    await Firebase.initializeApp();
    FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);
  } catch (e) {
    debugPrint('Firebase init notice (local development mode): $e');
  }

  // 3. Initialize Notification Channels & Native Audio
  await NotificationService().initialize(navigatorKey);

  runApp(const EdgeAiCctvApp());
}

class EdgeAiCctvApp extends StatelessWidget {
  const EdgeAiCctvApp({Key? key}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Edge AI CCTV Surveillance',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.darkTheme,
      navigatorKey: navigatorKey,
      home: const InitialRouter(),
    );
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
    try {
      final prefs = await SharedPreferences.getInstance();
      final serverUrl = prefs.getString('server_url');

      if (serverUrl == null || serverUrl.isEmpty) {
        // No server URL, try finding via mDNS or just go to Login/Setup
        _navigate(const LoginScreen());
        return;
      }

      // Check setup status
      final setupRes = await http.get(Uri.parse('$serverUrl/api/v1/setup/status')).timeout(const Duration(seconds: 3));
      if (setupRes.statusCode == 200) {
        final setupData = jsonDecode(setupRes.body);
        if (setupData['is_completed'] == false) {
          _navigate(const SetupWizardScreen());
          return;
        }
      }

      // Check token
      const storage = FlutterSecureStorage();
      final token = await storage.read(key: 'access_token');
      if (token != null && token.isNotEmpty) {
        _navigate(const AppShell());
      } else {
        _navigate(const LoginScreen());
      }
    } catch (e) {
      debugPrint('Startup error: $e');
      _navigate(const LoginScreen());
    }
  }

  void _navigate(Widget screen) {
    if (mounted) {
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => screen));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: const [
            Icon(Icons.shield_rounded, size: 80, color: AppTheme.cyberBlue),
            SizedBox(height: 24),
            CircularProgressIndicator(color: AppTheme.cyberBlue),
          ],
        ),
      ),
    );
  }
}
