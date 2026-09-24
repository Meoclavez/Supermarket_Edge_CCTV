import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:edge_ai_cctv/services/live_stream_transport.dart';
import 'package:edge_ai_cctv/widgets/mjpeg_view.dart';

Uint8List jpeg(int tag) => Uint8List.fromList([0xFF, 0xD8, 0xFF, 0xE0, tag, 0x00, 0xFF, 0x00, tag, 0xFF, 0xD9]);

List<int> part(Uint8List frame) => [
      ...'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.length}\r\n\r\n'.codeUnits,
      ...frame,
      ...'\r\n'.codeUnits,
    ];

void main() {
  group('transport choice', () {
    test('remote URL uses MJPEG straight away', () {
      expect(
          initialLiveTransport(activeUrl: 'https://store-a.example.com', remoteUrl: 'https://store-a.example.com'),
          LiveTransport.mjpeg);
    });

    test('non-LAN address with LAN-only WebRTC uses MJPEG', () {
      expect(
          initialLiveTransport(
              activeUrl: 'https://edge.example.net',
              iceInfo: {'turn_enabled': false, 'webrtc_scope': 'lan_only'}),
          LiveTransport.mjpeg);
    });

    test('LAN address tries WebRTC first', () {
      expect(
          initialLiveTransport(
              activeUrl: 'http://192.168.1.50:8000',
              remoteUrl: 'https://store-a.example.com',
              iceInfo: {'turn_enabled': false, 'webrtc_scope': 'lan_only'}),
          LiveTransport.webrtc);
    });

    test('non-LAN address with TURN configured may try WebRTC', () {
      expect(initialLiveTransport(activeUrl: 'https://edge.example.net', iceInfo: {'turn_enabled': true}),
          LiveTransport.webrtc);
    });

    test('stream URL carries camera id and token', () {
      final u = mjpegStreamUri('https://store-a.example.com', 'cam 1', token: 'TOKEN');
      expect(u.path, '/stream');
      expect(u.queryParameters, {'camera_id': 'cam 1', 'token': 'TOKEN'});
      expect(mjpegStreamUri('http://10.0.0.5:8000', 'c2').toString(), 'http://10.0.0.5:8000/stream?camera_id=c2');
    });
  });

  test('MJPEG parser splits frames, including frames split across chunks', () {
    final parser = MjpegFrameParser();
    final bytes = [...part(jpeg(1)), ...part(jpeg(2)), ...part(jpeg(3))];
    final frames = <Uint8List>[];
    // Feed in awkward 5-byte chunks so markers straddle chunk edges.
    for (var i = 0; i < bytes.length; i += 5) {
      frames.addAll(parser.add(bytes.sublist(i, i + 5 > bytes.length ? bytes.length : i + 5)));
    }
    expect(frames.length, 3);
    expect(frames.map((f) => f[4]), [1, 2, 3]);
    expect(frames.first, jpeg(1));
  });

  testWidgets('MjpegView shows frames from the stream and reports errors inline', (tester) async {
    final controller = StreamController<List<int>>();
    Uri? requested;
    final client = MockClient.streaming((req, _) async {
      requested = req.url;
      if (req.url.queryParameters['camera_id'] == 'bad') {
        return http.StreamedResponse(Stream.value('{"detail":"Not authenticated"}'.codeUnits), 401);
      }
      return http.StreamedResponse(controller.stream, 200,
          headers: {'content-type': 'multipart/x-mixed-replace; boundary=frame'});
    });

    final frames = <Uint8List>[];
    await http.runWithClient(() async {
      final uri = mjpegStreamUri('https://store-a.example.com', 'cam1', token: 'TOKEN');
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: MjpegView(uri: uri, onFrame: frames.add))));
      await tester.pump();
      controller.add(part(jpeg(7)));
      controller.add(part(jpeg(8)).sublist(0, 20));
      await tester.pump();
      expect(frames.length, 1);
      expect(find.byKey(const ValueKey('mjpeg-frame')), findsOneWidget);
      expect(requested!.queryParameters['token'], 'TOKEN');

      await tester.pumpWidget(MaterialApp(
          home: Scaffold(body: MjpegView(uri: mjpegStreamUri('https://store-a.example.com', 'bad'), onFrame: frames.add))));
      await tester.pump();
      await tester.pump();
      expect(find.textContaining('refused the video stream (HTTP 401)'), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
      await tester.pumpWidget(const SizedBox());
    }, () => client);
    unawaited(controller.close());
  });
}
