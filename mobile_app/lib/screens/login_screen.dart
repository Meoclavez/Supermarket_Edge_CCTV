import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:local_auth/local_auth.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'dart:convert';
import 'package:http/http.dart' as http;

import '../core/theme/app_theme.dart';
import 'app_shell.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({Key? key}) : super(key: key);

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _storage = const FlutterSecureStorage();
  final _localAuth = LocalAuthentication();
  
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _serverUrlController = TextEditingController();
  final _pairingCodeController = TextEditingController();

  bool _isPasswordVisible = false;
  bool _isLoading = false;
  String _errorMessage = '';
  
  bool _isScanningQR = false;
  int _loginMethod = 0; // 0: Username, 1: Pairing Code
  String _serverStatus = 'Not Connected';

  @override
  void initState() {
    super.initState();
    _loadSavedServer();
  }
  
  Future<void> _loadSavedServer() async {
    final prefs = await SharedPreferences.getInstance();
    final savedUrl = prefs.getString('server_url');
    if (savedUrl != null && savedUrl.isNotEmpty) {
      _serverUrlController.text = savedUrl;
      _testConnection(savedUrl);
    }
  }

  Future<void> _testConnection(String url) async {
    setState(() => _serverStatus = 'Connecting...');
    try {
      final response = await http.get(Uri.parse('$url/api/v1/health')).timeout(const Duration(seconds: 3));
      if (response.statusCode == 200) {
        setState(() => _serverStatus = 'Connected');
      } else {
        setState(() => _serverStatus = 'Unreachable');
      }
    } catch (e) {
      setState(() => _serverStatus = 'Unreachable');
    }
  }

  Future<void> _handleLogin() async {
    setState(() {
      _isLoading = true;
      _errorMessage = '';
    });

    final url = _serverUrlController.text.trim();
    
    try {
      if (_loginMethod == 0) {
        // Username / Password
        final res = await http.post(
          Uri.parse('$url/api/v1/auth/login'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'username': _usernameController.text,
            'password': _passwordController.text,
          }),
        ).timeout(const Duration(seconds: 5));

        if (res.statusCode == 200) {
          final data = jsonDecode(res.body);
          await _saveToken(data['access_token'], data['refresh_token'], url);
        } else {
          setState(() => _errorMessage = 'Invalid username or password.');
          return;
        }
      } else {
        // Pairing Code
        final res = await http.post(
          Uri.parse('$url/api/v1/auth/pair'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({'code': _pairingCodeController.text}),
        ).timeout(const Duration(seconds: 5));

        if (res.statusCode == 200) {
          final data = jsonDecode(res.body);
          await _saveToken(data['access_token'], data['refresh_token'], url);
        } else {
          setState(() => _errorMessage = 'Invalid pairing code.');
          return;
        }
      }
    } catch (e) {
      setState(() => _errorMessage = 'Connection failed: $e');
    } finally {
      setState(() => _isLoading = false);
    }
  }

  Future<void> _saveToken(String accessToken, String refreshToken, String url) async {
    await _storage.write(key: 'access_token', value: accessToken);
    await _storage.write(key: 'refresh_token', value: refreshToken);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('server_url', url);

    if (mounted) {
      Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const AppShell()));
    }
  }

  Future<void> _tryBiometric() async {
    try {
      final isAvailable = await _localAuth.canCheckBiometrics;
      if (!isAvailable) return;

      final didAuthenticate = await _localAuth.authenticate(
        localizedReason: 'Please authenticate to log in',
        options: const AuthenticationOptions(biometricOnly: true),
      );

      if (didAuthenticate) {
        final token = await _storage.read(key: 'access_token');
        if (token != null) {
          Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const AppShell()));
        }
      }
    } catch (e) {
      debugPrint('Biometric error: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_isScanningQR) {
      return Scaffold(
        body: MobileScanner(
          onDetect: (capture) {
            final List<Barcode> barcodes = capture.barcodes;
            for (final barcode in barcodes) {
              if (barcode.rawValue != null) {
                // Expecting URL + token format: {"url": "...", "token": "..."}
                try {
                  final data = jsonDecode(barcode.rawValue!);
                  if (data['url'] != null) {
                    _serverUrlController.text = data['url'];
                    // Auto login with token or pairing code could happen here
                    setState(() => _isScanningQR = false);
                  }
                } catch (_) {}
              }
            }
          },
        ),
        floatingActionButton: FloatingActionButton(
          child: const Icon(Icons.close),
          onPressed: () => setState(() => _isScanningQR = false),
        ),
      );
    }

    return Scaffold(
      backgroundColor: AppTheme.darkBackground,
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24.0),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 400),
            child: GlassCard(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.shield_rounded, size: 64, color: AppTheme.cyberBlue),
                  const SizedBox(height: 16),
                  const Text('EDGE AI CCTV', style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold, color: Colors.white)),
                  const SizedBox(height: 24),
                  
                  // Server Configuration
                  TextField(
                    controller: _serverUrlController,
                    decoration: InputDecoration(
                      labelText: 'Server URL',
                      hintText: 'http://192.168.1.100:443',
                      suffixIcon: IconButton(
                        icon: const Icon(Icons.refresh),
                        onPressed: () => _testConnection(_serverUrlController.text),
                      ),
                    ),
                    onChanged: (val) => _testConnection(val),
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Container(
                        width: 8, height: 8,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: _serverStatus == 'Connected' ? AppTheme.liveGreen : AppTheme.emergencyRed,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(_serverStatus, style: const TextStyle(color: Colors.white70, fontSize: 12)),
                    ],
                  ),
                  const SizedBox(height: 24),

                  // Tabs for Login Method
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      ChoiceChip(
                        label: const Text('Password'),
                        selected: _loginMethod == 0,
                        onSelected: (val) => setState(() => _loginMethod = 0),
                      ),
                      ChoiceChip(
                        label: const Text('Pairing Code'),
                        selected: _loginMethod == 1,
                        onSelected: (val) => setState(() => _loginMethod = 1),
                      ),
                    ],
                  ),
                  const SizedBox(height: 24),

                  if (_loginMethod == 0) ...[
                    TextField(
                      controller: _usernameController,
                      decoration: const InputDecoration(labelText: 'Username'),
                    ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: _passwordController,
                      obscureText: !_isPasswordVisible,
                      decoration: InputDecoration(
                        labelText: 'Password',
                        suffixIcon: IconButton(
                          icon: Icon(_isPasswordVisible ? Icons.visibility_off : Icons.visibility),
                          onPressed: () => setState(() => _isPasswordVisible = !_isPasswordVisible),
                        ),
                      ),
                    ),
                  ] else ...[
                    TextField(
                      controller: _pairingCodeController,
                      keyboardType: TextInputType.number,
                      decoration: const InputDecoration(labelText: '6-Digit Pairing Code', hintText: '123456'),
                    ),
                  ],

                  if (_errorMessage.isNotEmpty) ...[
                    const SizedBox(height: 16),
                    Text(_errorMessage, style: const TextStyle(color: AppTheme.emergencyRed)),
                  ],

                  const SizedBox(height: 24),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      onPressed: _isLoading ? null : _handleLogin,
                      child: _isLoading ? const CircularProgressIndicator(color: AppTheme.oledBlack) : const Text('LOGIN'),
                    ),
                  ),

                  const SizedBox(height: 16),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      IconButton(
                        icon: const Icon(Icons.fingerprint, color: AppTheme.cyberBlue),
                        onPressed: _tryBiometric,
                        tooltip: 'Biometric Login',
                      ),
                      IconButton(
                        icon: const Icon(Icons.qr_code_scanner, color: AppTheme.cyberBlue),
                        onPressed: () => setState(() => _isScanningQR = true),
                        tooltip: 'Scan QR Code',
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),
                  TextButton(
                    onPressed: () {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('Reset password via PC web dashboard.')),
                      );
                    },
                    child: const Text('Forgot Password?', style: TextStyle(color: AppTheme.textMuted)),
                  )
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
