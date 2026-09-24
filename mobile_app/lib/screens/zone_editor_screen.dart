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
  String? _selectedCameraId;
  bool _isLoading = false;
  String? _loadError;
  ZoneType _selectedTool = ZoneType.restrictedArea;
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
    setState(() {
      _isLoading = true;
      _loadError = null;
    });
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
      if (mounted) setState(() => _loadError = 'Cannot reach the edge server: $e');
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  Future<void> _fetchZonesForSelectedCamera() async {
    final cameraId = _selectedCameraId;
    if (cameraId == null) return;
    try {
      final zones = await ApiService().fetchCameraZones(cameraId);
      if (mounted) {
        setState(() {
          _loadError = null;
          _savedZones
            ..clear()
            ..addAll(zones);
        });
      }
    } catch (e) {
      // Show nothing rather than sample geometry: a zone list that is not
      // from the server would mislead the operator.
      if (mounted) {
        setState(() {
          _savedZones.clear();
          _loadError = 'Could not load zones: $e';
        });
      }
    }
  }

  void _startNewZone() {
    final cameraId = _selectedCameraId;
    if (cameraId == null) return;
    setState(() {
      _activeDraftZone = ZoneConfig(
        id: 'zone_${DateTime.now().millisecondsSinceEpoch}',
        cameraId: cameraId,
        name: 'New ${_selectedTool.label}',
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
    final cameraId = _selectedCameraId;
    if (_activeDraftZone == null || cameraId == null) return;
    final draft = _activeDraftZone!;
    setState(() {
      _activeDraftZone = null;
      _selectedVertexIndex = null;
    });

    try {
      final saved = await ApiService().saveCameraZone(cameraId, draft);
      if (!mounted) return;
      setState(() => _savedZones.add(saved));
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Zone saved on the edge server'), backgroundColor: context.palette.live),
      );
    } catch (e) {
      if (!mounted) return;
      // Keep the draft open so the operator can retry instead of losing it.
      setState(() => _activeDraftZone = draft);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Zone not saved: $e'), backgroundColor: context.palette.alert),
      );
    }
  }

  Future<void> _deleteZone(int index) async {
    final cameraId = _selectedCameraId;
    if (cameraId == null) return;
    final zone = _savedZones[index];
    try {
      await ApiService().deleteCameraZone(cameraId, zone.id);
      if (mounted) setState(() => _savedZones.remove(zone));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Zone not deleted: $e'), backgroundColor: context.palette.alert),
      );
    }
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
      backgroundColor: context.palette.background,
      appBar: AppBar(
        title: const Text('Camera Zones & Privacy Masks', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            tooltip: 'Reload zones from the edge server',
            icon: Icon(Icons.refresh_rounded, color: context.palette.accent),
            onPressed: _loadCamerasAndZones,
          ),
        ],
      ),
      body: Row(
        children: [
          Container(
            width: 320,
            decoration: BoxDecoration(
              color: context.palette.card,
              border: Border(right: BorderSide(color: context.palette.border, width: 1)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.all(12),
                  child: DropdownButtonFormField<String>(
                    isExpanded: true,
                    initialValue: _selectedCameraId,
                    decoration: InputDecoration(
                      labelText: 'Select Camera Stream',
                      filled: true,
                      fillColor: context.palette.background,
                      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                    ),
                    hint: const Text('No cameras available', style: TextStyle(fontSize: 12)),
                    items: _cameras
                        .map((c) => DropdownMenuItem(value: c.id, child: Text('${c.name} (${c.location})', style: const TextStyle(fontSize: 12))))
                        .toList(),
                    onChanged: (val) {
                      if (val != null) {
                        setState(() => _selectedCameraId = val);
                        _fetchZonesForSelectedCamera();
                      }
                    },
                  ),
                ),
                if (_isLoading) const LinearProgressIndicator(minHeight: 2),
                if (_loadError != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                    child: Text(_loadError!, style: TextStyle(color: context.palette.alert, fontSize: 11)),
                  ),
                Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Text('DRAWING TOOL', style: TextStyle(color: context.palette.dim(0.54), fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  child: Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      _buildToolChip(ZoneType.restrictedArea, Icons.lock_outline_rounded, context.palette.alert),
                      _buildToolChip(ZoneType.tripwire, Icons.timeline_rounded, context.palette.warning),
                      _buildToolChip(ZoneType.privacyMask, Icons.blur_on_rounded, context.palette.muted),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                if (_selectedTool == ZoneType.privacyMask) ...[
                  Padding(
                    padding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                    child: Text('MASK MODE', style: TextStyle(color: context.palette.dim(0.54), fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
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
                          onPressed: _activeDraftZone == null && _selectedCameraId != null ? _startNewZone : null,
                          icon: const Icon(Icons.add, size: 16),
                          label: const Text('New Zone', style: TextStyle(fontSize: 12)),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: context.palette.accent,
                            foregroundColor: context.palette.onAccent,
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                          ),
                        ),
                      ),
                      if (_activeDraftZone != null) ...[
                        const SizedBox(width: 8),
                        IconButton(
                          tooltip: 'Done / Save Zone',
                          icon: Icon(Icons.check_circle_rounded, color: context.palette.live),
                          onPressed: _saveDraftZone,
                        ),
                        IconButton(
                          tooltip: 'Cancel Draft',
                          icon: Icon(Icons.cancel_rounded, color: context.palette.alert),
                          onPressed: () => setState(() => _activeDraftZone = null),
                        ),
                      ],
                    ],
                  ),
                ),
                Divider(height: 24, color: context.palette.border),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                  child: Text('CONFIGURED ZONES (${_savedZones.length})', style: TextStyle(color: context.palette.dim(0.54), fontSize: 11, fontWeight: FontWeight.bold, letterSpacing: 1)),
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
                          color: context.palette.background,
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: context.palette.border),
                        ),
                        child: Row(
                          children: [
                            Switch(
                              value: zone.enabled,
                              activeColor: context.palette.live,
                              onChanged: (val) {
                                setState(() => zone.enabled = val);
                                final cameraId = _selectedCameraId;
                                if (cameraId != null) ApiService().saveCameraZone(cameraId, zone);
                              },
                            ),
                            const SizedBox(width: 8),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(zone.name, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 12)),
                                  Text('${zone.zoneType.label} • ${zone.polygonPoints.length} pts',
                                      style: TextStyle(color: context.palette.dim(0.54), fontSize: 10)),
                                ],
                              ),
                            ),
                            IconButton(
                              icon: Icon(Icons.delete_outline, size: 18, color: context.palette.dim(0.38)),
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

  Widget _buildToolChip(ZoneType type, IconData icon, Color color) {
    final label = type.label;
    final isSelected = _selectedTool == type;
    return ChoiceChip(
      label: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: isSelected ? context.palette.onAccent : color),
          const SizedBox(width: 6),
          Text(label, style: TextStyle(fontSize: 11, color: isSelected ? context.palette.onAccent : context.palette.text)),
        ],
      ),
      selected: isSelected,
      selectedColor: color,
      backgroundColor: context.palette.background,
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
      label: Text(label, style: TextStyle(fontSize: 11, color: isSelected ? context.palette.onAccent : context.palette.text)),
      selected: isSelected,
      selectedColor: context.palette.accent,
      backgroundColor: context.palette.background,
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
