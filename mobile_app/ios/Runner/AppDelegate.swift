import UIKit
import Flutter
import UserNotifications
import AVFoundation

@UIApplicationMain
@objc class AppDelegate: FlutterAppDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)

    UNUserNotificationCenter.current().delegate = self
    requestCriticalAlertAuthorization()

    let controller : FlutterViewController = window?.rootViewController as! FlutterViewController
    let criticalChannel = FlutterMethodChannel(name: "com.cctv.edge_ai_cctv/ios_critical_alert",
                                              binaryMessenger: controller.binaryMessenger)

    criticalChannel.setMethodCallHandler({ [weak self] (call: FlutterMethodCall, result: @escaping FlutterResult) -> Void in
      guard let self = self else { return }
      switch call.method {
      case "requestCriticalPermissions":
        self.requestCriticalAlertAuthorization()
        result(true)
      case "configureAudioSession":
        self.configureAudioSessionForCriticalAlerts()
        result(true)
      case "playFallbackAlarmSound":
        self.playSystemAlarmFallback()
        result(true)
      default:
        result(FlutterMethodNotImplemented)
      }
    })

    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  private func configureAudioSessionForCriticalAlerts() {
    do {
      let session = AVAudioSession.sharedInstance()
      try session.setCategory(.playback, mode: .default, options: [.duckOthers, .defaultToSpeaker])
      try session.setActive(true)
    } catch {
      print("Failed to configure AVAudioSession: \(error.localizedDescription)")
    }
  }

  private func playSystemAlarmFallback() {
    configureAudioSessionForCriticalAlerts()
    AudioServicesPlayAlertSound(SystemSoundID(1005)) // Standard High-Priority Alert Sound
  }

  private func requestCriticalAlertAuthorization() {
    let center = UNUserNotificationCenter.current()
    center.requestAuthorization(options: [.alert, .sound, .badge, .criticalAlert]) { granted, error in
      if let error = error {
        print("Error requesting Critical Alert permissions: \(error.localizedDescription)")
      } else {
        print("Critical Alert authorization granted: \(granted)")
      }
    }
    UIApplication.shared.registerForRemoteNotifications()
  }

  override func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    if #available(iOS 14.0, *) {
      completionHandler([.banner, .sound, .badge, .list])
    } else {
      completionHandler([.alert, .sound, .badge])
    }
  }
}
