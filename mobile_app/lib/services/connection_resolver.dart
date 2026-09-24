import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/server_entry.dart';

/// Shown inline wherever an address answered as some other store's server.
const String kDifferentServerMessage = 'This address is a different Edge CCTV server';

const String kIdentityPath = '/api/v1/device/identity';

/// `GET /api/v1/device/identity` (unauthenticated).
class DeviceIdentity {
  final String deviceId;
  final String deviceName;
  final String? appVersion;
  final int? apiVersion;
  final String? remoteUrl;
  final bool pairing;

  const DeviceIdentity({
    required this.deviceId,
    required this.deviceName,
    this.appVersion,
    this.apiVersion,
    this.remoteUrl,
    this.pairing = false,
  });

  static DeviceIdentity? tryParse(dynamic json) {
    if (json is! Map) return null;
    final id = json['device_id'];
    if (id is! String || !isValidDeviceId(id)) return null;
    final remote = json['remote_url'];
    return DeviceIdentity(
      deviceId: normaliseDeviceId(id),
      deviceName: (json['device_name'] ?? '').toString(),
      appVersion: json['app_version']?.toString(),
      apiVersion: json['api_version'] is int ? json['api_version'] as int : int.tryParse('${json['api_version']}'),
      remoteUrl: remote is String ? normaliseBaseUrl(remote) : null,
      pairing: json['pairing'] == true,
    );
  }
}

enum ProbeOutcome {
  /// Answered with the expected device_id (or any device_id when none was expected).
  match,

  /// Answered as an Edge CCTV server with a different device_id.
  differentServer,

  /// Answered, but not as an Edge CCTV server (or an old server without identity).
  notEdgeServer,

  /// No answer within the timeout.
  unreachable,
}

class IdentityProbe {
  final String url;
  final ProbeOutcome outcome;
  final DeviceIdentity? identity;
  final String? detail;

  const IdentityProbe(this.url, this.outcome, {this.identity, this.detail});

  /// One-line explanation suitable for an inline status row.
  String describe() {
    switch (outcome) {
      case ProbeOutcome.match:
        return 'Reached ${identity?.deviceName.isNotEmpty == true ? identity!.deviceName : 'this server'}';
      case ProbeOutcome.differentServer:
        final other = identity?.deviceName.isNotEmpty == true ? ' ("${identity!.deviceName}")' : '';
        return '$kDifferentServerMessage$other.';
      case ProbeOutcome.notEdgeServer:
        return 'This address answered, but not as an Edge CCTV server${detail != null ? ' ($detail)' : ''}.';
      case ProbeOutcome.unreachable:
        return 'No answer from this address${detail != null ? ' ($detail)' : ''}.';
    }
  }
}

class ResolveResult {
  /// The first candidate (in priority order) that proved it is the expected device.
  final String? url;
  final DeviceIdentity? identity;
  final List<IdentityProbe> probes;

  const ResolveResult(this.url, this.identity, this.probes);

  bool get resolved => url != null;
  List<IdentityProbe> get mismatched => probes.where((p) => p.outcome == ProbeOutcome.differentServer).toList();
}

/// Proves which candidate URL reaches a given server before the app uses it.
///
/// Every candidate is probed in parallel with a short timeout (longer for
/// remote https URLs, which go through a tunnel). A URL is accepted only when
/// the identity endpoint returns the expected `device_id`; an address that
/// answers with another id belongs to a different store and is rejected even
/// though it responds.
class ConnectionResolver {
  final Duration lanTimeout;
  final Duration remoteTimeout;

  const ConnectionResolver({
    this.lanTimeout = const Duration(seconds: 2),
    this.remoteTimeout = const Duration(seconds: 5),
  });

  Future<IdentityProbe> probe(String baseUrl, {String? expectedDeviceId}) async {
    final url = normaliseBaseUrl(baseUrl);
    if (url == null) return IdentityProbe(baseUrl, ProbeOutcome.unreachable, detail: 'not a valid address');
    // http.Client() honours http.runWithClient, which the tests use.
    final client = http.Client();
    try {
      final res = await client
          .get(Uri.parse('$url$kIdentityPath'), headers: const {'Accept': 'application/json'})
          .timeout(isLanUrl(url) ? lanTimeout : remoteTimeout);
      if (res.statusCode != 200) {
        return IdentityProbe(url, ProbeOutcome.notEdgeServer, detail: 'HTTP ${res.statusCode}');
      }
      DeviceIdentity? identity;
      try {
        identity = DeviceIdentity.tryParse(jsonDecode(res.body));
      } catch (_) {
        identity = null;
      }
      if (identity == null) return IdentityProbe(url, ProbeOutcome.notEdgeServer, detail: 'no device identity');
      if (expectedDeviceId != null && identity.deviceId != normaliseDeviceId(expectedDeviceId)) {
        return IdentityProbe(url, ProbeOutcome.differentServer, identity: identity);
      }
      return IdentityProbe(url, ProbeOutcome.match, identity: identity);
    } on TimeoutException {
      return IdentityProbe(url, ProbeOutcome.unreachable, detail: 'timed out');
    } catch (e) {
      return IdentityProbe(url, ProbeOutcome.unreachable, detail: _shortError(e));
    } finally {
      client.close();
    }
  }

  Future<ResolveResult> resolve(List<String> candidates, String expectedDeviceId) async {
    final ordered = <String>[];
    for (final c in candidates) {
      final n = normaliseBaseUrl(c);
      if (n != null && !ordered.contains(n)) ordered.add(n);
    }
    final probes = await Future.wait(ordered.map((u) => probe(u, expectedDeviceId: expectedDeviceId)));
    for (final p in probes) {
      if (p.outcome == ProbeOutcome.match) return ResolveResult(p.url, p.identity, probes);
    }
    return ResolveResult(null, null, probes);
  }

  static String _shortError(Object e) {
    final s = e.toString();
    if (s.contains('Connection refused')) return 'connection refused';
    if (s.contains('No route to host') || s.contains('Network is unreachable')) return 'network unreachable';
    if (s.contains('Failed host lookup')) return 'unknown host';
    if (s.contains('HandshakeException') || s.contains('CERTIFICATE')) return 'TLS certificate problem';
    return 'connection failed';
  }
}
