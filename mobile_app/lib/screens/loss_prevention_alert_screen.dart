import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/theft_incident.dart';
import '../services/api_service.dart';
import 'live_view_screen.dart';

/// Detail view for a single loss-prevention incident.
///
/// Opened from a push notification (only the incident id is known, so the
/// incident is fetched) or from the incident list (the incident is passed in).
/// The incident is suspicious behaviour for staff review, never a verdict.
class LossPreventionAlertScreen extends StatefulWidget {
  final String incidentId;
  final TheftIncident? initialIncident;

  const LossPreventionAlertScreen({Key? key, required this.incidentId, this.initialIncident}) : super(key: key);

  LossPreventionAlertScreen.fromIncident(TheftIncident incident, {Key? key})
      : incidentId = incident.id,
        initialIncident = incident,
        super(key: key);

  @override
  State<LossPreventionAlertScreen> createState() => _LossPreventionAlertScreenState();
}

class _LossPreventionAlertScreenState extends State<LossPreventionAlertScreen> {
  static const Map<String, String> _resolutions = {
    'RECOVERED_GOODS': 'Goods recovered',
    'FALSE_ALARM': 'False alarm',
    'SUSPECT_FLED': 'Person left',
    'POLICE_DISPATCHED': 'Police called',
  };

  final ApiService _api = ApiService();
  final TextEditingController _notesController = TextEditingController();

  TheftIncident? _incident;
  Map<String, String> _imageHeaders = const {};
  bool _loading = false;
  bool _busy = false;
  String? _error;
  bool _resolveFormOpen = false;
  String? _selectedResolution;

  @override
  void initState() {
    super.initState();
    _incident = widget.initialIncident;
    _api.authHeaders().then((h) {
      if (mounted) setState(() => _imageHeaders = h);
    });
    if (_incident == null) _reload();
  }

  @override
  void dispose() {
    _notesController.dispose();
    super.dispose();
  }

