/// One paired Edge CCTV server (one store), keyed by the server's
/// `device_id` (see `GET /api/v1/device/identity`).
///
/// Two stores can run the same software on the same private LAN address
/// (for example both at 192.168.1.50:8000), so an address never identifies a
/// server on its own. The app only talks to an address after that address has
/// answered with this entry's `device_id`.
class ServerEntry {
  final String deviceId;
  String name;

  /// Candidate base URLs, LAN addresses first and the remote https URL last.
  List<String> urls;

  /// The last URL that answered with this [deviceId].
  String? lastGoodUrl;

  /// The server's remote https URL (Cloudflare tunnel / direct), if enabled.
  String? remoteUrl;

  /// Id of this phone's paired-device record on the server, if known.
  String? pairedDeviceId;

  /// Push token last accepted by this server (so a refreshed token is resent).
  String? pushTokenSent;

  /// Alert preferences last saved to (or read from) the server.
  AlertPrefs? alertPrefs;

  DateTime? pairedAt;

  ServerEntry({
    required String deviceId,
    required this.name,
    required List<String> urls,
    this.lastGoodUrl,
    this.remoteUrl,
    this.pairedDeviceId,
    this.pushTokenSent,
    this.alertPrefs,
    this.pairedAt,
  })  : deviceId = normaliseDeviceId(deviceId),
        urls = orderCandidateUrls(urls);

  /// URLs in the order they should be tried: the last good one first, then
  /// LAN addresses, then remote ones.
  List<String> get candidates {
    final ordered = <String>[];
    if (lastGoodUrl != null) ordered.add(lastGoodUrl!);
    for (final u in orderCandidateUrls([...urls, if (remoteUrl != null) remoteUrl!])) {
      if (!ordered.contains(u)) ordered.add(u);
    }
    return ordered;
  }

  void mergeUrls(Iterable<String> more) {
    urls = orderCandidateUrls([...urls, ...more]);
  }

  String get shortId => deviceId.length > 8 ? deviceId.substring(0, 8) : deviceId;

  Map<String, dynamic> toJson() => {
        'device_id': deviceId,
        'name': name,
        'urls': urls,
        'last_good_url': lastGoodUrl,
        'remote_url': remoteUrl,
        'paired_device_id': pairedDeviceId,
        'push_token_sent': pushTokenSent,
        'alert_prefs': alertPrefs?.toJson(),
        'paired_at': pairedAt?.toIso8601String(),
      };

  factory ServerEntry.fromJson(Map<String, dynamic> j) => ServerEntry(
        deviceId: j['device_id'].toString(),
        name: (j['name'] ?? '').toString(),
        urls: (j['urls'] as List? ?? const []).map((e) => e.toString()).toList(),
        lastGoodUrl: j['last_good_url']?.toString(),
        remoteUrl: j['remote_url']?.toString(),
        pairedDeviceId: j['paired_device_id']?.toString(),
        pushTokenSent: j['push_token_sent']?.toString(),
        alertPrefs: j['alert_prefs'] is Map ? AlertPrefs.fromJson(Map<String, dynamic>.from(j['alert_prefs'])) : null,
        pairedAt: j['paired_at'] != null ? DateTime.tryParse(j['paired_at'].toString()) : null,
      );
}

final RegExp _uuidPattern = RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');

String normaliseDeviceId(String id) => id.trim().toLowerCase();

bool isValidDeviceId(String id) => _uuidPattern.hasMatch(normaliseDeviceId(id));

/// Normalises a server address typed by a person or received from a server
/// into a base URL (`scheme://host[:port][/path]`, no trailing slash).
/// Returns null when it is not a usable http(s) address.
String? normaliseBaseUrl(String raw) {
  var v = raw.trim();
  if (v.isEmpty) return null;
  if (!v.contains('://')) v = 'http://$v';
  final uri = Uri.tryParse(v);
  if (uri == null) return null;
  if (uri.scheme != 'http' && uri.scheme != 'https') return null;
  if (uri.host.isEmpty || uri.userInfo.isNotEmpty || uri.hasQuery || uri.hasFragment) return null;
  var path = uri.path;
  while (path.endsWith('/')) {
    path = path.substring(0, path.length - 1);
  }
  final port = uri.hasPort ? ':${uri.port}' : '';
  return '${uri.scheme}://${uri.host.toLowerCase()}$port$path';
}

/// True for addresses that can only be reached on a local network.
bool isLanUrl(String url) {
  final host = Uri.tryParse(url)?.host ?? '';
  if (host == 'localhost' || host.endsWith('.local') || host.endsWith('.lan')) return true;
  final parts = host.split('.');
  if (parts.length == 4 && parts.every((p) => int.tryParse(p) != null)) {
    final a = int.parse(parts[0]);
    final b = int.parse(parts[1]);
    return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && b >= 16 && b <= 31) || (a == 169 && b == 254) ||
        (a == 100 && b >= 64 && b <= 127);
  }
  return host.startsWith('[fe80') || host.startsWith('[fd') || host.startsWith('[fc');
}

