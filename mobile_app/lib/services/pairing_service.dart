import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../core/operator_policy.dart';
import '../models/server_entry.dart';
import 'connection_resolver.dart';
import 'pairing_payload.dart';
import 'server_registry.dart';

enum PairingErrorKind {
  invalidInput,
  unreachable,
  differentServer,
  deviceMismatch,
  badCode,
  badCredentials,
  forbidden,
  rateLimited,
  server,
}

/// A pairing failure with a sentence meant to be shown inline.
class PairingException implements Exception {
  final PairingErrorKind kind;
  final String message;

  /// Which form field the message belongs next to, when there is one.
  final String? field;

  const PairingException(this.kind, this.message, {this.field});

  @override
  String toString() => message;
}

/// The `phone` object sent with a claim or a login.
class PhoneInfo {
  final String name;
  final String platform;
  final String appInstanceId;
  final String? pushToken;

  const PhoneInfo({required this.name, required this.platform, required this.appInstanceId, this.pushToken});

  Map<String, dynamic> toJson() => {
        'name': name,
        'platform': platform,
        'app_instance_id': appInstanceId,
        'push_provider': pushToken != null ? 'fcm' : null,
        'push_token': pushToken,
      };

  static String currentPlatform() => defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android';

  static String defaultName() => defaultTargetPlatform == TargetPlatform.iOS ? 'iPhone' : 'Android phone';
}

class RemoveResult {
  /// True when the server confirmed it revoked this phone.
  final bool revokedOnServer;
  final String? note;
  const RemoveResult(this.revokedOnServer, this.note);
}

/// Pairs this phone with a server by claim code or by sign-in, and manages
/// the paired-device record afterwards. Both paths end with one
/// [ServerEntry] in [ServerRegistry] whose tokens are stored under that
/// server's device id.
class PairingService {
  final ServerRegistry registry;
  final Duration timeout;

  PairingService({ServerRegistry? registry, this.timeout = const Duration(seconds: 10)})
      : registry = registry ?? ServerRegistry();

  ConnectionResolver get _resolver => registry.resolver;

  Future<PhoneInfo> phoneInfo({String? name, String? pushToken}) async {
    final stored = await registry.phoneName();
    final chosen = (name ?? '').trim().isNotEmpty ? name!.trim() : (stored ?? PhoneInfo.defaultName());
    if ((name ?? '').trim().isNotEmpty) await registry.setPhoneName(chosen);
    return PhoneInfo(
      name: chosen,
      platform: PhoneInfo.currentPlatform(),
      appInstanceId: await registry.appInstanceId(),
      pushToken: pushToken,
    );
  }

  // ------------------------------------------------------------------ claim

  /// QR or manual pairing: `POST /api/v1/pairing/claim`.
  Future<ServerEntry> claim({
    required String deviceId,
    required String code,
    required List<String> urls,
    String? name,
    required PhoneInfo phone,
  }) async {
    if (!isValidDeviceId(deviceId)) {
      throw const PairingException(PairingErrorKind.invalidInput,
          'Enter the full device ID shown on the store dashboard (Settings, Pair a phone).', field: 'device_id');
    }
    final normCode = normalisePairingCode(code);
    if (normCode == null) {
      throw const PairingException(PairingErrorKind.invalidInput, 'The pairing code has 8 letters or digits, like ABCD-1234.',
          field: 'code');
    }
    final candidates = orderCandidateUrls(urls);
    if (candidates.isEmpty) {
      throw const PairingException(PairingErrorKind.invalidInput, 'Enter the edge server address, for example 192.168.1.20:8000.',
          field: 'address');
    }
    final id = normaliseDeviceId(deviceId);
    final resolved = await _resolveOrThrow(candidates, id);
    final url = resolved.url!;

    final http.Response res;
    try {
      res = await http
          .post(Uri.parse('$url/api/v1/pairing/claim'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'device_id': id, 'code': normCode, 'phone': phone.toJson()}))
          .timeout(timeout);
    } on TimeoutException {
      throw const PairingException(PairingErrorKind.unreachable, 'The edge server did not answer in time. Try again.');
    } catch (e) {
      throw PairingException(PairingErrorKind.unreachable, 'Cannot reach the edge server: ${_brief(e)}');
    }

