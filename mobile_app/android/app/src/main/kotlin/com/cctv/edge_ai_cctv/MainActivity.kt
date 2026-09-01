package com.cctv.edge_ai_cctv

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity: FlutterFragmentActivity() {
    private val CHANNEL = "com.cctv.edge_ai_cctv/emergency_channel"
    private var fallbackPlayer: MediaPlayer? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    // 60-second safety auto-sleep runnable to prevent device battery drain
    private val autoSleepRunnable = Runnable {
        clearWakeLockAndScreenFlags()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        createEmergencyNotificationChannel()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).setMethodCallHandler { call, result ->
            when (call.method) {
                "wakeScreenForEmergency" -> {
                    wakeAndUnlockScreen()
                    result.success(true)
                }
                "clearEmergencyWakeLock" -> {
                    clearWakeLockAndScreenFlags()
                    result.success(true)
                }
                "createEmergencyChannel" -> {
                    createEmergencyNotificationChannel()
                    result.success(true)
                }
                "playFallbackAlarmSound" -> {
                    playFallbackAlarm()
                    result.success(true)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun createEmergencyNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val emergencyChannelId = "cctv_emergency_channel"
            val emergencyChannelName = "CCTV Critical Emergency Alarms"
            val importance = NotificationManager.IMPORTANCE_HIGH

            val audioAttributes = AudioAttributes.Builder()
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .setUsage(AudioAttributes.USAGE_ALARM)
                .build()

            val defaultAlarmSound: Uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)

            val channel = NotificationChannel(emergencyChannelId, emergencyChannelName, importance).apply {
                description = "Critical alerts for Fall Detection, Intrusion, and Weapon Anomaly"
                setSound(defaultAlarmSound, audioAttributes)
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 500, 200, 500, 200, 1000)
                setBypassDnd(true)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
                setShowBadge(true)
            }

            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun wakeAndUnlockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        // Schedule auto-sleep after 60 seconds
        mainHandler.removeCallbacks(autoSleepRunnable)
        mainHandler.postDelayed(autoSleepRunnable, 60_000)
    }

    private fun clearWakeLockAndScreenFlags() {
        mainHandler.removeCallbacks(autoSleepRunnable)
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(false)
            setTurnScreenOn(false)
        }
        fallbackPlayer?.stop()
        fallbackPlayer?.release()
        fallbackPlayer = null
    }

    private fun playFallbackAlarm() {
        try {
            val alertUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            fallbackPlayer?.release()
            fallbackPlayer = MediaPlayer.create(this, alertUri)?.apply {
                isLooping = true
                start()
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    override fun onDestroy() {
        clearWakeLockAndScreenFlags()
        super.onDestroy()
    }
}
