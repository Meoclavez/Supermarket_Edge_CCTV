import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../services/api_service.dart';
import '../core/server_time.dart';

/// Exported incident clips from `GET /api/v1/dvr/archives`.
class ClipArchivesScreen extends StatefulWidget {
  const ClipArchivesScreen({Key? key}) : super(key: key);

  @override
  State<ClipArchivesScreen> createState() => _ClipArchivesScreenState();
}

class _ClipArchivesScreenState extends State<ClipArchivesScreen> {
  List<Map<String, dynamic>> _archives = [];
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
      final list = await ApiService().getArchives();
      if (mounted) setState(() => _archives = list);
    } catch (e) {
      if (mounted) setState(() => _error = 'Could not load archives: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  String _subtitle(Map<String, dynamic> a) {
    final fmt = DateFormat('d MMM HH:mm');
    final start = parseServerTime(a['start_time']);
    final secs = (a['duration_seconds'] as num?)?.toDouble() ?? 0;
    final mb = ((a['file_size_bytes'] as num?)?.toDouble() ?? 0) / (1024 * 1024);
    final when = start != null ? fmt.format(start) : 'unknown time';
    return '${a['camera_name'] ?? a['camera_id'] ?? ''} • $when (${(secs / 60).toStringAsFixed(1)} min)\n'
        '${mb.toStringAsFixed(1)} MB • ${a['status'] ?? ''}';
  }

  @override
  Widget build(BuildContext context) {
    Widget body;
    if (_loading) {
      body = Center(child: CircularProgressIndicator(color: context.palette.accent));
    } else if (_error != null) {
      body = Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_error!, textAlign: TextAlign.center)));
    } else if (_archives.isEmpty) {
      body = Center(child: Text('No exported clips yet', style: TextStyle(color: context.palette.dim(0.60))));
    } else {
      body = RefreshIndicator(
        onRefresh: _load,
        child: ListView.builder(
          padding: const EdgeInsets.all(16),
          itemCount: _archives.length,
          itemBuilder: (context, index) {
            final arch = _archives[index];
            return Card(
              color: context.palette.card,
              margin: const EdgeInsets.only(bottom: 12),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10),
                side: BorderSide(color: context.palette.border),
              ),
              child: ListTile(
                leading: Icon(Icons.video_file_outlined, color: context.palette.accent, size: 36),
                title: Text(arch['title']?.toString() ?? 'Clip', style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                subtitle: Text(_subtitle(arch), style: TextStyle(color: context.palette.dim(0.60), fontSize: 11)),
              ),
            );
          },
        ),
      );
    }

    return Scaffold(
      backgroundColor: context.palette.background,
      appBar: AppBar(
        title: const Text('Incident clip archives', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [IconButton(icon: const Icon(Icons.refresh), onPressed: _loading ? null : _load)],
      ),
      body: body,
    );
  }
}
