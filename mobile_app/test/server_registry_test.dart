import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

import 'support/mock_edge_server.dart';

void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;
  const sharedLan = 'http://192.168.1.50:8000';

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
  });

  test('two stores at the same LAN IP with different device ids do not collide', () async {
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    await registry.upsert(ServerEntry(deviceId: a, name: 'Store A', urls: [sharedLan, 'https://a.example.com']));
    await registry.saveTokens(a, accessToken: 'ACCESS-A', refreshToken: 'REFRESH-A');
    await registry.upsert(ServerEntry(deviceId: b, name: 'Store B', urls: [sharedLan]));
    await registry.saveTokens(b, accessToken: 'ACCESS-B', refreshToken: 'REFRESH-B');

    expect(registry.servers.map((s) => s.deviceId).toSet(), {a, b});
    expect(await registry.accessToken(a), 'ACCESS-A');
    expect(await registry.accessToken(b), 'ACCESS-B');
    expect(await const FlutterSecureStorage().read(key: 'srv.$a.access_token'), 'ACCESS-A');
    expect(await const FlutterSecureStorage().read(key: 'srv.$b.refresh_token'), 'REFRESH-B');

    // Persisted separately and reloaded separately.
    final reloaded = ServerRegistry.resetForTest();
    await reloaded.load();
    expect(reloaded.servers.length, 2);
    expect(reloaded.byId(a)!.urls, [sharedLan, 'https://a.example.com']);
    expect(reloaded.byId(b)!.urls, [sharedLan]);

    // Today the box at the shared IP is store B: B resolves there, A must not.
    final server = MockEdgeServer({
      sharedLan: FakeBox(b, 'Store B'),
      'https://a.example.com': FakeBox(a, 'Store A'),
    });
    await http.runWithClient(() async {
      final ra = await reloaded.resolve(a);
      final rb = await reloaded.resolve(b);
      expect(ra.url, 'https://a.example.com');
      expect(ra.mismatched.single.url, sharedLan);
      expect(rb.url, sharedLan);
    }, server.client);
    expect(reloaded.urlFor(a), 'https://a.example.com');
    expect(reloaded.urlFor(b), sharedLan);

    // Removing one store leaves the other and its tokens alone.
    await reloaded.remove(b);
    expect(reloaded.byId(b), isNull);
    expect(await reloaded.accessToken(b), isNull);
    expect(await reloaded.accessToken(a), 'ACCESS-A');
  });

  test('a store whose only address now answers as another store is unreachable, not merged', () async {
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    await registry.upsert(ServerEntry(deviceId: a, name: 'Store A', urls: [sharedLan]));
    final server = MockEdgeServer({sharedLan: FakeBox(b, 'Store B')});
    final r = await http.runWithClient(() => registry.resolve(a), server.client);
    expect(r.resolved, isFalse);
    expect(registry.reachabilityOf(a), ServerReachability.differentServer);
    expect(registry.byId(a)!.name, 'Store A');
    expect(registry.byId(b), isNull);
  });

  test('legacy single-server settings migrate under the device id the old address reports', () async {
    SharedPreferences.setMockInitialValues({'server_url': sharedLan});
    FlutterSecureStorage.setMockInitialValues({'access_token': 'OLD-ACCESS', 'refresh_token': 'OLD-REFRESH'});
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    final server = MockEdgeServer({sharedLan: FakeBox(a, 'Main St', remoteUrl: 'https://a.example.com')});
    final outcome = await http.runWithClient(() => registry.migrateLegacy(), server.client);
    expect(outcome, MigrationOutcome.migrated);
    expect(registry.activeId, a);
    expect(registry.active!.name, 'Main St');
    expect(registry.active!.candidates, [sharedLan, 'https://a.example.com']);
    expect(await registry.accessToken(a), 'OLD-ACCESS');
    expect(await registry.refreshToken(a), 'OLD-REFRESH');
    expect(await const FlutterSecureStorage().read(key: 'access_token'), isNull);
    expect((await SharedPreferences.getInstance()).getString('server_url'), isNull);
  });

  test('legacy migration waits while the old server is unreachable', () async {
    SharedPreferences.setMockInitialValues({'server_url': sharedLan});
    FlutterSecureStorage.setMockInitialValues({'access_token': 'OLD-ACCESS'});
    final registry = ServerRegistry.resetForTest();
    await registry.load();
    final outcome = await http.runWithClient(() => registry.migrateLegacy(), MockEdgeServer({}).client);
    expect(outcome, MigrationOutcome.deferred);
    expect(registry.servers, isEmpty);
    expect(await const FlutterSecureStorage().read(key: 'access_token'), 'OLD-ACCESS');
  });

  test('app instance id is generated once and kept', () async {
    final registry = ServerRegistry.resetForTest();
    final first = await registry.appInstanceId();
    expect(isValidDeviceId(first), isTrue);
    expect(await ServerRegistry.resetForTest().appInstanceId(), first);
  });
}
