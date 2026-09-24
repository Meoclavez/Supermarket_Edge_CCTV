import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// Fake edge servers following CONTRACT2 ("Device identity", "Pairing"):
/// each base URL maps to the identity of the box that answers there.
/// Addresses not in [boxes] refuse the connection.
class MockEdgeServer {
  final Map<String, FakeBox> boxes;
  final List<http.Request> requests = [];

  /// Forces the claim endpoint to answer with this status (403, 409, 429).
  int? claimStatus;
  int? loginStatus;

  MockEdgeServer(this.boxes);

  static const storeA = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  static const storeB = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';

  List<http.Request> hits(String path) => requests.where((r) => r.url.path == path).toList();

  MockClient client() => MockClient((req) async {
        requests.add(req);
        final base = '${req.url.scheme}://${req.url.host}:${req.url.port}';
        final box = boxes[base] ?? boxes['${req.url.scheme}://${req.url.host}'];
        if (box == null) throw const SocketException('Connection refused');
        return box.handle(req, this);
      });
}

class FakeBox {
  final String deviceId;
  final String name;
  final String? remoteUrl;
  final String validCode;
  final Map<String, dynamic> prefs = {'event_types': null, 'min_severity': 'INFO', 'camera_ids': null, 'quiet_hours': null};
  final List<String> revoked = [];
  String? pushToken;

  FakeBox(this.deviceId, this.name, {this.remoteUrl, this.validCode = 'ABCD-2345'});

  String get short => deviceId.substring(0, 4);

  http.Response _json(Object body, [int status = 200, Map<String, String> headers = const {}]) =>
      http.Response(jsonEncode(body), status, headers: {'content-type': 'application/json', ...headers});

  bool _authed(http.Request r) => r.headers['Authorization'] == 'Bearer ACCESS-$short';

  Future<http.Response> handle(http.Request r, MockEdgeServer server) async {
    final path = r.url.path;
    if (path == '/api/v1/device/identity' && r.method == 'GET') {
      return _json({
        'device_id': deviceId,
        'device_name': name,
        'app_version': '1.2.0',
        'api_version': 1,
        'pairing': true,
        'remote_url': remoteUrl,
      });
    }
    if (path == '/api/v1/pairing/claim' && r.method == 'POST') {
      final body = jsonDecode(r.body) as Map<String, dynamic>;
      final forced = server.claimStatus;
      if (forced == 429) {
        return _json({'detail': 'Too many attempts from this address. Wait 15 minutes and try again.'}, 429,
            {'retry-after': '900'});
      }
      if (forced == 409 || body['device_id'] != deviceId) return _json({'detail': 'device_mismatch'}, 409);
      if (forced == 403 || body['code'] != validCode) {
        return _json({'detail': 'Pairing code is wrong, expired or already used.'}, 403);
      }
      return _json({
        'paired_device_id': 'pd-$short',
        'device_id': deviceId,
        'device_name': name,
        'access_token': 'ACCESS-$short',
        'refresh_token': 'REFRESH-$short',
        'token_type': 'bearer',
        'urls': ['http://10.0.0.5:8000'],
      });
    }
    if (path == '/api/v1/auth/login' && r.method == 'POST') {
      final body = jsonDecode(r.body) as Map<String, dynamic>;
      if (server.loginStatus == 429) {
        return _json({'detail': 'Too many failed attempts from this address.'}, 429, {'retry-after': '600'});
      }
      if (body['password'] != 'correct-horse') return _json({'detail': 'Invalid username or password'}, 401);
      return _json({
        'paired_device_id': 'pd-$short',
        'device_id': deviceId,
        'device_name': name,
        'access_token': 'ACCESS-$short',
        'refresh_token': 'REFRESH-$short',
        'token_type': 'bearer',
        'urls': <String>[],
      });
    }
    if (path == '/api/v1/pairing/devices/me/push' && r.method == 'PUT') {
      if (!_authed(r)) return _json({'detail': 'Not authenticated'}, 401);
      pushToken = (jsonDecode(r.body) as Map)['push_token']?.toString();
      return _json({'id': 'pd-$short'});
    }
    if (path == '/api/v1/pairing/devices' && r.method == 'GET') {
      if (!_authed(r)) return _json({'detail': 'Not authenticated'}, 401);
      return _json({
        'devices': [
          {'id': 'pd-$short', 'name': 'Test phone', 'platform': 'android', 'revoked': false, 'alert_prefs': prefs}
        ],
        'count': 1,
      });
    }
    if (path.startsWith('/api/v1/pairing/devices/') && r.method == 'PATCH') {
      if (!_authed(r)) return _json({'detail': 'Not authenticated'}, 401);
      prefs
        ..clear()
        ..addAll(Map<String, dynamic>.from((jsonDecode(r.body) as Map)['alert_prefs'] as Map));
      return _json({'id': 'pd-$short', 'alert_prefs': prefs});
    }
    if (path.startsWith('/api/v1/pairing/devices/') && r.method == 'DELETE') {
      if (!_authed(r)) return _json({'detail': 'Not authenticated'}, 401);
      revoked.add(path.split('/').last);
      return _json({'revoked': true});
    }
    return _json({'detail': 'unexpected $path'}, 404);
  }
}
