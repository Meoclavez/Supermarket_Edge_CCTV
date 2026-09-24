import 'dart:async';

import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../core/theme/theme_controller.dart';
import '../services/push_service.dart';
import '../services/server_registry.dart';
import 'pairing_screen.dart';
import 'server_settings_screen.dart';

/// App settings: the stores this phone is paired with (and which one is in
/// use), this phone's identity and push status.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({Key? key}) : super(key: key);

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _registry = ServerRegistry();
  final _phoneName = TextEditingController();
  String _instanceId = '';
  String? _message;

  @override
  void initState() {
    super.initState();
    _loadPhone();
  }

  @override
  void dispose() {
    _phoneName.dispose();
    super.dispose();
  }

  Future<void> _loadPhone() async {
    final name = await _registry.phoneName();
    final id = await _registry.appInstanceId();
    if (!mounted) return;
    setState(() {
      _phoneName.text = name ?? '';
      _instanceId = id;
    });
  }

  Future<void> _savePhoneName() async {
    await _registry.setPhoneName(_phoneName.text);
    if (mounted) {
      setState(() => _message = 'Phone name saved. It is sent to a server the next time you pair or sign in.');
    }
  }

  Future<void> _openServer(String deviceId) async {
    final result = await Navigator.of(context)
        .push<String>(MaterialPageRoute(builder: (_) => ServerSettingsScreen(deviceId: deviceId)));
    if (mounted && result != null) setState(() => _message = result);
  }

  Future<void> _addStore() async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (ctx) => PairingScreen(onPaired: (entry) {
        Navigator.of(ctx).pop();
        if (mounted) setState(() => _message = 'Added ${entry.name}. It is now the active store.');
      }),
    ));
  }

  Future<void> _signOutActive() async {
    final entry = _registry.active;
    if (entry == null) return;
    await _registry.clearTokens(entry.deviceId);
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => PairingScreen(reSignInDeviceId: entry.deviceId)),
      (_) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([_registry, PushService(), ThemeController()]),
      builder: (context, _) {
        final push = PushService();
        final active = _registry.active;
        final p = context.palette;
        return Scaffold(
          backgroundColor: p.background,
          appBar: AppBar(title: const Text('Settings')),
          body: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (_message != null)
                Container(
                  margin: const EdgeInsets.only(bottom: 12),
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: p.accent.withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(children: [
                    Expanded(child: Text(_message!)),
                    IconButton(
                      tooltip: 'Dismiss',
                      icon: const Icon(Icons.close, size: 18),
                      onPressed: () => setState(() => _message = null),
                    ),
                  ]),
                ),
              _header('Stores'),
              ..._registry.servers.map((s) {
                final isActive = s.deviceId == _registry.activeId;
                return ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(isActive ? Icons.radio_button_checked : Icons.radio_button_off,
                      color: isActive ? p.accent : p.dim(0.54)),
                  title: Text(s.name),
                  subtitle: Text('ID ${s.shortId}...  ${_registry.urlFor(s.deviceId) ?? ''}',
                      style: const TextStyle(fontSize: 12)),
                  onTap: () async {
                    await _registry.setActive(s.deviceId);
                    unawaited(_registry.resolve(s.deviceId));
                  },
                  trailing: IconButton(
                    tooltip: 'Server settings',
                    icon: const Icon(Icons.tune),
                    onPressed: () => _openServer(s.deviceId),
                  ),
                );
              }),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(onPressed: _addStore, icon: const Icon(Icons.add), label: const Text('Add a store')),
              ),
              if (active != null)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: _signOutActive,
                    icon: Icon(Icons.logout, color: p.alert),
                    label: Text('Sign out of ${active.name}', style: TextStyle(color: p.alert)),
                  ),
                ),
              Divider(color: p.border, height: 32),
              _header('Appearance'),
              SegmentedButton<ThemeMode>(
                key: const ValueKey('theme-mode'),
                segments: const [
                  ButtonSegment(value: ThemeMode.light, icon: Icon(Icons.light_mode_outlined), label: Text('Light')),
                  ButtonSegment(value: ThemeMode.dark, icon: Icon(Icons.dark_mode_outlined), label: Text('Dark')),
                  ButtonSegment(value: ThemeMode.system, icon: Icon(Icons.brightness_auto_outlined), label: Text('System')),
                ],
                selected: {ThemeController().value},
                showSelectedIcon: false,
                onSelectionChanged: (sel) => ThemeController().setMode(sel.first),
              ),
              const SizedBox(height: 6),
              Text('System follows the phone\'s light or dark setting.', style: TextStyle(color: p.muted, fontSize: 12)),
              Divider(color: p.border, height: 32),
              _header('This phone'),
              Row(children: [
                Expanded(
                  child: TextField(controller: _phoneName, decoration: const InputDecoration(labelText: 'Phone name')),
                ),
                const SizedBox(width: 8),
                OutlinedButton(onPressed: _savePhoneName, child: const Text('Save')),
              ]),
              const SizedBox(height: 8),
              Text('App instance ID: $_instanceId', style: TextStyle(color: p.muted, fontSize: 12)),
              const SizedBox(height: 12),
              Row(children: [
                Icon(push.status == PushStatus.ready ? Icons.notifications_active : Icons.notifications_off,
                    color: push.status == PushStatus.ready ? p.live : p.muted, size: 20),
                const SizedBox(width: 8),
                Expanded(child: Text(push.statusLabel, key: const ValueKey('push-status'))),
              ]),
              if (push.detail != null)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(push.detail!, style: TextStyle(color: p.muted, fontSize: 12)),
                ),
              if (push.status != PushStatus.ready)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text('While the app is open, alerts still arrive through the live connection to the active store.',
                      style: TextStyle(color: p.muted, fontSize: 12)),
                ),
              Divider(color: p.border, height: 32),
              _header('About'),
              Text('Store Edge CCTV 1.2.0', style: TextStyle(color: p.muted)),
            ],
          ),
        );
      },
    );
  }

  Widget _header(String title) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Text(title, style: TextStyle(color: context.palette.accent, fontWeight: FontWeight.bold, fontSize: 14)),
      );
}
