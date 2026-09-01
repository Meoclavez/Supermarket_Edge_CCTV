import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import '../models/camera_feed.dart';
import '../models/zone_model.dart';
import '../services/api_service.dart';
import '../widgets/zone_canvas_painter.dart';

class ZoneEditorScreen extends StatefulWidget {
  const ZoneEditorScreen({Key? key}) : super(key: key);

  @override
  State<ZoneEditorScreen> createState() => _ZoneEditorScreenState();
}

class _ZoneEditorScreenState extends State<ZoneEditorScreen> {
  List<CameraFeed> _cameras = [];
  String _selectedCameraId = 'cam_01';
  bool _isLoading = false;
  ZoneType _selectedTool = ZoneType.intrusion;
  TripwireDirection _tripwireDir = TripwireDirection.bidirectional;
  MaskMode _maskMode = MaskMode.blackout;

  final List<ZoneConfig> _savedZones = [];
  ZoneConfig? _activeDraftZone;
  int? _selectedVertexIndex;
  int? _hoveredVertexIndex;
  Offset? _activeMousePosition;

  @override
  void initState() {
    super.initState();
    _loadCamerasAndZones();
  }

  Future<void> _loadCamerasAndZones() async {
    setState(() => _isLoading = true);
    try {
      final cams = await ApiService().getCameras();
      setState(() {
        _cameras = cams;
        if (cams.isNotEmpty) {
          _selectedCameraId = cams.first.id;
        }
      });
      await _fetchZonesForSelectedCamera();
    } catch (e) {
      _loadSampleZones();
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  Future<void> _fetchZonesForSelectedCamera() async {
    try {
      final zones = await ApiService().fetchCameraZones(_selectedCameraId);
      if (mounted) {
        setState(() {
          _savedZones.clear();
          _savedZones.addAll(zones);
        });
      }
    } catch (e) {
      // Fall back to sample if offline
      if (_savedZones.isEmpty) {
        _loadSampleZones();
      }
    }
  }

  void _loadSampleZones() {
    _savedZones.addAll([
      ZoneConfig(
        id: 'zone_1',
        cameraId: 'cam_01',
        name: 'Driveway Intrusion Polygon',
        zoneType: ZoneType.intrusion,
        polygonPoints: [
          Point2D(x: 0.15, y: 0.35),
          Point2D(x: 0.65, y: 0.30),
          Point2D(x: 0.85, y: 0.75),
          Point2D(x: 0.20, y: 0.80),
        ],
      ),
      ZoneConfig(
        id: 'zone_2',
        cameraId: 'cam_01',
        name: 'Front Gate Tripwire',
        zoneType: ZoneType.tripwire,
        lineStart: Point2D(x: 0.1, y: 0.2),
        lineEnd: Point2D(x: 0.9, y: 0.2),
        direction: TripwireDirection.aToB,
      ),
      ZoneConfig(
        id: 'zone_3',
        cameraId: 'cam_01',
        name: 'Neighbor Window Mask',
        zoneType: ZoneType.privacyMask,
        polygonPoints: [
          Point2D(x: 0.70, y: 0.05),
          Point2D(x: 0.95, y: 0.05),
          Point2D(x: 0.95, y: 0.25),
          Point2D(x: 0.70, y: 0.25),
        ],
      ),
    ]);
  }

  void _startNewZone() {
    setState(() {
      _activeDraftZone = ZoneConfig(
        id: 'zone_${DateTime.now().millisecondsSinceEpoch}',
        cameraId: _selectedCameraId,
        name: 'New ${_selectedTool.name.toUpperCase()} Zone',
        zoneType: _selectedTool,
        direction: _tripwireDir,
        maskMode: _maskMode,
      );
      _selectedVertexIndex = null;
    });
  }

  void _handleCanvasTap(Offset localPos, Size canvasSize) {
    if (_activeDraftZone == null) return;

    final normalized = _toNormalized(localPos, canvasSize);

    setState(() {
      if (_activeDraftZone!.zoneType == ZoneType.tripwire) {
        if (_activeDraftZone!.lineStart == null) {
          _activeDraftZone!.lineStart = normalized;
        } else if (_activeDraftZone!.lineEnd == null) {
          _activeDraftZone!.lineEnd = normalized;
        }
      } else {
        if (_activeDraftZone!.polygonPoints.length >= 3) {
          final firstPt = _toCanvasOffset(_activeDraftZone!.polygonPoints.first, canvasSize);
          if ((firstPt - localPos).distance < 24) {
            _saveDraftZone();
            return;
          }
        }
        _activeDraftZone!.polygonPoints.add(normalized);
      }
    });
  }

  void _handlePanUpdate(DragUpdateDetails details, Size canvasSize) {
    if (_activeDraftZone == null || _selectedVertexIndex == null) return;

    final normalized = _toNormalized(details.localPosition, canvasSize);
    setState(() {
      if (_activeDraftZone!.zoneType == ZoneType.tripwire) {
        if (_selectedVertexIndex == 0) {
          _activeDraftZone!.lineStart = normalized;
        } else if (_selectedVertexIndex == 1) {
          _activeDraftZone!.lineEnd = normalized;
        }
      } else {
        if (_selectedVertexIndex! < _activeDraftZone!.polygonPoints.length) {
          _activeDraftZone!.polygonPoints[_selectedVertexIndex!] = normalized;
        }
      }
    });
  }

  Future<void> _saveDraftZone() async {
    if (_activeDraftZone == null) return;
    final draft = _activeDraftZone!;
    setState(() {
      _savedZones.add(draft);
      _activeDraftZone = null;
      _selectedVertexIndex = null;
    });

    try {
      await ApiService().saveCameraZone(_selectedCameraId, draft);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Zone saved and synchronized with HailoRT AI pipeline'), backgroundColor: AppTheme.liveGreen),
      );
    } catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Saved locally (Offline): $e'), backgroundColor: AppTheme.warningOrange),
      );
    }
  }

  Future<void> _deleteZone(int index) async {
    final zone = _savedZones[index];
    setState(() => _savedZones.removeAt(index));

    try {
      await ApiService().deleteCameraZone(_selectedCameraId, zone.id);
    } catch (_) {}
  }

  Point2D _toNormalized(Offset local, Size size) {
    return Point2D(
      x: (local.dx / size.width).clamp(0.0, 1.0),
      y: (local.dy / size.height).clamp(0.0, 1.0),
    );
  }

  Offset _toCanvasOffset(Point2D pt, Size size) {
    return Offset(pt.x * size.width, pt.y * size.height);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      appBar: AppBar(
        title: const Text('Interactive Zone & Privacy Mask Canvas', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            tooltip: 'Sync Zones with Backend',
            icon: const Icon(Icons.cloud_upload_outlined, color: AppTheme.cyberBlue),
            onPressed: () async {
              await _fetchZonesForSelectedCamera();
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text('Synchronized ${_savedZones.length} zones with HailoRT edge service.')),
              );
            },
          ),
        ],
      ),
      body: Row(
        children: [
          Container(
            width: 320,
            decoration: const BoxDecoration(
              color: AppTheme.cardSurface,
              border: Border(right: BorderSide(color: AppTheme.borderHighlight, width: 1)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: DropdownButtonFormField<String>(
                    value: _selectedCameraId,
                    decoration: InputDecoration(
                      labelText: 'Select Camera Stream',
                      filled: true,
                      fillColor: AppTheme.darkBackground,
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                    ),
                    items: (_cameras.isNotEmpty
                            ? _cameras.map((c) => DropdownMenuItem(value: c.id, child: Text('${c.name} (${c.location})', style: const TextStyle(fontSize: 12))))
                            : [
                                const DropdownMenuItem(value: 'cam_01', child: Text('Camera 01 - Main Gate', style: TextStyle(fontSize: 12))),
                                const DropdownMenuItem(value: 'cam_02', child: Text('Camera 02 - Backyard Patio', style: TextStyle(fontSize: 12))),
                              ])
                        .toList(),
                    onChanged: (val) {
                      if (val != null) {
                        setState(() => _selectedCameraId = val);
                        _fetchZonesForSelectedCamera();
                      }
                    },
                  ),
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Text('DRAWING TOOL', style: TextStyle(color: Colors.white54, fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  child: Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      _buildToolChip(ZoneType.intrusion, 'Intrusion', Icons.security_rounded, AppTheme.emergencyRed),
                      _buildToolChip(ZoneType.tripwire, 'Tripwire', Icons.timeline_rounded, AppTheme.warningOrange),
                      _buildToolChip(ZoneType.privacyMask, 'Blur & Exclude Area', Icons.blur_on_rounded, Colors.grey),
                      _buildToolChip(ZoneType.door, 'Door ROI', Icons.door_front_door_outlined, AppTheme.cyberBlue),
                      _buildToolChip(ZoneType.package, 'Package Zone', Icons.inventory_2_outlined, AppTheme.liveGreen),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                if (_selectedTool == ZoneType.privacyMask) ...[
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                    child: Text('MASK MODE', style: TextStyle(color: Colors.white54, fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
                  ),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    child: Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        _buildMaskModeChip(MaskMode.blur, 'Blur'),
                        _buildMaskModeChip(MaskMode.blackout, 'Blackout'),
                        _buildMaskModeChip(MaskMode.mosaic, 'Mosaic'),
                      ],
                    ),
                  ),
                  const SizedBox(height: 12),
                ],
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  child: Row(
                    children: [
                      Expanded(
                        child: ElevatedButton.icon(
                          onPressed: _activeDraftZone == null ? _startNewZone : null,
                          icon: const Icon(Icons.add, size: 16),
                          label: const Text('New Zone', style: TextStyle(fontSize: 12)),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: AppTheme.cyberBlue,
                            foregroundColor: Colors.black,
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                          ),
                        ),
                      ),
                      if (_activeDraftZone != null) ...[
                        const SizedBox(width: 8),
                        IconButton(
                          tooltip: 'Done / Save Zone',
                          icon: const Icon(Icons.check_circle_rounded, color: AppTheme.liveGreen),
                          onPressed: _saveDraftZone,
                        ),
                        IconButton(
                          tooltip: 'Cancel Draft',
                          icon: const Icon(Icons.cancel_rounded, color: AppTheme.emergencyRed),
                          onPressed: () => setState(() => _activeDraftZone = null),
                        ),
                      ],
                    ],
                  ),
                ),
                const Divider(height: 24, color: AppTheme.borderHighlight),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Text('CONFIGURED ZONES (${_savedZones.length})', style: const TextStyle(color: Colors.white54, fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
                ),
                Expanded(
                  child: ListView.builder(
                    itemCount: _savedZones.length,
                    itemBuilder: (context, index) {
                      final zone = _savedZones[index];
                      return Container(
                        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: AppTheme.darkBackground,
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: AppTheme.borderHighlight),
                        ),
                        child: Row(
                          children: [
                            Switch(
                              value: zone.enabled,
                              activeColor: AppTheme.liveGreen,
                              onChanged: (val) {
                                setState(() => zone.enabled = val);
                                ApiService().saveCameraZone(_selectedCameraId, zone);
                              },
                            ),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(zone.name, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 12)),
                                  Text('${zone.zoneType.name.toUpperCase()} • ${zone.polygonPoints.length} pts',
                                      style: const TextStyle(color: Colors.white54, fontSize: 10)),
                                ],
                              ),
                            ),
                            IconButton(
                              icon: const Icon(Icons.delete_outline, size: 18, color: Colors.white38),
                              onPressed: () => _deleteZone(index),
                            ),
                          ],
                        ),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
          Expanded(
            child: LayoutBuilder(
              builder: (context, constraints) {
                final canvasSize = Size(constraints.maxWidth, constraints.maxHeight);

                return MouseRegion(
                  onHover: (event) {
                    setState(() {
                      _activeMousePosition = event.localPosition;
                    });
                  },
                  child: GestureDetector(
                    onTapDown: (details) => _handleCanvasTap(details.localPosition, canvasSize),
                    onPanDown: (details) {
                      if (_activeDraftZone == null) return;
                      final local = details.localPosition;
                      if (_activeDraftZone!.zoneType == ZoneType.tripwire) {
                        if (_activeDraftZone!.lineStart != null && (_toCanvasOffset(_activeDraftZone!.lineStart!, canvasSize) - local).distance < 20) {
                          _selectedVertexIndex = 0;
                        } else if (_activeDraftZone!.lineEnd != null && (_toCanvasOffset(_activeDraftZone!.lineEnd!, canvasSize) - local).distance < 20) {
                          _selectedVertexIndex = 1;
                        }
                      } else {
                        for (int i = 0; i < _activeDraftZone!.polygonPoints.length; i++) {
                          if ((_toCanvasOffset(_activeDraftZone!.polygonPoints[i], canvasSize) - local).distance < 20) {
                            _selectedVertexIndex = i;
                            break;
                          }
                        }
                      }
                    },
                    onPanUpdate: (details) => _handlePanUpdate(details, canvasSize),
                    onPanEnd: (_) => setState(() => _selectedVertexIndex = null),
                    child: CustomPaint(
                      size: canvasSize,
                      painter: ZoneCanvasPainter(
                        snapshotImage: null,
                        existingZones: _savedZones,
                        activeDraftZone: _activeDraftZone,
                        selectedVertexIndex: _selectedVertexIndex,
                        hoveredVertexIndex: _hoveredVertexIndex,
                        activeMousePosition: _activeMousePosition,
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildToolChip(ZoneType type, String label, IconData icon, Color color) {
    final isSelected = _selectedTool == type;
    return ChoiceChip(
      label: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: isSelected ? Colors.black : color),
          const SizedBox(width: 6),
          Text(label, style: TextStyle(fontSize: 11, color: isSelected ? Colors.black : Colors.white)),
        ],
      ),
      selected: isSelected,
      selectedColor: color,
      backgroundColor: AppTheme.darkBackground,
      onSelected: (val) {
        if (val) {
          setState(() {
            _selectedTool = type;
            if (_activeDraftZone != null) {
              _activeDraftZone!.zoneType = type;
            }
          });
        }
      },
    );
  }

  Widget _buildMaskModeChip(MaskMode mode, String label) {
    final isSelected = _maskMode == mode;
    return ChoiceChip(
      label: Text(label, style: TextStyle(fontSize: 11, color: isSelected ? Colors.black : Colors.white)),
      selected: isSelected,
      selectedColor: AppTheme.cyberBlue,
      backgroundColor: AppTheme.darkBackground,
      onSelected: (val) {
        if (val) {
          setState(() {
            _maskMode = mode;
            if (_activeDraftZone != null) {
              _activeDraftZone!.maskMode = mode;
            }
          });
        }
      },
    );
  }
}
