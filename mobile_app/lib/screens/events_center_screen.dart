import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../core/theme/app_theme.dart';
import '../models/security_event.dart';
import '../services/api_service.dart';
import 'clip_player_screen.dart';

class EventsCenterScreen extends StatefulWidget {
  const EventsCenterScreen({Key? key}) : super(key: key);

  @override
  State<EventsCenterScreen> createState() => _EventsCenterScreenState();
}

class _EventsCenterScreenState extends State<EventsCenterScreen> {
  final ApiService _apiService = ApiService();
  List<SecurityEvent> _events = [];
  bool _isLoading = true;
  String _selectedFilter = 'ALL';

  @override
  void initState() {
    super.initState();
    _loadEvents();
  }

  Future<void> _loadEvents() async {
    setState(() => _isLoading = true);
    try {
      final list = await _apiService.getEvents(
        severity: _selectedFilter == 'ALL' ? null : _selectedFilter,
      );
      setState(() {
        _events = list;
        _isLoading = false;
      });
    } catch (_) {
      setState(() => _isLoading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: context.palette.background,
      appBar: AppBar(
        title: const Text('AI INCIDENT & ALERTS CENTER', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            icon: Icon(Icons.refresh_rounded, color: context.palette.accent),
            onPressed: _loadEvents,
          ),
        ],
      ),
      body: Column(
        children: [
          // Filter Chips
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
            color: context.palette.card,
            child: Row(
              children: [
                _buildFilterChip('ALL'),
                const SizedBox(width: 8),
                _buildFilterChip('HIGH', color: context.palette.alert),
                const SizedBox(width: 8),
                _buildFilterChip('WARNING', color: context.palette.warning),
                const SizedBox(width: 8),
                _buildFilterChip('INFO', color: context.palette.accent),
              ],
            ),
          ),
          Expanded(
            child: _isLoading
                ? Center(child: CircularProgressIndicator(color: context.palette.accent))
                : _events.isEmpty
                    ? Center(
                        child: Text('No AI security incidents found for this filter.', style: TextStyle(color: context.palette.dim(0.54))),
                      )
                    : ListView.builder(
                        padding: const EdgeInsets.all(16),
                        itemCount: _events.length,
                        itemBuilder: (context, index) {
                          final event = _events[index];
                          return _buildEventCard(event);
                        },
                      ),
          ),
        ],
      ),
    );
  }

  Widget _buildFilterChip(String label, {Color? color}) {
    final isSelected = _selectedFilter == label;
    return GestureDetector(
      onTap: () {
        setState(() => _selectedFilter = label);
        _loadEvents();
      },
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: isSelected ? (color ?? context.palette.accent) : context.palette.hairline(0.10),
          borderRadius: BorderRadius.circular(20),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: isSelected ? context.palette.onAccent : context.palette.dim(0.70),
            fontSize: 11,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    );
  }

  Widget _buildEventCard(SecurityEvent event) {
    final timeStr = DateFormat('yyyy-MM-dd  HH:mm:ss').format(event.timestamp);

    return Card(
      color: context.palette.card,
      margin: const EdgeInsets.only(bottom: 12),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: BorderSide(
          color: event.isHigh ? context.palette.alert.withValues(alpha: 0.5) : context.palette.border,
        ),
      ),
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        leading: Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: event.isHigh ? context.palette.alert.withValues(alpha: 0.2) : context.palette.accent.withValues(alpha: 0.2),
            shape: BoxShape.circle,
          ),
          child: Icon(
            event.isHigh ? Icons.warning_rounded : Icons.shield_rounded,
            color: event.isHigh ? context.palette.alert : context.palette.accent,
          ),
        ),
        title: Text(
          event.eventType.replaceAll('_', ' '),
          style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
        ),
        subtitle: Text(
          '${event.location} (${event.cameraName})\n$timeStr • Conf: ${(event.confidence * 100).toStringAsFixed(0)}%',
          style: TextStyle(color: context.palette.dim(0.60), fontSize: 11),
        ),
        trailing: ElevatedButton(
          style: ElevatedButton.styleFrom(
            backgroundColor: context.palette.accent,
            foregroundColor: context.palette.onAccent,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          ),
          child: const Text('Play Clip', style: TextStyle(fontSize: 11, fontWeight: FontWeight.bold)),
          onPressed: () {
            Navigator.push(
              context,
              MaterialPageRoute(builder: (context) => ClipPlayerScreen(event: event)),
            );
          },
        ),
      ),
    );
  }
}
