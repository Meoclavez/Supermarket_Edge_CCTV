import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../services/api_service.dart';
import '../core/server_time.dart';

/// Recording storage and disk health from `GET /api/v1/storage/health`.
///
/// Every value shown comes from the edge server. Fields the server leaves
/// null (for example SMART data on a disk that does not report it) are shown
/// as "n/a" rather than estimated.
class StorageHealthScreen extends StatefulWidget {
  const StorageHealthScreen({Key? key}) : super(key: key);

  @override
  State<StorageHealthScreen> createState() => _StorageHealthScreenState();
}

class _StorageHealthScreenState extends State<StorageHealthScreen> {
  Map<String, dynamic>? _data;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await ApiService().getStorageHealth();
      if (mounted) setState(() => _data = data);
    } catch (e) {
      if (mounted) setState(() => _error = 'Could not load storage health: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  static double _num(dynamic v) => (v as num?)?.toDouble() ?? 0.0;
  static String _opt(dynamic v, [String suffix = '']) => v == null ? 'n/a' : '$v$suffix';

  @override
  Widget build(BuildContext context) {
    Widget body;
    if (_loading && _data == null) {
      body = Center(child: CircularProgressIndicator(color: context.palette.accent));
    } else if (_data == null) {
      body = Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_error ?? 'No data', textAlign: TextAlign.center)));
    } else {
      final d = _data!;
      final disks = List<Map<String, dynamic>>.from(d['smart_status'] ?? []);
      final quotas = List<Map<String, dynamic>>.from(d['camera_quotas'] ?? []);
      body = RefreshIndicator(
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(_error!, style: TextStyle(color: context.palette.warning, fontSize: 12)),
              ),
            _buildPoolCard(d),
            const SizedBox(height: 16),
            _sectionTitle('DISKS'),
            if (disks.isEmpty) Text('The server reported no disk SMART data.', style: TextStyle(color: context.palette.dim(0.60))),
            for (final disk in disks) _buildDiskCard(disk),
            const SizedBox(height: 16),
            _sectionTitle('PER-CAMERA RECORDING'),
            if (quotas.isEmpty) Text('No recordings yet.', style: TextStyle(color: context.palette.dim(0.60))),
            for (final q in quotas) _buildQuotaTile(q),
          ],
        ),
      );
    }

    return Scaffold(
      backgroundColor: context.palette.background,
      appBar: AppBar(
        title: const Text('Storage health', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [IconButton(icon: const Icon(Icons.refresh_rounded), onPressed: _loading ? null : _load)],
      ),
      body: body,
    );
  }

  Widget _sectionTitle(String t) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(t, style: TextStyle(color: context.palette.dim(0.70), fontWeight: FontWeight.bold, fontSize: 12, letterSpacing: 0.8)),
      );

  Widget _card(Widget child) => Container(
        margin: const EdgeInsets.only(bottom: 10),
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: context.palette.card,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: context.palette.border),
        ),
        child: child,
      );

  Widget _buildPoolCard(Map<String, dynamic> d) {
    final usedPct = _num(d['used_percent']);
    final color = usedPct > 85 ? context.palette.alert : context.palette.accent;
    return _card(Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _sectionTitle('RECORDING POOL'),
        Text(d['storage_root']?.toString() ?? '', style: TextStyle(fontFamily: 'monospace', fontSize: 12, color: context.palette.dim(0.60))),
        const SizedBox(height: 10),
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: (usedPct / 100).clamp(0.0, 1.0),
            minHeight: 10,
            backgroundColor: context.palette.hairline(0.12),
            valueColor: AlwaysStoppedAnimation<Color>(color),
          ),
        ),
        const SizedBox(height: 10),
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            _stat('Used', '${_num(d['used_gb']).toStringAsFixed(1)} GB (${usedPct.toStringAsFixed(1)}%)'),
            _stat('Free', '${_num(d['free_gb']).toStringAsFixed(1)} GB'),
            _stat('Total', '${_num(d['total_gb']).toStringAsFixed(0)} GB'),
          ],
        ),
        const SizedBox(height: 6),
        Text('Archives: ${_num(d['archives_used_gb']).toStringAsFixed(1)} GB • '
            '${d['is_external_mount'] == true ? 'external mount' : 'internal disk'}',
            style: TextStyle(color: context.palette.dim(0.54), fontSize: 11)),
      ],
    ));
  }

  Widget _stat(String label, String value) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: TextStyle(color: context.palette.dim(0.54), fontSize: 11)),
          Text(value, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
        ],
      );

  Widget _buildDiskCard(Map<String, dynamic> disk) {
    final status = disk['health_status']?.toString() ?? 'UNKNOWN';
    final ok = status.toUpperCase() == 'PASSED';
    return _card(Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text('${disk['model'] ?? 'Disk'} (${disk['device'] ?? ''})',
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
            ),
            Text(status, style: TextStyle(color: ok ? context.palette.live : context.palette.alert, fontWeight: FontWeight.bold, fontSize: 11)),
          ],
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 16,
          runSpacing: 6,
          children: [
            _stat('Temperature', _opt(disk['temperature_celsius'], ' °C')),
            _stat('Wear level', _opt(disk['wear_level_percent'], '%')),
            _stat('Power-on', _opt(disk['power_on_hours'], ' h')),
            _stat('Reallocated', _opt(disk['reallocated_sectors'])),
          ],
        ),
      ],
    ));
  }

  Widget _buildQuotaTile(Map<String, dynamic> q) {
    final used = _num(q['used_gb']);
    final quota = _num(q['quota_gb']);
    final progress = quota > 0 ? (used / quota).clamp(0.0, 1.0) : 0.0;
    final oldest = parseServerTime(q['oldest_segment']);
    return _card(Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(q['camera_name']?.toString() ?? q['camera_id']?.toString() ?? 'Camera',
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
        const SizedBox(height: 6),
        LinearProgressIndicator(
          value: progress,
          backgroundColor: context.palette.hairline(0.12),
          valueColor: AlwaysStoppedAnimation<Color>(progress > 0.85 ? context.palette.alert : context.palette.accent),
        ),
        const SizedBox(height: 6),
        Text(
          '${used.toStringAsFixed(1)} / ${quota.toStringAsFixed(0)} GB • ${q['segment_count'] ?? 0} segments • '
          'oldest ${oldest != null ? DateFormat('d MMM HH:mm').format(oldest) : 'n/a'}',
          style: TextStyle(color: context.palette.dim(0.54), fontSize: 11),
        ),
      ],
    ));
  }
}
