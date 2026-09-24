import 'dart:typed_data';

import '../models/server_entry.dart';

/// How live video reaches the phone.
///
/// WebRTC is LAN-only by default: `GET /api/v1/webrtc/ice-servers` returns
/// STUN only (`turn_enabled: false`, `webrtc_scope: "lan_only"`). Away from
/// the store, and whenever WebRTC does not connect quickly, the app shows the
/// server's MJPEG stream (`GET /stream?camera_id=...&token=...`), which works
/// through the HTTPS tunnel.
enum LiveTransport { webrtc, mjpeg }

/// How long WebRTC may take to connect before live view switches to MJPEG.
const Duration kWebRtcConnectTimeout = Duration(seconds: 6);

/// Picks the first transport to try for [activeUrl].
///
/// MJPEG is used straight away when the app is talking to the server through
/// its remote URL (or any non-LAN address), or when the server says WebRTC is
/// LAN-only and the phone is not on a LAN address.
LiveTransport initialLiveTransport({required String activeUrl, String? remoteUrl, Map<String, dynamic>? iceInfo}) {
  final url = normaliseBaseUrl(activeUrl) ?? activeUrl;
  final remote = remoteUrl == null ? null : normaliseBaseUrl(remoteUrl);
  if (remote != null && url == remote) return LiveTransport.mjpeg;
  if (!isLanUrl(url)) {
    final turn = iceInfo?['turn_enabled'] == true || iceInfo?['webrtc_scope'] == 'internet';
    return turn ? LiveTransport.webrtc : LiveTransport.mjpeg;
  }
  return LiveTransport.webrtc;
}

/// `GET /stream?camera_id=<id>&token=<access token>[&fps=n]` on [baseUrl].
Uri mjpegStreamUri(String baseUrl, String cameraId, {String? token, int? fps}) {
  final base = Uri.parse(baseUrl);
  return base.replace(
    path: '${base.path}/stream',
    queryParameters: {
      'camera_id': cameraId,
      if (fps != null && fps > 0) 'fps': '$fps',
      if (token != null && token.isNotEmpty) 'token': token,
    },
  );
}

/// Splits a `multipart/x-mixed-replace` MJPEG byte stream into JPEG frames.
///
/// It looks for the JPEG start (FF D8) and end (FF D9) markers, so it does not
/// depend on the boundary name or on part headers, and it copes with frames
/// split across network chunks.
class MjpegFrameParser {
  final int maxFrameBytes;
  final BytesBuilder _buf = BytesBuilder(copy: false);
  Uint8List _pending = Uint8List(0);

  MjpegFrameParser({this.maxFrameBytes = 8 * 1024 * 1024});

  /// Adds a chunk and returns every complete frame it finished.
  List<Uint8List> add(List<int> chunk) {
    _buf
      ..add(_pending)
      ..add(chunk);
    var data = _buf.takeBytes();
    final frames = <Uint8List>[];
    var searchFrom = 0;
    while (true) {
      final start = _find(data, 0xD8, searchFrom);
      if (start < 0) {
        // Keep a trailing 0xFF in case the next chunk starts with 0xD8.
        data = data.isNotEmpty && data.last == 0xFF ? Uint8List.fromList([0xFF]) : Uint8List(0);
        break;
      }
      final end = _find(data, 0xD9, start + 2);
      if (end < 0) {
        data = Uint8List.sublistView(data, start);
        if (data.length > maxFrameBytes) data = Uint8List(0); // garbage, resync
        break;
      }
      frames.add(Uint8List.fromList(Uint8List.sublistView(data, start, end + 2)));
      searchFrom = end + 2;
    }
    _pending = Uint8List.fromList(data);
    return frames;
  }

  /// Index of the 0xFF that starts marker `FF <second>`, or -1.
  static int _find(Uint8List d, int second, int from) {
    for (var i = from; i + 1 < d.length; i++) {
      if (d[i] == 0xFF && d[i + 1] == second) return i;
    }
    return -1;
  }
}
