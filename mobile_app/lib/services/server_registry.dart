import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import '../models/server_entry.dart';
import 'connection_resolver.dart';

enum ServerReachability { unknown, resolving, reachable, unreachable, differentServer }

enum MigrationOutcome { nothingToMigrate, migrated, deferred }

enum RefreshOutcome { refreshed, sessionLost, failed }

/// All servers (stores) this phone is paired with, keyed by `device_id`.
///
/// Server entries live in SharedPreferences; tokens live in the platform
/// secure store under `srv.<device_id>.access_token` /
/// `srv.<device_id>.refresh_token`, so two stores never share or overwrite
/// each other's session even when they sit at the same LAN address.
class ServerRegistry extends ChangeNotifier {
  static ServerRegistry _instance = ServerRegistry._(const ConnectionResolver());
  factory ServerRegistry() => _instance;
  ServerRegistry._(this._resolver);

  /// Replaces the singleton with a fresh, empty registry (tests only).
  @visibleForTesting
  static ServerRegistry resetForTest({ConnectionResolver resolver = const ConnectionResolver()}) {
    _instance = ServerRegistry._(resolver);
    return _instance;
  }

  static const String prefsServersKey = 'edge_servers_v1';
  static const String prefsActiveKey = 'edge_active_device_id';
  static const String prefsInstanceIdKey = 'app_instance_id';
  static const String prefsPhoneNameKey = 'phone_name';
  static const List<String> legacyUrlKeys = ['server_url', 'edge_server_base_url'];

  static String accessTokenKey(String deviceId) => 'srv.${normaliseDeviceId(deviceId)}.access_token';
  static String refreshTokenKey(String deviceId) => 'srv.${normaliseDeviceId(deviceId)}.refresh_token';

  static const FlutterSecureStorage _secure = FlutterSecureStorage();

  final ConnectionResolver _resolver;
  final Map<String, ServerEntry> _servers = {};
  final Map<String, ServerReachability> _reachability = {};
  final Map<String, ResolveResult> _lastResult = {};
  final Map<String, Future<ResolveResult>> _inflight = {};
  String? _activeId;
  bool _loaded = false;
  StreamSubscription<List<ConnectivityResult>>? _connectivitySub;
  Timer? _connectivityDebounce;

  ConnectionResolver get resolver => _resolver;
  bool get isLoaded => _loaded;

  List<ServerEntry> get servers {
    final list = _servers.values.toList();
    list.sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    return list;
  }

  ServerEntry? byId(String? deviceId) => deviceId == null ? null : _servers[normaliseDeviceId(deviceId)];
  bool isKnown(String? deviceId) => byId(deviceId) != null;
  String? get activeId => _activeId;
  ServerEntry? get active => byId(_activeId);

  /// Base URL to use for the active server: the last URL proven to reach it.
  String? get activeUrl => urlFor(_activeId);
  String? urlFor(String? deviceId) {
    final e = byId(deviceId);
    return e?.lastGoodUrl ?? (e != null && e.candidates.isNotEmpty ? e.candidates.first : null);
  }

  ServerReachability reachabilityOf(String deviceId) =>
      _reachability[normaliseDeviceId(deviceId)] ?? ServerReachability.unknown;
  ResolveResult? lastResolveOf(String deviceId) => _lastResult[normaliseDeviceId(deviceId)];

