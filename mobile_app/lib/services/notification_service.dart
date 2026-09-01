import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:audioplayers/audioplayers.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:url_launcher/url_launcher.dart';
import '../models/camera_feed.dart';
import '../models/security_event.dart';
import '../screens/emergency_alert_screen.dart';
import '../screens/live_view_screen.dart';
import 'api_service.dart';

const String kActionViewLive = 'ACTION_VIEW_LIVE';
const String kActionMute5m = 'ACTION_MUTE_5M';
const String kActionCallContact = 'ACTION_CALL_CONTACT';
const String kCategoryEmergency = 'CATEGORY_EMERGENCY_ALARM';

/// Top-level background action handler executed when user taps an action button on lockscreen
@pragma('vm:entry-point')
void notificationTapBackground(NotificationResponse notificationResponse) async {
  WidgetsFlutterBinding.ensureInitialized();
  debugPrint('Notification background action received: ${notificationResponse.actionId}');

  final String? payloadStr = notificationResponse.payload;
  Map<String, dynamic> payload = {};
  if (payloadStr != null) {
    try {
      payload = jsonDecode(payloadStr);
    } catch (_) {}
  }

  final String cameraId = payload['camera_id'] ?? '';

  switch (notificationResponse.actionId) {
    case kActionMute5m:
      try {
        final apiService = ApiService();
        await apiService.init();
        await apiService.muteCameraAlerts(cameraId, durationMinutes: 5);
      } catch (e) {
        debugPrint('Failed to mute alerts in background: $e');
      }
      break;

    case kActionCallContact:
      final Uri phoneUri = Uri.parse('tel:${payload['emergency_phone'] ?? '911'}');
      if (await canLaunchUrl(phoneUri)) {
        await launchUrl(phoneUri);
      }
      break;
  }
}

class NotificationService {
  static final NotificationService _instance = NotificationService._internal();
  factory NotificationService() => _instance;
  NotificationService._internal();

  final FlutterLocalNotificationsPlugin _localNotifications = FlutterLocalNotificationsPlugin();
  final MethodChannel _androidChannel = const MethodChannel('com.cctv.edge_ai_cctv/emergency_channel');
  final MethodChannel _iosChannel = const MethodChannel('com.cctv.edge_ai_cctv/ios_critical_alert');
  final AudioPlayer _alarmPlayer = AudioPlayer();
  GlobalKey<NavigatorState>? navigatorKey;
  Timer? _vibrationTimer;

