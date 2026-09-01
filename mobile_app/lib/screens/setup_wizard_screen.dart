import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

import '../core/theme/app_theme.dart';
import 'app_shell.dart';
import 'login_screen.dart';

class SetupWizardScreen extends StatefulWidget {
  const SetupWizardScreen({Key? key}) : super(key: key);

  @override
  State<SetupWizardScreen> createState() => _SetupWizardScreenState();
}

class _SetupWizardScreenState extends State<SetupWizardScreen> {
  int _currentStep = 0;
  final _serverUrlController = TextEditingController();
  final _usernameController = TextEditingController();
  final _displayNameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _confirmPasswordController = TextEditingController();
  
  bool _isLoading = false;
  String _serverStatus = '';
  Map<String, dynamic>? _hardwareReport;
  List<dynamic> _cameras = [];

  Future<void> _testServer() async {
    setState(() { _isLoading = true; _serverStatus = 'Connecting...'; });
    try {
      final res = await http.get(Uri.parse('${_serverUrlController.text}/api/v1/setup/status')).timeout(const Duration(seconds: 3));
      if (res.statusCode == 200) {
        setState(() => _serverStatus = 'Connected');
      } else {
        setState(() => _serverStatus = 'Server error');
      }
    } catch (e) {
      setState(() => _serverStatus = 'Unreachable');
    }
    setState(() => _isLoading = false);
  }

