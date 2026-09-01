import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';

class ClipArchivesScreen extends StatefulWidget {
  const ClipArchivesScreen({Key? key}) : super(key: key);

  @override
  State<ClipArchivesScreen> createState() => _ClipArchivesScreenState();
}

class _ClipArchivesScreenState extends State<ClipArchivesScreen> {
  final List<Map<String, dynamic>> _archives = [
    {
      'id': 'arch_01',
      'camera_name': 'Front Door Entrance',
      'title': 'Suspicious Package Theft Incident',
      'time_range': 'Today 14:02 - 14:07 (5 min)',
      'size': '42.5 MB',
      'created_at': '2 hours ago'
    },
    {
      'id': 'arch_02',
      'camera_name': 'Living Room',
      'title': 'Kinematic Fall Event Verification',
      'time_range': 'Yesterday 09:15 - 09:20 (5 min)',
      'size': '38.1 MB',
      'created_at': '1 day ago'
    },
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('INCIDENT ARCHIVES & EXPORT MANAGER', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            tooltip: 'Export Custom Time Window',
            icon: const Icon(Icons.download_for_offline_outlined, color: AppTheme.cyberBlue),
            onPressed: () {
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(content: Text('Custom interval export requested.')),
              );
            },
          ),
        ],
      ),
      body: ListView.builder(
        padding: const EdgeInsets.all(16),
        itemCount: _archives.length,
        itemBuilder: (context, index) {
          final arch = _archives[index];
          return Card(
            color: AppTheme.cardSurface,
            margin: const EdgeInsets.only(bottom: 12),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(10),
              side: const BorderSide(color: AppTheme.borderHighlight),
            ),
            child: ListTile(
              leading: const Icon(Icons.video_file_outlined, color: AppTheme.cyberBlue, size: 36),
              title: Text(arch['title'], style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
              subtitle: Text(
                '${arch['camera_name']} • ${arch['time_range']}\nSize: ${arch['size']} • ${arch['created_at']}',
                style: const TextStyle(color: Colors.white60, fontSize: 11),
              ),
              trailing: IconButton(
                icon: const Icon(Icons.file_download_outlined, color: AppTheme.liveGreen),
                onPressed: () {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(content: Text('Downloading ${arch['title']} MP4...')),
                  );
                },
              ),
            ),
          );
        },
      ),
    );
  }
}
