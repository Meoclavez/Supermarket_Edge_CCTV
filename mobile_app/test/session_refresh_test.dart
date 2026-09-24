import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:edge_ai_cctv/models/server_entry.dart';
import 'package:edge_ai_cctv/services/server_registry.dart';

/// CONTRACT2_CHANGES: on 401 refresh once; a refresh that returns 401 means
/// the phone was revoked, so tokens are cleared and the app returns to pairing.
void main() {
  const a = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  const url = 'http://192.168.1.50:8000';

  Future<ServerRegistry> setup() async {
    SharedPreferences.setMockInitialValues({});
    FlutterSecureStorage.setMockInitialValues({});
    final r = ServerRegistry.resetForTest();
    await r.load();
    await r.upsert(ServerEntry(deviceId: a, name: 'A', urls: [url], lastGoodUrl: url));
    await r.saveTokens(a, accessToken: 'OLD', refreshToken: 'R1');
    return r;
  }

  test('401 triggers one refresh and the request is repeated with the new token', () async {
    final r = await setup();
    final seen = <String?>[];
    final client = MockClient((req) async {
      if (req.url.path == '/api/v1/auth/refresh') return http.Response('{"access_token":"NEW","token_type":"bearer"}', 200);
      seen.add(req.headers['Authorization']);
      return req.headers['Authorization'] == 'Bearer NEW' ? http.Response('{}', 200) : http.Response('{}', 401);
    });
    final res = await http.runWithClient(
        () => r.authorizedSend(a, (h) => http.get(Uri.parse('$url/api/v1/pairing/devices'), headers: h)), () => client);
    expect(res.statusCode, 200);
    expect(seen, ['Bearer OLD', 'Bearer NEW']);
    expect(await r.accessToken(a), 'NEW');
  });

  test('a rejected refresh clears the session and reports it', () async {
    final r = await setup();
    String? lost;
    r.onSessionLost = (id) => lost = id;
    final client = MockClient((req) async => http.Response('{"detail":"revoked"}', 401));
    final res = await http.runWithClient(
        () => r.authorizedSend(a, (h) => http.get(Uri.parse('$url/api/v1/pairing/devices'), headers: h)), () => client);
    expect(res.statusCode, 401);
    expect(lost, a);
    expect(await r.accessToken(a), isNull);
    expect(r.byId(a), isNotNull, reason: 'the store stays listed so the user can sign in again');
  });
}