    if (res.statusCode != 200) throw _claimError(res);
    final data = _decodeMap(res.body);
    final access = data['access_token']?.toString();
    if (access == null || access.isEmpty) {
      throw const PairingException(PairingErrorKind.server, 'The server accepted the code but returned no session. Try again.');
    }
    final returnedId = data['device_id']?.toString();
    if (returnedId != null && normaliseDeviceId(returnedId) != id) {
      throw const PairingException(PairingErrorKind.deviceMismatch,
          'The server answered for a different store. Nothing was saved.');
    }
    final serverUrls = (data['urls'] is List) ? (data['urls'] as List).map((e) => e.toString()).toList() : <String>[];
    await registry.saveTokens(id, accessToken: access, refreshToken: data['refresh_token']?.toString());
    return registry.upsert(
      ServerEntry(
        deviceId: id,
        name: (data['device_name'] ?? resolved.identity?.deviceName ?? name ?? '').toString(),
        urls: [url, ...serverUrls, ...candidates, if (resolved.identity?.remoteUrl != null) resolved.identity!.remoteUrl!],
        lastGoodUrl: url,
        remoteUrl: resolved.identity?.remoteUrl,
        pairedDeviceId: data['paired_device_id']?.toString(),
        pushTokenSent: phone.pushToken,
        pairedAt: DateTime.now(),
      ),
      makeActive: true,
    );
  }

  Future<ServerEntry> claimPayload(PairingPayload p, PhoneInfo phone) =>
      claim(deviceId: p.deviceId, code: p.code, urls: p.urls, name: p.name, phone: phone);

  PairingException _claimError(http.Response res) {
    final detail = _detail(res.body);
    switch (res.statusCode) {
      case 409:
        return const PairingException(PairingErrorKind.deviceMismatch,
            'This pairing code belongs to a different Edge CCTV server (device_mismatch). '
            'Scan the code shown by the store you want to add.');
      case 403:
        return PairingException(PairingErrorKind.badCode,
            detail ?? 'The pairing code is wrong, expired or already used. Create a new code on the store dashboard.',
            field: 'code');
      case 429:
        return PairingException(PairingErrorKind.rateLimited,
            describeHttpError(429, res.body, retryAfter: res.headers['retry-after']));
      default:
        return PairingException(PairingErrorKind.server, describeHttpError(res.statusCode, res.body));
    }
  }

  // ---------------------------------------------------------------- sign in

  /// Username/password: proves the address is an Edge CCTV server (and the
  /// expected one when [expectedDeviceId] is given) through the identity
  /// endpoint, then `POST /api/v1/auth/login` with the `phone` object.
  Future<ServerEntry> signIn({
    required String address,
    required String username,
    required String password,
    required PhoneInfo phone,
    String? expectedDeviceId,
  }) async {
    final url = normaliseBaseUrl(address);
    if (url == null) {
      throw const PairingException(PairingErrorKind.invalidInput, 'Enter the edge server address, for example 192.168.1.20:8000.',
          field: 'address');
    }
    if (username.trim().isEmpty || password.isEmpty) {
      throw const PairingException(PairingErrorKind.invalidInput, 'Enter your username and password.');
    }
    final probe = await _resolver.probe(url, expectedDeviceId: expectedDeviceId);
    _throwForProbe(probe);
    final identity = probe.identity!;

    final http.Response res;
    try {
      res = await http
          .post(Uri.parse('$url/api/v1/auth/login'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'username': username.trim(), 'password': password, 'phone': phone.toJson()}))
          .timeout(timeout);
    } on TimeoutException {
      throw const PairingException(PairingErrorKind.unreachable, 'The edge server did not answer in time. Try again.');
    } catch (e) {
      throw PairingException(PairingErrorKind.unreachable, 'Cannot reach the edge server: ${_brief(e)}');
    }

    if (res.statusCode != 200) {
      final detail = _detail(res.body);
      switch (res.statusCode) {
        case 401:
          throw PairingException(PairingErrorKind.badCredentials, detail ?? 'Wrong username or password.');
        case 409:
          throw const PairingException(PairingErrorKind.deviceMismatch,
              '$kDifferentServerMessage (device_mismatch). Nothing was saved.', field: 'address');
        case 403:
          throw PairingException(PairingErrorKind.forbidden, detail ?? 'This account may not sign in from the app.');
        case 429:
          throw PairingException(
              PairingErrorKind.rateLimited, describeHttpError(429, res.body, retryAfter: res.headers['retry-after']));
        default:
          throw PairingException(PairingErrorKind.server, describeHttpError(res.statusCode, res.body));
      }
    }
    final data = _decodeMap(res.body);
    final access = data['access_token']?.toString();
    if (access == null || access.isEmpty) {
      throw const PairingException(PairingErrorKind.server, 'The server signed you in but returned no session. Try again.');
    }
    final returnedId = data['device_id']?.toString();
    if (returnedId != null && normaliseDeviceId(returnedId) != identity.deviceId) {
      throw const PairingException(PairingErrorKind.deviceMismatch,
          'The server answered for a different store. Nothing was saved.');
    }
    await registry.saveTokens(identity.deviceId, accessToken: access, refreshToken: data['refresh_token']?.toString());
    final serverUrls = (data['urls'] is List) ? (data['urls'] as List).map((e) => e.toString()).toList() : <String>[];
    return registry.upsert(
      ServerEntry(
        deviceId: identity.deviceId,
        name: (data['device_name'] ?? identity.deviceName).toString(),
        urls: [url, ...serverUrls, if (identity.remoteUrl != null) identity.remoteUrl!],
        lastGoodUrl: url,
        remoteUrl: identity.remoteUrl,
        pairedDeviceId: data['paired_device_id']?.toString(),
        pushTokenSent: data['paired_device_id'] != null ? phone.pushToken : null,
        pairedAt: DateTime.now(),
      ),
      makeActive: true,
    );
  }

  // ---------------------------------------------------------- after pairing

  /// `PUT /api/v1/pairing/devices/me/push` on one server.
  Future<bool> sendPushToken(ServerEntry entry, String token) async {
    final url = registry.urlFor(entry.deviceId);
    if (url == null || !await registry.hasSession(entry.deviceId)) return false;
    try {
      final res = await registry.authorizedSend(
          entry.deviceId,
          (headers) => http
              .put(Uri.parse('$url/api/v1/pairing/devices/me/push'),
                  headers: {'Content-Type': 'application/json', ...headers},
                  body: jsonEncode({'push_provider': 'fcm', 'push_token': token}))
              .timeout(timeout));
      if (res.statusCode >= 200 && res.statusCode < 300) {
        entry.pushTokenSent = token;
        await registry.save(entry);
        return true;
      }
      debugPrint('Push token update for ${entry.shortId} returned HTTP ${res.statusCode}');
    } catch (e) {
      debugPrint('Push token update for ${entry.shortId} failed: ${_brief(e)}');
    }
    return false;
  }

  /// Sends [token] to every server that has not accepted it yet.
  Future<int> syncPushToken(String token) async {
    var sent = 0;
    for (final entry in registry.servers) {
      if (entry.pushTokenSent == token) continue;
      if (await sendPushToken(entry, token)) sent++;
    }
    return sent;
  }

  /// Revokes this phone on the server (best effort), then forgets the server
  /// and its tokens locally.
  Future<RemoveResult> removeServer(String deviceId) async {
    final entry = registry.byId(deviceId);
    if (entry == null) return const RemoveResult(false, null);
    var revoked = false;
    String? note;
    final url = registry.urlFor(entry.deviceId);
    final headers = await registry.authHeadersFor(entry.deviceId);
    if (entry.pairedDeviceId == null) {
      note = 'This phone was not registered as a paired device, so there was nothing to revoke on the server.';
    } else if (url == null || headers.isEmpty) {
      note = 'No session for this server; revoke the phone from the store dashboard.';
    } else {
      try {
        final res = await http
            .delete(Uri.parse('$url/api/v1/pairing/devices/${Uri.encodeComponent(entry.pairedDeviceId!)}'), headers: headers)
            .timeout(const Duration(seconds: 5));
        revoked = (res.statusCode >= 200 && res.statusCode < 300) || res.statusCode == 404;
        if (!revoked) note = 'The server refused to revoke this phone (HTTP ${res.statusCode}); revoke it from the store dashboard.';
      } catch (e) {
        note = 'The server could not be reached (${_brief(e)}); revoke this phone from the store dashboard.';
      }
    }
    await registry.remove(entry.deviceId);
    return RemoveResult(revoked, note);
  }

  /// This phone's paired-device record, from `GET /api/v1/pairing/devices`.
  Future<Map<String, dynamic>?> fetchOwnRecord(ServerEntry entry) async {
    final url = registry.urlFor(entry.deviceId);
    if (url == null) throw const PairingException(PairingErrorKind.unreachable, 'Server address unknown.');
    final res = await registry.authorizedSend(
        entry.deviceId, (headers) => http.get(Uri.parse('$url/api/v1/pairing/devices'), headers: headers).timeout(timeout));
    if (res.statusCode == 401) {
      throw const PairingException(PairingErrorKind.badCredentials,
          'The server no longer accepts this phone (unpaired or session expired). Pair it again.');
    }
    if (res.statusCode != 200) throw PairingException(PairingErrorKind.server, describeHttpError(res.statusCode, res.body));
    final decoded = jsonDecode(res.body);
    final list = decoded is List ? decoded : (decoded is Map ? (decoded['devices'] ?? decoded['items'] ?? []) : []);
    final instanceId = await registry.appInstanceId();
    for (final item in list as List) {
      if (item is! Map) continue;
      final m = Map<String, dynamic>.from(item);
      final id = (m['id'] ?? m['paired_device_id'])?.toString();
      if ((entry.pairedDeviceId != null && id == entry.pairedDeviceId) || m['app_instance_id'] == instanceId) {
        if (entry.pairedDeviceId == null && id != null) {
          entry.pairedDeviceId = id;
          await registry.save(entry);
        }
        final prefs = m['alert_prefs'] ?? m['prefs'];
        if (prefs is Map) {
          entry.alertPrefs = AlertPrefs.fromJson(Map<String, dynamic>.from(prefs));
          await registry.save(entry);
        }
        return m;
      }
    }
    return null;
  }

  /// `PATCH /api/v1/pairing/devices/{id}` with new alert preferences.
  Future<AlertPrefs> saveAlertPrefs(ServerEntry entry, AlertPrefs prefs) async {
    final url = registry.urlFor(entry.deviceId);
    if (url == null || entry.pairedDeviceId == null) {
      throw const PairingException(PairingErrorKind.invalidInput,
          'This phone is not registered as a paired device on this server. Pair it again to set alert preferences.');
    }
    final http.Response res;
    try {
      res = await registry.authorizedSend(
          entry.deviceId,
          (headers) => http
              .patch(Uri.parse('$url/api/v1/pairing/devices/${Uri.encodeComponent(entry.pairedDeviceId!)}'),
                  headers: {'Content-Type': 'application/json', ...headers},
                  body: jsonEncode({'alert_prefs': prefs.toJson()}))
              .timeout(timeout));
    } catch (e) {
      throw PairingException(PairingErrorKind.unreachable, 'Cannot reach the edge server: ${_brief(e)}');
    }
    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw PairingException(PairingErrorKind.server, describeHttpError(res.statusCode, res.body));
    }
    var saved = prefs;
    try {
      final m = _decodeMap(res.body);
      final p = m['alert_prefs'] ?? m['prefs'];
      if (p is Map) saved = AlertPrefs.fromJson(Map<String, dynamic>.from(p));
    } catch (_) {}
    entry.alertPrefs = saved;
    await registry.save(entry);
    return saved;
  }

  // ---------------------------------------------------------------- helpers

  Future<ResolveResult> _resolveOrThrow(List<String> candidates, String deviceId) async {
    final result = await _resolver.resolve(candidates, deviceId);
    if (result.resolved) return result;
    if (result.mismatched.isNotEmpty) {
      throw PairingException(PairingErrorKind.differentServer, result.mismatched.first.describe(), field: 'address');
    }
    final notEdge = result.probes.where((p) => p.outcome == ProbeOutcome.notEdgeServer);
    if (notEdge.isNotEmpty) {
      throw PairingException(PairingErrorKind.unreachable, notEdge.first.describe(), field: 'address');
    }
    throw PairingException(
        PairingErrorKind.unreachable,
        'Could not reach the edge server at ${candidates.join(', ')}. '
        'Check that this phone is on the store network, or that remote access is enabled.',
        field: 'address');
  }

  void _throwForProbe(IdentityProbe probe) {
    switch (probe.outcome) {
      case ProbeOutcome.match:
        return;
      case ProbeOutcome.differentServer:
        throw PairingException(PairingErrorKind.differentServer, probe.describe(), field: 'address');
      case ProbeOutcome.notEdgeServer:
      case ProbeOutcome.unreachable:
        throw PairingException(PairingErrorKind.unreachable, probe.describe(), field: 'address');
    }
  }

  static Map<String, dynamic> _decodeMap(String body) {
    try {
      final d = jsonDecode(body);
      return d is Map ? Map<String, dynamic>.from(d) : {};
    } catch (_) {
      return {};
    }
  }

  static String? _detail(String body) {
    final d = _decodeMap(body)['detail'];
    return d is String && d.isNotEmpty ? d : null;
  }

  static String _brief(Object e) {
    final s = e.toString();
    return s.length > 120 ? '${s.substring(0, 120)}...' : s;
  }
}
