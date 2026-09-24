import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:http/http.dart' as http;

import '../screens/app_shell.dart';
import '../screens/loss_prevention_alert_screen.dart';
import '../screens/loss_prevention_screen.dart';
import 'push_router.dart';
import 'push_service.dart';
import 'server_registry.dart';

export 'push_router.dart' show kCategoryLossPrevention;

/// Android channel for loss-prevention alerts. Must equal the server's
/// `ANDROID_CHANNEL_ID`; it is also the FCM default channel in
/// AndroidManifest.xml.
const String kLossPreventionChannelId = 'loss_prevention_alerts';
const String kLossPreventionChannelName = 'Loss prevention';
const String kActionViewAlert = 'ACTION_VIEW_ALERT';
const String kActionAcknowledge = 'ACTION_ACKNOWLEDGE';

Map<String, dynamic> _decodePayload(String? payload) {
  if (payload == null) return {};
  try {
    final decoded = jsonDecode(payload);
    return decoded is Map<String, dynamic> ? decoded : {};
  } catch (_) {
    return {};
  }
}

/// Acknowledges an incident on the server that raised it (not necessarily
/// the one currently selected in the app).
Future<void> _acknowledgeOn(ServerRegistry registry, PushRoute route) async {
  final id = route.incidentId;
  if (id == null) return;
  final url = registry.urlFor(route.deviceId);
  if (url == null) return;
  final headers = await registry.authHeadersFor(route.deviceId);
  await http
      .post(Uri.parse('$url/api/v1/theft/incidents/${Uri.encodeComponent(id)}/acknowledge'), headers: headers)
      .timeout(const Duration(seconds: 10));
}

/// Runs in a background isolate when the operator taps "Acknowledge" on the
/// notification without opening the app.
@pragma('vm:entry-point')
void notificationTapBackground(NotificationResponse response) async {
  WidgetsFlutterBinding.ensureInitialized();
  if (response.actionId != kActionAcknowledge) return;
  try {
    final registry = ServerRegistry();
    await registry.load();
    final route = routeForPush(_decodePayload(response.payload), registry.isKnown);
    if (route != null) await _acknowledgeOn(registry, route);
  } catch (e) {
    debugPrint('Background acknowledge failed: $e');
  }
}

/// Local notifications and alert routing for every paired store.
///
/// Alerts come from FCM (when this build has a Firebase config) and, while
/// the app is open, from the server's websocket live channel. Both carry the
/// sending server's `device_id`; alerts from servers this phone does not know
/// are dropped, and a tap opens the alert on the server that raised it.
class NotificationService {
  static final NotificationService _instance = NotificationService._internal();
  factory NotificationService() => _instance;
  NotificationService._internal();

  final FlutterLocalNotificationsPlugin _localNotifications = FlutterLocalNotificationsPlugin();
  final Map<String, DateTime> _recent = {};
  GlobalKey<NavigatorState>? navigatorKey;
  bool _localReady = false;

  Future<void> initialize(GlobalKey<NavigatorState> navKey) async {
    navigatorKey = navKey;

    final darwinCategories = <DarwinNotificationCategory>[
      DarwinNotificationCategory(
        kCategoryLossPrevention,
        actions: <DarwinNotificationAction>[
          DarwinNotificationAction.plain(
            kActionViewAlert,
            'View alert',
            options: <DarwinNotificationActionOption>{DarwinNotificationActionOption.foreground},
          ),
          DarwinNotificationAction.plain(kActionAcknowledge, 'Acknowledge'),
        ],
      ),
    ];

    final initSettings = InitializationSettings(
      android: const AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(
        requestAlertPermission: true,
        requestBadgePermission: true,
        requestSoundPermission: true,
        notificationCategories: darwinCategories,
      ),
    );

    try {
      await _localNotifications.initialize(
        initSettings,
        onDidReceiveNotificationResponse: _handleNotificationResponse,
        onDidReceiveBackgroundNotificationResponse: notificationTapBackground,
      );
      _localReady = true;
    } catch (e) {
      debugPrint('Local notifications unavailable: $e');
    }

    if (_localReady && !kIsWeb && Platform.isAndroid) {
      try {
        final android =
            _localNotifications.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();
        await android?.createNotificationChannel(const AndroidNotificationChannel(
          kLossPreventionChannelId,
          kLossPreventionChannelName,
          description: 'Suspicious behaviour flagged for staff review',
          importance: Importance.high,
        ));
        await android?.requestNotificationsPermission();
      } catch (e) {
        debugPrint('Android notification channel notice: $e');
      }
    }

    await PushService().init(
      onForegroundMessage: _onForegroundPush,
      onMessageOpened: (RemoteMessage m) => openAlert(m.data),
    );
  }

