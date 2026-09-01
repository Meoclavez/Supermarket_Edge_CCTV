import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import 'multi_cam_grid_screen.dart';
import 'dvr_playback_screen.dart';
import 'events_center_screen.dart';
import 'zone_editor_screen.dart';
import 'clip_archives_screen.dart';
import 'storage_health_screen.dart';
import 'settings_screen.dart';
import '../core/error_recovery.dart';

enum NavSection {
  liveGrid,
  dvrTimeline,
  eventsCenter,
  zoneEditor,
  clipArchives,
  storageHealth,
  settings,
}

class AppShell extends StatefulWidget {
  final NavSection initialSection;
  const AppShell({Key? key, this.initialSection = NavSection.liveGrid}) : super(key: key);

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  late int _currentIndex;
  bool _isSidebarCollapsed = false;

  final List<NavSectionItem> _navItems = const [
    NavSectionItem(
      section: NavSection.liveGrid,
      label: 'Live Grid Wall',
      icon: Icons.grid_view_rounded,
      selectedIcon: Icons.grid_view_rounded,
      badgeCount: 0,
    ),
    NavSectionItem(
      section: NavSection.dvrTimeline,
      label: '24/7 DVR Playback',
      icon: Icons.history_rounded,
      selectedIcon: Icons.history_toggle_off_rounded,
      badgeCount: 0,
    ),
    NavSectionItem(
      section: NavSection.eventsCenter,
      label: 'AI Incident Center',
      icon: Icons.notifications_active_outlined,
      selectedIcon: Icons.notifications_active_rounded,
      badgeCount: 3,
      badgeColor: AppTheme.emergencyRed,
    ),
    NavSectionItem(
      section: NavSection.zoneEditor,
      label: 'Zone & Mask Editor',
      icon: Icons.draw_outlined,
      selectedIcon: Icons.draw_rounded,
      badgeCount: 0,
    ),
    NavSectionItem(
      section: NavSection.clipArchives,
      label: 'Incident Archives',
      icon: Icons.video_library_outlined,
      selectedIcon: Icons.video_library_rounded,
      badgeCount: 0,
    ),
    NavSectionItem(
      section: NavSection.storageHealth,
      label: 'Storage & Health',
      icon: Icons.storage_rounded,
      selectedIcon: Icons.storage_rounded,
      badgeCount: 0,
    ),
    NavSectionItem(
      section: NavSection.settings,
      label: 'Settings',
      icon: Icons.settings_outlined,
      selectedIcon: Icons.settings_rounded,
      badgeCount: 0,
    ),
  ];

  @override
  void initState() {
    super.initState();
    _currentIndex = widget.initialSection.index;
  }

  Widget _buildBody() {
    Widget child;
    switch (NavSection.values[_currentIndex]) {
      case NavSection.liveGrid:
        child = const MultiCamGridScreen();
        break;
      case NavSection.dvrTimeline:
        child = const DVRPlaybackScreen();
        break;
      case NavSection.eventsCenter:
        child = const EventsCenterScreen();
        break;
      case NavSection.zoneEditor:
        child = const ZoneEditorScreen();
        break;
      case NavSection.clipArchives:
        child = const ClipArchivesScreen();
        break;
      case NavSection.storageHealth:
        child = const StorageHealthScreen();
        break;
      case NavSection.settings:
        child = const SettingsScreen();
        break;
    }
    
    return Column(
      children: [
        ListenableBuilder(
          listenable: ConnectionMonitor(),
          builder: (context, _) {
            return OfflineIndicatorBanner(state: ConnectionMonitor().state);
          },
        ),
        Expanded(child: ErrorBoundary(child: child)),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        if (constraints.maxWidth < 600) {
          return _buildMobileLayout();
        } else if (constraints.maxWidth <= 1100) {
          return _buildTabletLayout();
        } else {
          return _buildDesktopLayout();
        }
      },
    );
  }

