import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/security_event.dart';
import '../services/api_service.dart';
import '../services/notification_service.dart';
import 'live_view_screen.dart';
import 'clip_player_screen.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({Key? key}) : super(key: key);

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  final ApiService _apiService = ApiService();
  List<CameraFeed> _cameras = [];
  List<SecurityEvent> _recentEvents = [];
  bool _isLoading = true;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _initAndLoad();
  }

  Future<void> _initAndLoad() async {
    await _apiService.init();
    await _loadData();
  }

  Future<void> _loadData() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });
    try {
      final cameras = await _apiService.getCameras();
      final events = await _apiService.getEvents();
      setState(() {
        _cameras = cameras;
        _recentEvents = events;
        _errorMessage = null;
        _isLoading = false;
      });
    } catch (e) {
      setState(() {
        _errorMessage = e.toString().replaceAll('Exception:', '').trim();
        _isLoading = false;
      });
    }
  }

  void _showSettingsDialog() {
    final controller = TextEditingController(text: _apiService.baseUrl);
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: AppTheme.cardSurface,
        title: const Text('Edge Server Configuration', style: TextStyle(fontWeight: FontWeight.bold)),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Enter Edge Mini PC LAN IP, Tailscale IP, or VPN domain:',
              style: TextStyle(fontSize: 12, color: Colors.white70),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: controller,
              decoration: const InputDecoration(
                hintText: 'http://192.168.1.100:8000',
                labelText: 'Server Base URL',
                border: OutlineInputBorder(),
                prefixIcon: Icon(Icons.dns),
              ),
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () async {
              await _apiService.setBaseUrl(controller.text);
              Navigator.pop(context);
              _loadData();
            },
            child: const Text('Save & Reconnect'),
          ),
        ],
      ),
    );
  }

  void _triggerTestEmergencyAlert() async {
    try {
      final event = await _apiService.triggerSimulatedEvent(
        cameraId: _cameras.isNotEmpty ? _cameras.first.id : 'cam_living_room',
        eventType: 'FALL_DETECTED',
        severity: 'CRITICAL',
      );
      await NotificationService().triggerEmergencyTakeover(event);
      _loadData();
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Trigger failed: $e'), backgroundColor: AppTheme.emergencyRed),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final bool isOffline = _errorMessage != null;

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            Container(
              width: 10,
              height: 10,
              decoration: BoxDecoration(
                color: isOffline ? AppTheme.emergencyRed : AppTheme.liveGreen,
                shape: BoxShape.circle,
              ),
            ),
            const SizedBox(width: 8),
            const Text(
              'EDGE CCTV AI',
              style: TextStyle(fontWeight: FontWeight.bold, letterSpacing: 1.2),
            ),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'Server Settings',
            onPressed: _showSettingsDialog,
          ),
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _loadData,
          ),
          IconButton(
            icon: const Icon(Icons.warning_amber_rounded, color: AppTheme.emergencyRed),
            tooltip: 'Simulate Fall Alert (DND Bypass Test)',
            onPressed: _triggerTestEmergencyAlert,
          ),
        ],
      ),
      body: _isLoading
          ? const Center(child: CircularProgressIndicator(color: AppTheme.cyberBlue))
          : isOffline && _cameras.isEmpty
              ? _buildOfflineErrorView()
              : RefreshIndicator(
                  onRefresh: _loadData,
                  child: SingleChildScrollView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.all(16.0),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        if (isOffline) _buildInlineOfflineWarning(),
                        _buildSystemHealthBanner(isOffline),
                        const SizedBox(height: 24),
                        const Text(
                          'LIVE CAMERA FEEDS',
                          style: TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.bold,
                            color: Colors.white70,
                            letterSpacing: 1.1,
                          ),
                        ),
                        const SizedBox(height: 12),
                        _buildCameraGrid(),
                        const SizedBox(height: 28),
                        const Text(
                          'RECENT AI SECURITY EVENTS',
                          style: TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.bold,
                            color: Colors.white70,
                            letterSpacing: 1.1,
                          ),
                        ),
                        const SizedBox(height: 12),
                        _buildEventsList(),
                      ],
                    ),
                  ),
                ),
    );
  }

  Widget _buildOfflineErrorView() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24.0),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.cloud_off_rounded, color: AppTheme.emergencyRed, size: 64),
            const SizedBox(height: 16),
            const Text(
              'Cannot Reach Edge AI Server',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            Text(
              _errorMessage ?? 'Network connection failed.',
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.white60, fontSize: 13),
            ),
            const SizedBox(height: 8),
            Text('Target: ${_apiService.baseUrl}', style: const TextStyle(fontSize: 12, color: AppTheme.cyberBlue)),
            const SizedBox(height: 24),
            Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                OutlinedButton.icon(
                  onPressed: _showSettingsDialog,
                  icon: const Icon(Icons.settings),
                  label: const Text('Configure IP'),
                ),
                const SizedBox(width: 12),
                ElevatedButton.icon(
                  onPressed: _loadData,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Retry Connection'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildInlineOfflineWarning() {
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppTheme.emergencyRed.withOpacity(0.15),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: AppTheme.emergencyRed.withOpacity(0.5)),
      ),
      child: Row(
        children: [
          const Icon(Icons.warning_amber_rounded, color: AppTheme.emergencyRed),
          const SizedBox(width: 8),
          Expanded(child: Text('Offline: $_errorMessage', style: const TextStyle(fontSize: 12, color: Colors.white))),
          TextButton(onPressed: _loadData, child: const Text('Retry')),
        ],
      ),
    );
  }

  Widget _buildSystemHealthBanner(bool isOffline) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      decoration: BoxDecoration(
        color: AppTheme.cardSurface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: isOffline ? AppTheme.emergencyRed : AppTheme.borderHighlight),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Edge Hardware: Intel N100 + Hailo-8 M.2', style: TextStyle(fontWeight: FontWeight.bold)),
              const SizedBox(height: 4),
              Text(
                isOffline ? 'Server Unreachable: ${_apiService.baseUrl}' : 'Inference: 7.2ms (HailoRT) • Decode: VA-API',
                style: const TextStyle(fontSize: 12, color: Colors.white54),
              ),
            ],
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: BoxDecoration(
              color: isOffline ? AppTheme.emergencyRed.withOpacity(0.15) : AppTheme.liveGreen.withOpacity(0.15),
              borderRadius: BorderRadius.circular(20),
            ),
            child: Text(
              isOffline ? 'DISCONNECTED' : '100% EDGE',
              style: TextStyle(
                color: isOffline ? AppTheme.emergencyRed : AppTheme.liveGreen,
                fontSize: 11,
                fontWeight: FontWeight.bold,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildCameraGrid() {
    return GridView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: 2,
        crossAxisSpacing: 12,
        mainAxisSpacing: 12,
        childAspectRatio: 1.1,
      ),
      itemCount: _cameras.length,
      itemBuilder: (context, index) {
        final camera = _cameras[index];
        return GestureDetector(
          onTap: () {
            Navigator.push(
              context,
              MaterialPageRoute(builder: (context) => LiveViewScreen(camera: camera)),
            );
          },
          child: Container(
            decoration: BoxDecoration(
              color: AppTheme.cardSurface,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: AppTheme.borderHighlight),
            ),
            clipBehavior: Clip.antiAlias,
            child: Stack(
              fit: StackFit.expand,
              children: [
                Image.network(
                  '${_apiService.baseUrl}/api/v1/cameras/${camera.id}/snapshot',
                  fit: BoxFit.cover,
                  errorBuilder: (context, error, stackTrace) => Container(
                    color: Colors.black54,
                    child: const Center(child: Icon(Icons.videocam_off, color: Colors.white30)),
                  ),
                ),
                Container(
                  decoration: const BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.topCenter,
                      end: Alignment.bottomCenter,
                      colors: [Colors.transparent, Colors.black87],
                    ),
                  ),
                ),
                Positioned(
                  bottom: 8,
                  left: 8,
                  right: 8,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        camera.name,
                        style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      const SizedBox(height: 2),
                      Row(
                        children: [
                          Container(
                            width: 6,
                            height: 6,
                            decoration: BoxDecoration(
                              color: camera.isOnline ? AppTheme.liveGreen : AppTheme.emergencyRed,
                              shape: BoxShape.circle,
                            ),
                          ),
                          const SizedBox(width: 4),
                          Text(
                            camera.location,
                            style: const TextStyle(fontSize: 10, color: Colors.white70),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildEventsList() {
    if (_recentEvents.isEmpty) {
      return Container(
        padding: const EdgeInsets.all(24),
        alignment: Alignment.center,
        child: const Text('No recent anomalies detected.', style: TextStyle(color: Colors.white38)),
      );
    }

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: _recentEvents.length,
      itemBuilder: (context, index) {
        final event = _recentEvents[index];
        final timeStr = DateFormat('hh:mm:ss a').format(event.timestamp);

        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: Container(
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: event.isCritical ? AppTheme.emergencyRed.withOpacity(0.2) : AppTheme.warningOrange.withOpacity(0.2),
                shape: BoxShape.circle,
              ),
              child: Icon(
                event.isCritical ? Icons.emergency : Icons.warning,
                color: event.isCritical ? AppTheme.emergencyRed : AppTheme.warningOrange,
                size: 20,
              ),
            ),
            title: Text(
              event.eventType.replaceAll('_', ' '),
              style: TextStyle(
                fontWeight: FontWeight.bold,
                fontSize: 14,
                color: event.isCritical ? AppTheme.emergencyRed : Colors.white,
              ),
            ),
            subtitle: Text('${event.location} • $timeStr', style: const TextStyle(fontSize: 12, color: Colors.white60)),
            trailing: const Icon(Icons.play_circle_fill, color: AppTheme.cyberBlue, size: 28),
            onTap: () {
              Navigator.push(
                context,
                MaterialPageRoute(builder: (context) => ClipPlayerScreen(event: event)),
              );
            },
          ),
        );
      },
    );
  }
}
