import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';

import 'pairing_service.dart';

/// Push is optional: this build carries no `google-services.json` /
/// `GoogleService-Info.plist`, so Firebase initialisation fails and the app
/// runs on the websocket live channel only (while it is open).
enum PushStatus { unknown, notConfigured, unsupported, permissionDenied, ready }

const String kPushNotConfiguredMessage = 'Push not configured on this build';

@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // Messages with a `notification` block are shown by the OS; tapping one
  // is handled by onMessageOpenedApp / getInitialMessage.
  try {
    await Firebase.initializeApp();
  } catch (_) {}
  debugPrint('Background push ${message.messageId} for device ${message.data['device_id']}');
}

class PushService extends ChangeNotifier {
  static final PushService _instance = PushService._();
  factory PushService() => _instance;
  PushService._();

  PushStatus _status = PushStatus.unknown;
  String? _token;
  String? _detail;
  StreamSubscription<String>? _refreshSub;

  PushStatus get status => _status;
  String? get token => _token;
  String? get detail => _detail;

  String get statusLabel {
    switch (_status) {
      case PushStatus.ready:
        return _token != null ? 'Push enabled on this phone' : 'Push enabled, waiting for a token';
      case PushStatus.permissionDenied:
        return 'Notifications are turned off for this app in system settings';
      case PushStatus.unsupported:
        return 'Push is only available on Android and iOS';
      case PushStatus.notConfigured:
        return kPushNotConfiguredMessage;
      case PushStatus.unknown:
        return 'Push not started';
    }
  }

  /// Initialises Firebase if this build has a config, requests permission,
  /// fetches the FCM token and keeps every paired server up to date with it.
  Future<void> init({
    required void Function(RemoteMessage) onForegroundMessage,
    required void Function(RemoteMessage) onMessageOpened,
  }) async {
    if (kIsWeb || !(Platform.isAndroid || Platform.isIOS)) {
      _set(PushStatus.unsupported);
      return;
    }
    try {
      await Firebase.initializeApp();
    } catch (e) {
      _set(PushStatus.notConfigured,
          detail: 'Add android/app/google-services.json and ios/Runner/GoogleService-Info.plist to enable push.');
      debugPrint('Firebase not configured: $e');
      return;
    }
    try {
      FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
      final messaging = FirebaseMessaging.instance;
      final settings = await messaging.requestPermission(alert: true, badge: true, sound: true);
      if (settings.authorizationStatus != AuthorizationStatus.authorized &&
          settings.authorizationStatus != AuthorizationStatus.provisional) {
        _set(PushStatus.permissionDenied);
      } else {
        _token = await messaging.getToken();
        _set(PushStatus.ready);
        if (_token != null) unawaited(PairingService().syncPushToken(_token!));
        _refreshSub ??= messaging.onTokenRefresh.listen((t) {
          _token = t;
          notifyListeners();
          unawaited(PairingService().syncPushToken(t));
        });
      }
      FirebaseMessaging.onMessage.listen(onForegroundMessage);
      FirebaseMessaging.onMessageOpenedApp.listen(onMessageOpened);
      final initial = await messaging.getInitialMessage();
      if (initial != null) onMessageOpened(initial);
    } catch (e) {
      _set(PushStatus.notConfigured, detail: 'Firebase messaging failed to start: $e');
    }
  }

  @visibleForTesting
  void debugSetStatus(PushStatus s, {String? token, String? detail}) {
    _token = token;
    _set(s, detail: detail);
  }

  void _set(PushStatus s, {String? detail}) {
    _status = s;
    _detail = detail;
    notifyListeners();
  }
}
