import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:local_auth/local_auth.dart';
import 'package:local_auth/error_codes.dart' as auth_error;

class BiometricAuthService {
  static final BiometricAuthService _instance = BiometricAuthService._internal();
  factory BiometricAuthService() => _instance;
  BiometricAuthService._internal();

  final LocalAuthentication _auth = LocalAuthentication();
  bool _isAuthenticated = false;
  DateTime? _lastAuthenticatedTime;

  // 60-second grace period before re-prompting
  static const Duration gracePeriod = Duration(seconds: 60);

  bool get isAuthenticated {
    if (_lastAuthenticatedTime == null) return false;
    final isExpired = DateTime.now().difference(_lastAuthenticatedTime!) > gracePeriod;
    if (isExpired) _isAuthenticated = false;
    return _isAuthenticated;
  }

  Future<bool> canAuthenticate() async {
    try {
      final bool canCheck = await _auth.canCheckBiometrics;
      final bool isDeviceSupported = await _auth.isDeviceSupported();
      return canCheck && isDeviceSupported;
    } on PlatformException catch (e) {
      debugPrint('Biometric capability check notice: $e');
      return false;
    }
  }

  Future<List<BiometricType>> getAvailableBiometrics() async {
    try {
      return await _auth.getAvailableBiometrics();
    } on PlatformException catch (e) {
      debugPrint('Failed to get available biometrics: $e');
      return <BiometricType>[];
    }
  }

  Future<bool> authenticate({
    required String reason,
    bool biometricOnly = false,
    bool sensitiveTransaction = true,
  }) async {
    try {
      final canAuth = await canAuthenticate();
      if (!canAuth) {
        return true; // Bypass on unsupported hardware
      }

      final bool didAuthenticate = await _auth.authenticate(
        localizedReason: reason,
        options: AuthenticationOptions(
          stickyAuth: true,
          biometricOnly: biometricOnly,
          sensitiveTransaction: sensitiveTransaction,
          useErrorDialogs: true,
        ),
      );

      if (didAuthenticate) {
        _isAuthenticated = true;
        _lastAuthenticatedTime = DateTime.now();
      }
      return didAuthenticate;
    } on PlatformException catch (e) {
      debugPrint('Authentication error: ${e.code} - ${e.message}');
      return false;
    }
  }

  void lock() {
    _isAuthenticated = false;
    _lastAuthenticatedTime = null;
  }
}
