import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/screens/server_settings_screen.dart';
import 'package:edge_ai_cctv/screens/settings_screen.dart';
import 'package:edge_ai_cctv/services/push_service.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

import 'support/mock_edge_server.dart';

void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;
  const lanA = 'http://192.168.1.50:8000';

  late MockEdgeServer server;
  late ServerRegistry registry;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    registry = ServerRegistry.resetForTest();
    await registry.load();
    await registry.upsert(
        ServerEntry(deviceId: a, name: 'Store A', urls: [lanA], lastGoodUrl: lanA, pairedDeviceId: 'pd-aaaa'),
        makeActive: true);
    await registry.saveTokens(a, accessToken: 'ACCESS-aaaa');
    await registry.upsert(ServerEntry(deviceId: b, name: 'Store B', urls: [lanA]));
    await registry.saveTokens(b, accessToken: 'ACCESS-bbbb');
    server = MockEdgeServer({lanA: FakeBox(a, 'Store A')});
    PushService().debugSetStatus(PushStatus.notConfigured);
  });

  Future<void> tap(WidgetTester tester, Finder f) async {
    await tester.ensureVisible(f);
    await tester.tap(f);
    await tester.pumpAndSettle();
  }

  testWidgets('shows identity and push status, saves alert prefs and removes the server inline', (tester) async {
    tester.view.physicalSize = const Size(1080, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await http.runWithClient(() async {
      await tester.pumpWidget(const MaterialApp(home: SettingsScreen()));
      await tester.pumpAndSettle();
      expect(find.text('Push not configured on this build'), findsOneWidget);
      expect(find.text('Store A'), findsOneWidget);
      expect(find.text('Store B'), findsOneWidget);

      await tap(tester, find.byTooltip('Server settings').first);
      expect(find.byType(ServerSettingsScreen), findsOneWidget);
      expect(find.text(a), findsOneWidget, reason: 'full device id shown');
      expect(find.text('Paired'), findsOneWidget);
      expect(find.text('Push not configured on this build'), findsOneWidget);

      // Invalid quiet hours are refused inline without a request.
      final patchesBefore = server.requests.where((r) => r.method == 'PATCH').length;
      await tap(tester, find.widgetWithText(SwitchListTile, 'All alert types'));
      await tap(tester, find.widgetWithText(FilterChip, 'Suspected theft'));
      await tap(tester, find.widgetWithText(SwitchListTile, 'Quiet hours'));
      await tester.enterText(find.widgetWithText(TextField, 'From (HH:MM)'), '25:00');
      await tap(tester, find.widgetWithText(ElevatedButton, 'Save alert preferences'));
      expect(find.textContaining('24-hour times'), findsOneWidget);
      expect(server.requests.where((r) => r.method == 'PATCH').length, patchesBefore);

      await tester.enterText(find.widgetWithText(TextField, 'From (HH:MM)'), '22:00');
      await tap(tester, find.widgetWithText(ElevatedButton, 'Save alert preferences'));
      expect(find.textContaining('Saved.'), findsOneWidget);
      expect(server.boxes[lanA]!.prefs['event_types'], ['THEFT_SUSPECTED']);
      expect(server.boxes[lanA]!.prefs['quiet_hours'], {'start': '22:00', 'end': '07:00'});

      // Removal asks inline first; Cancel keeps everything.
      await tap(tester, find.widgetWithText(OutlinedButton, 'Remove this server'));
      await tap(tester, find.widgetWithText(TextButton, 'Cancel'));
      expect(registry.byId(a), isNotNull);
      await tap(tester, find.widgetWithText(OutlinedButton, 'Remove this server'));
      await tap(tester, find.widgetWithText(ElevatedButton, 'Remove'));
      expect(server.boxes[lanA]!.revoked, ['pd-aaaa']);
      expect(registry.byId(a), isNull);
      expect(await registry.accessToken(a), isNull);
      expect(await registry.accessToken(b), 'ACCESS-bbbb');
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(find.textContaining('Store A removed'), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
    }, server.client);
  });
}
