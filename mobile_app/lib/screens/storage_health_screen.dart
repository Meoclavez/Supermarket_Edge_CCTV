import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';

class CameraQuotaInfo {
  final String name;
  final double usedGb;
  final double quotaGb;
  final int segments;
  final String oldest;

  CameraQuotaInfo({
    required this.name,
    required this.usedGb,
    required this.quotaGb,
    required this.segments,
    required this.oldest,
  });
}

class StorageHealthScreen extends StatefulWidget {
  const StorageHealthScreen({Key? key}) : super(key: key);

  @override
  State<StorageHealthScreen> createState() => _StorageHealthScreenState();
}

class _StorageHealthScreenState extends State<StorageHealthScreen> with SingleTickerProviderStateMixin {
  late AnimationController _animController;

  final double _totalDiskGb = 1000.0;
  final double _usedDiskGb = 428.4;
  final int _driveTempCelsius = 41;
  final int _wearLevelPercent = 98;
  final int _reallocatedSectors = 0;
  final int _powerOnHours = 1420;
  final String _smartHealth = 'PASSED';
  final String _driveModel = 'Samsung 980 NVMe 1TB (PCIe 3.0 x4)';

  final List<CameraQuotaInfo> _cameraQuotas = [
    CameraQuotaInfo(name: 'Camera 01 - Main Gate', usedGb: 142.5, quotaGb: 250.0, segments: 2420, oldest: '7 days ago'),
    CameraQuotaInfo(name: 'Camera 02 - Backyard Patio', usedGb: 118.0, quotaGb: 250.0, segments: 1980, oldest: '7 days ago'),
    CameraQuotaInfo(name: 'Camera 03 - Warehouse Bay', usedGb: 124.9, quotaGb: 250.0, segments: 2110, oldest: '7 days ago'),
    CameraQuotaInfo(name: 'Camera 04 - Front Porch', usedGb: 43.0, quotaGb: 250.0, segments: 720, oldest: '3 days ago'),
  ];

  @override
  void initState() {
    super.initState();
    _animController = AnimationController(vsync: this, duration: const Duration(milliseconds: 1200))..forward();
  }