  Widget _buildMobileLayout() {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      body: SafeArea(child: _buildBody()),
      bottomNavigationBar: Container(
        decoration: const BoxDecoration(
          color: AppTheme.cardSurface,
          border: Border(top: BorderSide(color: AppTheme.borderHighlight, width: 1)),
        ),
        child: NavigationBar(
          selectedIndex: _currentIndex,
          onDestinationSelected: (index) => setState(() => _currentIndex = index),
          backgroundColor: Colors.transparent,
          indicatorColor: AppTheme.cyberBlue.withOpacity(0.25),
          elevation: 0,
          labelBehavior: NavigationDestinationLabelBehavior.onlyShowSelected,
          destinations: _navItems.map((item) {
            return NavigationDestination(
              icon: _buildBadge(item, Icon(item.icon, color: Colors.white70)),
              selectedIcon: _buildBadge(item, Icon(item.selectedIcon, color: AppTheme.cyberBlue)),
              label: item.label,
            );
          }).toList(),
        ),
      ),
    );
  }

  Widget _buildTabletLayout() {
    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _currentIndex,
            onDestinationSelected: (index) => setState(() => _currentIndex = index),
            backgroundColor: AppTheme.cardSurface,
            indicatorColor: AppTheme.cyberBlue.withOpacity(0.2),
            leading: const Padding(
              padding: EdgeInsets.symmetric(vertical: 16),
              child: Icon(Icons.shield_rounded, color: AppTheme.cyberBlue, size: 32),
            ),
            labelType: NavigationRailLabelType.selected,
            destinations: _navItems.map((item) {
              return NavigationRailDestination(
                icon: Tooltip(
                  message: item.label,
                  child: _buildBadge(item, Icon(item.icon, color: Colors.white70)),
                ),
                selectedIcon: Tooltip(
                  message: item.label,
                  child: _buildBadge(item, Icon(item.selectedIcon, color: AppTheme.cyberBlue)),
                ),
                label: Text(item.label, style: const TextStyle(fontSize: 11)),
              );
            }).toList(),
          ),
          const VerticalDivider(width: 1, color: AppTheme.borderHighlight),
          Expanded(child: _buildBody()),
        ],
      ),
    );
  }

  Widget _buildDesktopLayout() {
    final double sidebarWidth = _isSidebarCollapsed ? 80 : 260;

    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      body: Row(
        children: [
          AnimatedContainer(
            duration: const Duration(milliseconds: 200),
            width: sidebarWidth,
            decoration: const BoxDecoration(
              color: AppTheme.cardSurface,
              border: Border(right: BorderSide(color: AppTheme.borderHighlight, width: 1)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 20),
                  child: Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.all(8),
                        decoration: BoxDecoration(
                          color: AppTheme.cyberBlue.withOpacity(0.15),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: const Icon(Icons.shield_rounded, color: AppTheme.cyberBlue, size: 24),
                      ),
                      if (!_isSidebarCollapsed) ...[
                        const SizedBox(width: 12),
                        const Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('EDGE AI CCTV', style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 14, letterSpacing: 0.8)),
                              Text('N100 + Hailo-8 NPU', style: TextStyle(color: AppTheme.liveGreen, fontSize: 11, fontWeight: FontWeight.w500)),
                            ],
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const Divider(height: 1, color: AppTheme.borderHighlight),
                const SizedBox(height: 12),
                Expanded(
                  child: ListView.builder(
                    itemCount: _navItems.length,
                    itemBuilder: (context, index) {
                      final item = _navItems[index];
                      final isSelected = _currentIndex == index;

                      return Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                        child: InkWell(
                          onTap: () => setState(() => _currentIndex = index),
                          borderRadius: BorderRadius.circular(8),
                          child: Container(
                            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                            decoration: BoxDecoration(
                              color: isSelected ? AppTheme.cyberBlue.withOpacity(0.15) : Colors.transparent,
                              borderRadius: BorderRadius.circular(8),
                              border: isSelected ? Border.all(color: AppTheme.cyberBlue.withOpacity(0.4)) : null,
                            ),
                            child: Row(
                              children: [
                                _buildBadge(
                                  item,
                                  Icon(
                                    isSelected ? item.selectedIcon : item.icon,
                                    color: isSelected ? AppTheme.cyberBlue : Colors.white70,
                                    size: 22,
                                  ),
                                ),
                                if (!_isSidebarCollapsed) ...[
                                  const SizedBox(width: 14),
                                  Expanded(
                                    child: Text(
                                      item.label,
                                      style: TextStyle(
                                        color: isSelected ? Colors.white : Colors.white70,
                                        fontWeight: isSelected ? FontWeight.bold : FontWeight.normal,
                                        fontSize: 13,
                                      ),
                                    ),
                                  ),
                                ],
                              ],
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                ),
                const Divider(height: 1, color: AppTheme.borderHighlight),
                InkWell(
                  onTap: () => setState(() => _isSidebarCollapsed = !_isSidebarCollapsed),
                  child: Container(
                    padding: const EdgeInsets.all(16),
                    child: Row(
                      mainAxisAlignment: _isSidebarCollapsed ? MainAxisAlignment.center : MainAxisAlignment.start,
                      children: [
                        Icon(_isSidebarCollapsed ? Icons.chevron_right_rounded : Icons.chevron_left_rounded, color: Colors.white54),
                        if (!_isSidebarCollapsed) ...[
                          const SizedBox(width: 12),
                          const Text('Collapse Sidebar', style: TextStyle(color: Colors.white54, fontSize: 12)),
                        ],
                      ],
                    ),
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

  Widget _buildBadge(NavSectionItem item, Widget iconWidget) {
    if (item.badgeCount == 0) return iconWidget;
    return Badge(
      label: Text('${item.badgeCount}'),
      backgroundColor: item.badgeColor ?? AppTheme.cyberBlue,
      child: iconWidget,
    );
  }
}

class NavSectionItem {
  final NavSection section;
  final String label;
  final IconData icon;
  final IconData selectedIcon;
  final int badgeCount;
  final Color? badgeColor;

  const NavSectionItem({
    required this.section,
    required this.label,
    required this.icon,
    required this.selectedIcon,
    this.badgeCount = 0,
    this.badgeColor,
  });
}
