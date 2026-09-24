import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:intl/intl.dart';

import '../core/theme/app_theme.dart';
import '../models/server_entry.dart';
import '../services/connection_resolver.dart';
import '../services/pairing_service.dart';
import '../services/push_service.dart';
import '../services/server_registry.dart';
import 'pairing_screen.dart';
import '../core/server_time.dart';

/// Settings for one paired store server: identity, connection, paired
/// status, alert preferences and removal. All confirmations are inline.
class ServerSettingsScreen extends StatefulWidget {
  final String deviceId;
  const ServerSettingsScreen({Key? key, required this.deviceId}) : super(key: key);

  @override
  State<ServerSettingsScreen> createState() => _ServerSettingsScreenState();
}

class _ServerSettingsScreenState extends State<ServerSettingsScreen> {
  final _registry = ServerRegistry();
  final _service = PairingService();

  bool _hasSession = false;
  bool _loadingRecord = false;
  Map<String, dynamic>? _record;
  String? _recordError;
  ResolveResult? _probe;
  bool _checking = false;

  // Alert preferences editor
  bool _allTypes = true;
  final Set<String> _types = {};
  String _minSeverity = 'INFO';
  bool _allCameras = true;
  final Set<String> _cameraIds = {};
  List<Map<String, String>> _cameras = [];
  bool _quiet = false;
  final _quietStart = TextEditingController(text: '22:00');
  final _quietEnd = TextEditingController(text: '07:00');
  bool _savingPrefs = false;
  String? _prefsMessage;
  String? _prefsError;

  // Removal
  bool _confirmRemove = false;
  bool _removing = false;
  String? _removeError;

  ServerEntry? get _entry => _registry.byId(widget.deviceId);

  @override
  void initState() {
    super.initState();
    _applyPrefs(_entry?.alertPrefs ?? const AlertPrefs());
    _load();
  }

  @override
  void dispose() {
    _quietStart.dispose();
    _quietEnd.dispose();
    super.dispose();
  }

  void _applyPrefs(AlertPrefs p) {
    _allTypes = p.eventTypes == null;
    _types
      ..clear()
      ..addAll(p.eventTypes ?? const []);
    _minSeverity = p.minSeverity;
    _allCameras = p.cameraIds == null;
    _cameraIds
      ..clear()
      ..addAll(p.cameraIds ?? const []);
    _quiet = p.hasQuietHours;
    if (p.quietStart != null) _quietStart.text = p.quietStart!;
    if (p.quietEnd != null) _quietEnd.text = p.quietEnd!;
  }

  Future<void> _load() async {
    final entry = _entry;
    if (entry == null) return;
    final session = await _registry.hasSession(entry.deviceId);
    if (!mounted) return;
    setState(() {
      _hasSession = session;
      _loadingRecord = session;
      _recordError = null;
    });
    if (!session) return;
    unawaited(_loadCameras(entry));
    try {
      final rec = await _service.fetchOwnRecord(entry);
      if (!mounted) return;
      setState(() {
        _record = rec;
        if (rec == null && entry.pairedDeviceId != null) {
          _recordError = 'This phone is no longer listed on the server. It may have been removed from the dashboard.';
        }
        if (entry.alertPrefs != null) _applyPrefs(entry.alertPrefs!);
      });
    } on PairingException catch (e) {
      if (mounted) setState(() => _recordError = e.message);
    } catch (e) {
      if (mounted) setState(() => _recordError = 'Could not load this phone\'s record: $e');
    } finally {
      if (mounted) setState(() => _loadingRecord = false);
    }
  }

  Future<void> _loadCameras(ServerEntry entry) async {
    final url = _registry.urlFor(entry.deviceId);
    if (url == null) return;
    try {
      final res = await http
          .get(Uri.parse('$url/api/v1/cameras'), headers: await _registry.authHeadersFor(entry.deviceId))
          .timeout(const Duration(seconds: 8));
      if (res.statusCode != 200) return;
      final data = jsonDecode(res.body);
      final list = data is Map ? (data['cameras'] as List? ?? const []) : (data is List ? data : const []);
      final cams = <Map<String, String>>[];
      for (final c in list) {
        if (c is Map && c['id'] != null) cams.add({'id': c['id'].toString(), 'name': (c['name'] ?? c['id']).toString()});
      }
      if (mounted) setState(() => _cameras = cams);
    } catch (_) {
      // Camera filter falls back to "all cameras" when the list is unavailable.
    }
  }

