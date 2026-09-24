import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:edge_ai_cctv/services/connection_resolver.dart';

import 'support/mock_edge_server.dart';

void main() {
  const a = MockEdgeServer.storeA;
  const b = MockEdgeServer.storeB;

  test('picks the candidate whose device_id matches and rejects the mismatched one', () async {
    // Store B's box now sits at the LAN address store A used to have; store A
    // is reachable only through its remote URL.
    final server = MockEdgeServer({
      'http://192.168.1.50:8000': FakeBox(b, 'Store B'),
      'https://store-a.example.com': FakeBox(a, 'Store A'),
    });
    final result = await http.runWithClient(
      () => const ConnectionResolver().resolve(['http://192.168.1.50:8000', 'https://store-a.example.com'], a),
      server.client,
    );
    expect(result.url, 'https://store-a.example.com');
    expect(result.identity!.deviceId, a);
    expect(result.mismatched.map((p) => p.url), ['http://192.168.1.50:8000']);
    expect(result.mismatched.single.describe(), startsWith(kDifferentServerMessage));
  });

  test('prefers the earlier (LAN) candidate when several match', () async {
    final server = MockEdgeServer({
      'http://192.168.1.50:8000': FakeBox(a, 'Store A'),
      'https://store-a.example.com': FakeBox(a, 'Store A'),
    });
    final result = await http.runWithClient(
      () => const ConnectionResolver().resolve(['http://192.168.1.50:8000', 'https://store-a.example.com'], a),
      server.client,
    );
    expect(result.url, 'http://192.168.1.50:8000');
  });

  test('an address answering with another device_id is never accepted', () async {
    final server = MockEdgeServer({'http://192.168.1.50:8000': FakeBox(b, 'Store B')});
    final result = await http.runWithClient(
      () => const ConnectionResolver().resolve(['http://192.168.1.50:8000', 'http://10.9.9.9:8000'], a),
      server.client,
    );
    expect(result.resolved, isFalse);
    expect(result.probes.map((p) => p.outcome), [ProbeOutcome.differentServer, ProbeOutcome.unreachable]);
  });

  test('a web server that is not an Edge CCTV box is not accepted', () async {
    final probe = await http.runWithClient(
      () => const ConnectionResolver().probe('http://192.168.1.1', expectedDeviceId: a),
      () => MockClient((_) async => http.Response('<html>router login</html>', 200)),
    );
    expect(probe.outcome, ProbeOutcome.notEdgeServer);
  });
}
