import '../models/server_entry.dart';

/// A pairing offer from the store dashboard, either scanned as a QR code or
/// typed in by hand. The QR form is
/// `edgecctv://pair?v=1&d=<device_id>&n=<name>&c=<code>&u=<url>&u=<url>...`.
class PairingPayload {
  final String deviceId;
  final String? name;
  final String code;
  final List<String> urls;

  const PairingPayload({required this.deviceId, required this.code, required this.urls, this.name});

  static const _allowedKeys = {'v', 'd', 'n', 'c', 'u'};

  /// Parses a QR payload strictly. Anything that is not a version-1
  /// `edgecctv://pair` link with a valid device id, code and at least one
  /// http(s) URL throws a [FormatException] whose message can be shown as-is.
  static PairingPayload parseQr(String raw) {
    final text = raw.trim();
    final uri = Uri.tryParse(text);
    if (uri == null || uri.scheme != 'edgecctv' || uri.host != 'pair' || (uri.path.isNotEmpty && uri.path != '/')) {
      throw const FormatException('This QR code is not an Edge CCTV pairing code.');
    }
    if (uri.hasFragment || uri.userInfo.isNotEmpty || uri.hasPort) {
      throw const FormatException('This QR code is not an Edge CCTV pairing code.');
    }
    final Map<String, List<String>> params;
    try {
      params = uri.queryParametersAll;
    } catch (_) {
      throw const FormatException('The pairing code in this QR code is damaged. Create a new one on the dashboard.');
    }
    for (final key in params.keys) {
      if (!_allowedKeys.contains(key)) {
        throw FormatException('The pairing QR code has an unexpected field "$key".');
      }
    }
    String single(String key, String label) {
      final values = params[key] ?? const [];
      if (values.length != 1 || values.first.trim().isEmpty) {
        throw FormatException('The pairing QR code is missing its $label.');
      }
      return values.first.trim();
    }

    final version = single('v', 'version');
    if (version != '1') {
      throw const FormatException('This pairing code needs a newer version of the app.');
    }
    final deviceId = single('d', 'server device ID');
    if (!isValidDeviceId(deviceId)) {
      throw const FormatException('The pairing QR code has an invalid server device ID.');
    }
    final code = normalisePairingCode(single('c', 'pairing code'));
    if (code == null) {
      throw const FormatException('The pairing QR code has an invalid pairing code.');
    }
    final names = params['n'] ?? const [];
    if (names.length > 1) throw const FormatException('The pairing QR code has more than one server name.');

    final rawUrls = params['u'] ?? const [];
    final urls = <String>[];
    for (final u in rawUrls) {
      final n = normaliseBaseUrl(u);
      if (n == null || !u.trim().contains('://')) {
        throw FormatException('The pairing QR code has an invalid server address: $u');
      }
      urls.add(n);
    }
    if (urls.isEmpty) throw const FormatException('The pairing QR code has no server address.');

    return PairingPayload(
      deviceId: normaliseDeviceId(deviceId),
      name: names.isEmpty || names.first.trim().isEmpty ? null : names.first.trim(),
      code: code,
      urls: orderCandidateUrls(urls),
    );
  }
}

/// `abcd1234`, `ABCD 1234` and `abcd-1234` all become `ABCD-1234`.
/// Returns null unless the input is exactly 8 letters/digits.
String? normalisePairingCode(String raw) {
  final chars = raw.toUpperCase().replaceAll(RegExp(r'[\s-]'), '');
  if (!RegExp(r'^[A-Z0-9]{8}$').hasMatch(chars)) return null;
  return '${chars.substring(0, 4)}-${chars.substring(4)}';
}