  Future<void> _checkConnection() async {
    setState(() => _checking = true);
    final result = await _registry.resolve(widget.deviceId);
    if (!mounted) return;
    setState(() {
      _probe = result;
      _checking = false;
    });
  }

  String? _validatePrefs() {
    if (!_allTypes && _types.isEmpty) return 'Pick at least one alert type, or choose all types.';
    if (!_allCameras && _cameraIds.isEmpty) return 'Pick at least one camera, or choose all cameras.';
    if (_quiet) {
      final s = parseHhMm(_quietStart.text);
      final e = parseHhMm(_quietEnd.text);
      if (s == null || e == null) return 'Quiet hours must be 24-hour times like 22:00 and 07:00.';
      if (s == e) return 'Quiet hours start and end must differ.';
    }
    return null;
  }

  Future<void> _savePrefs() async {
    final entry = _entry;
    if (entry == null) return;
    final problem = _validatePrefs();
    if (problem != null) {
      setState(() {
        _prefsError = problem;
        _prefsMessage = null;
      });
      return;
    }
    final prefs = AlertPrefs(
      eventTypes: _allTypes ? null : (_types.toList()..sort()),
      minSeverity: _minSeverity,
      cameraIds: _allCameras ? null : (_cameraIds.toList()..sort()),
      quietStart: _quiet ? _quietStart.text.trim() : null,
      quietEnd: _quiet ? _quietEnd.text.trim() : null,
    );
    setState(() {
      _savingPrefs = true;
      _prefsError = null;
      _prefsMessage = null;
    });
    try {
      final saved = await _service.saveAlertPrefs(entry, prefs);
      if (!mounted) return;
      setState(() {
        _applyPrefs(saved);
        _prefsMessage = 'Saved. ${_describePrefs(saved)}';
      });
    } on PairingException catch (e) {
      if (mounted) setState(() => _prefsError = e.message);
    } catch (e) {
      if (mounted) setState(() => _prefsError = 'Could not save: $e');
    } finally {
      if (mounted) setState(() => _savingPrefs = false);
    }
  }

  String _describePrefs(AlertPrefs p) {
    final types = p.eventTypes == null ? 'all alert types' : '${p.eventTypes!.length} alert type(s)';
    final cams = p.cameraIds == null ? 'all cameras' : '${p.cameraIds!.length} camera(s)';
    final quiet = p.hasQuietHours ? ', silent ${p.quietStart} to ${p.quietEnd}' : '';
    return 'This phone gets $types at ${p.minSeverity} or above from $cams$quiet.';
  }

