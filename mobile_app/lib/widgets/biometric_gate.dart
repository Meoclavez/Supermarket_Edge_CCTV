import 'package:flutter/material.dart';
import '../core/theme/app_theme.dart';
import '../services/biometric_auth_service.dart';

class BiometricGate extends StatefulWidget {
  final Widget child;
  final String promptReason;
  final bool requireAuth;

  const BiometricGate({
    Key? key,
    required this.child,
    this.promptReason = 'Verify identity to access secure surveillance feed',
    this.requireAuth = true,
  }) : super(key: key);

  @override
  State<BiometricGate> createState() => _BiometricGateState();
}

class _BiometricGateState extends State<BiometricGate> with WidgetsBindingObserver {
  final BiometricAuthService _bioService = BiometricAuthService();
  bool _unlocked = false;
  bool _isChecking = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (!widget.requireAuth || _bioService.isAuthenticated) {
      _unlocked = true;
    } else {
      _triggerBiometrics();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused) {
      _bioService.lock();
      if (mounted && widget.requireAuth) {
        setState(() => _unlocked = false);
      }
    } else if (state == AppLifecycleState.resumed && !_unlocked && widget.requireAuth) {
      _triggerBiometrics();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  Future<void> _triggerBiometrics() async {
    if (_isChecking) return;
    setState(() => _isChecking = true);

    final success = await _bioService.authenticate(reason: widget.promptReason);
    if (mounted) {
      setState(() {
        _unlocked = success;
        _isChecking = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_unlocked) return widget.child;

    return Scaffold(
      backgroundColor: context.palette.background,
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(32.0),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Container(
                padding: const EdgeInsets.all(24),
                decoration: BoxDecoration(
                  color: context.palette.card,
                  shape: BoxShape.circle,
                  border: Border.all(color: context.palette.accent.withValues(alpha: 0.5), width: 2),
                ),
                child: Icon(Icons.fingerprint_rounded, size: 64, color: context.palette.accent),
              ),
              const SizedBox(height: 24),
              Text(
                'Biometric Authentication Required',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: context.palette.text),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(
                widget.promptReason,
                style: TextStyle(fontSize: 13, color: context.palette.dim(0.60)),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 32),
              ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: context.palette.accent,
                  foregroundColor: context.palette.onAccent,
                  padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 14),
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                ),
                onPressed: _triggerBiometrics,
                icon: const Icon(Icons.lock_open_rounded),
                label: const Text('Unlock Feed', style: TextStyle(fontWeight: FontWeight.bold)),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