  @override
  void dispose() {
    _animController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final double usedPct = (_usedDiskGb / _totalDiskGb) * 100;

    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('System Telemetry & Storage SMART Health', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            tooltip: 'Refresh Diagnostics',
            icon: const Icon(Icons.refresh_rounded, color: AppTheme.cyberBlue),
            onPressed: () {
              _animController.reset();
              _animController.forward();
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('NVMe SMART metrics refreshed.')),
              );
            },
          ),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            LayoutBuilder(
              builder: (context, constraints) {
                if (constraints.maxWidth > 800) {
                  return Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(flex: 4, child: _buildPrimaryStorageGaugeCard(usedPct)),
                      const SizedBox(width: 16),
                      Expanded(flex: 6, child: _buildDriveSmartCard()),
                    ],
                  );
                } else {
                  return Column(
                    children: [
                      _buildPrimaryStorageGaugeCard(usedPct),
                      const SizedBox(height: 16),
                      _buildDriveSmartCard(),
                    ],
                  );
                }
              },
            ),
            const SizedBox(height: 20),
            _buildCameraQuotasCard(),
            const SizedBox(height: 20),
            _buildHardwarePipelineCard(),
          ],
        ),
      ),
    );
  }

  Widget _buildPrimaryStorageGaugeCard(double usedPct) {
    return Card(
      color: AppTheme.cardSurface,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14), side: const BorderSide(color: AppTheme.borderHighlight)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          children: [
            const Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text('PRIMARY 24/7 DVR POOL', style: TextStyle(color: Colors.white70, fontWeight: FontWeight.bold, fontSize: 12, letterSpacing: 0.8)),
                Icon(Icons.storage_rounded, color: AppTheme.cyberBlue, size: 20),
              ],
            ),
            const SizedBox(height: 16),
            SizedBox(
              height: 160,
              width: 160,
              child: AnimatedBuilder(
                animation: _animController,
                builder: (context, _) {
                  return CustomPaint(
                    painter: StorageRingGaugePainter(
                      percent: (_usedDiskGb / _totalDiskGb) * _animController.value,
                      color: usedPct > 85 ? AppTheme.emergencyRed : AppTheme.cyberBlue,
                    ),
                    child: Center(
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text('${usedPct.toStringAsFixed(1)}%', style: const TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
                          const Text('USED', style: TextStyle(color: Colors.white54, fontSize: 10, letterSpacing: 1)),
                        ],
                      ),
                    ),
                  );
                },
              ),
            ),
            const SizedBox(height: 16),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceAround,
              children: [
                _buildStatColumn('Used Space', '${_usedDiskGb.toStringAsFixed(1)} GB', AppTheme.cyberBlue),
                _buildStatColumn('Free Space', '${(_totalDiskGb - _usedDiskGb).toStringAsFixed(1)} GB', AppTheme.liveGreen),
                _buildStatColumn('Capacity', '${_totalDiskGb.toStringAsFixed(0)} GB', Colors.white70),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildDriveSmartCard() {
    return Card(
      color: AppTheme.cardSurface,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14), side: const BorderSide(color: AppTheme.borderHighlight)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('NVME SSD SMART HEALTH', style: TextStyle(color: Colors.white70, fontWeight: FontWeight.bold, fontSize: 12, letterSpacing: 0.8)),
                    const SizedBox(height: 4),
                    Text(_driveModel, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600, fontSize: 13)),
                  ],
                ),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                  decoration: BoxDecoration(
                    color: AppTheme.liveGreen.withOpacity(0.15),
                    borderRadius: BorderRadius.circular(20),
                    border: Border.all(color: AppTheme.liveGreen.withOpacity(0.5)),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.check_circle_rounded, size: 12, color: AppTheme.liveGreen),
                      const SizedBox(width: 4),
                      Text(_smartHealth, style: const TextStyle(color: AppTheme.liveGreen, fontSize: 11, fontWeight: FontWeight.bold)),
                    ],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 20),
            GridView.count(
              crossAxisCount: 2,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              childAspectRatio: 2.8,
              crossAxisSpacing: 12,
              mainAxisSpacing: 12,
              children: [
                _buildSmartMetricTile('Drive Temperature', '$_driveTempCelsius °C', Icons.thermostat_rounded, _driveTempCelsius > 65 ? AppTheme.warningOrange : AppTheme.liveGreen),
                _buildSmartMetricTile('Remaining Health', '$_wearLevelPercent%', Icons.health_and_safety_rounded, AppTheme.cyberBlue),
                _buildSmartMetricTile('Power-On Hours', '$_powerOnHours hrs', Icons.timer_rounded, Colors.white70),
                _buildSmartMetricTile('Reallocated Sectors', '$_reallocatedSectors', Icons.disc_full_rounded, _reallocatedSectors > 0 ? AppTheme.emergencyRed : AppTheme.liveGreen),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildCameraQuotasCard() {
    return Card(
      color: AppTheme.cardSurface,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14), side: const BorderSide(color: AppTheme.borderHighlight)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('PER-CAMERA 24/7 DVR QUOTA & RETENTION ALLOCATION',
                style: TextStyle(color: Colors.white70, fontWeight: FontWeight.bold, fontSize: 12, letterSpacing: 0.8)),
            const SizedBox(height: 16),
            ListView.separated(
              itemCount: _cameraQuotas.length,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              separatorBuilder: (_, __) => const Divider(color: AppTheme.borderHighlight, height: 20),
              itemBuilder: (context, index) {
                final quota = _cameraQuotas[index];
                final double progress = quota.usedGb / quota.quotaGb;

                return Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Text(quota.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 13)),
                        Text('${quota.usedGb.toStringAsFixed(1)} GB / ${quota.quotaGb.toStringAsFixed(0)} GB (${(progress * 100).toStringAsFixed(0)}%)',
                            style: const TextStyle(color: AppTheme.cyberBlue, fontSize: 12, fontWeight: FontWeight.w600)),
                      ],
                    ),
                    const SizedBox(height: 8),
                    ClipRRect(
                      borderRadius: BorderRadius.circular(6),
                      child: LinearProgressIndicator(
                        value: progress,
                        minHeight: 8,
                        backgroundColor: AppTheme.darkBackground,
                        valueColor: AlwaysStoppedAnimation<Color>(progress > 0.85 ? AppTheme.emergencyRed : AppTheme.cyberBlue),
                      ),
                    ),
                    const SizedBox(height: 6),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Text('1-min segments: ${quota.segments}', style: const TextStyle(color: Colors.white54, fontSize: 11)),
                        Text('Oldest segment: ${quota.oldest}', style: const TextStyle(color: Colors.white54, fontSize: 11)),
                      ],
                    ),
                  ],
                );
              },
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildHardwarePipelineCard() {
    return Card(
      color: AppTheme.cardSurface,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14), side: const BorderSide(color: AppTheme.borderHighlight)),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('HARDWARE ACCELERATION ENGINES (INTEL N100 + HAILO-8 M.2)',
                style: TextStyle(color: Colors.white70, fontWeight: FontWeight.bold, fontSize: 12, letterSpacing: 0.8)),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: _buildEngineStatusTile(
                    'Intel QuickSync / VA-API',
                    '/dev/dri/renderD128',
                    'Zero-Copy H.264 Remux & Dec',
                    AppTheme.liveGreen,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: _buildEngineStatusTile(
                    'HailoRT NPU (Hailo-8)',
                    '/dev/hailo0 (PCIe)',
                    'YOLOv8 + 17-Keypoint Pose (8ms)',
                    AppTheme.cyberBlue,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildEngineStatusTile(String title, String device, String desc, Color color) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppTheme.darkBackground,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.borderHighlight),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(width: 8, height: 8, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
              const SizedBox(width: 6),
              Text(title, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 12)),
            ],
          ),
          const SizedBox(height: 4),
          Text(device, style: const TextStyle(color: Colors.white54, fontSize: 10, fontFamily: 'monospace')),
          const SizedBox(height: 4),
          Text(desc, style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.w500)),
        ],
      ),
    );
  }

  Widget _buildSmartMetricTile(String label, String value, IconData icon, Color valColor) {
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: AppTheme.darkBackground,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: AppTheme.borderHighlight),
      ),
      child: Row(
        children: [
          Icon(icon, color: valColor, size: 20),
          const SizedBox(width: 10),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Text(label, style: const TextStyle(color: Colors.white54, fontSize: 10)),
              Text(value, style: TextStyle(color: valColor, fontWeight: FontWeight.bold, fontSize: 13)),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildStatColumn(String label, String value, Color color) {
    return Column(
      children: [
        Text(value, style: TextStyle(color: color, fontWeight: FontWeight.bold, fontSize: 14)),
        const SizedBox(height: 2),
        Text(label, style: const TextStyle(color: Colors.white54, fontSize: 10)),
      ],
    );
  }
}

class StorageRingGaugePainter extends CustomPainter {
  final double percent;
  final Color color;

  StorageRingGaugePainter({required this.percent, required this.color});

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = (size.width - 24) / 2;

    final bgPaint = Paint()
      ..color = Colors.white12
      ..style = PaintingStyle.stroke
      ..strokeWidth = 14
      ..strokeCap = StrokeCap.round;
    canvas.drawCircle(center, radius, bgPaint);

    final sweepPaint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 14
      ..strokeCap = StrokeCap.round;

    final double sweepAngle = 2 * math.pi * percent;
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: radius),
      -math.pi / 2,
      sweepAngle,
      false,
      sweepPaint,
    );
  }

  @override
  bool shouldRepaint(covariant StorageRingGaugePainter oldDelegate) =>
      oldDelegate.percent != percent || oldDelegate.color != color;
}