  Future<void> _createAdmin() async {
    if (_passwordController.text != _confirmPasswordController.text) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Passwords do not match')));
      return;
    }
    setState(() => _isLoading = true);
    try {
      final res = await http.post(
        Uri.parse('${_serverUrlController.text}/api/v1/setup/admin'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'username': _usernameController.text,
          'display_name': _displayNameController.text,
          'password': _passwordController.text,
        }),
      );
      if (res.statusCode == 200) {
        setState(() => _currentStep++);
        _scanHardware();
      }
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error: $e')));
    }
    setState(() => _isLoading = false);
  }

  Future<void> _scanHardware() async {
    setState(() => _isLoading = true);
    try {
      final res = await http.post(Uri.parse('${_serverUrlController.text}/api/v1/setup/hardware-scan'));
      if (res.statusCode == 200) {
        setState(() => _hardwareReport = jsonDecode(res.body));
      }
    } catch (e) {
      debugPrint('Hardware scan error: $e');
    }
    setState(() => _isLoading = false);
  }

  Future<void> _scanCameras() async {
    setState(() => _isLoading = true);
    try {
      final res = await http.post(Uri.parse('${_serverUrlController.text}/api/v1/setup/camera-scan'));
      if (res.statusCode == 200) {
        setState(() => _cameras = jsonDecode(res.body)['cameras'] ?? []);
      }
    } catch (e) {
      debugPrint('Camera scan error: $e');
    }
    setState(() => _isLoading = false);
  }

  Future<void> _saveDiscoveredCameras() async {
    if (_cameras.isEmpty) return;
    try {
      final payload = {
        'cameras': _cameras.map((c) => {
          'name': c['name'] ?? 'Discovered IP Camera',
          'location': c['location'] ?? 'Default Location',
          'rtsp_url': c['url'] ?? '',
        }).toList(),
      };
      await http.post(
        Uri.parse('${_serverUrlController.text}/api/v1/setup/add-cameras'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(payload),
      );
    } catch (e) {
      debugPrint('Error saving discovered cameras: $e');
    }
  }

  Future<void> _completeSetup() async {
    setState(() => _isLoading = true);
    try {
      final res = await http.post(Uri.parse('${_serverUrlController.text}/api/v1/setup/complete'));
      if (res.statusCode == 200) {
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('server_url', _serverUrlController.text);
        if (mounted) {
          Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const LoginScreen()));
        }
      }
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error finalizing setup: $e')));
    }
    setState(() => _isLoading = false);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(title: const Text('Initial Setup')),
      body: Stepper(
        currentStep: _currentStep,
        onStepCancel: () => setState(() => _currentStep > 0 ? _currentStep -= 1 : null),
        onStepContinue: () {
          if (_currentStep == 0) _currentStep++;
          else if (_currentStep == 1) _createAdmin();
          else if (_currentStep == 2) {
            _currentStep++;
            _scanCameras();
          }
          else if (_currentStep == 3) {
            _saveDiscoveredCameras();
            _currentStep++;
          }
          else if (_currentStep == 4) _currentStep++;
          else if (_currentStep == 5) _completeSetup();
        },
        controlsBuilder: (context, details) {
          return Padding(
            padding: const EdgeInsets.only(top: 16.0),
            child: Row(
              children: [
                if (_currentStep < 5)
                  ElevatedButton(
                    onPressed: details.onStepContinue,
                    child: _isLoading ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Text('Continue'),
                  )
                else
                  ElevatedButton(
                    onPressed: details.onStepContinue,
                    child: _isLoading ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Text('Launch System'),
                  ),
                const SizedBox(width: 8),
                if (_currentStep > 0)
                  TextButton(onPressed: details.onStepCancel, child: const Text('Back')),
              ],
            ),
          );
        },
        steps: [
          Step(
            title: const Text('Server Connection'),
            content: Column(
              children: [
                TextField(
                  controller: _serverUrlController,
                  decoration: const InputDecoration(labelText: 'Edge Server URL', hintText: 'http://192.168.x.x:443'),
                ),
                const SizedBox(height: 8),
                ElevatedButton(onPressed: _testServer, child: const Text('Test Connection')),
                Text('Status: $_serverStatus', style: TextStyle(color: _serverStatus == 'Connected' ? AppTheme.liveGreen : Colors.white)),
              ],
            ),
            isActive: _currentStep >= 0,
          ),
          Step(
            title: const Text('Create Admin Account'),
            content: Column(
              children: [
                TextField(controller: _usernameController, decoration: const InputDecoration(labelText: 'Username')),
                const SizedBox(height: 8),
                TextField(controller: _displayNameController, decoration: const InputDecoration(labelText: 'Display Name')),
                const SizedBox(height: 8),
                TextField(controller: _passwordController, obscureText: true, decoration: const InputDecoration(labelText: 'Password')),
                const SizedBox(height: 8),
                TextField(controller: _confirmPasswordController, obscureText: true, decoration: const InputDecoration(labelText: 'Confirm Password')),
              ],
            ),
            isActive: _currentStep >= 1,
          ),
          Step(
            title: const Text('Hardware Detection'),
            content: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (_hardwareReport != null) ...[
                  ListTile(
                    leading: Icon(_hardwareReport!['hailo_detected'] == true ? Icons.check_circle : Icons.warning, color: _hardwareReport!['hailo_detected'] == true ? AppTheme.liveGreen : AppTheme.warningOrange),
                    title: const Text('Hailo-8 AI Accelerator'),
                    subtitle: Text(_hardwareReport!['hailo_detected'] == true ? 'Detected (8 TOPS)' : 'Not detected - CPU Fallback'),
                  ),
                  ListTile(
                    leading: Icon(_hardwareReport!['quicksync_detected'] == true ? Icons.check_circle : Icons.warning, color: _hardwareReport!['quicksync_detected'] == true ? AppTheme.liveGreen : AppTheme.warningOrange),
                    title: const Text('Intel QuickSync VA-API'),
                    subtitle: Text(_hardwareReport!['quicksync_detected'] == true ? 'Active' : 'Software decode fallback'),
                  ),
                ] else
                  const Text('No hardware report available.'),
              ],
            ),
            isActive: _currentStep >= 2,
          ),
          Step(
            title: const Text('Camera Setup'),
            content: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ElevatedButton(onPressed: _scanCameras, child: const Text('Scan Network for Cameras')),
                const SizedBox(height: 8),
                if (_cameras.isNotEmpty)
                  ..._cameras.map((cam) => ListTile(title: Text(cam['name']), subtitle: Text(cam['url']))).toList()
                else
                  const Text('No cameras detected.'),
              ],
            ),
            isActive: _currentStep >= 3,
          ),
          Step(
            title: const Text('Notifications'),
            content: const Text('Set up mobile push notifications (optional). You can enable this later in settings.'),
            isActive: _currentStep >= 4,
          ),
          Step(
            title: const Text('Summary'),
            content: const Text('Review your settings. Click Launch System to finish.'),
            isActive: _currentStep >= 5,
          ),
        ],
      ),
    );
  }
}
