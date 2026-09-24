import 'dart:async';

import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../services/connection_resolver.dart';
import '../services/server_registry.dart';
import '../screens/pairing_screen.dart';

/// Slim bar at the top of the app showing which store is in use, with a menu
/// to switch stores or add one.
class ServerSwitcherBar extends StatelessWidget {
  const ServerSwitcherBar({Key? key}) : super(key: key);

  static const _addValue = '__add__';

  @override
  Widget build(BuildContext context) {
    final registry = ServerRegistry();
    return ListenableBuilder(
      listenable: registry,
      builder: (context, _) {
        final active = registry.active;
        if (active == null) return const SizedBox.shrink();
        final reach = registry.reachabilityOf(active.deviceId);
        final color = switch (reach) {
          ServerReachability.reachable => context.palette.live,
          ServerReachability.resolving || ServerReachability.unknown => context.palette.caution,
          _ => context.palette.alert,
        };
        final note = switch (reach) {
          ServerReachability.differentServer => kDifferentServerMessage,
          ServerReachability.unreachable => 'Not reachable',
          ServerReachability.resolving => 'Connecting...',
          _ => null,
        };
        return Material(
          color: context.palette.card,
          child: PopupMenuButton<String>(
            tooltip: 'Switch store',
            onSelected: (value) async {
              if (value == _addValue) {
                await Navigator.of(context).push(MaterialPageRoute(
                  builder: (ctx) => PairingScreen(onPaired: (_) => Navigator.of(ctx).pop()),
                ));
                return;
              }
              await registry.setActive(value);
              unawaited(registry.resolve(value));
            },
            itemBuilder: (_) => [
              ...registry.servers.map((s) => PopupMenuItem<String>(
                    value: s.deviceId,
                    child: Row(children: [
                      Icon(s.deviceId == registry.activeId ? Icons.check : Icons.store_mall_directory_outlined, size: 18),
                      const SizedBox(width: 8),
                      Expanded(child: Text(s.name, overflow: TextOverflow.ellipsis)),
                    ]),
                  )),
              const PopupMenuDivider(),
              const PopupMenuItem<String>(
                value: _addValue,
                child: Row(children: [Icon(Icons.add, size: 18), SizedBox(width: 8), Text('Add a store')]),
              ),
            ],
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Row(children: [
                Container(width: 8, height: 8, decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    note == null ? active.name : '${active.name}: $note',
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
                  ),
                ),
                Icon(Icons.unfold_more, size: 18, color: context.palette.dim(0.54)),
              ]),
            ),
          ),
        );
      },
    );
  }
}
