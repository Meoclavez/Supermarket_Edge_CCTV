import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../services/api_service.dart';
import 'live_view_screen.dart';

class MultiCamGridScreen extends StatefulWidget {
  const MultiCamGridScreen({Key? key}) : super(key: key);

  @override
  State<MultiCamGridScreen> createState() => _MultiCamGridScreenState();
}

class _MultiCamGridScreenState extends State<MultiCamGridScreen> {
  final ApiService _apiService = ApiService();
  List<CameraFeed> _cameras = [];
  bool _isLoading = true;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _loadCameras();
  }

  Future<void> _loadCameras() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });
    try {
      final list = await _apiService.getCameras();
      setState(() {
        _cameras = list;
        _isLoading = false;
      });
    } catch (e) {
      setState(() {
        _errorMessage = e.toString();
        _isLoading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('LIVE MULTI-CAM WALL (<300ms WebRTC)', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded, color: AppTheme.cyberBlue),
            onPressed: _loadCameras,
          ),
        ],
      ),
      body: _isLoading
          ? const Center(child: CircularProgressIndicator(color: AppTheme.cyberBlue))
          : _errorMessage != null
              ? Center(
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      const Icon(Icons.wifi_off_rounded, color: AppTheme.warningOrange, size: 48),
                      const SizedBox(height: 12),
                      Text(_errorMessage!, style: const TextStyle(color: Colors.white70, fontSize: 13), textAlign: TextAlign.center),
                      const SizedBox(height: 16),
                      ElevatedButton(onPressed: _loadCameras, child: const Text('Retry Connection')),
                    ],
                  ),
                )
              : LayoutBuilder(
                  builder: (context, constraints) {
                    int crossAxisCount = 1;
                    int count = _cameras.length;
                    if (count >= 5) {
                      crossAxisCount = constraints.maxWidth > 800 ? 4 : 2;
                    } else if (count >= 2) {
                      crossAxisCount = 2;
                    } else {
                      crossAxisCount = 1;
                    }

                    return GridView.builder(
                      padding: const EdgeInsets.all(16),
                      gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                        crossAxisCount: crossAxisCount,
                        crossAxisSpacing: 16,
                        mainAxisSpacing: 16,
                        childAspectRatio: 16 / 10,
                      ),
                      itemCount: _cameras.length,
                      itemBuilder: (context, index) {
                        final cam = _cameras[index];
                        return _buildCameraTile(cam);
                      },
                    );
                  },
                ),
    );
  }

  Widget _buildCameraTile(CameraFeed camera) {
    final bool isOffline = !camera.isOnline;

    return InkWell(
      onTap: () {
        Navigator.push(
          context,
          MaterialPageRoute(builder: (context) => LiveViewScreen(camera: camera)),
        );
      },
      borderRadius: BorderRadius.circular(12),
      child: Container(
        decoration: BoxDecoration(
          color: AppTheme.cardSurface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: isOffline ? AppTheme.warningOrange.withOpacity(0.5) : AppTheme.borderHighlight,
          ),
        ),
        clipBehavior: Clip.antiAlias,
        child: Stack(
          children: [
            Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    isOffline ? Icons.videocam_off_outlined : Icons.videocam_outlined,
                    color: isOffline ? AppTheme.warningOrange.withOpacity(0.6) : Colors.white24,
                    size: 44,
                  ),
                  const SizedBox(height: 8),
                  if (isOffline) ...[
                    Text(
                      camera.errorMessage ?? 'Camera Connection Lost',
                      style: const TextStyle(color: AppTheme.warningOrange, fontSize: 11, fontWeight: FontWeight.bold),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 6),
                    ElevatedButton.icon(
                      onPressed: () async {
                        ScaffoldMessenger.of(context).showSnackBar(
                          SnackBar(content: Text('Scanning subnets for ${camera.name}...')),
                        );
                        try {
                          final res = await _apiService.triggerAutoRecover(camera.id);
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(
                              content: Text(res['message'] ?? 'Scan complete'),
                              backgroundColor: res['status'] == 'success' ? AppTheme.liveGreen : AppTheme.warningOrange,
                            ),
                          );
                          _loadCameras();
                        } catch (e) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            SnackBar(content: Text('Recovery failed: $e'), backgroundColor: AppTheme.emergencyRed),
                          );
                        }
                      },
                      icon: const Icon(Icons.autorenew_rounded, size: 14),
                      label: const Text('Auto-Recover IP', style: TextStyle(fontSize: 10)),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: AppTheme.cyberBlue,
                        foregroundColor: Colors.black,
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                        minimumSize: Size.zero,
                        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      ),
                    ),
                  ] else ...[
                    Text(
                      'Tap for <300ms WebRTC Stream',
                      style: TextStyle(color: Colors.white.withOpacity(0.4), fontSize: 11),
                    ),
                  ],
                ],
              ),
            ),
            Positioned(
              top: 10,
              left: 10,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                decoration: BoxDecoration(
                  color: Colors.black.withOpacity(0.7),
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 8,
                      height: 8,
                      decoration: BoxDecoration(
                        color: isOffline ? AppTheme.warningOrange : AppTheme.liveGreen,
                        shape: BoxShape.circle,
                      ),
                    ),
                    const SizedBox(width: 6),
                    Text(camera.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 11)),
                  ],
                ),
              ),
            ),
            Positioned(
              top: 10,
              right: 10,
              child: Row(
                children: [
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
                    decoration: BoxDecoration(
                      color: Colors.black.withOpacity(0.7),
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: Text('30 FPS', style: const TextStyle(color: Colors.greenAccent, fontSize: 9, fontWeight: FontWeight.bold)),
                  ),
                  const SizedBox(width: 4),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
                    decoration: BoxDecoration(
                      color: Colors.red.withOpacity(0.8),
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: const Row(
                      children: [
                        Icon(Icons.warning_amber_rounded, size: 10, color: Colors.white),
                        SizedBox(width: 2),
                        Text('1', style: TextStyle(color: Colors.white, fontSize: 9, fontWeight: FontWeight.bold)),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            Positioned(
              bottom: 10,
              left: 10,
              right: 10,
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Text(camera.location, style: const TextStyle(color: Colors.white60, fontSize: 10)),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: isOffline ? AppTheme.warningOrange.withOpacity(0.15) : AppTheme.cyberBlue.withOpacity(0.15),
                      borderRadius: BorderRadius.circular(4),
                      border: Border.all(
                        color: isOffline ? AppTheme.warningOrange.withOpacity(0.4) : AppTheme.cyberBlue.withOpacity(0.4),
                      ),
                    ),
                    child: Text(
                      isOffline ? 'OFFLINE' : 'HAILO AI ACTIVE',
                      style: TextStyle(
                        color: isOffline ? AppTheme.warningOrange : AppTheme.cyberBlue,
                        fontSize: 9,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