/// De-duplicated, normalised, LAN first, remote last (stable within groups).
List<String> orderCandidateUrls(Iterable<String> urls) {
  final seen = <String>{};
  final lan = <String>[];
  final remote = <String>[];
  for (final raw in urls) {
    final u = normaliseBaseUrl(raw);
    if (u == null || !seen.add(u)) continue;
    (isLanUrl(u) ? lan : remote).add(u);
  }
  return [...lan, ...remote];
}

/// Severity order used by the server's alert preferences.
const List<String> kSeverityLevels = ['INFO', 'WARNING', 'HIGH'];

int severityRank(String? s) {
  switch ((s ?? '').toUpperCase()) {
    case 'CRITICAL':
      return 3;
    case 'HIGH':
      return 2;
    case 'WARNING':
    case 'MEDIUM':
      return 1;
    default:
      return 0;
  }
}

/// Event types the server can dispatch (CONTRACT2 "Alert dispatch / push").
const Map<String, String> kDispatchableEventTypes = {
  'THEFT_SUSPECTED': 'Suspected theft',
  'CONCEALMENT': 'Concealment',
  'SHELF_SWEEP': 'Shelf sweep',
  'EXIT_WITHOUT_CHECKOUT': 'Exit without checkout',
  'LOITERING': 'Loitering',
  'QUEUE_ALERT': 'Queue too long',
  'CAMERA_OFFLINE': 'Camera offline',
  'RESTRICTED_AREA': 'Restricted area',
  'TRIPWIRE_ALERT': 'Tripwire crossed',
};

/// Loss-prevention event types; pushes for these open the incident screens.
const Set<String> kLossPreventionEventTypes = {
  'THEFT_SUSPECTED',
  'CONCEALMENT',
  'SHELF_SWEEP',
  'EXIT_WITHOUT_CHECKOUT',
};

/// Per paired-device alert preferences, mirroring the server's
/// `{event_types, min_severity, camera_ids, quiet_hours}`.
class AlertPrefs {
  /// null means every event type.
  final List<String>? eventTypes;
  final String minSeverity;

  /// null means every camera.
  final List<String>? cameraIds;
  final String? quietStart;
  final String? quietEnd;

  const AlertPrefs({this.eventTypes, this.minSeverity = 'INFO', this.cameraIds, this.quietStart, this.quietEnd});

  bool get hasQuietHours => quietStart != null && quietEnd != null;

  Map<String, dynamic> toJson() => {
        'event_types': eventTypes,
        'min_severity': minSeverity,
        'camera_ids': cameraIds,
        'quiet_hours': hasQuietHours ? {'start': quietStart, 'end': quietEnd} : null,
      };

  factory AlertPrefs.fromJson(Map<String, dynamic> j) {
    final qh = j['quiet_hours'];
    final sev = (j['min_severity'] ?? 'INFO').toString().toUpperCase();
    return AlertPrefs(
      eventTypes: j['event_types'] is List ? (j['event_types'] as List).map((e) => e.toString()).toList() : null,
      minSeverity: kSeverityLevels.contains(sev) ? sev : 'INFO',
      cameraIds: j['camera_ids'] is List ? (j['camera_ids'] as List).map((e) => e.toString()).toList() : null,
      quietStart: qh is Map ? qh['start']?.toString() : null,
      quietEnd: qh is Map ? qh['end']?.toString() : null,
    );
  }

  /// Same rule the server applies before pushing; used for the websocket
  /// fallback so both channels agree.
  bool matches({required String? eventType, required String? severity, String? cameraId, DateTime? at}) {
    if (eventTypes != null && (eventType == null || !eventTypes!.contains(eventType))) return false;
    if (severityRank(severity) < severityRank(minSeverity)) return false;
    if (cameraIds != null && cameraId != null && !cameraIds!.contains(cameraId)) return false;
    if (hasQuietHours && inQuietHours(at ?? DateTime.now())) return false;
    return true;
  }

  bool inQuietHours(DateTime t) {
    final s = parseHhMm(quietStart);
    final e = parseHhMm(quietEnd);
    if (s == null || e == null || s == e) return false;
    final m = t.hour * 60 + t.minute;
    return s < e ? (m >= s && m < e) : (m >= s || m < e);
  }
}

/// Minutes since midnight for `HH:MM`, or null when invalid.
int? parseHhMm(String? v) {
  final m = RegExp(r'^([01]\d|2[0-3]):([0-5]\d)$').firstMatch((v ?? '').trim());
  if (m == null) return null;
  return int.parse(m.group(1)!) * 60 + int.parse(m.group(2)!);
}
