import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:developer' as developer;
import 'package:http/http.dart' as http;
import '../core/constants/api_constants.dart';
import '../models/camera_feed.dart';
import '../models/feature_config.dart';
import '../models/security_event.dart';
import '../models/theft_incident.dart';
import '../models/zone_model.dart';
import 'server_registry.dart';
import '../core/server_time.dart';

class ApiService {
  static final ApiService _instance = ApiService._internal();
  factory ApiService() => _instance;
  ApiService._internal();

  final Duration _normalTimeout = const Duration(seconds: 10);
  
  final List<Map<String, dynamic>> _offlineQueue = [];

  /// Base URL of the active server: the last address that proved (through
  /// `GET /api/v1/device/identity`) that it is that server. See
  /// [ServerRegistry] and [ConnectionResolver].
  String get baseUrl => ServerRegistry().activeUrl ?? ApiConstants.defaultBaseUrl;
  String get _baseUrl => baseUrl;

  /// Loads the paired servers (kept for callers that initialise the API
  /// client directly, such as background isolates).
  Future<void> init() async {
    if (!ServerRegistry().isLoaded) await ServerRegistry().load();
  }

  Future<http.Response> _sendRequestWithRetry(
    Future<http.Response> Function() requestFunc,
    String endpoint,
    {int maxRetries = 3}
  ) async {
    int attempts = 0;
    bool refreshed = false;
    while (attempts < maxRetries) {
      try {
        developer.log('API Request: $endpoint', name: 'ApiService');
        final response = await requestFunc();

        // Expired session: refresh once and repeat the request (the request
        // closures read the auth header when they run). A refresh that is
        // itself rejected sends the app back to pairing (onSessionLost).
        if (response.statusCode == 401 && !refreshed && ServerRegistry().activeId != null) {
          refreshed = true;
          final outcome = await ServerRegistry().refreshSession(ServerRegistry().activeId!);
          if (outcome == RefreshOutcome.refreshed) continue;
        }

        if (response.statusCode >= 200 && response.statusCode < 300) {
          return response;
        } else {
          developer.log('API Error: $endpoint | Status: ${response.statusCode} | Body: ${response.body}', name: 'ApiService', level: 900);
          throw HttpException('Server returned HTTP ${response.statusCode}');
        }
      } on SocketException catch (e) {
        developer.log('SocketException on $endpoint: $e', name: 'ApiService', level: 900);
        attempts++;
        if (attempts >= maxRetries) {
          unawaited(ServerRegistry().resolveActive());
          throw const SocketException('Cannot reach Edge Server.');
        }
      } on TimeoutException catch (e) {
        developer.log('TimeoutException on $endpoint: $e', name: 'ApiService', level: 900);
        attempts++;
        if (attempts >= maxRetries) {
          unawaited(ServerRegistry().resolveActive());
          throw TimeoutException('Request timed out');
        }
      } catch (e) {
        developer.log('Exception on $endpoint: $e', name: 'ApiService', level: 900);
        rethrow;
      }
      
      final backoff = Duration(seconds: pow(2, attempts).toInt());
      developer.log('Retrying $endpoint in ${backoff.inSeconds} seconds...', name: 'ApiService');
      await Future.delayed(backoff);
    }
    throw Exception('Failed after $maxRetries attempts');
  }

  void _processOfflineQueue() async {
    if (_offlineQueue.isEmpty) return;
    developer.log('Processing offline queue (${_offlineQueue.length} items)', name: 'ApiService');
    
    final queueCopy = List<Map<String, dynamic>>.from(_offlineQueue);
    _offlineQueue.clear();
    
    for (var item in queueCopy) {
      try {
        if (item['type'] == 'acknowledge_event') {
          await acknowledgeEvent(item['eventId']);
        }
      } catch (e) {
        // Re-queue if still failing
        _offlineQueue.add(item);
      }
    }
  }
  
  void notifyConnectionRestored() {
    _processOfflineQueue();
  }

  Future<List<CameraFeed>> getCameras() async {
    final endpoint = '$_baseUrl${ApiConstants.camerasEndpoint}';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    final Map<String, dynamic> data = jsonDecode(response.body);
    final List<dynamic> list = data['cameras'] ?? [];
    return list.map((c) => CameraFeed.fromJson(c)).toList();
  }

