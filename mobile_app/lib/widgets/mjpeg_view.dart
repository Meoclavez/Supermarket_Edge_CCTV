import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../core/theme/app_theme.dart';
import '../services/live_stream_transport.dart';

/// Shows an MJPEG (`multipart/x-mixed-replace`) stream, frame by frame.
///
/// Used for live view when WebRTC cannot connect (the phone is off the store
/// LAN, or WebRTC timed out). Errors are shown inline with a retry button,
/// and a dropped stream reconnects on its own.
class MjpegView extends StatefulWidget {
  final Uri uri;
  final Map<String, String> headers;
  final BoxFit fit;

  /// Called for every decoded frame (used by tests and the live badge).
  final void Function(Uint8List frame)? onFrame;

  const MjpegView({Key? key, required this.uri, this.headers = const {}, this.fit = BoxFit.contain, this.onFrame})
      : super(key: key);

  @override
  State<MjpegView> createState() => _MjpegViewState();
}

class _MjpegViewState extends State<MjpegView> {
  http.Client? _client;
  StreamSubscription<List<int>>? _sub;
  Uint8List? _frame;
  String? _error;
  Timer? _retry;
  int _failures = 0;

  @override
  void initState() {
    super.initState();
    _open();
  }

  @override
  void didUpdateWidget(MjpegView old) {
    super.didUpdateWidget(old);
    if (old.uri != widget.uri) {
      _close();
      _frame = null;
      _open();
    }
  }

  @override
  void dispose() {
    _close();
    super.dispose();
  }

  void _close() {
    _retry?.cancel();
    _sub?.cancel();
    _sub = null;
    _client?.close();
    _client = null;
  }

  Future<void> _open() async {
    _close();
    setState(() => _error = null);
    final client = http.Client();
    _client = client;
    try {
      final req = http.Request('GET', widget.uri)..headers.addAll(widget.headers);
      final res = await client.send(req).timeout(const Duration(seconds: 10));
      if (!mounted || _client != client) return;
      if (res.statusCode != 200) {
        _fail(res.statusCode == 401 || res.statusCode == 403
            ? 'The server refused the video stream (HTTP ${res.statusCode}). Sign in again.'
            : 'The server returned HTTP ${res.statusCode} for the video stream.',
            retry: res.statusCode >= 500);
        return;
      }
      final parser = MjpegFrameParser();
      _sub = res.stream.listen(
        (chunk) {
          final frames = parser.add(chunk);
          if (frames.isEmpty || !mounted) return;
          _failures = 0;
          final last = frames.last;
          widget.onFrame?.call(last);
          setState(() => _frame = last);
        },
        onError: (Object e) => _fail('The video stream was interrupted.', retry: true),
        onDone: () => _fail('The video stream ended.', retry: true),
        cancelOnError: true,
      );
    } catch (e) {
      if (mounted && _client == client) _fail('Could not open the video stream.', retry: true);
    }
  }

  void _fail(String message, {required bool retry}) {
    if (!mounted) return;
    setState(() => _error = message);
    if (retry) {
      _failures++;
      _retry?.cancel();
      _retry = Timer(Duration(seconds: _failures.clamp(1, 10) * 2), _open);
    }
  }

  @override
  Widget build(BuildContext context) {
    final frame = _frame;
    return Stack(
      fit: StackFit.expand,
      children: [
        if (frame != null)
          Image.memory(
            frame,
            key: const ValueKey('mjpeg-frame'),
            fit: widget.fit,
            gaplessPlayback: true,
            errorBuilder: (_, __, ___) => const SizedBox.shrink(),
          )
        else if (_error == null)
          const Center(child: CircularProgressIndicator(color: AppTheme.cyberBlue)),
        if (_error != null)
          Align(
            alignment: frame == null ? Alignment.center : Alignment.bottomCenter,
            child: Container(
              margin: const EdgeInsets.all(12),
              padding: const EdgeInsets.all(10),
              color: Colors.black.withValues(alpha: 0.7),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                Flexible(child: Text(_error!, style: const TextStyle(color: Colors.white70, fontSize: 12))),
                TextButton(onPressed: _open, child: const Text('Retry')),
              ]),
            ),
          ),
      ],
    );
  }
}
