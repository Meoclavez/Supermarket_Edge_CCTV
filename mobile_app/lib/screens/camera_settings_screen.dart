import 'package:flutter/material.dart';
import '../models/camera_feed.dart';
import '../models/feature_config.dart';
import '../services/api_service.dart';

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

  @override
  void initState() {
    super.initState();
    _config = widget.camera.features;
  }

  Future<void> _saveFeature(String key, bool val) async {
    try {
      await _apiService.updateCameraFeatures(widget.camera.id, _config);
      widget.onUpdated();
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text("Failed to update feature")));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text("${widget.camera.name} Features"),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text("Granular AI Feature Gating", style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold, color: Color(0xFF38BDF8))),
          const SizedBox(height: 8),
          const Text("Turn off unneeded features to save Mini PC CPU/GPU power and memory bandwidth.", style: TextStyle(color: Colors.white70, fontSize: 13)),
          const SizedBox(height: 20),
          _buildSwitchTile(
            "Elderly Fall Detection",
            "17-Keypoint YOLO-Pose skeletal tracking (Heavy compute). Enable in living rooms and bedrooms.",
            _config.fallDetection,
            (val) {
              setState(() => _config.fallDetection = val);
              _saveFeature("fall_detection", val);
            },
          ),
          _buildSwitchTile(
            "Door Ajar & Entry Monitoring",
            "Monitors door ROI for open state or nighttime breaches.",
            _config.doorMonitoring,
            (val) {
              setState(() => _config.doorMonitoring = val);
              _saveFeature("door_monitoring", val);
            },
          ),
          _buildSwitchTile(
            "Package & Property Theft",
            "Stationary porch parcel tracking & removal alert.",
            _config.packageTheftTracking,
            (val) {
              setState(() => _config.packageTheftTracking = val);
              _saveFeature("package_theft_tracking", val);
            },
          ),
          _buildSwitchTile(
            "Danger Zone Inactivity",
            "Alerts if person is stationary in high-risk zones (e.g. bathroom).",
            _config.inactivityAlerts,
            (val) {
              setState(() => _config.inactivityAlerts = val);
              _saveFeature("inactivity_alerts", val);
            },
          ),
          _buildSwitchTile(
            "Continuous 24/7 Disk Recording",
            "Records full resolution stream directly to internal SSD.",
            _config.continuousRecording,
            (val) {
              setState(() => _config.continuousRecording = val);
              _saveFeature("continuous_recording", val);
            },
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
        subtitle: Text(subtitle, style: const TextStyle(fontSize: 12, color: Colors.white60)),
        value: value,
        activeColor: const Color(0xFF10B981),
        onChanged: onChanged,
      ),
    );
  }
}