  Future<List<SecurityEvent>> getEvents({String? severity}) async {
    String endpoint = '$_baseUrl${ApiConstants.eventsEndpoint}';
    if (severity != null) {
      endpoint += '?severity=$severity';
    }
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    final Map<String, dynamic> data = jsonDecode(response.body);
    final List<dynamic> list = data['events'] ?? [];
    return list.map((e) => SecurityEvent.fromJson(e)).toList();
  }

  /// Holds loss-prevention phone pushes for one camera (alerts are still
  /// logged and still reach the dashboard). `durationMinutes: 0` resumes
  /// pushes. Returns the server's `muted_until` (null when resumed).
  Future<DateTime?> holdCameraPushes(String cameraId, {required int durationMinutes}) async {
    final endpoint = '$_baseUrl${ApiConstants.camerasEndpoint}/$cameraId/mute';
    final response = await _sendRequestWithRetry(
      () async => http
          .post(Uri.parse(endpoint), headers: {'Content-Type': 'application/json', ...await authHeaders()}, body: jsonEncode({'duration_minutes': durationMinutes}))
          .timeout(_normalTimeout),
      endpoint,
      maxRetries: 2,
    );
    final data = jsonDecode(response.body) as Map<String, dynamic>;
    final until = data['muted_until']?.toString();
    if (until == null || until.isEmpty) return null;
    return parseServerTime(until);
  }