  Future<void> initialize(GlobalKey<NavigatorState> navKey) async {
    navigatorKey = navKey;

    // 1. Configure AudioSession for Critical Alarm Playback (Ducking + Loudspeaker)
    try {
      await AudioPlayer.global.setAudioContext(AudioContext(
        iOS: AudioContextIOS(
          category: AVAudioSessionCategory.playback,
          options: [
            AVAudioSessionOptions.duckOthers,
            AVAudioSessionOptions.defaultToSpeaker,
          ],
        ),
        android: AudioContextAndroid(
          isSpeakerphoneOn: true,
          stayAwake: true,
          contentType: AndroidContentType.sonification,
          usageType: AndroidUsageType.alarm,
          audioFocus: AndroidAudioFocus.gainTransientMayDuck,
        ),
      ));
    } catch (e) {
      debugPrint('AudioContext config notice: $e');
    }

    // 2. Configure Local Notification Categories & Actions
    const AndroidInitializationSettings androidSettings = AndroidInitializationSettings('@mipmap/ic_launcher');

    final List<DarwinNotificationCategory> darwinCategories = [
      DarwinNotificationCategory(
        kCategoryEmergency,
        actions: <DarwinNotificationAction>[
          DarwinNotificationAction.plain(
            kActionViewLive,
            '👁️ View Live',
            options: <DarwinNotificationActionOption>{
              DarwinNotificationActionOption.foreground,
            },
          ),
          DarwinNotificationAction.plain(
            kActionMute5m,
            '🔕 Mute 5m',
            options: <DarwinNotificationActionOption>{
              DarwinNotificationActionOption.destructive,
            },
          ),
          DarwinNotificationAction.plain(
            kActionCallContact,
            '📞 Call Contact',
            options: <DarwinNotificationActionOption>{
              DarwinNotificationActionOption.foreground,
            },
          ),
        ],
        options: <DarwinNotificationCategoryOption>{
          DarwinNotificationCategoryOption.customDismissAction,
          DarwinNotificationCategoryOption.allowInCarPlay,
        },
      ),
    ];

    final DarwinInitializationSettings darwinSettings = DarwinInitializationSettings(
      requestAlertPermission: true,
      requestBadgePermission: true,
      requestSoundPermission: true,
      requestCriticalPermission: true,
      notificationCategories: darwinCategories,
    );

    final InitializationSettings initSettings = InitializationSettings(
      android: androidSettings,
      iOS: darwinSettings,
    );

    await _localNotifications.initialize(
      initSettings,
      onDidReceiveNotificationResponse: _handleForegroundNotificationResponse,
      onDidReceiveBackgroundNotificationResponse: notificationTapBackground,
    );

    // 3. Initialize Platform Notification Channels
    if (!kIsWeb) {
      if (Platform.isAndroid) {
        try {
          await _androidChannel.invokeMethod('createEmergencyChannel');
        } catch (e) {
          debugPrint('Android emergency channel notice: $e');
        }
      } else if (Platform.isIOS) {
        try {
          await _iosChannel.invokeMethod('requestCriticalPermissions');
        } catch (e) {
          debugPrint('iOS Critical Alert notice: $e');
        }
      }
    }

    // 4. Register Firebase Messaging & Listeners
    try {
      final messaging = FirebaseMessaging.instance;
      NotificationSettings settings = await messaging.requestPermission(
        alert: true,
        announcement: true,
        badge: true,
        criticalAlert: true,
        sound: true,
      );

      if (settings.authorizationStatus == AuthorizationStatus.authorized ||
          settings.authorizationStatus == AuthorizationStatus.provisional) {
        final token = await messaging.getToken();
        if (token != null) {
          debugPrint('FCM Device Token: $token');
          await ApiService().registerDevice(token, kIsWeb ? 'web' : (Platform.isIOS ? 'ios' : 'android'));
        }
        
        messaging.onTokenRefresh.listen((token) async {
          debugPrint('FCM Token Refreshed: $token');
          await ApiService().registerDevice(token, kIsWeb ? 'web' : (Platform.isIOS ? 'ios' : 'android'));
        });
      } else {
        debugPrint('Notification permissions denied.');
        // Wait a frame for navigation context to be ready
        Future.delayed(const Duration(seconds: 2), () {
          _showPermissionDeniedDialog();
        });
      }

      FirebaseMessaging.onMessage.listen((RemoteMessage message) {
        debugPrint('FCM Message Received: ${message.messageId}');
        if (message.data['is_emergency'] == 'true' || message.data['type'] == 'CRITICAL_ALERT') {
          _navigateToEventFromPayload(message.data);
        }
      });

      FirebaseMessaging.onMessageOpenedApp.listen((RemoteMessage message) {
        debugPrint('FCM Message Tapped: ${message.messageId}');
        _navigateToEventFromPayload(message.data);
      });

      final initialMessage = await messaging.getInitialMessage();
      if (initialMessage != null) {
        debugPrint('FCM Initial Message Tapped: ${initialMessage.messageId}');
        _navigateToEventFromPayload(initialMessage.data);
      }
    } catch (e) {
      debugPrint('Firebase messaging listener notice: $e');
    }
  }
  
