import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/services/pairing_payload.dart';
import 'package:edge_ai_cctv/services/pairing_service.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

import 'support/mock_edge_server.dart';

void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;
  const lan = 'http://192.168.1.50:8000';

  late ServerRegistry registry;
  late PairingService service;
  late MockEdgeServer server;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    registry = ServerRegistry.resetForTest();
    await registry.load();
    service = PairingService(registry: registry);
    server = MockEdgeServer({
      lan: FakeBox(a, 'Store A', remoteUrl: 'https://a.example.com'),
      'http://192.168.1.60:8000': FakeBox(b, 'Store B'),
    });
  });

  Future<PhoneInfo> phone() => service.phoneInfo(name: 'Manager phone', pushToken: 'FCM-TOKEN-1');

  test('QR claim sends the phone object and stores tokens under the device id namespace', () async {
    final payload = PairingPayload.parseQr('edgecctv://pair?v=1&d=$a&n=Store%20A&c=abcd2345&u=http://192.168.1.50:8000');
    final entry = await http.runWithClient(() async => service.claimPayload(payload, await phone()), server.client);

    // Identity was checked before the claim.
    final paths = server.requests.map((r) => r.url.path).toList();
    expect(paths.indexOf('/api/v1/device/identity'), lessThan(paths.indexOf('/api/v1/pairing/claim')));

    final body = jsonDecode(server.hits('/api/v1/pairing/claim').single.body) as Map<String, dynamic>;
    expect(body['device_id'], a);
    expect(body['code'], 'ABCD-2345');
    final ph = body['phone'] as Map<String, dynamic>;
    expect(ph['name'], 'Manager phone');
    expect(ph['platform'], anyOf('android', 'ios'));
    expect(ph['app_instance_id'], await registry.appInstanceId());
    expect(ph['push_provider'], 'fcm');
    expect(ph['push_token'], 'FCM-TOKEN-1');

    expect(entry.deviceId, a);
    expect(entry.pairedDeviceId, 'pd-aaaa');
    expect(entry.pushTokenSent, 'FCM-TOKEN-1');
    expect(registry.activeId, a);
    expect(entry.candidates.first, lan);
    expect(entry.candidates.last, 'https://a.example.com');
    expect(await const FlutterSecureStorage().read(key: 'srv.$a.access_token'), 'ACCESS-aaaa');
    expect(await const FlutterSecureStorage().read(key: 'srv.$a.refresh_token'), 'REFRESH-aaaa');
    expect(await const FlutterSecureStorage().read(key: 'access_token'), isNull, reason: 'no un-namespaced token');
    expect(await const FlutterSecureStorage().read(key: 'srv.$b.access_token'), isNull);
  });

  test('claim against an address that is a different store is refused before sending the code', () async {
    await expectLater(
      http.runWithClient(
          () async => service.claim(deviceId: a, code: 'ABCD-2345', urls: ['http://192.168.1.60:8000'], phone: await phone()),
          server.client),
      throwsA(isA<PairingException>()
          .having((e) => e.kind, 'kind', PairingErrorKind.differentServer)
          .having((e) => e.message, 'message', contains('This address is a different Edge CCTV server'))),
    );
    expect(server.hits('/api/v1/pairing/claim'), isEmpty);
    expect(registry.servers, isEmpty);
  });

  test('409, 403 and 429 from claim map to distinct errors and store nothing', () async {
    Future<PairingException> attempt() async {
      try {
        await http.runWithClient(
            () async => service.claim(deviceId: a, code: 'ABCD-2345', urls: [lan], phone: await phone()), server.client);
      } on PairingException catch (e) {
        return e;
      }
      fail('expected a PairingException');
    }

    server.claimStatus = 409;
    expect((await attempt()).kind, PairingErrorKind.deviceMismatch);
    server.claimStatus = 403;
    expect((await attempt()).kind, PairingErrorKind.badCode);
    server.claimStatus = 429;
    final limited = await attempt();
    expect(limited.kind, PairingErrorKind.rateLimited);
    expect(limited.message, contains('Too many attempts'));
    expect(registry.servers, isEmpty);
    expect(await registry.accessToken(a), isNull);
  });

  test('sign-in verifies identity first, sends the phone and ends in the same entry shape', () async {
    final entry = await http.runWithClient(
        () async => service.signIn(address: '192.168.1.50:8000', username: 'manager', password: 'correct-horse', phone: await phone()),
        server.client);
    final paths = server.requests.map((r) => r.url.path).toList();
    expect(paths.first, '/api/v1/device/identity');
    final body = jsonDecode(server.hits('/api/v1/auth/login').single.body) as Map<String, dynamic>;
    expect(body['phone']['app_instance_id'], await registry.appInstanceId());
    expect(entry.deviceId, a);
    expect(entry.pairedDeviceId, 'pd-aaaa');
    expect(await registry.accessToken(a), 'ACCESS-aaaa');
  });

  test('re-sign-in refuses an address that now belongs to another store', () async {
    await expectLater(
      http.runWithClient(
          () async => service.signIn(
              address: 'http://192.168.1.60:8000',
              username: 'manager',
              password: 'correct-horse',
              phone: await phone(),
              expectedDeviceId: a),
          server.client),
      throwsA(isA<PairingException>().having((e) => e.kind, 'kind', PairingErrorKind.differentServer)),
    );
    expect(server.hits('/api/v1/auth/login'), isEmpty, reason: 'password never sent to the wrong store');
  });

  test('refreshed push token is sent to every paired server; remove revokes and clears tokens', () async {
    server.boxes['http://192.168.1.60:8000'] = FakeBox(b, 'Store B');
    await http.runWithClient(() async {
      await service.claim(deviceId: a, code: 'ABCD-2345', urls: [lan], phone: await phone());
      await service.claim(deviceId: b, code: 'ABCD-2345', urls: ['http://192.168.1.60:8000'], phone: await phone());
      expect(registry.servers.length, 2);

      final sent = await service.syncPushToken('FCM-TOKEN-2');
      expect(sent, 2);
      expect(server.hits('/api/v1/pairing/devices/me/push').length, 2);
      expect(server.boxes[lan]!.pushToken, 'FCM-TOKEN-2');
      expect(server.boxes['http://192.168.1.60:8000']!.pushToken, 'FCM-TOKEN-2');
      expect(await service.syncPushToken('FCM-TOKEN-2'), 0, reason: 'already accepted');

      final saved = await service.saveAlertPrefs(
          registry.byId(a)!, const AlertPrefs(eventTypes: ['THEFT_SUSPECTED'], minSeverity: 'HIGH', quietStart: '22:00', quietEnd: '07:00'));
      expect(saved.eventTypes, ['THEFT_SUSPECTED']);
      expect(server.boxes[lan]!.prefs['quiet_hours'], {'start': '22:00', 'end': '07:00'});

      final removed = await service.removeServer(a);
      expect(removed.revokedOnServer, isTrue);
      expect(server.boxes[lan]!.revoked, ['pd-aaaa']);
    }, server.client);
    expect(registry.byId(a), isNull);
    expect(await registry.accessToken(a), isNull);
    expect(await registry.accessToken(b), 'ACCESS-bbbb');
    expect(registry.activeId, b);
  });
}
