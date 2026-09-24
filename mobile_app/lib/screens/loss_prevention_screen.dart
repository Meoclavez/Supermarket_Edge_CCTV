import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/theft_incident.dart';
import '../services/api_service.dart';
import 'loss_prevention_alert_screen.dart';

/// List of loss-prevention incidents from `GET /api/v1/theft/incidents`.
class LossPreventionScreen extends StatefulWidget {
  const LossPreventionScreen({Key? key}) : super(key: key);

  @override
  State<LossPreventionScreen> createState() => _LossPreventionScreenState();
}

class _LossPreventionScreenState extends State<LossPreventionScreen> {
  static const Map<String?, String> _filters = {
    'ACTIVE': 'Open',
    'ACKNOWLEDGED': 'Acknowledged',
    'RESOLVED': 'Resolved',
    null: 'All',
  };

  final ApiService _api = ApiService();
  String? _status = 'ACTIVE';
  List<TheftIncident> _incidents = [];
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
      final list = await _api.getTheftIncidents(status: _status);
      if (mounted) setState(() => _incidents = list);
    } catch (e) {
      if (mounted) setState(() => _error = 'Could not load incidents: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Loss prevention', style: TextStyle(fontWeight: FontWeight.bold)),
        actions: [IconButton(icon: const Icon(Icons.refresh), onPressed: _loading ? null : _load)],
      ),
      body: Column(
        children: [
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: [
                for (final f in _filters.entries)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(f.value),
                      selected: _status == f.key,
                      onSelected: (_) {
                        setState(() => _status = f.key);
                        _load();
                      },
                    ),
                  ),
              ],
            ),
          ),
          Expanded(child: _buildBody()),
        ],
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) return Center(child: CircularProgressIndicator(color: context.palette.accent));
    if (_error != null) {
      return Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_error!, textAlign: TextAlign.center)));
    }
    if (_incidents.isEmpty) {
      return Center(child: Text('No incidents', style: TextStyle(color: context.palette.dim(0.60))));
    }
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.builder(
        padding: const EdgeInsets.symmetric(horizontal: 12),
        itemCount: _incidents.length,
        itemBuilder: (context, i) {
          final inc = _incidents[i];
          return Card(
            margin: const EdgeInsets.only(bottom: 8),
            child: ListTile(
              leading: Icon(Icons.policy_outlined, color: inc.isOpen ? context.palette.alert : context.palette.dim(0.38)),
              title: Text(inc.ruleLabel, style: const TextStyle(fontWeight: FontWeight.bold)),
              subtitle: Text(
                '${inc.cameraName} • ${DateFormat('d MMM HH:mm').format(inc.timestamp)} • '
                '${(inc.confidence * 100).toStringAsFixed(0)}% • ${inc.status}',
                style: TextStyle(fontSize: 12, color: context.palette.dim(0.60)),
              ),
              trailing: const Icon(Icons.chevron_right),
              onTap: () async {
                await Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => LossPreventionAlertScreen.fromIncident(inc)),
                );
                _load();
              },
            ),
          );
        },
      ),
    );
  }
}
