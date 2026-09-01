import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../core/theme/app_theme.dart';
import 'login_screen.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({Key? key}) : super(key: key);

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _storage = const FlutterSecureStorage();
  String _serverUrl = '';

  @override
  void initState() {
    super.initState();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _serverUrl = prefs.getString('server_url') ?? 'Unknown';
    });
  }

  Future<void> _logout() async {
    await _storage.delete(key: 'access_token');
    await _storage.delete(key: 'refresh_token');
    if (mounted) {
      Navigator.pushAndRemoveUntil(
        context,
        MaterialPageRoute(builder: (_) => const LoginScreen()),
        (route) => false,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('Settings'),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout, color: AppTheme.emergencyRed),
            onPressed: _logout,
            tooltip: 'Logout',
          )
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16.0),
        children: [
          _buildSectionHeader('Server Connection'),
          ListTile(
            title: const Text('Current Server URL'),
            subtitle: Text(_serverUrl),
            trailing: const Icon(Icons.edit, size: 20),
            onTap: () {},
          ),
          const Divider(color: AppTheme.borderHighlight),
          
          _buildSectionHeader('Hardware & Storage'),
          ListTile(
            title: const Text('Hardware Status'),
            subtitle: const Text('Hailo-8 / Intel QuickSync'),
            trailing: const Icon(Icons.memory, size: 20),
            onTap: () {},
          ),
          ListTile(
            title: const Text('Storage Policy'),
            subtitle: const Text('Retention, disk usage, cleanup'),
            trailing: const Icon(Icons.storage, size: 20),
            onTap: () {},
          ),
          const Divider(color: AppTheme.borderHighlight),

          _buildSectionHeader('Cameras & Notifications'),
          ListTile(
            title: const Text('Manage Cameras'),
            subtitle: const Text('Add, remove, or edit RTSP streams'),
            trailing: const Icon(Icons.videocam, size: 20),
            onTap: () {},
          ),
          ListTile(
            title: const Text('Notifications'),
            subtitle: const Text('Push alerts, sound settings'),
            trailing: const Icon(Icons.notifications, size: 20),
            onTap: () {},
          ),
          const Divider(color: AppTheme.borderHighlight),

          _buildSectionHeader('Security & Account'),
          ListTile(
            title: const Text('Change Password'),
            trailing: const Icon(Icons.lock, size: 20),
            onTap: () {},
          ),
          ListTile(
            title: const Text('Biometric Login'),
            trailing: Switch(
              value: true,
              onChanged: (val) {},
              activeColor: AppTheme.cyberBlue,
            ),
          ),
          const Divider(color: AppTheme.borderHighlight),

          _buildSectionHeader('About'),
          const ListTile(
            title: Text('Version'),
            subtitle: Text('1.2.0 (Build 42)'),
          ),
        ],
      ),
    );
  }

  Widget _buildSectionHeader(String title) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8.0, horizontal: 4.0),
      child: Text(
        title,
        style: const TextStyle(
          color: AppTheme.cyberBlue,
          fontWeight: FontWeight.bold,
          fontSize: 14,
        ),
      ),
    );
  }
}