  Future<void> _remove() async {
    final entry = _entry;
    if (entry == null) return;
    setState(() {
      _removing = true;
      _removeError = null;
    });
    try {
      final result = await _service.removeServer(entry.deviceId);
      if (!mounted) return;
      final navigator = Navigator.of(context);
      if (_registry.servers.isEmpty) {
        navigator.pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const PairingScreen()), (_) => false);
      } else {
        navigator.pop(result.revokedOnServer
            ? '${entry.name} removed and this phone was revoked on its server.'
            : '${entry.name} removed from this phone. ${result.note ?? ''}'.trim());
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _removeError = 'Could not remove: $e';
          _removing = false;
        });
      }
    }
  }

  // ---------------------------------------------------------------------- UI

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([_registry, PushService()]),
      builder: (context, _) {
        final entry = _entry;
        if (entry == null) {
          return Scaffold(appBar: AppBar(title: const Text('Server')), body: const Center(child: Text('This server was removed.')));
        }
        return Scaffold(
          backgroundColor: context.palette.background,
          appBar: AppBar(title: Text(entry.name)),
          body: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _section('Server'),
              _kv('Name', entry.name),
              _kv('Device ID', entry.deviceId, selectable: true),
              _kv('Address in use', _registry.urlFor(entry.deviceId) ?? 'none yet'),
              _kv('Connection', _reachabilityText(entry)),
              if (entry.remoteUrl != null) _kv('Remote URL', entry.remoteUrl!),
              Align(
                alignment: Alignment.centerLeft,
                child: OutlinedButton.icon(
                  onPressed: _checking ? null : _checkConnection,
                  icon: const Icon(Icons.wifi_find, size: 18),
                  label: Text(_checking ? 'Checking...' : 'Check connection'),
                ),
              ),
              if (_probe != null) ..._probe!.probes.map((p) => Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Icon(_probeIcon(p.outcome), size: 16, color: _probeColor(p.outcome)),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text('${p.url}: ${p.describe()}', style: TextStyle(color: _probeColor(p.outcome), fontSize: 12)),
                      ),
                    ]),
                  )),
              Divider(color: context.palette.border, height: 32),
              _section('This phone on the server'),
              ..._pairedStatus(entry),
              Divider(color: context.palette.border, height: 32),
              _section('Alert preferences'),
              ..._prefsEditor(entry),
              Divider(color: context.palette.border, height: 32),
              _section('Remove'),
              ..._removeSection(entry),
            ],
          ),
        );
      },
    );
  }

  String _reachabilityText(ServerEntry entry) {
    switch (_registry.reachabilityOf(entry.deviceId)) {
      case ServerReachability.reachable:
        return 'Verified: the address answers with this device ID';
      case ServerReachability.resolving:
        return 'Checking...';
      case ServerReachability.differentServer:
        return '$kDifferentServerMessage at every known address';
      case ServerReachability.unreachable:
        return 'Not reachable from this network';
      case ServerReachability.unknown:
        return 'Not checked yet';
    }
  }

  IconData _probeIcon(ProbeOutcome o) => o == ProbeOutcome.match
      ? Icons.check_circle_outline
      : o == ProbeOutcome.differentServer
          ? Icons.report_gmailerrorred
          : Icons.cloud_off;

  Color _probeColor(ProbeOutcome o) => o == ProbeOutcome.match
      ? context.palette.live
      : o == ProbeOutcome.differentServer
          ? context.palette.alert
          : context.palette.muted;

  List<Widget> _pairedStatus(ServerEntry entry) {
    final push = PushService();
    final widgets = <Widget>[];
    if (!_hasSession) {
      widgets.add(Text('Signed out on this phone.', style: TextStyle(color: context.palette.alert)));
      widgets.add(Align(
        alignment: Alignment.centerLeft,
        child: TextButton(
          onPressed: () => Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => PairingScreen(
              reSignInDeviceId: entry.deviceId,
              onPaired: (_) {
                Navigator.of(context).pop();
                _load();
              },
            ),
          )),
          child: const Text('Sign in again'),
        ),
      ));
    } else if (_loadingRecord) {
      widgets.add(LinearProgressIndicator(color: context.palette.accent));
    } else if (_record != null) {
      final r = _record!;
      final revoked = r['revoked'] == true;
      widgets.add(_kv('Status', revoked ? 'Revoked on the server' : 'Paired'));
      widgets.add(_kv('Phone name', (r['name'] ?? '').toString()));
      widgets.add(_kv('Paired record ID', (r['id'] ?? entry.pairedDeviceId ?? '').toString(), selectable: true));
      final pairedAt = parseServerTime(r['paired_at']);
      if (pairedAt != null) widgets.add(_kv('Paired at', DateFormat('d MMM yyyy, HH:mm').format(pairedAt)));
      final pushInfo = r['push'];
      if (pushInfo is Map) {
        widgets.add(_kv('Push token on server', pushInfo['token_registered'] == true ? 'registered' : 'none'));
        if (pushInfo['last_status'] != null) widgets.add(_kv('Last push', pushInfo['last_status'].toString()));
      }
    } else {
      widgets.add(_kv(
          'Status',
          entry.pairedDeviceId != null
              ? 'Paired (record ${entry.pairedDeviceId})'
              : 'Signed in, but not registered as a paired phone: no pushes from this server'));
    }
    if (_recordError != null) {
      widgets.add(Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Text(_recordError!, style: TextStyle(color: context.palette.alert, fontSize: 13)),
      ));
    }
    widgets.add(_kv('Push on this build', push.statusLabel));
    if (push.status == PushStatus.notConfigured) {
      widgets.add(Text(
        'Alerts still arrive while the app is open, through the live connection to the server.',
        style: TextStyle(color: context.palette.muted, fontSize: 12),
      ));
    }
    return widgets;
  }

  List<Widget> _prefsEditor(ServerEntry entry) {
    final disabled = !_hasSession || _savingPrefs;
    return [
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('All alert types'),
        value: _allTypes,
        onChanged: disabled ? null : (v) => setState(() => _allTypes = v),
      ),
      if (!_allTypes)
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: kDispatchableEventTypes.entries
              .map((e) => FilterChip(
                    label: Text(e.value),
                    selected: _types.contains(e.key),
                    onSelected: disabled
                        ? null
                        : (sel) => setState(() => sel ? _types.add(e.key) : _types.remove(e.key)),
                  ))
              .toList(),
        ),
      const SizedBox(height: 8),
      Row(children: [
        const Expanded(child: Text('Minimum severity')),
        DropdownButton<String>(
          value: _minSeverity,
          items: kSeverityLevels.map((s) => DropdownMenuItem(value: s, child: Text(s))).toList(),
          onChanged: disabled ? null : (v) => setState(() => _minSeverity = v ?? 'INFO'),
        ),
      ]),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('All cameras'),
        value: _allCameras,
        onChanged: disabled ? null : (v) => setState(() => _allCameras = v),
      ),
      if (!_allCameras)
        _cameras.isEmpty
            ? Text('The camera list could not be loaded from this server.',
                style: TextStyle(color: context.palette.muted, fontSize: 12))
            : Wrap(
                spacing: 6,
                runSpacing: 6,
                children: _cameras
                    .map((c) => FilterChip(
                          label: Text(c['name']!),
                          selected: _cameraIds.contains(c['id']),
                          onSelected: disabled
                              ? null
                              : (sel) => setState(() => sel ? _cameraIds.add(c['id']!) : _cameraIds.remove(c['id'])),
                        ))
                    .toList(),
              ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        title: const Text('Quiet hours'),
        subtitle: const Text('No pushes from this store during these hours'),
        value: _quiet,
        onChanged: disabled ? null : (v) => setState(() => _quiet = v),
      ),
      if (_quiet)
        Row(children: [
          Expanded(
            child: TextField(
              controller: _quietStart,
              enabled: !disabled,
              decoration: const InputDecoration(labelText: 'From (HH:MM)'),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: TextField(
              controller: _quietEnd,
              enabled: !disabled,
              decoration: const InputDecoration(labelText: 'To (HH:MM)'),
            ),
          ),
        ]),
      const SizedBox(height: 12),
      Align(
        alignment: Alignment.centerLeft,
        child: ElevatedButton(
          onPressed: disabled ? null : _savePrefs,
          child: Text(_savingPrefs ? 'Saving...' : 'Save alert preferences'),
        ),
      ),
      if (_prefsMessage != null)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text(_prefsMessage!, style: TextStyle(color: context.palette.live)),
        ),
      if (_prefsError != null)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text(_prefsError!, style: TextStyle(color: context.palette.alert)),
        ),
    ];
  }

  List<Widget> _removeSection(ServerEntry entry) {
    if (!_confirmRemove) {
      return [
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton.icon(
            style: OutlinedButton.styleFrom(foregroundColor: context.palette.alert),
            onPressed: () => setState(() => _confirmRemove = true),
            icon: const Icon(Icons.link_off),
            label: const Text('Remove this server'),
          ),
        ),
      ];
    }
    return [
      Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          border: Border.all(color: context.palette.alert.withValues(alpha: 0.6)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(
            'Remove ${entry.name} from this phone? The phone is revoked on the server if it can be reached, '
            'and its sign-in for this store is deleted. Alerts from this store stop.',
          ),
          const SizedBox(height: 12),
          Row(children: [
            TextButton(
              onPressed: _removing ? null : () => setState(() => _confirmRemove = false),
              child: const Text('Cancel'),
            ),
            const SizedBox(width: 8),
            ElevatedButton(
              style: ElevatedButton.styleFrom(backgroundColor: context.palette.alert),
              onPressed: _removing ? null : _remove,
              child: Text(_removing ? 'Removing...' : 'Remove'),
            ),
          ]),
          if (_removeError != null) Text(_removeError!, style: TextStyle(color: context.palette.alert)),
        ]),
      ),
    ];
  }

  Widget _section(String title) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(title, style: TextStyle(color: context.palette.accent, fontWeight: FontWeight.bold, fontSize: 14)),
      );

  Widget _kv(String k, String v, {bool selectable = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 140, child: Text(k, style: TextStyle(color: context.palette.muted, fontSize: 13))),
          Expanded(child: selectable ? SelectableText(v, style: const TextStyle(fontSize: 13)) : Text(v, style: const TextStyle(fontSize: 13))),
        ]),
      );
}
