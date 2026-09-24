import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import 'settings_screen.dart';
import '../models/camera_feed.dart';
import '../models/security_event.dart';
import '../services/api_service.dart';
import 'live_view_screen.dart';
import 'loss_prevention_screen.dart';
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
  Map<String, dynamic>? _hardware;
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
      Map<String, dynamic>? hardware;
      try {
        hardware = await _apiService.getHardwareProfile();
      } catch (_) {
        hardware = null;
      }
      setState(() {
        _hardware = hardware;
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

  /// Server addresses are managed per store (keyed by device id) in
  /// Settings; there is no free-form URL editor here.
  void _showSettingsDialog() {
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const SettingsScreen()));
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
                color: isOffline ? context.palette.alert : context.palette.live,
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
            icon: Icon(Icons.policy_outlined, color: context.palette.alert),
            tooltip: 'Loss prevention alerts',
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (context) => const LossPreventionScreen()),
            ),
          ),
        ],
      ),
      body: _isLoading
          ? Center(child: CircularProgressIndicator(color: context.palette.accent))
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
                        Text(
                          'LIVE CAMERA FEEDS',
                          style: TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.bold,
                            color: context.palette.dim(0.70),
                            letterSpacing: 1.1,
                          ),
                        ),
                        const SizedBox(height: 12),
                        _buildCameraGrid(),
                        const SizedBox(height: 28),
                        Text(
                          'RECENT AI SECURITY EVENTS',
                          style: TextStyle(
                            fontSize: 14,
                            fontWeight: FontWeight.bold,
                            color: context.palette.dim(0.70),
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
            Icon(Icons.cloud_off_rounded, color: context.palette.alert, size: 64),
            const SizedBox(height: 16),
            const Text(
              'Cannot Reach Edge AI Server',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 8),
            Text(
              _errorMessage ?? 'Network connection failed.',
              textAlign: TextAlign.center,
              style: TextStyle(color: context.palette.dim(0.60), fontSize: 13),
            ),
            const SizedBox(height: 8),
            Text('Target: ${_apiService.baseUrl}', style: TextStyle(fontSize: 12, color: context.palette.accent)),
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
        color: context.palette.alert.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: context.palette.alert.withValues(alpha: 0.5)),
      ),
      child: Row(
        children: [
          Icon(Icons.warning_amber_rounded, color: context.palette.alert),
          const SizedBox(width: 8),
          Expanded(child: Text('Offline: $_errorMessage', style: TextStyle(fontSize: 12, color: context.palette.text))),
          TextButton(onPressed: _loadData, child: const Text('Retry')),
        ],
      ),
    );
  }

  Widget _buildSystemHealthBanner(bool isOffline) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      decoration: BoxDecoration(
        color: context.palette.card,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: isOffline ? context.palette.alert : context.palette.border),
      ),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Edge server: ${_hardware?['device_name'] ?? _apiService.baseUrl}', style: const TextStyle(fontWeight: FontWeight.bold)),
                const SizedBox(height: 4),
                Text(
                  isOffline
                      ? 'Server Unreachable: ${_apiService.baseUrl}'
                      : _hardware == null
                          ? 'Hardware profile unavailable'
                          : _hardware!['inference_available'] == true
                              ? 'Inference: ${_hardware!['inference_backend']} (${_hardware!['inference_provider']}) • Decode: ${_hardware!['decoder_capability']}'
                              : 'Inference unavailable: ${_hardware!['inference_error'] ?? 'no backend'}',
                  style: TextStyle(fontSize: 12, color: context.palette.dim(0.54)),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: BoxDecoration(
              color: isOffline ? context.palette.alert.withValues(alpha: 0.15) : context.palette.live.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(20),
            ),
            child: Text(
              isOffline ? 'DISCONNECTED' : '100% EDGE',
              style: TextStyle(
                color: isOffline ? context.palette.alert : context.palette.live,
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
              color: context.palette.card,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: context.palette.border),
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
                        style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13),
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
                              color: camera.isOnline ? AppTheme.liveGreen : AppTheme.alertRed,
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
        child: Text('No recent anomalies detected.', style: TextStyle(color: context.palette.dim(0.38))),
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
                color: event.isHigh ? context.palette.alert.withValues(alpha: 0.2) : context.palette.warning.withValues(alpha: 0.2),
                shape: BoxShape.circle,
              ),
              child: Icon(
                event.isHigh ? Icons.report_rounded : Icons.warning,
                color: event.isHigh ? context.palette.alert : context.palette.warning,
                size: 20,
              ),
            ),
            title: Text(
              event.eventType.replaceAll('_', ' '),
              style: TextStyle(
                fontWeight: FontWeight.bold,
                fontSize: 14,
                color: event.isHigh ? context.palette.alert : context.palette.text,
              ),
            ),
            subtitle: Text('${event.location} • $timeStr', style: TextStyle(fontSize: 12, color: context.palette.dim(0.60))),
            trailing: Icon(Icons.play_circle_fill, color: context.palette.accent, size: 28),
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
