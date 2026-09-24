import 'package:flutter_test/flutter_test.dart';

import 'package:edge_ai_cctv/services/pairing_payload.dart';

void main() {
  const id = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';

  group('PairingPayload.parseQr', () {
    test('valid payload with LAN and remote URLs', () {
      final p = PairingPayload.parseQr(
          'edgecctv://pair?v=1&d=$id&n=Main%20St%20Store&c=abcd-2345'
          '&u=https%3A%2F%2Fstore.example.com&u=http%3A%2F%2F192.168.1.50%3A8000%2F');
      expect(p.deviceId, id);
      expect(p.name, 'Main St Store');
      expect(p.code, 'ABCD-2345');
      // LAN first, remote https last, trailing slash removed.
      expect(p.urls, ['http://192.168.1.50:8000', 'https://store.example.com']);
    });

    test('device id is normalised to lower case', () {
      final p = PairingPayload.parseQr('edgecctv://pair?v=1&d=${id.toUpperCase()}&c=ABCD2345&u=http://10.0.0.2:8000');
      expect(p.deviceId, id);
      expect(p.code, 'ABCD-2345');
    });

    void rejects(String raw, String reason) {
      expect(() => PairingPayload.parseQr(raw), throwsA(isA<FormatException>()), reason: reason);
    }

    test('wrong scheme or host is rejected', () {
      rejects('https://evil.example.com/pair?v=1&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'https scheme');
      rejects('edgecctv://login?v=1&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'wrong host');
      rejects('otherapp://pair?v=1&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'other scheme');
      rejects('{"url":"http://10.0.0.2:8000","token":"x"}', 'old JSON QR format');
      rejects('', 'empty');
    });

    test('malformed payloads are rejected', () {
      rejects('edgecctv://pair?v=2&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'unknown version');
      rejects('edgecctv://pair?d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'missing version');
      rejects('edgecctv://pair?v=1&d=not-a-uuid&c=ABCD-2345&u=http://10.0.0.2:8000', 'bad device id');
      rejects('edgecctv://pair?v=1&c=ABCD-2345&u=http://10.0.0.2:8000', 'missing device id');
      rejects('edgecctv://pair?v=1&d=$id&c=ABC&u=http://10.0.0.2:8000', 'short code');
      rejects('edgecctv://pair?v=1&d=$id&u=http://10.0.0.2:8000', 'missing code');
      rejects('edgecctv://pair?v=1&d=$id&c=ABCD-2345', 'no url');
      rejects('edgecctv://pair?v=1&d=$id&c=ABCD-2345&u=ftp://10.0.0.2', 'non-http url');
      rejects('edgecctv://pair?v=1&d=$id&c=ABCD-2345&u=10.0.0.2:8000', 'url without scheme');
      rejects('edgecctv://pair?v=1&d=$id&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000', 'duplicate device id');
      rejects('edgecctv://pair?v=1&d=$id&c=ABCD-2345&u=http://10.0.0.2:8000&token=abc', 'unknown field');
      rejects('edgecctv://pair?v=1&d=$id&c=ABCD-2345&u=http://user:pw@10.0.0.2:8000', 'credentials in url');
    });
  });

  test('normalisePairingCode', () {
    expect(normalisePairingCode('abcd 2345'), 'ABCD-2345');
    expect(normalisePairingCode('ABCD-2345'), 'ABCD-2345');
    expect(normalisePairingCode('ABCD-234'), isNull);
    expect(normalisePairingCode('ABCD-2345-X'), isNull);
  });
}