  Future<Map<String, dynamic>> getCameraTimeline(String cameraId, String dateStr) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/timeline?date=$dateStr';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    return jsonDecode(response.body);
  }

  Future<Map<String, dynamic>> getStorageHealth() async {
    final endpoint = '$_baseUrl/api/v1/storage/health';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    return jsonDecode(response.body);
  }

  Future<void> acknowledgeEvent(String eventId) async {
    final endpoint = '$_baseUrl${ApiConstants.eventsEndpoint}/$eventId/acknowledge';
    try {
      await _sendRequestWithRetry(
        () async => http.post(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
        endpoint,
        maxRetries: 2
      );
    } catch (e) {
      developer.log('Acknowledge event failed, queueing offline: $e', name: 'ApiService');
      _offlineQueue.add({'type': 'acknowledge_event', 'eventId': eventId});
    }
  }

  Future<List<ZoneConfig>> fetchCameraZones(String cameraId) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/zones';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    final List<dynamic> list = jsonDecode(response.body);
    return list.map((z) => ZoneConfig.fromJson(z as Map<String, dynamic>)).toList();
  }

  Future<ZoneConfig> saveCameraZone(String cameraId, ZoneConfig zone) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/zones';
    final response = await _sendRequestWithRetry(
      () async => http.post(
        Uri.parse(endpoint),
        headers: {'Content-Type': 'application/json', ...await authHeaders()},
        body: jsonEncode(zone.toJson()),
      ).timeout(_normalTimeout),
      endpoint
    );
    return ZoneConfig.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  Future<void> deleteCameraZone(String cameraId, String zoneId) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/zones/$zoneId';
    await _sendRequestWithRetry(
      () async => http.delete(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
  }

  Future<Map<String, dynamic>> diagnoseCamera(String cameraId) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/diagnostics';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> triggerAutoRecover(String cameraId) async {
    final endpoint = '$_baseUrl/api/v1/cameras/$cameraId/auto-recover';
    final response = await _sendRequestWithRetry(
      () async => http.post(Uri.parse(endpoint), headers: await authHeaders()).timeout(const Duration(seconds: 20)),
      endpoint
    );
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<List<Map<String, dynamic>>> fetchNetworkInterfaces() async {
    final endpoint = '$_baseUrl/api/v1/cameras/network/interfaces';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint
    );
    final data = jsonDecode(response.body);
    return List<Map<String, dynamic>>.from(data['interfaces'] ?? []);
  }

  // ---------------------------------------------------------------------------
  // Auth and media helpers
  // ---------------------------------------------------------------------------

  /// Bearer header for the active server's session (tokens are stored per
  /// server device id), or an empty map when signed out.
  Future<Map<String, String>> authHeaders() => ServerRegistry().authHeadersFor(ServerRegistry().activeId);

  /// Turns a server-relative media path (e.g. `/api/v1/...`) into an absolute
  /// URL on the configured edge server. Returns null for null/empty input.
  String? resolveMediaUrl(String? pathOrUrl) {
    if (pathOrUrl == null || pathOrUrl.trim().isEmpty) return null;
    final v = pathOrUrl.trim();
    if (v.startsWith('http://') || v.startsWith('https://')) return v;
    return v.startsWith('/') ? '$_baseUrl$v' : '$_baseUrl/$v';
  }

  // ---------------------------------------------------------------------------
  // Camera analytics feature flags
  // ---------------------------------------------------------------------------

  Future<FeatureConfig> updateCameraFeatures(String cameraId, FeatureConfig config) async {
    final endpoint = '$_baseUrl${ApiConstants.camerasEndpoint}/$cameraId/features';
    final response = await _sendRequestWithRetry(
      () async => http.put(Uri.parse(endpoint), headers: {'Content-Type': 'application/json', ...await authHeaders()}, body: jsonEncode(config.toJson())).timeout(_normalTimeout),
      endpoint,
    );
    return FeatureConfig.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  // ---------------------------------------------------------------------------
  // Loss prevention (theft) incidents: /api/v1/theft/...
  // ---------------------------------------------------------------------------

  Future<List<TheftIncident>> getTheftIncidents({String? status, int limit = 50}) async {
    final uri = Uri.parse('$_baseUrl${ApiConstants.theftIncidentsEndpoint}').replace(queryParameters: {
      if (status != null) 'status': status,
      'limit': '$limit',
    });
    final response = await _sendRequestWithRetry(
      () async => http.get(uri, headers: await authHeaders()).timeout(_normalTimeout),
      uri.toString(),
    );
    final Map<String, dynamic> data = jsonDecode(response.body);
    final List<dynamic> list = data['incidents'] ?? [];
    return list.map((e) => TheftIncident.fromJson(e as Map<String, dynamic>)).toList();
  }

  /// `GET /api/v1/theft/incidents/{id}`. Returns null on 404.
  Future<TheftIncident?> findTheftIncident(String incidentId) async {
    final endpoint = '$_baseUrl${ApiConstants.theftIncidentsEndpoint}/${Uri.encodeComponent(incidentId)}';
    final id = ServerRegistry().activeId;
    final response = id == null
        ? await http.get(Uri.parse(endpoint)).timeout(_normalTimeout)
        : await ServerRegistry()
            .authorizedSend(id, (h) => http.get(Uri.parse(endpoint), headers: h).timeout(_normalTimeout));
    if (response.statusCode == 404) return null;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw HttpException('Server returned HTTP ${response.statusCode}');
    }
    return TheftIncident.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  Future<TheftIncident> acknowledgeTheftIncident(String incidentId) async {
    final endpoint = '$_baseUrl${ApiConstants.theftIncidentsEndpoint}/$incidentId/acknowledge';
    final response = await _sendRequestWithRetry(
      () async => http.post(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint,
      maxRetries: 2,
    );
    return TheftIncident.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  /// [resolution] is one of the values `TheftResolveRequest` documents:
  /// RECOVERED_GOODS, POLICE_DISPATCHED, SUSPECT_FLED, FALSE_ALARM.
  Future<TheftIncident> resolveTheftIncident(String incidentId, {required String resolution, String? notes}) async {
    final endpoint = '$_baseUrl${ApiConstants.theftIncidentsEndpoint}/$incidentId/resolve';
    final response = await _sendRequestWithRetry(
      () async => http
          .post(
            Uri.parse(endpoint),
            headers: {'Content-Type': 'application/json', ...await authHeaders()},
            body: jsonEncode({
              'resolution': resolution,
              if (notes != null && notes.trim().isNotEmpty) 'notes': notes.trim(),
            }),
          )
          .timeout(_normalTimeout),
      endpoint,
      maxRetries: 2,
    );
    return TheftIncident.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  /// Runtime-probed inference/decode capabilities (`HardwareProfile`).
  Future<Map<String, dynamic>> getHardwareProfile() async {
    final endpoint = '$_baseUrl/api/v1/system/hardware';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint,
      maxRetries: 1,
    );
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<List<Map<String, dynamic>>> getArchives() async {
    final endpoint = '$_baseUrl/api/v1/dvr/archives';
    final response = await _sendRequestWithRetry(
      () async => http.get(Uri.parse(endpoint), headers: await authHeaders()).timeout(_normalTimeout),
      endpoint,
    );
    final data = jsonDecode(response.body);
    return List<Map<String, dynamic>>.from(data['archives'] ?? []);
  }
}
