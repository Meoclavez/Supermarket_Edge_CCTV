import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/screens/setup_wizard_screen.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

/// Drives the real wizard against a fake edge server that follows
/// edge_backend/app/routes/setup.py.
void main() {
  const deviceId = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  late List<http.Request> requests;
  late int adminStatus;

  MockClient server() => MockClient((req) async {
        requests.add(req);
        final path = req.url.path;
        if (path == '/api/v1/device/identity') {
          return http.Response(jsonEncode({'device_id': deviceId, 'device_name': 'Main St', 'api_version': 1, 'pairing': true, 'remote_url': null}), 200);
        }
        if (path == '/api/v1/auth/login') {
          final body = jsonDecode(req.body) as Map<String, dynamic>;
          if (body['phone'] == null) return http.Response('{"detail":"phone expected"}', 422);
          return http.Response(
              jsonEncode({'paired_device_id': 'pd-1', 'device_id': deviceId, 'device_name': 'Main St', 'access_token': 'PHONE-ACCESS', 'refresh_token': 'PHONE-REFRESH', 'token_type': 'bearer', 'urls': []}),
              200);
        }
        if (path == '/api/v1/auth/status') {
          return http.Response(jsonEncode({'admin_exists': false, 'authenticated': false, 'setup_code_required': true}), 200);
        }
        if (path == '/api/v1/setup/admin') {
          if (adminStatus == 403) {
            return http.Response(jsonEncode({'detail': 'Setup code is missing or incorrect. It is printed in the server log.'}), 403);
          }
          if (adminStatus == 429) {
            return http.Response(jsonEncode({'detail': 'Too many failed attempts from this address. Wait 15 minutes and try again.'}), 429,
                headers: {'retry-after': '900'});
          }
          return http.Response(
              jsonEncode({'status': 'success', 'user_id': 'u1', 'token_type': 'bearer', 'access_token': 'ACCESS', 'refresh_token': 'REFRESH'}),
              200);
        }
        if (path == '/api/v1/setup/hardware-scan') {
          if (req.headers['Authorization'] != 'Bearer ACCESS') return http.Response('{"detail":"Not authenticated"}', 401);
          return http.Response(
              jsonEncode({'hardware': {'hailo_available': false, 'vaapi_available': true, 'hw_decode_supported': false, 'cpu_info': 'Test CPU'}}),
              200);
        }
        return http.Response('{"detail":"unexpected"}', 404);
      });

  setUp(() async {
    requests = [];
    adminStatus = 200;
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await ServerRegistry.resetForTest().load();
  });

  Future<void> fillAccount(WidgetTester tester, {required String code, required String user, required String pw, String? confirm}) async {
    await tester.enterText(find.widgetWithText(TextField, 'Setup code'), code);
    await tester.enterText(find.widgetWithText(TextField, 'Username'), user);
    await tester.enterText(find.widgetWithText(TextField, 'Password'), pw);
    await tester.enterText(find.widgetWithText(TextField, 'Confirm password'), confirm ?? pw);
  }

  Future<void> tapButton(WidgetTester tester, String label) async {
    // Stepper keeps inactive steps (and their controls) in the tree behind
    // IgnorePointer, so only the hit-testable button is the real one.
    final button = find.widgetWithText(ElevatedButton, label).hitTestable();
    expect(button, findsOneWidget, reason: 'exactly one tappable "$label" button');
    await tester.ensureVisible(button);
    await tester.tap(button);
    await tester.pumpAndSettle();
  }

  Future<void> reachAccountStep(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(const MaterialApp(home: SetupWizardScreen()));
    await tester.enterText(find.widgetWithText(TextField, 'Edge server address'), 'edge.local:8000');
    await tapButton(tester, 'Test connection');
    expect(find.text('Connected. Ready for first-run setup.'), findsOneWidget);
    await tapButton(tester, 'Continue');
    expect(find.textContaining('storage/setup_code.txt'), findsOneWidget);
  }

  testWidgets('client-side policy errors are inline and send nothing', (tester) async {
    await http.runWithClient(() async {
      await reachAccountStep(tester);
      final before = requests.length;

      await fillAccount(tester, code: '', user: 'manager', pw: 'longenough1');
      await tapButton(tester, 'Create account');
      expect(find.text('Enter the setup code from the edge server.'), findsOneWidget);

      await fillAccount(tester, code: 'ABCD-1234', user: 'manager', pw: 'short');
      await tapButton(tester, 'Create account');
      expect(find.text('Password must be at least 8 characters.'), findsOneWidget);

      await fillAccount(tester, code: 'ABCD-1234', user: 'Manager01', pw: 'manager01');
      await tapButton(tester, 'Create account');
      expect(find.text('Password must not be the same as the username.'), findsOneWidget);

      await fillAccount(tester, code: 'ABCD-1234', user: 'manager', pw: 'longenough1', confirm: 'different1');
      await tapButton(tester, 'Create account');
      expect(find.text('Passwords do not match.'), findsOneWidget);

      expect(requests.length, before, reason: 'invalid forms must not reach the server');
      expect(find.byType(AlertDialog), findsNothing);
    }, server);
  });

  testWidgets('403 and 429 from the server are shown inline', (tester) async {
    await http.runWithClient(() async {
      await reachAccountStep(tester);
      adminStatus = 403;
      await fillAccount(tester, code: 'WRNG-CODE', user: 'manager', pw: 'longenough1');
      await tapButton(tester, 'Create account');
      expect(find.textContaining('Setup code is missing or incorrect'), findsOneWidget);

      adminStatus = 429;
      await tapButton(tester, 'Create account');
      expect(find.textContaining('Too many failed attempts'), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
    }, server);
  });

  testWidgets('sends setup_code, stores the token and uses it for the next step', (tester) async {
    await http.runWithClient(() async {
      await reachAccountStep(tester);
      await fillAccount(tester, code: 'abcd-1234', user: 'manager', pw: 'longenough1');
      await tapButton(tester, 'Create account');

      final adminReq = requests.firstWhere((r) => r.url.path == '/api/v1/setup/admin');
      final body = jsonDecode(adminReq.body) as Map<String, dynamic>;
      expect(body['setup_code'], 'abcd-1234');
      expect(body['username'], 'manager');
      expect(body['password'], 'longenough1');
      expect(adminReq.url.toString(), startsWith('http://edge.local:8000/'));

      final hwReq = requests.firstWhere((r) => r.url.path == '/api/v1/setup/hardware-scan');
      expect(hwReq.headers['Authorization'], 'Bearer ACCESS');
      expect(find.text('CPU: Test CPU'), findsOneWidget);
      expect(find.text('Operator account created. You are signed in.'), findsOneWidget);

      // The server is stored under its device id, and this phone is signed in
      // (and registered as a paired phone) with its own session.
      final registry = ServerRegistry();
      expect(registry.activeId, deviceId);
      expect(registry.active!.name, 'Main St');
      expect(registry.urlFor(deviceId), 'http://edge.local:8000');
      expect(registry.active!.pairedDeviceId, 'pd-1');
      expect(await const FlutterSecureStorage().read(key: 'srv.$deviceId.access_token'), 'PHONE-ACCESS');
      expect(await const FlutterSecureStorage().read(key: 'srv.$deviceId.refresh_token'), 'PHONE-REFRESH');
      expect(await const FlutterSecureStorage().read(key: 'access_token'), isNull);
      final login = jsonDecode(requests.firstWhere((r) => r.url.path == '/api/v1/auth/login').body) as Map<String, dynamic>;
      expect(login['phone']['app_instance_id'], isNotEmpty);
    }, server);
  });
}
