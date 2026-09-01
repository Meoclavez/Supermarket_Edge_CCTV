import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

class DiscoveredEdgeNode {
  final String ip;
  final int port;
  final String serverName;
  final String version;
  final bool tlsEnabled;
  final int pingLatencyMs;

  DiscoveredEdgeNode({
    required this.ip,
    required this.port,
    required this.serverName,
    required this.version,
    required this.tlsEnabled,
    required this.pingLatencyMs,
  });

  String get baseUrl => '${tlsEnabled ? "https" : "http"}://$ip:$port';
}

class EdgeDiscoveryService {
  static final EdgeDiscoveryService _instance = EdgeDiscoveryService._internal();
  factory EdgeDiscoveryService() => _instance;
  EdgeDiscoveryService._internal();

  /// Scans local network for Edge CCTV server via mDNS and parallel subnet ping
  Future<List<DiscoveredEdgeNode>> discoverEdgeServers({
    Duration timeout = const Duration(seconds: 3),
  }) async {
    if (kIsWeb) return []; // Discovery not supported on Web via dart:io
    
    final List<DiscoveredEdgeNode> discovered = [];

    // 1. Scan via Multicast DNS query on 224.0.0.251:5353
    try {
      final mdnsResult = await _discoverViaMdns(timeout);
      discovered.addAll(mdnsResult);
    } catch (e) {
      debugPrint('mDNS probe notice: $e');
    }

    // 2. If mDNS returned no results (e.g. Wi-Fi client isolation), fallback to parallel subnet probe
    if (discovered.isEmpty) {
      final subnetResult = await _probeLocalSubnet(timeout);
      discovered.addAll(subnetResult);
    }

    return discovered;
  }

  Future<List<DiscoveredEdgeNode>> _discoverViaMdns(Duration timeout) async {
    final List<DiscoveredEdgeNode> results = [];
    RawDatagramSocket? socket;

    try {
      socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
      socket.broadcastEnabled = true;

      final queryPacket = _buildMdnsPtrQuery('_cctv-edge._tcp.local');
      socket.send(queryPacket, InternetAddress('224.0.0.251'), 5353);

      final completer = Completer<List<DiscoveredEdgeNode>>();
      final stopwatch = Stopwatch()..start();

      socket.listen((RawSocketEvent event) {
        if (event == RawSocketEvent.read) {
          final datagram = socket?.receive();
          if (datagram != null) {
            final node = _parseMdnsResponse(datagram.data, datagram.address.address, stopwatch.elapsedMilliseconds);
            if (node != null && !results.any((r) => r.ip == node.ip)) {
              results.add(node);
            }
          }
        }
      });

      Future.delayed(timeout, () {
        if (!completer.isCompleted) completer.complete(results);
      });

      return await completer.future;
    } catch (e) {
      debugPrint('Error during mDNS scan: $e');
      return results;
    } finally {
      socket?.close();
    }
  }

  Future<List<DiscoveredEdgeNode>> _probeLocalSubnet(Duration timeout) async {
    final List<DiscoveredEdgeNode> nodes = [];
    try {
      final interfaces = await NetworkInterface.list(type: InternetAddressType.IPv4);
      String? localSubnetPrefix;

      for (var iface in interfaces) {
        for (var addr in iface.addresses) {
          if (!addr.isLoopback && addr.address.startsWith('192.168.')) {
            final parts = addr.address.split('.');
            localSubnetPrefix = '${parts[0]}.${parts[1]}.${parts[2]}';
            break;
          }
        }
      }

      if (localSubnetPrefix == null) return nodes;

      final futures = <Future>[];
      for (int i = 1; i <= 254; i++) {
        final targetIp = '$localSubnetPrefix.$i';
        futures.add(() async {
          try {
            final sw = Stopwatch()..start();
            final res = await http.get(
              Uri.parse('http://$targetIp:8000/api/v1/health'),
            ).timeout(const Duration(milliseconds: 700));

            if (res.statusCode == 200) {
              final data = jsonDecode(res.body);
              if (data['status'] == 'healthy' || data.containsKey('hardware')) {
                nodes.add(DiscoveredEdgeNode(
                  ip: targetIp,
                  port: 8000,
                  serverName: 'Edge CCTV Core',
                  version: '1.2.0',
                  tlsEnabled: false,
                  pingLatencyMs: sw.elapsedMilliseconds,
                ));
              }
            }
          } catch (_) {}
        }());
      }

      await Future.wait(futures);
    } catch (e) {
      debugPrint('Subnet probe notice: $e');
    }
    return nodes;
  }

  List<int> _buildMdnsPtrQuery(String serviceType) {
    final buffer = <int>[
      0x00, 0x00,
      0x00, 0x00,
      0x00, 0x01,
      0x00, 0x00,
      0x00, 0x00,
      0x00, 0x00,
    ];

    for (final label in serviceType.split('.')) {
      if (label.isEmpty) continue;
      buffer.add(label.length);
      buffer.addAll(utf8.encode(label));
    }
    buffer.add(0x00);
    buffer.addAll([0x00, 0x0c]);
    buffer.addAll([0x00, 0x01]);
    return buffer;
  }

  DiscoveredEdgeNode? _parseMdnsResponse(List<int> data, String senderIp, int latency) {
    try {
      final text = String.fromCharCodes(data);
      if (text.contains('Edge-CCTV') || text.contains('_cctv-edge')) {
        return DiscoveredEdgeNode(
          ip: senderIp,
          port: 8000,
          serverName: 'Intel N100 Edge Core',
          version: '1.2.0',
          tlsEnabled: true,
          pingLatencyMs: latency,
        );
      }
    } catch (_) {}
    return null;
  }
}
