import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

import '../models/server_entry.dart';
import 'notification_service.dart';
import 'server_registry.dart';

/// The server's websocket alert feed (`/api/v1/events/ws`), used while the
/// app is open. It is the fallback when push is not configured on this
/// build, and it keeps alerts flowing if FCM is delayed. Alerts are shown
/// through [NotificationService.showAlert], which drops duplicates that also
/// arrive by push.
class LiveAlertChannel {
  static final LiveAlertChannel _instance = LiveAlertChannel._();
  factory LiveAlertChannel() => _instance;
  LiveAlertChannel._();

  WebSocket? _socket;
  String? _connectedKey;
  String? _targetKey;
  Timer? _retry;
  int _attempt = 0;
  bool _running = false;

  bool get isConnected => _socket != null;

  void start() {
    if (_running) return;
    _running = true;
    ServerRegistry().addListener(_onRegistryChanged);
    _connect();
  }

  /// Called when the app goes to the background.
  void stop() {
    _running = false;
    _targetKey = null;
    ServerRegistry().removeListener(_onRegistryChanged);
    _retry?.cancel();
    _close();
  }

  void _onRegistryChanged() {
    final key = _keyFor();
    if (key != _targetKey) {
      _retry?.cancel();
      _close();
      _attempt = 0;
      _connect();
    }
  }

  String? _keyFor() {
    final r = ServerRegistry();
    final id = r.activeId;
    final url = r.activeUrl;
    return id == null || url == null ? null : '$id@$url';
  }

  void _close() {
    final s = _socket;
    _socket = null;
    _connectedKey = null;
    if (s != null) unawaited(s.close().catchError((_) {}));
  }

  Future<void> _connect() async {
    if (!_running) return;
    final registry = ServerRegistry();
    final entry = registry.active;
    final url = registry.activeUrl;
    _targetKey = _keyFor();
    if (entry == null || url == null) return;
    final key = '${entry.deviceId}@$url';
    final token = await registry.accessToken(entry.deviceId);
    final base = Uri.parse(url);
    final wsUri = base.replace(
      scheme: base.scheme == 'https' ? 'wss' : 'ws',
      path: '${base.path}/api/v1/events/ws',
      queryParameters: token != null && token.isNotEmpty ? {'token': token} : null,
    );
    try {
      final socket = await WebSocket.connect(wsUri.toString()).timeout(const Duration(seconds: 8));
      if (!_running || _keyFor() != key) {
        await socket.close();
        return;
      }
      _socket = socket;
      _connectedKey = key;
      _attempt = 0;
      socket.pingInterval = const Duration(seconds: 25);
      socket.listen(
        (msg) => _onMessage(entry, msg),
        onDone: () => _scheduleReconnect(key),
        onError: (_) => _scheduleReconnect(key),
        cancelOnError: true,
      );
    } catch (e) {
      debugPrint('Live alert channel: ${e.runtimeType}');
      _scheduleReconnect(key);
    }
  }

  void _scheduleReconnect(String key) {
    if (_connectedKey == key) {
      _socket = null;
      _connectedKey = null;
    }
    if (!_running) return;
    _retry?.cancel();
    _attempt++;
    if (_attempt == 3) {
      // Repeated failures: the address may have changed (Wi-Fi to mobile data).
      unawaited(ServerRegistry().resolveActive());
    }
    final delay = Duration(seconds: min(60, pow(2, min(_attempt, 6)).toInt()));
    _retry = Timer(delay, _connect);
  }

  void _onMessage(ServerEntry entry, dynamic raw) {
    if (raw is! String) return;
    Map<String, dynamic> msg;
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return;
      msg = Map<String, dynamic>.from(decoded);
    } catch (_) {
      return;
    }
    final data = alertFromLiveMessage(msg, entry);
    if (data == null) return;
    final prefs = entry.alertPrefs ?? const AlertPrefs();
    if (!prefs.matches(
        eventType: data['event_type']?.toString(),
        severity: data['severity']?.toString(),
        cameraId: data['camera_id']?.toString())) {
      return;
    }
    NotificationService().showAlert(
      title: (data['title'] ?? _titleFor(data)).toString(),
      body: (data['body'] ?? data['description'] ?? '').toString(),
      data: data,
    );
  }

  static String _titleFor(Map<String, dynamic> data) {
    final type = data['event_type']?.toString() ?? 'ALERT';
    return kDispatchableEventTypes[type] ?? type.replaceAll('_', ' ').toLowerCase();
  }
}

/// Normalises one websocket message into an alert payload tagged with the
/// connected server's device id, or null when it is not an alert for that
/// server (a message that names another device id is dropped).
Map<String, dynamic>? alertFromLiveMessage(Map<String, dynamic> msg, ServerEntry connectedTo) {
  final inner = msg['data'];
  final m = (msg['event_type'] == null && inner is Map) ? Map<String, dynamic>.from(inner) : Map<String, dynamic>.from(msg);
  if (m['event_type'] == null) return null;
  final claimed = m['device_id'];
  if (claimed is String && claimed.isNotEmpty && normaliseDeviceId(claimed) != connectedTo.deviceId) return null;
  m['device_id'] = connectedTo.deviceId;
  m['device_name'] ??= connectedTo.name;
  m['alert_id'] ??= m['id'];
  return m;
}