  Future<void> _reload() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final inc = await _api.findTheftIncident(widget.incidentId);
      if (!mounted) return;
      setState(() {
        _incident = inc ?? _incident;
        _error = inc == null ? 'Incident ${widget.incidentId} was not found on the edge server.' : null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = 'Could not load the incident: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _acknowledge() async {
    setState(() => _busy = true);
    try {
      final updated = await _api.acknowledgeTheftIncident(widget.incidentId);
      if (!mounted) return;
      setState(() => _incident = updated);
      _toast('Incident acknowledged', context.palette.live);
    } catch (e) {
      _toast('Acknowledge failed: $e', context.palette.alert);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _resolve() async {
    final resolution = _selectedResolution;
    if (resolution == null) return;
    setState(() => _busy = true);
    try {
      final updated = await _api.resolveTheftIncident(
        widget.incidentId,
        resolution: resolution,
        notes: _notesController.text,
      );
      if (!mounted) return;
      setState(() {
        _incident = updated;
        _resolveFormOpen = false;
      });
      _toast('Incident resolved: ${_resolutions[resolution]}', context.palette.live);
    } catch (e) {
      _toast('Resolve failed: $e', context.palette.alert);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _holdPushes(TheftIncident inc, int minutes) async {
    setState(() => _busy = true);
    try {
      final until = await _api.holdCameraPushes(inc.cameraId, durationMinutes: minutes);
      _toast(
        until == null
            ? 'Phone pushes resumed for ${inc.cameraName}'
            : 'Phone pushes from ${inc.cameraName} held until ${DateFormat('HH:mm').format(until)} (alerts still logged)',
        context.palette.live,
      );
    } catch (e) {
      _toast('Could not change pushes for this camera: $e', context.palette.alert);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _openLive(TheftIncident inc) {
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (context) => LiveViewScreen(camera: CameraFeed.stub(id: inc.cameraId, name: inc.cameraName, location: inc.department)),
      ),
    );
  }

  void _toast(String message, Color color) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message), backgroundColor: color));
  }

  Color _severityColor(String severity) {
    switch (severity.toUpperCase()) {
      case 'HIGH':
        return context.palette.alert;
      case 'MEDIUM':
        return context.palette.warning;
      default:
        return context.palette.accent;
    }
  }

  @override
  Widget build(BuildContext context) {
    final inc = _incident;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Loss prevention alert', style: TextStyle(fontWeight: FontWeight.bold)),
        actions: [
          IconButton(icon: const Icon(Icons.refresh), tooltip: 'Reload', onPressed: _loading ? null : _reload),
        ],
      ),
      body: inc == null
          ? Center(
              child: _loading
                  ? CircularProgressIndicator(color: context.palette.accent)
                  : Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(_error ?? 'Incident not loaded.', textAlign: TextAlign.center, style: TextStyle(color: context.palette.dim(0.70))),
                    ),
            )
          : RefreshIndicator(
              onRefresh: _reload,
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(_error!, style: TextStyle(color: context.palette.warning, fontSize: 12)),
                    ),
                  _buildHeader(inc),
                  const SizedBox(height: 12),
                  _buildEvidence(inc),
                  const SizedBox(height: 12),
                  _buildDetails(inc),
                  const SizedBox(height: 16),
                  _buildActions(inc),
                  if (_resolveFormOpen) ...[
                    const SizedBox(height: 12),
                    _buildResolveForm(),
                  ],
                  const SizedBox(height: 24),
                ],
              ),
            ),
    );
  }

  Widget _buildHeader(TheftIncident inc) {
    final color = _severityColor(inc.severity);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(Icons.policy_outlined, color: color, size: 28),
            const SizedBox(width: 10),
            Expanded(
              child: Text(inc.ruleLabel, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w800)),
            ),
          ],
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 6,
          children: [
            _chip(inc.severity, color),
            _chip(inc.status.replaceAll('_', ' '), inc.isOpen ? context.palette.warning : context.palette.live),
          ],
        ),
        const SizedBox(height: 8),
        Text(
          '${inc.reviewLabel ?? 'Suspicious behaviour for staff review'}. '
          'Check the evidence and live view before approaching anyone.',
          style: TextStyle(color: context.palette.dim(0.60), fontSize: 12),
        ),
      ],
    );
  }

  Widget _chip(String text, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withValues(alpha: 0.6)),
      ),
      child: Text(text, style: TextStyle(color: color, fontSize: 11, fontWeight: FontWeight.bold)),
    );
  }

  Widget _buildEvidence(TheftIncident inc) {
    final url = _api.resolveMediaUrl(inc.evidenceSnapshotUrl);
    Widget placeholder(IconData icon, String text) => Container(
          height: 180,
          alignment: Alignment.center,
          color: context.palette.card,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, color: context.palette.dim(0.38), size: 36),
              const SizedBox(height: 8),
              Text(text, style: TextStyle(color: context.palette.dim(0.54), fontSize: 12)),
            ],
          ),
        );

    return ClipRRect(
      borderRadius: BorderRadius.circular(10),
      child: url == null
          ? placeholder(Icons.image_not_supported_outlined, 'No evidence snapshot for this incident')
          : AspectRatio(
              aspectRatio: 16 / 9,
              child: Container(
                color: Colors.black,
                child: Image.network(
                  url,
                  headers: _imageHeaders,
                  fit: BoxFit.contain,
                  loadingBuilder: (context, child, progress) =>
                      progress == null ? child : Center(child: CircularProgressIndicator(color: context.palette.accent)),
                  errorBuilder: (context, error, stack) => placeholder(Icons.broken_image_outlined, 'Evidence snapshot could not be loaded'),
                ),
              ),
            ),
    );
  }

  Widget _buildDetails(TheftIncident inc) {
    final time = DateFormat('EEE d MMM, HH:mm:ss').format(inc.timestamp);
    final rows = <MapEntry<String, String>>[
      MapEntry('Rule', inc.ruleCode),
      MapEntry('Confidence', '${(inc.confidence * 100).toStringAsFixed(0)}%'),
      MapEntry('Camera', inc.cameraName),
      MapEntry('Time', time),
      if (inc.department.isNotEmpty) MapEntry('Department', inc.department),
      if (inc.shelfZoneId != null) MapEntry('Shelf zone', inc.shelfZoneId!),
      if (inc.guardId != null) MapEntry('Acknowledged by', inc.guardId!),
      if (inc.resolution != null) MapEntry('Resolution', inc.resolution!.replaceAll('_', ' ')),
      if (inc.notes != null) MapEntry('Notes', inc.notes!),
    ];
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: context.palette.card,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.palette.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final r in rows)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(width: 120, child: Text(r.key, style: TextStyle(color: context.palette.dim(0.60), fontSize: 13))),
                  Expanded(child: Text(r.value, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13))),
                ],
              ),
            ),
          if (inc.evidence.isNotEmpty) ...[
            Divider(color: context.palette.border),
            for (final line in inc.evidence)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Text('• $line', style: TextStyle(color: context.palette.dim(0.70), fontSize: 12)),
              ),
          ] else if (inc.evidenceSummary.isNotEmpty) ...[
            Divider(color: context.palette.border),
            Text(inc.evidenceSummary, style: TextStyle(color: context.palette.dim(0.70), fontSize: 12)),
          ],
        ],
      ),
    );
  }

  Widget _buildActions(TheftIncident inc) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ElevatedButton.icon(
          style: ElevatedButton.styleFrom(
            backgroundColor: context.palette.accent,
            foregroundColor: context.palette.onAccent,
            minimumSize: const Size.fromHeight(50),
          ),
          icon: const Icon(Icons.videocam),
          label: const Text('View live', style: TextStyle(fontWeight: FontWeight.bold)),
          onPressed: inc.cameraId.isEmpty ? null : () => _openLive(inc),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: OutlinedButton.icon(
                style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(46)),
                icon: const Icon(Icons.task_alt),
                label: const Text('Acknowledge'),
                onPressed: !_busy && inc.canAcknowledge ? _acknowledge : null,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: OutlinedButton.icon(
                style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(46)),
                icon: const Icon(Icons.check_circle_outline),
                label: const Text('Resolve'),
                onPressed: !_busy && inc.canResolve ? () => setState(() => _resolveFormOpen = !_resolveFormOpen) : null,
              ),
            ),
          ],
        ),
        const SizedBox(height: 4),
        Row(
          children: [
            Expanded(
              child: TextButton.icon(
                icon: const Icon(Icons.notifications_paused_outlined, size: 18),
                label: const Text('Hold pushes 30 min', style: TextStyle(fontSize: 12)),
                onPressed: _busy || inc.cameraId.isEmpty ? null : () => _holdPushes(inc, 30),
              ),
            ),
            Expanded(
              child: TextButton.icon(
                icon: const Icon(Icons.notifications_active_outlined, size: 18),
                label: const Text('Resume pushes', style: TextStyle(fontSize: 12)),
                onPressed: _busy || inc.cameraId.isEmpty ? null : () => _holdPushes(inc, 0),
              ),
            ),
          ],
        ),
      ],
    );
  }

  /// Inline form (no modal dialog) so the operator can review before submitting.
  Widget _buildResolveForm() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: context.palette.card,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: context.palette.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Outcome', style: TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final e in _resolutions.entries)
                ChoiceChip(
                  label: Text(e.value),
                  selected: _selectedResolution == e.key,
                  onSelected: (sel) => setState(() => _selectedResolution = sel ? e.key : null),
                ),
            ],
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _notesController,
            maxLines: 2,
            decoration: const InputDecoration(labelText: 'Notes (optional)', border: OutlineInputBorder()),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(onPressed: _busy ? null : () => setState(() => _resolveFormOpen = false), child: const Text('Cancel')),
              const SizedBox(width: 8),
              ElevatedButton(
                onPressed: !_busy && _selectedResolution != null ? _resolve : null,
                child: const Text('Submit'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