  // FCM does not display notifications while the app is in the foreground,
  // so show a local one instead of taking over the screen.
  void _onForegroundPush(RemoteMessage message) {
    showAlert(
      title: message.notification?.title ?? message.data['title']?.toString() ?? 'Store alert',
      body: message.notification?.body ?? message.data['body']?.toString() ?? '',
      data: message.data,
    );
  }

  /// Returns true when [data] is new (not shown in the last few minutes),
  /// so an alert arriving by both FCM and the websocket is shown once.
  @visibleForTesting
  bool markIfNew(Map<String, dynamic> data) {
    final now = DateTime.now();
    _recent.removeWhere((_, t) => now.difference(t) > const Duration(minutes: 5));
    final ident = data['alert_id'] ?? data['incident_id'] ?? data['id'] ?? '${data['event_type']}:${data['camera_id']}';
    final key = '${data['device_id']}|$ident';
    if (_recent.containsKey(key)) return false;
    _recent[key] = now;
    return true;
  }

  /// Shows a high-priority local notification for an alert from a paired
  /// server. Alerts for unknown servers are ignored.
  Future<void> showAlert({required String title, required String body, required Map<String, dynamic> data}) async {
    final registry = ServerRegistry();
    final route = routeForPush(data, registry.isKnown);
    if (route == null) {
      debugPrint('Ignoring alert for unknown server ${data['device_id']}');
      return;
    }
    if (!markIfNew(data) || !_localReady) return;
    final entry = registry.byId(route.deviceId);
    final storeName = entry?.name ?? data['device_name']?.toString();
    final shownTitle = registry.servers.length > 1 && storeName != null ? '$storeName: $title' : title;
    final payload = jsonEncode({
      'device_id': route.deviceId,
      if (route.incidentId != null) 'incident_id': route.incidentId,
      if (route.eventType != null) 'event_type': route.eventType,
      if (data['type'] != null) 'type': data['type'],
    });
    final canAcknowledge = route.destination == PushDestination.incident;

    final androidDetails = AndroidNotificationDetails(
      kLossPreventionChannelId,
      kLossPreventionChannelName,
      channelDescription: 'Suspicious behaviour flagged for staff review',
      importance: Importance.high,
      priority: Priority.high,
      category: AndroidNotificationCategory.message,
      visibility: NotificationVisibility.private,
      autoCancel: true,
      actions: <AndroidNotificationAction>[
        const AndroidNotificationAction(kActionViewAlert, 'View alert', showsUserInterface: true),
        if (canAcknowledge) const AndroidNotificationAction(kActionAcknowledge, 'Acknowledge', cancelNotification: true),
      ],
    );

    const darwinDetails = DarwinNotificationDetails(
      categoryIdentifier: kCategoryLossPrevention,
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
      interruptionLevel: InterruptionLevel.timeSensitive,
    );

    try {
      await _localNotifications.show(
        '${route.deviceId}${route.incidentId ?? data['alert_id'] ?? '$title$body'}'.hashCode,
        shownTitle,
        body,
        NotificationDetails(android: androidDetails, iOS: darwinDetails),
        payload: payload,
      );
    } catch (e) {
      debugPrint('Local notification failed: $e');
    }
  }

  void _handleNotificationResponse(NotificationResponse response) async {
    final data = _decodePayload(response.payload);
    if (response.actionId == kActionAcknowledge) {
      final route = routeForPush(data, ServerRegistry().isKnown);
      if (route == null) return;
      try {
        await _acknowledgeOn(ServerRegistry(), route);
      } catch (e) {
        debugPrint('Acknowledge from notification failed: $e');
      }
      return;
    }
    openAlert(data);
  }

  /// Switches to the server that raised the alert and opens its screen:
  /// the incident, the incident list, or the event centre.
  Future<void> openAlert(Map<String, dynamic> data) async {
    final registry = ServerRegistry();
    final route = routeForPush(data, registry.isKnown);
    if (route == null) return;
    await registry.setActive(route.deviceId);
    unawaited(registry.resolve(route.deviceId));
    final nav = navigatorKey?.currentState;
    if (nav == null) return;
    switch (route.destination) {
      case PushDestination.incident:
        nav.push(MaterialPageRoute(builder: (_) => LossPreventionAlertScreen(incidentId: route.incidentId!)));
        break;
      case PushDestination.incidentList:
        nav.push(MaterialPageRoute(builder: (_) => const LossPreventionScreen()));
        break;
      case PushDestination.events:
        nav.pushAndRemoveUntil(
          MaterialPageRoute(builder: (_) => const AppShell(initialSection: NavSection.eventsCenter)),
          (_) => false,
        );
        break;
    }
  }
}
