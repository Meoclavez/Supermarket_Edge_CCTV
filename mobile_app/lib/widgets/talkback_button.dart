import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../core/theme/app_theme.dart';
import '../services/webrtc_service.dart';

/// Push-to-talk store announcement through the camera's own speaker
/// (WebRTC audio backchannel via go2rtc). Audio only reaches the shop floor
/// if the camera has a speaker and backchannel support.
class TalkbackButton extends StatefulWidget {
  final WebRtcService webrtcService;

  const TalkbackButton({Key? key, required this.webrtcService}) : super(key: key);

  @override
  State<TalkbackButton> createState() => _TalkbackButtonState();
}

class _TalkbackButtonState extends State<TalkbackButton> with SingleTickerProviderStateMixin {
  bool _isTalking = false;
  late AnimationController _waveController;

  @override
  void initState() {
    super.initState();
    _waveController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 600),
    );
  }

  @override
  void dispose() {
    _waveController.dispose();
    super.dispose();
  }

  void _startTalking() {
    if (!widget.webrtcService.setTalkbackActive(true)) {
      ScaffoldMessenger.maybeOf(context)?.showSnackBar(
        const SnackBar(content: Text('Microphone unavailable: allow microphone access to speak through the camera.')),
      );
      return;
    }
    HapticFeedback.heavyImpact();
    _waveController.repeat(reverse: true);
    setState(() => _isTalking = true);
  }

  void _stopTalking() {
    if (!_isTalking) return;
    HapticFeedback.lightImpact();
    widget.webrtcService.setTalkbackActive(false);
    _waveController.stop();
    _waveController.reset();
    setState(() => _isTalking = false);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTapDown: (_) => _startTalking(),
      onTapUp: (_) => _stopTalking(),
      onTapCancel: () => _stopTalking(),
      child: AnimatedBuilder(
        animation: _waveController,
        builder: (context, child) {
          return Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: _isTalking ? context.palette.alert : context.palette.card,
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: _isTalking ? context.palette.alert : context.palette.accent,
                    width: _isTalking ? 2 + (_waveController.value * 4) : 1.5,
                  ),
                  boxShadow: _isTalking
                      ? [
                          BoxShadow(
                            color: context.palette.alert.withValues(alpha: 0.6),
                            blurRadius: 16 * _waveController.value,
                            spreadRadius: 4 * _waveController.value,
                          )
                        ]
                      : [],
                ),
                child: Icon(
                  _isTalking ? Icons.mic : Icons.mic_none,
                  color: _isTalking ? Colors.white : context.palette.accent,
                  size: 24,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                _isTalking ? 'SPEAKING' : 'Store PA (hold)',
                style: TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.bold,
                  color: _isTalking ? context.palette.alert : context.palette.dim(0.70),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
