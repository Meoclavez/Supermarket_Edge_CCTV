import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/screens/loss_prevention_screen.dart';
import 'package:edge_ai_cctv/services/notification_service.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

/// Tapping a push switches to the store that sent it before opening the
/// screen, and pushes from unknown stores do nothing.
void main() {
  const a = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  const b = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';

  testWidgets('push routing by device_id selects the right server entry', (tester) async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    await registry.upsert(ServerEntry(deviceId: a, name: 'Store A', urls: ['http://192.168.1.50:8000']), makeActive: true);
    await registry.upsert(ServerEntry(deviceId: b, name: 'Store B', urls: ['http://192.168.1.50:8000']));
    await registry.saveTokens(b, accessToken: 'ACCESS-B');

    final requested = <Uri>[];
    final client = MockClient((req) async {
      requested.add(req.url);
      if (req.url.path == '/api/v1/device/identity') {
        return http.Response('{"device_id":"$b","device_name":"Store B","api_version":1}', 200);
      }
      return http.Response('{"incidents":[]}', 200);
    });

    await http.runWithClient(() async {
      final navKey = GlobalKey<NavigatorState>();
      await tester.pumpWidget(MaterialApp(navigatorKey: navKey, home: const Text('home')));
      NotificationService().navigatorKey = navKey;

      await NotificationService().openAlert({'device_id': 'cccccccc-3333-4333-8333-cccccccccccc', 'event_type': 'CONCEALMENT'});
      await tester.pumpAndSettle();
      expect(registry.activeId, a, reason: 'unknown server ignored');
      expect(find.text('home'), findsOneWidget);

      await NotificationService().openAlert({'device_id': b, 'event_type': 'CONCEALMENT', 'alert_id': 'evt_1'});
      await tester.pumpAndSettle();
      expect(registry.activeId, b);
      expect(find.byType(LossPreventionScreen), findsOneWidget);
      // The incident list was loaded from store B's session.
      expect(requested.any((u) => u.path.startsWith('/api/v1/theft/incidents')), isTrue);
    }, () => client);
  });
}