  void _showPermissionDeniedDialog() {
    if (navigatorKey?.currentState?.overlay?.context != null) {
      final context = navigatorKey!.currentState!.overlay!.context;
      showDialog(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Notifications Disabled'),
          content: const Text('Critical security alerts may not reach you in time. Please enable notifications in your device settings.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context), child: const Text('Dismiss')),
          ],
        ),
      );
    }
  }

  void _navigateToEventFromPayload(Map<String, dynamic> data) {
    final event = SecurityEvent(
      id: data['event_id'] ?? DateTime.now().millisecondsSinceEpoch.toString(),
      cameraId: data['camera_id'] ?? 'cam_unknown',
      cameraName: data['camera_name'] ?? 'Camera',
      location: data['location'] ?? 'Monitored Zone',
      eventType: data['event_type'] ?? 'EMERGENCY_DETECTED',
      severity: 'CRITICAL',
      confidence: 0.95,
      timestamp: DateTime.tryParse(data['timestamp'] ?? '') ?? DateTime.now(),
      clipUrl: data['clip_url'],
      snapshotUrl: data['snapshot_url'],
      acknowledged: false,
    );
    triggerEmergencyTakeover(event);
  }

  Future<void> showInteractiveAlert({
    required SecurityEvent event,
    String emergencyPhone = '911',
  }) async {
    final payload = jsonEncode({
      'event_id': event.id,
      'camera_id': event.cameraId,
      'camera_name': event.cameraName,
      'location': event.location,
      'event_type': event.eventType,
      'emergency_phone': emergencyPhone,
      'timestamp': event.timestamp.toIso8601String(),
    });

    final AndroidNotificationDetails androidDetails = AndroidNotificationDetails(
      'cctv_emergency_channel',
      'CCTV Critical Emergency Alarms',
      channelDescription: 'Lockscreen actionable emergency alerts',
      importance: Importance.max,
      priority: Priority.high,
      category: AndroidNotificationCategory.alarm,
      visibility: NotificationVisibility.public,
      fullScreenIntent: true,
      ongoing: true,
      autoCancel: false,
      actions: <AndroidNotificationAction>[
        const AndroidNotificationAction(
          kActionViewLive,
          'View Live',
          showsUserInterface: true,
          cancelNotification: false,
        ),
        const AndroidNotificationAction(
          kActionMute5m,
          'Mute 5m',
          showsUserInterface: false,
          cancelNotification: true,
        ),
        const AndroidNotificationAction(
          kActionCallContact,
          'Call Contact',
          showsUserInterface: true,
          cancelNotification: true,
        ),
      ],
    );

    final DarwinNotificationDetails darwinDetails = DarwinNotificationDetails(
      categoryIdentifier: kCategoryEmergency,
      presentAlert: true,
      presentBadge: true,
      presentSound: true,
      interruptionLevel: InterruptionLevel.critical,
    );

    final NotificationDetails platformDetails = NotificationDetails(
      android: androidDetails,
      iOS: darwinDetails,
    );

    await _localNotifications.show(
      event.id.hashCode,
      '🚨 CRITICAL: ${event.eventType.replaceAll('_', ' ')}',
      '${event.location} (${event.cameraName}) - Tap to act immediately',
      platformDetails,
      payload: payload,
    );
  }

  void _handleForegroundNotificationResponse(NotificationResponse response) async {
    final String? payloadStr = response.payload;
    if (payloadStr == null) return;
    final payload = jsonDecode(payloadStr);
    final String cameraId = payload['camera_id'] ?? '';
    final String cameraName = payload['camera_name'] ?? 'Camera';
    final String location = payload['location'] ?? 'Zone';

    switch (response.actionId) {
      case kActionViewLive:
        navigatorKey?.currentState?.push(
          MaterialPageRoute(
            builder: (context) => LiveViewScreen(
              camera: CameraFeed(
                id: cameraId,
                name: cameraName,
                location: location,
                rtspUrl: '',
                webrtcUrl: '',
                status: 'ONLINE',
                fps: 30,
                resolution: '1080p',
                isAiEnabled: true,
                aiModels: [],
              ),
            ),
          ),
        );
        break;

      case kActionMute5m:
        await stopAlarm();
        break;

      case kActionCallContact:
        final Uri phoneUri = Uri.parse('tel:${payload['emergency_phone'] ?? '911'}');
        if (await canLaunchUrl(phoneUri)) {
          await launchUrl(phoneUri);
        }
        break;

      default:
        final event = SecurityEvent.fromJson(payload);
        triggerEmergencyTakeover(event);
        break;
    }
  }

  Future<void> triggerEmergencyTakeover(SecurityEvent event) async {
    await showInteractiveAlert(event: event);

    if (!kIsWeb && Platform.isAndroid) {
      try {
        await _androidChannel.invokeMethod('wakeScreenForEmergency');
      } catch (e) {
        debugPrint('Failed to wake screen: $e');
      }
    }

    try {
      await _alarmPlayer.setReleaseMode(ReleaseMode.loop);
      await _alarmPlayer.play(AssetSource('sounds/emergency_siren.mp3'));
    } catch (e) {
      if (!kIsWeb) {
        if (Platform.isAndroid) {
          await _androidChannel.invokeMethod('playFallbackAlarmSound');
        } else if (Platform.isIOS) {
          await _iosChannel.invokeMethod('playFallbackAlarmSound');
        }
      }
    }

    _vibrationTimer?.cancel();
    _vibrationTimer = Timer.periodic(const Duration(milliseconds: 600), (_) {
      HapticFeedback.heavyImpact();
    });

    if (navigatorKey?.currentState != null) {
      navigatorKey!.currentState!.push(
        MaterialPageRoute(
          fullscreenDialog: true,
          builder: (context) => EmergencyAlertScreen(
            event: event,
            onDismiss: stopAlarm,
          ),
        ),
      );
    }
  }

  Future<void> stopAlarm() async {
    _vibrationTimer?.cancel();
    _vibrationTimer = null;
    try {
      await _alarmPlayer.stop();
    } catch (_) {}

    if (!kIsWeb && Platform.isAndroid) {
      try {
        await _androidChannel.invokeMethod('clearEmergencyWakeLock');
      } catch (_) {}
    }
  }
}
