import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/security_event.dart';
import '../models/camera_feed.dart';
import '../services/biometric_auth_service.dart';
import '../services/api_service.dart';
import 'live_view_screen.dart';

class EmergencyAlertScreen extends StatefulWidget {
  final SecurityEvent event;
  final VoidCallback onDismiss;

  const EmergencyAlertScreen({
    Key? key,
    required this.event,
    required this.onDismiss,
  }) : super(key: key);

  @override
  State<EmergencyAlertScreen> createState() => _EmergencyAlertScreenState();
}

class _EmergencyAlertScreenState extends State<EmergencyAlertScreen> with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 700),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  void _handleBiometricDismiss() async {
    final bioService = BiometricAuthService();
    final bool authenticated = await bioService.authenticate(
      reason: 'Authenticate with Face ID / Fingerprint to dismiss critical emergency alarm',
      sensitiveTransaction: true,
    );

    if (authenticated) {
      widget.onDismiss();
      try {
        await ApiService().acknowledgeEvent(widget.event.id);
      } catch (_) {}
      if (mounted) {
        Navigator.pop(context);
      }
    } else {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Biometric verification failed. Alarm remains active.'),
            backgroundColor: AppTheme.emergencyRed,
          ),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final timeStr = DateFormat('hh:mm:ss a').format(widget.event.timestamp);

    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: AnimatedBuilder(
          animation: _pulseController,
          builder: (context, child) {
            return Container(
              decoration: BoxDecoration(
                border: Border.all(
                  color: AppTheme.emergencyRed.withOpacity(0.4 + (_pulseController.value * 0.6)),
                  width: 4 * _pulseController.value,
                ),
              ),
              child: child,
            );
          },
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24.0, vertical: 16.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.center,
              children: [
                const SizedBox(height: 20),
                Container(
                  padding: const EdgeInsets.all(20),
                  decoration: BoxDecoration(
                    color: AppTheme.emergencyRed.withOpacity(0.2),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.warning_rounded,
                    color: AppTheme.emergencyRed,
                    size: 64,
                  ),
                ),
                const SizedBox(height: 16),
                Text(
                  'CRITICAL ALARM DETECTED',
                  style: TextStyle(
                    color: AppTheme.emergencyRed,
                    fontSize: 20,
                    fontWeight: FontWeight.bold,
                    letterSpacing: 1.5,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  widget.event.eventType.replaceAll('_', ' '),
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 24,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 24),
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: AppTheme.cardSurface,
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: AppTheme.borderHighlight),
                  ),
                  child: Column(
                    children: [
                      _buildDetailRow('Camera', widget.event.cameraName),
                      const Divider(color: AppTheme.borderHighlight),
                      _buildDetailRow('Location', widget.event.location),
                      const Divider(color: AppTheme.borderHighlight),
                      _buildDetailRow('Timestamp', timeStr),
                      const Divider(color: AppTheme.borderHighlight),
                      _buildDetailRow('AI Confidence', '${(widget.event.confidence * 100).toStringAsFixed(1)}%'),
                    ],
                  ),
                ),
                const Spacer(),
                ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: AppTheme.cyberBlue,
                    foregroundColor: Colors.black,
                    minimumSize: const Size.fromHeight(54),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  icon: const Icon(Icons.videocam, size: 24),
                  label: const Text(
                    'VIEW LIVE FEED (<300ms WebRTC)',
                    style: TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                  ),
                  onPressed: () {
                    widget.onDismiss();
                    Navigator.pushReplacement(
                      context,
                      MaterialPageRoute(
                        builder: (context) => LiveViewScreen(
                          camera: CameraFeed(
                            id: widget.event.cameraId,
                            name: widget.event.cameraName,
                            location: widget.event.location,
                            rtspUrl: '',
                            webrtcUrl: '',
                            status: 'ONLINE',
                            fps: 30,
                            resolution: '1920x1080',
                            isAiEnabled: true,
                            aiModels: [],
                          ),
                        ),
                      ),
                    );
                  },
                ),
                const SizedBox(height: 12),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Colors.white70,
                    minimumSize: const Size.fromHeight(50),
                    side: const BorderSide(color: Colors.white24),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  icon: const Icon(Icons.fingerprint, size: 20),
                  label: const Text('DISMISS ALARM (BIOMETRIC VERIFY)'),
                  onPressed: _handleBiometricDismiss,
                ),
                const SizedBox(height: 16),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildDetailRow(String label, String value) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(label, style: const TextStyle(color: Colors.white60, fontSize: 13)),
        Text(value, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13)),
      ],
    );
  }
}
