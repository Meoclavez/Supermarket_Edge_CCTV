import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../core/theme/app_theme.dart';
import '../services/webrtc_service.dart';

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
    HapticFeedback.heavyImpact();
    widget.webrtcService.setTalkbackActive(true);
    _waveController.repeat(reverse: true);
    setState(() => _isTalking = true);
  }

  void _stopTalking() {
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
                  color: _isTalking ? AppTheme.emergencyRed : AppTheme.cardSurface,
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: _isTalking ? AppTheme.emergencyRed : AppTheme.cyberBlue,
                    width: _isTalking ? 2 + (_waveController.value * 4) : 1.5,
                  ),
                  boxShadow: _isTalking
                      ? [
                          BoxShadow(
                            color: AppTheme.emergencyRed.withOpacity(0.6),
                            blurRadius: 16 * _waveController.value,
                            spreadRadius: 4 * _waveController.value,
                          )
                        ]
                      : [],
                ),
                child: Icon(
                  _isTalking ? Icons.mic : Icons.mic_none,
                  color: _isTalking ? Colors.white : AppTheme.cyberBlue,
                  size: 24,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                _isTalking ? 'HOLD TO TALK' : '2-Way Audio',
                style: TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.bold,
                  color: _isTalking ? AppTheme.emergencyRed : Colors.white70,
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