  // ----------------------------------------------------------------- storage

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _servers.clear();
    final raw = prefs.getString(prefsServersKey);
    if (raw != null && raw.isNotEmpty) {
      try {
        final list = jsonDecode(raw) as List;
        for (final item in list) {
          if (item is! Map) continue;
          final entry = ServerEntry.fromJson(Map<String, dynamic>.from(item));
          if (isValidDeviceId(entry.deviceId)) _servers[entry.deviceId] = entry;
        }
      } catch (e) {
        debugPrint('Server list could not be read: $e');
      }
    }
    _activeId = prefs.getString(prefsActiveKey);
    if (_activeId == null || !_servers.containsKey(_activeId)) {
      _activeId = _servers.isEmpty ? null : servers.first.deviceId;
    }
    _loaded = true;
    notifyListeners();
  }

  Future<void> _persist() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(prefsServersKey, jsonEncode(_servers.values.map((e) => e.toJson()).toList()));
    if (_activeId != null) {
      await prefs.setString(prefsActiveKey, _activeId!);
    } else {
      await prefs.remove(prefsActiveKey);
    }
  }

  /// Adds or updates the entry for `entry.deviceId`. An existing entry keeps
  /// its known URLs (merged) and any field the new entry leaves null.
  Future<ServerEntry> upsert(ServerEntry entry, {bool makeActive = false}) async {
    final existing = _servers[entry.deviceId];
    if (existing != null) {
      entry.mergeUrls(existing.urls);
      entry.lastGoodUrl ??= existing.lastGoodUrl;
      entry.remoteUrl ??= existing.remoteUrl;
      entry.pairedDeviceId ??= existing.pairedDeviceId;
      entry.pushTokenSent ??= existing.pushTokenSent;
      entry.alertPrefs ??= existing.alertPrefs;
      entry.pairedAt ??= existing.pairedAt;
      if (entry.name.trim().isEmpty) entry.name = existing.name;
    }
    if (entry.name.trim().isEmpty) entry.name = 'Edge CCTV ${entry.deviceId.substring(0, 4)}';
    _servers[entry.deviceId] = entry;
    if (makeActive || _activeId == null) _activeId = entry.deviceId;
    await _persist();
    notifyListeners();
    return entry;
  }

  /// Persists changes made directly to an entry's fields.
  Future<void> save(ServerEntry entry) async {
    if (!_servers.containsKey(entry.deviceId)) return;
    await _persist();
    notifyListeners();
  }

  Future<void> remove(String deviceId) async {
    final id = normaliseDeviceId(deviceId);
    await clearTokens(id);
    _servers.remove(id);
    _reachability.remove(id);
    _lastResult.remove(id);
    if (_activeId == id) _activeId = _servers.isEmpty ? null : servers.first.deviceId;
    await _persist();
    notifyListeners();
  }

  Future<void> setActive(String deviceId) async {
    final id = normaliseDeviceId(deviceId);
    if (!_servers.containsKey(id)) return;
    if (_activeId == id) return;
    _activeId = id;
    await _persist();
    notifyListeners();
  }

  // ------------------------------------------------------------------ tokens

  Future<void> saveTokens(String deviceId, {required String accessToken, String? refreshToken}) async {
    await _secure.write(key: accessTokenKey(deviceId), value: accessToken);
    if (refreshToken != null && refreshToken.isNotEmpty) {
      await _secure.write(key: refreshTokenKey(deviceId), value: refreshToken);
    }
  }

  Future<String?> accessToken(String deviceId) => _safeRead(accessTokenKey(deviceId));
  Future<String?> refreshToken(String deviceId) => _safeRead(refreshTokenKey(deviceId));

  Future<bool> hasSession(String deviceId) async => (await accessToken(deviceId))?.isNotEmpty == true;

  Future<void> clearTokens(String deviceId) async {
    try {
      await _secure.delete(key: accessTokenKey(deviceId));
      await _secure.delete(key: refreshTokenKey(deviceId));
    } catch (e) {
      debugPrint('Secure storage delete failed: $e');
    }
  }

  Future<String?> _safeRead(String key) async {
    try {
      return await _secure.read(key: key);
    } catch (e) {
      debugPrint('Secure storage read failed: $e');
      return null;
    }
  }

  Future<Map<String, String>> authHeadersFor(String? deviceId) async {
    if (deviceId == null) return {};
    final token = await accessToken(deviceId);
    return token != null && token.isNotEmpty ? {'Authorization': 'Bearer $token'} : {};
  }

  /// Called when a server rejects this phone's refresh token (the phone was
  /// revoked on the dashboard, or paired again elsewhere). The app returns to
  /// the pairing screen for that server.
  void Function(String deviceId)? onSessionLost;

  final Map<String, Future<RefreshOutcome>> _refreshing = {};

  /// Uses the refresh token when the access token expires within [margin].
  Future<bool> refreshIfExpiring(String deviceId, {Duration margin = const Duration(minutes: 2)}) async {
    final access = await accessToken(deviceId);
    if (access == null) return false;
    final exp = jwtExpiry(access);
    if (exp == null || exp.isAfter(DateTime.now().add(margin))) return false;
    return await refreshSession(deviceId) == RefreshOutcome.refreshed;
  }

  /// `POST /api/v1/auth/refresh`, once at a time per server. A 401 means the
  /// session is gone: the tokens are cleared and [onSessionLost] is called.
  Future<RefreshOutcome> refreshSession(String deviceId) {
    final id = normaliseDeviceId(deviceId);
    final running = _refreshing[id];
    if (running != null) return running;
    final future = _doRefresh(id).whenComplete(() {
      _refreshing.remove(id);
    });
    _refreshing[id] = future;
    return future;
  }

  Future<RefreshOutcome> _doRefresh(String id) async {
    final refresh = await refreshToken(id);
    final url = urlFor(id);
    if (url == null) return RefreshOutcome.failed;
    if (refresh == null || refresh.isEmpty) {
      await _sessionLost(id);
      return RefreshOutcome.sessionLost;
    }
    try {
      final res = await http
          .post(Uri.parse('$url/api/v1/auth/refresh'),
              headers: {'Content-Type': 'application/json'}, body: jsonEncode({'refresh_token': refresh}))
          .timeout(const Duration(seconds: 8));
      if (res.statusCode == 401 || res.statusCode == 403) {
        await _sessionLost(id);
        return RefreshOutcome.sessionLost;
      }
      if (res.statusCode != 200) return RefreshOutcome.failed;
      final token = (jsonDecode(res.body) as Map)['access_token']?.toString();
      if (token == null || token.isEmpty) return RefreshOutcome.failed;
      await saveTokens(id, accessToken: token);
      return RefreshOutcome.refreshed;
    } catch (e) {
      debugPrint('Token refresh failed: ${e.runtimeType}');
      return RefreshOutcome.failed;
    }
  }

  Future<void> _sessionLost(String id) async {
    await clearTokens(id);
    notifyListeners();
    onSessionLost?.call(id);
  }

  /// Sends an authenticated request to [deviceId]'s server. On a 401 it
  /// refreshes the session once and repeats the request with the new token.
  Future<http.Response> authorizedSend(
      String deviceId, Future<http.Response> Function(Map<String, String> authHeaders) send) async {
    final first = await send(await authHeadersFor(deviceId));
    if (first.statusCode != 401) return first;
    if (await refreshSession(deviceId) != RefreshOutcome.refreshed) return first;
    return send(await authHeadersFor(deviceId));
  }

  // --------------------------------------------------------------- resolving

  /// Proves which candidate URL reaches [deviceId] and remembers it.
  Future<ResolveResult> resolve(String deviceId) {
    final id = normaliseDeviceId(deviceId);
    final running = _inflight[id];
    if (running != null) return running;
    // A block body: returning the removed future from whenComplete would make
    // the future wait on itself.
    final future = _doResolve(id).whenComplete(() {
      _inflight.remove(id);
    });
    _inflight[id] = future;
    return future;
  }

  Future<ResolveResult?> resolveActive() async => _activeId == null ? null : resolve(_activeId!);

  Future<ResolveResult> _doResolve(String id) async {
    final entry = _servers[id];
    if (entry == null) return const ResolveResult(null, null, []);
    _reachability[id] = ServerReachability.resolving;
    notifyListeners();
    final result = await _resolver.resolve(entry.candidates, id);
    _lastResult[id] = result;
    final current = _servers[id];
    if (current == null) return result; // removed while resolving
    if (result.resolved) {
      _reachability[id] = ServerReachability.reachable;
      current.lastGoodUrl = result.url;
      final identity = result.identity;
      if (identity != null) {
        if (identity.deviceName.isNotEmpty) current.name = identity.deviceName;
        current.remoteUrl = identity.remoteUrl;
        if (identity.remoteUrl != null) current.mergeUrls([identity.remoteUrl!]);
      }
      await _persist();
    } else {
      _reachability[id] =
          result.mismatched.isNotEmpty ? ServerReachability.differentServer : ServerReachability.unreachable;
    }
    notifyListeners();
    return result;
  }

  /// Re-resolves the active server whenever the network changes (Wi-Fi to
  /// mobile data and back), so the app moves between the LAN address and the
  /// remote URL without being told.
  void startNetworkWatch() {
    _connectivitySub ??= Connectivity().onConnectivityChanged.listen((_) {
      _connectivityDebounce?.cancel();
      _connectivityDebounce = Timer(const Duration(seconds: 1), () => resolveActive());
    }, onError: (Object e) => debugPrint('Connectivity watch notice: $e'));
  }

  // ------------------------------------------------------------ this phone

  /// A random id generated once per install; identifies this app instance to
  /// every server so re-pairing updates the same record.
  Future<String> appInstanceId() async {
    final prefs = await SharedPreferences.getInstance();
    final existing = prefs.getString(prefsInstanceIdKey);
    if (existing != null && existing.isNotEmpty) return existing;
    final id = generateUuidV4();
    await prefs.setString(prefsInstanceIdKey, id);
    return id;
  }

  Future<String?> phoneName() async => (await SharedPreferences.getInstance()).getString(prefsPhoneNameKey);

  Future<void> setPhoneName(String name) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(prefsPhoneNameKey, name.trim());
  }

  // --------------------------------------------------------------- migration

  /// Moves the pre-multi-server settings (`server_url` plus un-namespaced
  /// `access_token`/`refresh_token`) into a server entry keyed by the device
  /// id the old address reports. When the old server cannot be reached the
  /// legacy settings are kept and migration is retried on the next start.
  Future<MigrationOutcome> migrateLegacy() async {
    final prefs = await SharedPreferences.getInstance();
    String? legacyUrl;
    for (final k in legacyUrlKeys) {
      final v = prefs.getString(k);
      if (v != null && v.trim().isNotEmpty) {
        legacyUrl = v;
        break;
      }
    }
    final legacyAccess = await _safeRead('access_token');
    final legacyRefresh = await _safeRead('refresh_token');
    if (legacyUrl == null) {
      if (legacyAccess != null || legacyRefresh != null) await _deleteLegacyTokens();
      return MigrationOutcome.nothingToMigrate;
    }
    final probe = await _resolver.probe(legacyUrl);
    if (probe.outcome != ProbeOutcome.match || probe.identity == null) {
      return MigrationOutcome.deferred;
    }
    final identity = probe.identity!;
    await upsert(
      ServerEntry(
        deviceId: identity.deviceId,
        name: identity.deviceName,
        urls: [probe.url, if (identity.remoteUrl != null) identity.remoteUrl!],
        lastGoodUrl: probe.url,
        remoteUrl: identity.remoteUrl,
      ),
      makeActive: _activeId == null,
    );
    if (legacyAccess != null && legacyAccess.isNotEmpty && !await hasSession(identity.deviceId)) {
      await saveTokens(identity.deviceId, accessToken: legacyAccess, refreshToken: legacyRefresh);
    }
    await _deleteLegacyTokens();
    for (final k in legacyUrlKeys) {
      await prefs.remove(k);
    }
    return MigrationOutcome.migrated;
  }

  Future<void> _deleteLegacyTokens() async {
    try {
      await _secure.delete(key: 'access_token');
      await _secure.delete(key: 'refresh_token');
    } catch (_) {}
  }
}

/// Expiry (`exp`) of a JWT, or null when it cannot be read.
DateTime? jwtExpiry(String token) {
  final parts = token.split('.');
  if (parts.length != 3) return null;
  try {
    final payload = jsonDecode(utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))));
    final exp = payload is Map ? payload['exp'] : null;
    if (exp is num) return DateTime.fromMillisecondsSinceEpoch((exp * 1000).toInt(), isUtc: true);
  } catch (_) {}
  return null;
}

String generateUuidV4() {
  final rnd = Random.secure();
  final b = List<int>.generate(16, (_) => rnd.nextInt(256));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  final h = b.map((x) => x.toRadixString(16).padLeft(2, '0')).join();
  return '${h.substring(0, 8)}-${h.substring(8, 12)}-${h.substring(12, 16)}-${h.substring(16, 20)}-${h.substring(20)}';
}
