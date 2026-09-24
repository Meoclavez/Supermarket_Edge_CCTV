import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/screens/pairing_screen.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

import 'support/mock_edge_server.dart';

/// Drives the real pairing screen against fake edge servers built from
/// CONTRACT2. Every failure must appear inline; no dialog may open.
void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;
  const lanA = 'http://192.168.1.50:8000';
  const lanB = 'http://192.168.1.60:8000';

  late MockEdgeServer server;
  ServerEntry? paired;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    await ServerRegistry.resetForTest().load();
    server = MockEdgeServer({lanA: FakeBox(a, 'Store A'), lanB: FakeBox(b, 'Store B')});
    paired = null;
  });

  Future<void> open(WidgetTester tester, {PairingMethod method = PairingMethod.scan}) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(home: PairingScreen(initialMethod: method, onPaired: (e) => paired = e)));
    await tester.pumpAndSettle();
  }

  Future<void> tap(WidgetTester tester, Finder f) async {
    await tester.ensureVisible(f);
    await tester.tap(f);
    await tester.pumpAndSettle();
  }

  Future<void> fillCodeForm(WidgetTester tester, {required String address, required String id, required String code}) async {
    await tester.enterText(find.widgetWithText(TextField, 'Server address'), address);
    await tester.enterText(find.widgetWithText(TextField, 'Device ID'), id);
    await tester.enterText(find.widgetWithText(TextField, 'Pairing code'), code);
    await tap(tester, find.widgetWithText(ElevatedButton, 'Pair this phone'));
  }

  testWidgets('a malformed or foreign QR link is rejected inline and nothing is sent', (tester) async {
    await http.runWithClient(() async {
      await open(tester);
      await tester.enterText(find.widgetWithText(TextField, 'Or paste the pairing link'), 'https://example.com/pair?d=x');
      await tap(tester, find.widgetWithText(OutlinedButton, 'Use pairing link'));
      expect(find.text('This QR code is not an Edge CCTV pairing code.'), findsOneWidget);

      await tester.enterText(find.widgetWithText(TextField, 'Or paste the pairing link'), 'edgecctv://pair?v=1&d=$a&c=AB');
      await tap(tester, find.widgetWithText(OutlinedButton, 'Use pairing link'));
      expect(find.text('The pairing QR code has an invalid pairing code.'), findsOneWidget);

      expect(server.requests, isEmpty);
      expect(find.byType(AlertDialog), findsNothing);
      expect(find.byType(Dialog), findsNothing);
    }, server.client);
  });

  testWidgets('a valid pasted QR link pairs and stores tokens under the device id', (tester) async {
    await http.runWithClient(() async {
      await open(tester);
      await tester.enterText(find.widgetWithText(TextField, 'Or paste the pairing link'),
          'edgecctv://pair?v=1&d=$a&n=Store%20A&c=ABCD-2345&u=${Uri.encodeComponent(lanA)}');
      await tap(tester, find.widgetWithText(OutlinedButton, 'Use pairing link'));
      expect(paired?.deviceId, a);
      expect(find.text('Paired with Store A.'), findsOneWidget);
      expect(await const FlutterSecureStorage().read(key: 'srv.$a.access_token'), 'ACCESS-aaaa');
    }, server.client);
  });

  testWidgets('manual pairing: different server, bad code, device_mismatch and rate limit are inline', (tester) async {
    await http.runWithClient(() async {
      await open(tester, method: PairingMethod.code);

      // Store B's address with store A's device id.
      await fillCodeForm(tester, address: '192.168.1.60:8000', id: a, code: 'ABCD-2345');
      expect(find.textContaining('This address is a different Edge CCTV server'), findsOneWidget);
      expect(server.hits('/api/v1/pairing/claim'), isEmpty);

      await fillCodeForm(tester, address: '192.168.1.50:8000', id: a, code: 'WXYZ-9999');
      expect(find.text('Pairing code is wrong, expired or already used.'), findsOneWidget);
      expect(find.textContaining('different Edge CCTV server'), findsNothing, reason: 'old error cleared');

      server.claimStatus = 409;
      await fillCodeForm(tester, address: '192.168.1.50:8000', id: a, code: 'ABCD-2345');
      expect(find.textContaining('device_mismatch'), findsOneWidget);

      server.claimStatus = 429;
      await fillCodeForm(tester, address: '192.168.1.50:8000', id: a, code: 'ABCD-2345');
      expect(find.textContaining('Too many attempts'), findsOneWidget);

      await fillCodeForm(tester, address: '192.168.1.50:8000', id: 'short-id', code: 'ABCD-2345');
      expect(find.textContaining('full device ID'), findsOneWidget);

      await fillCodeForm(tester, address: '192.168.1.99:8000', id: a, code: 'ABCD-2345');
      expect(find.textContaining('Could not reach the edge server'), findsOneWidget);

      expect(paired, isNull);
      expect(ServerRegistry().servers, isEmpty);
      expect(find.byType(AlertDialog), findsNothing);

      server.claimStatus = null;
      await fillCodeForm(tester, address: '192.168.1.50:8000', id: a, code: 'abcd 2345');
      expect(paired?.deviceId, a);
      final claim = jsonDecode(server.hits('/api/v1/pairing/claim').last.body) as Map<String, dynamic>;
      expect(claim['code'], 'ABCD-2345');
    }, server.client);
  });

  testWidgets('sign-in: wrong password, rate limit and wrong store are inline; success pairs', (tester) async {
    await http.runWithClient(() async {
      await open(tester, method: PairingMethod.signIn);
      Future<void> signIn(String address, String password) async {
        await tester.enterText(find.widgetWithText(TextField, 'Server address'), address);
        await tester.enterText(find.widgetWithText(TextField, 'Username'), 'manager');
        await tester.enterText(find.widgetWithText(TextField, 'Password'), password);
        await tap(tester, find.widgetWithText(ElevatedButton, 'Sign in'));
      }

      await signIn('192.168.1.50:8000', 'wrong');
      expect(find.text('Invalid username or password'), findsOneWidget);

      server.loginStatus = 429;
      await signIn('192.168.1.50:8000', 'correct-horse');
      expect(find.textContaining('Too many failed attempts'), findsOneWidget);
      server.loginStatus = null;

      await signIn('192.168.1.50:8000', 'correct-horse');
      expect(paired?.deviceId, a);
      final login = jsonDecode(server.hits('/api/v1/auth/login').last.body) as Map<String, dynamic>;
      expect(login['phone']['app_instance_id'], isNotEmpty);
      expect(find.byType(AlertDialog), findsNothing);
    }, server.client);
  });

  testWidgets('re-sign-in to a known store refuses another store at the same address', (tester) async {
    await ServerRegistry().upsert(ServerEntry(deviceId: a, name: 'Store A', urls: [lanB]));
    await http.runWithClient(() async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(MaterialApp(home: PairingScreen(reSignInDeviceId: a, onPaired: (e) => paired = e)));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(TextField, lanB), findsOneWidget, reason: 'address prefilled');
      await tester.enterText(find.widgetWithText(TextField, 'Username'), 'manager');
      await tester.enterText(find.widgetWithText(TextField, 'Password'), 'correct-horse');
      await tap(tester, find.widgetWithText(ElevatedButton, 'Sign in'));
      expect(find.textContaining('This address is a different Edge CCTV server'), findsOneWidget);
      expect(server.hits('/api/v1/auth/login'), isEmpty);
      expect(paired, isNull);
    }, server.client);
  });
}
