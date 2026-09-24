import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/feature_config.dart';
import '../services/api_service.dart';

/// Per-camera retail analytics toggles (`PUT /api/v1/cameras/{id}/features`).
class CameraSettingsScreen extends StatefulWidget {
  final CameraFeed camera;
  final VoidCallback onUpdated;

  const CameraSettingsScreen({Key? key, required this.camera, required this.onUpdated}) : super(key: key);

  @override
  State<CameraSettingsScreen> createState() => _CameraSettingsScreenState();
}

class _CameraSettingsScreenState extends State<CameraSettingsScreen> {
  final ApiService _apiService = ApiService();
  late FeatureConfig _config;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _config = FeatureConfig.fromJson(widget.camera.features.toJson());
  }

  /// Applies [change] locally, saves, and rolls back if the server rejects it.
  Future<void> _update(void Function(FeatureConfig c) change, void Function(FeatureConfig c) undo) async {
    setState(() {
      change(_config);
      _saving = true;
    });
    try {
      final saved = await _apiService.updateCameraFeatures(widget.camera.id, _config);
      if (!mounted) return;
      setState(() => _config = saved);
      widget.onUpdated();
    } catch (e) {
      if (!mounted) return;
      setState(() => undo(_config));
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Setting not saved: $e'), backgroundColor: context.palette.alert),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('${widget.camera.name} analytics')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Store analytics', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: context.palette.accent)),
          const SizedBox(height: 8),
          Text(
            'Turn off analytics a camera does not need to save edge server compute.',
            style: TextStyle(color: context.palette.dim(0.70), fontSize: 13),
          ),
          if (_saving) const Padding(padding: EdgeInsets.only(top: 12), child: LinearProgressIndicator(minHeight: 2)),
          const SizedBox(height: 20),
          _buildSwitchTile(
            'Loss prevention',
            'Pose-based detection of suspicious behaviour (for example concealment) for staff review.',
            _config.theftDetection,
            (val) => _update((c) => c.theftDetection = val, (c) => c.theftDetection = !val),
          ),
          _buildSwitchTile(
            'Shelf interaction',
            'Counts reach-to-shelf interactions per product zone for the engagement funnel.',
            _config.shelfInteraction,
            (val) => _update((c) => c.shelfInteraction = val, (c) => c.shelfInteraction = !val),
          ),
          _buildSwitchTile(
            'People counting',
            'Footfall, occupancy and dwell time used for heatmaps and traffic reports.',
            _config.peopleCounting,
            (val) => _update((c) => c.peopleCounting = val, (c) => c.peopleCounting = !val),
          ),
        ],
      ),
    );
  }

  Widget _buildSwitchTile(String title, String subtitle, bool value, ValueChanged<bool> onChanged) {
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: SwitchListTile(
        title: Text(title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
        subtitle: Text(subtitle, style: TextStyle(fontSize: 12, color: context.palette.dim(0.60))),
        value: value,
        activeThumbColor: context.palette.live,
        onChanged: _saving ? null : onChanged,
      ),
    );
  }
}
