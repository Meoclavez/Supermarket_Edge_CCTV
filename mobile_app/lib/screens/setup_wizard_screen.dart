import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../core/operator_policy.dart';
import '../core/theme/app_theme.dart';
import '../models/server_entry.dart';
import '../services/connection_resolver.dart';
import '../services/pairing_service.dart';
import '../services/push_service.dart';
import '../services/server_registry.dart';
import 'app_shell.dart';
import 'pairing_screen.dart';

/// First-run wizard, following edge_backend/app/routes/setup.py.
///
/// 1. Server: `GET /api/v1/auth/status` (unauthenticated) says whether an
///    operator account exists.
/// 2. Account: `POST /api/v1/setup/admin` with username, password,
///    display_name and the one-time `setup_code`. The reply carries
///    `access_token` / `refresh_token`, which are stored and sent as a bearer
///    token on every later step (once an account exists the step endpoints
///    require a session).
/// 3+. hardware-scan, camera-scan, add-cameras, complete.
///
/// Errors are shown inline under the step that caused them, never in a dialog.
class SetupWizardScreen extends StatefulWidget {
  const SetupWizardScreen({Key? key}) : super(key: key);

  @override
  State<SetupWizardScreen> createState() => _SetupWizardScreenState();
}

class _SetupWizardScreenState extends State<SetupWizardScreen> {
  static const _timeout = Duration(seconds: 10);

  int _currentStep = 0;
  final _serverUrlController = TextEditingController();
  final _setupCodeController = TextEditingController();
  final _usernameController = TextEditingController();
  final _displayNameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _confirmPasswordController = TextEditingController();

  bool _isLoading = false;
  bool _showPassword = false;
  bool? _serverReachable;
  bool? _adminExists;
  String? _accessToken;
  String? _pairNote;
  String? _error;
  Map<String, dynamic>? _hardware;
  List<Map<String, dynamic>> _cameras = [];
  bool _camerasScanned = false;

  @override
  void dispose() {
    for (final c in [
      _serverUrlController,
      _setupCodeController,
      _usernameController,
      _displayNameController,
      _passwordController,
      _confirmPasswordController,
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  String get _baseUrl {
    var url = _serverUrlController.text.trim();
    if (url.isNotEmpty && !url.startsWith('http://') && !url.startsWith('https://')) url = 'http://$url';
    while (url.endsWith('/')) {
      url = url.substring(0, url.length - 1);
    }
    return url;
  }

  Map<String, String> get _jsonHeaders => {
        'Content-Type': 'application/json',
        if (_accessToken != null) 'Authorization': 'Bearer $_accessToken',
      };

  void _fail(String message) {
    if (mounted) setState(() => _error = message);
  }

  String _errorFrom(http.Response res) =>
      describeHttpError(res.statusCode, res.body, retryAfter: res.headers['retry-after']);

  Future<void> _run(Future<void> Function() action) async {
    setState(() {
      _isLoading = true;
      _error = null;
    });
    try {
      await action();
    } on TimeoutException {
      _fail('The edge server did not answer in time. Check the address and your Wi-Fi.');
    } catch (e) {
      _fail('Cannot reach the edge server: $e');
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  // ---------------------------------------------------------------- step 0
  Future<void> _testServer() => _run(() async {
        if (_baseUrl.isEmpty) {
          _fail('Enter the edge server address.');
          return;
        }
        final res = await http.get(Uri.parse('$_baseUrl/api/v1/auth/status')).timeout(_timeout);
        if (res.statusCode != 200) {
          setState(() => _serverReachable = false);
          _fail(_errorFrom(res));
          return;
        }
        final data = jsonDecode(res.body) as Map<String, dynamic>;
        setState(() {
          _serverReachable = true;
          _adminExists = data['admin_exists'] == true;
        });
      });

  // ---------------------------------------------------------------- step 1
  String? _validateAccountForm() {
    final code = normaliseSetupCode(_setupCodeController.text);
    if (code.isEmpty) return 'Enter the setup code from the edge server.';
    final username = _usernameController.text.trim();
    final usernameProblem = usernamePolicyError(username);
    if (usernameProblem != null) return usernameProblem;
    final passwordProblem = passwordPolicyError(_passwordController.text, username: username);
    if (passwordProblem != null) return passwordProblem;
    if (_passwordController.text != _confirmPasswordController.text) return 'Passwords do not match.';
    return null;
  }

  Future<void> _createAdmin() => _run(() async {
        final problem = _validateAccountForm();
        if (problem != null) {
          _fail(problem);
          return;
        }
        final res = await http
            .post(
              Uri.parse('$_baseUrl/api/v1/setup/admin'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({
                'username': _usernameController.text.trim(),
                'display_name': _displayNameController.text.trim(),
                'password': _passwordController.text,
                'setup_code': _setupCodeController.text.trim(),
              }),
            )
            .timeout(_timeout);
        if (res.statusCode != 200) {
          _fail(_errorFrom(res));
          return;
        }
        final data = jsonDecode(res.body) as Map<String, dynamic>;
        final access = data['access_token']?.toString();
        if (access == null || access.isEmpty) {
          _fail('The server created the account but returned no session. Sign in to continue.');
          return;
        }
        final paired = await _registerServer(access, data['refresh_token']?.toString());
        if (!paired) return;

        _passwordController.clear();
        _confirmPasswordController.clear();
        _setupCodeController.clear();
        setState(() {
          _accessToken = access;
          _adminExists = true;
          _currentStep = 2;
        });
        await _scanHardware();
      });

  /// Stores this server under its device id (from the identity endpoint),
  /// then signs this phone in with the new account so it is registered as a
  /// paired phone and can receive alerts. If that sign-in fails the setup
  /// session is kept and the phone can be paired later from Settings.
  Future<bool> _registerServer(String setupAccess, String? setupRefresh) async {
    final registry = ServerRegistry();
    final probe = await registry.resolver.probe(_baseUrl);
    final identity = probe.identity;
    if (probe.outcome != ProbeOutcome.match || identity == null) {
      _fail('The account was created, but the server did not report its device ID: ${probe.describe()}');
      return false;
    }
    await registry.saveTokens(identity.deviceId, accessToken: setupAccess, refreshToken: setupRefresh);
    await registry.upsert(
      ServerEntry(
        deviceId: identity.deviceId,
        name: identity.deviceName,
        urls: [_baseUrl, if (identity.remoteUrl != null) identity.remoteUrl!],
        lastGoodUrl: _baseUrl,
        remoteUrl: identity.remoteUrl,
      ),
      makeActive: true,
    );
    try {
      final service = PairingService();
      await service.signIn(
        address: _baseUrl,
        username: _usernameController.text.trim(),
        password: _passwordController.text,
        phone: await service.phoneInfo(pushToken: PushService().token),
        expectedDeviceId: identity.deviceId,
      );
    } on PairingException catch (e) {
      _pairNote = 'This phone is signed in but not yet registered for alerts (${e.message}). '
          'Pair it from Settings when setup is finished.';
    }
    return true;
  }

  // ---------------------------------------------------------------- step 2+
  Future<void> _scanHardware() async {
    final res = await http.post(Uri.parse('$_baseUrl/api/v1/setup/hardware-scan'), headers: _jsonHeaders).timeout(_timeout);
    if (res.statusCode != 200) {
      _fail(_errorFrom(res));
      return;
    }
    final data = jsonDecode(res.body) as Map<String, dynamic>;
    setState(() => _hardware = Map<String, dynamic>.from(data['hardware'] ?? {}));
  }

  Future<void> _scanCameras() => _run(() async {
        // Network discovery can take a while.
        final res = await http
            .post(Uri.parse('$_baseUrl/api/v1/setup/camera-scan'), headers: _jsonHeaders, body: jsonEncode({}))
            .timeout(const Duration(seconds: 60));
        if (res.statusCode != 200) {
          _fail(_errorFrom(res));
          return;
        }
        final data = jsonDecode(res.body) as Map<String, dynamic>;
        setState(() {
          _cameras = List<Map<String, dynamic>>.from(data['cameras'] ?? []);
          _camerasScanned = true;
        });
      });

  static String _cameraTitle(Map<String, dynamic> cam) =>
      (cam['model_name'] ?? cam['manufacturer'] ?? cam['host'] ?? cam['device_path'] ?? 'Camera').toString();

  static String? _firstStreamUrl(Map<String, dynamic> cam) {
    final streams = cam['stream_urls'];
    if (streams is List && streams.isNotEmpty && streams.first is Map) {
      final url = (streams.first as Map)['url']?.toString();
      if (url != null && url.isNotEmpty) return url;
    }
    return null;
  }

  /// Saves discovered cameras that expose a stream URL. Returns false (with
  /// an inline error) if the server refused.
  Future<bool> _saveDiscoveredCameras() async {
    final items = [
      for (final c in _cameras)
        if (_firstStreamUrl(c) != null)
          {
            'name': _cameraTitle(c),
            'location': (c['host'] ?? c['device_path'] ?? '').toString(),
            'rtsp_url': _firstStreamUrl(c),
          }
    ];
    if (items.isEmpty) return true;
    final res = await http
        .post(Uri.parse('$_baseUrl/api/v1/setup/add-cameras'), headers: _jsonHeaders, body: jsonEncode({'cameras': items}))
        .timeout(_timeout);
    if (res.statusCode != 200) {
      _fail(_errorFrom(res));
      return false;
    }
    return true;
  }

  Future<void> _completeSetup() => _run(() async {
        final res = await http.post(Uri.parse('$_baseUrl/api/v1/setup/complete'), headers: _jsonHeaders).timeout(_timeout);
        if (res.statusCode != 200) {
          _fail(_errorFrom(res));
          return;
        }
        if (mounted) {
          Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const AppShell()));
        }
      });

  void _onContinue() {
    if (_isLoading) return;
    switch (_currentStep) {
      case 0:
        if (_serverReachable == true) {
          setState(() {
            _error = null;
            _currentStep = 1;
          });
        } else {
          _testServer();
        }
        break;
      case 1:
        if (_accessToken == null) _createAdmin();
        break;
      case 2:
        setState(() => _currentStep = 3);
        _scanCameras();
        break;
      case 3:
        _run(() async {
          if (await _saveDiscoveredCameras()) setState(() => _currentStep = 4);
        });
        break;
      case 4:
        setState(() => _currentStep = 5);
        break;
      case 5:
        _completeSetup();
        break;
    }
  }

  void _onBack() {
    // The account cannot be created twice, so never step back into step 1
    // once it exists.
    final minStep = _accessToken != null ? 2 : 0;
    if (_currentStep > minStep) {
      setState(() {
        _error = null;
        _currentStep -= 1;
      });
    }
  }

  // ---------------------------------------------------------------- UI
  Widget _errorLine() {
    if (_error == null) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, color: context.palette.alert, size: 18),
          const SizedBox(width: 8),
          Expanded(child: Text(_error!, style: TextStyle(color: context.palette.alert, fontSize: 13))),
        ],
      ),
    );
  }

  Widget _stepContent(int step, Widget child) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [child, if (_currentStep == step) _errorLine()],
      );

  Widget _passwordField(TextEditingController c, String label, {String? helper}) => TextField(
        controller: c,
        obscureText: !_showPassword,
        autocorrect: false,
        enableSuggestions: false,
        decoration: InputDecoration(
          labelText: label,
          helperText: helper,
          suffixIcon: IconButton(
            tooltip: _showPassword ? 'Hide password' : 'Show password',
            icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
            onPressed: () => setState(() => _showPassword = !_showPassword),
          ),
        ),
      );

  Widget _buildAccountStep() {
    if (_accessToken != null) {
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('Operator account created. You are signed in.', style: TextStyle(color: context.palette.live)),
        if (_pairNote != null) Text(_pairNote!, style: TextStyle(color: context.palette.muted, fontSize: 12)),
      ]);
    }
    if (_adminExists == true) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('An operator account already exists on this server. Sign in to finish setup.'),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: () => Navigator.pushReplacement(context, MaterialPageRoute(builder: (_) => const PairingScreen(initialMethod: PairingMethod.signIn))),
            child: const Text('Go to sign in'),
          ),
        ],
      );
    }
    return Column(
      children: [
        TextField(
          controller: _setupCodeController,
          textCapitalization: TextCapitalization.characters,
          autocorrect: false,
          enableSuggestions: false,
          maxLength: 16,
          decoration: const InputDecoration(
            labelText: 'Setup code',
            hintText: 'XXXX-XXXX',
            helperText: 'One-time code printed in the edge server log at startup and\n'
                'stored in storage/setup_code.txt on the server.',
            helperMaxLines: 3,
          ),
        ),
        const SizedBox(height: 8),
        TextField(
          controller: _usernameController,
          autocorrect: false,
          enableSuggestions: false,
          decoration: const InputDecoration(
            labelText: 'Username',
            helperText: '3-64 characters: letters, digits, dot, dash, underscore',
          ),
        ),
        const SizedBox(height: 8),
        TextField(controller: _displayNameController, decoration: const InputDecoration(labelText: 'Display name (optional)')),
        const SizedBox(height: 8),
        _passwordField(_passwordController, 'Password',
            helper: 'At least $kPasswordMinLength characters, not the same as the username'),
        const SizedBox(height: 8),
        _passwordField(_confirmPasswordController, 'Confirm password'),
      ],
    );
  }

  Widget _buildHardwareStep() {
    final hw = _hardware;
    if (hw == null) return const Text('No hardware report yet.');
    Widget row(String label, bool? ok, String yes, String no) => ListTile(
          dense: true,
          contentPadding: EdgeInsets.zero,
          leading: Icon(ok == true ? Icons.check_circle : Icons.remove_circle_outline,
              color: ok == true ? context.palette.live : context.palette.dim(0.38)),
          title: Text(label),
          subtitle: Text(ok == true ? yes : no),
        );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('CPU: ${hw['cpu_info'] ?? 'unknown'}', style: TextStyle(color: context.palette.dim(0.70))),
        row('Hailo NPU (/dev/hailo0)', hw['hailo_available'] as bool?, 'Present', 'Not present'),
        row('VA-API render node', hw['vaapi_available'] as bool?, 'Present', 'Not present'),
        row('Hardware video decode', hw['hw_decode_supported'] as bool?, 'Supported (vainfo)', 'Not detected, software decode'),
        Text('The inference backend is chosen automatically at runtime from what is present.',
            style: TextStyle(color: context.palette.dim(0.54), fontSize: 12)),
      ],
    );
  }

  Widget _buildCameraStep() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        OutlinedButton(onPressed: _isLoading ? null : _scanCameras, child: const Text('Scan again')),
        const SizedBox(height: 8),
        if (_cameras.isNotEmpty)
          for (final cam in _cameras)
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(_cameraTitle(cam)),
              subtitle: Text(_firstStreamUrl(cam) ??
                  (cam['requires_credentials'] == true ? 'Needs credentials: add it later from the dashboard' : 'No stream found')),
            )
        else
          Text(_camerasScanned ? 'No cameras answered the scan. You can add cameras later.' : 'Scanning...'),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final lastStep = _currentStep == 5;
    return Scaffold(
      backgroundColor: context.palette.background,
      appBar: AppBar(title: const Text('Initial setup')),
      body: Stepper(
        currentStep: _currentStep,
        // Flutter's light default puts white step numbers on a 38% grey
        // circle; use readable muted and primary fills in the light theme.
        connectorColor: context.palette.isDark
            ? null
            : WidgetStateProperty.resolveWith((states) =>
                states.contains(WidgetState.selected) ? context.palette.accent : context.palette.muted),
        onStepContinue: _onContinue,
        onStepCancel: _onBack,
        controlsBuilder: (context, details) {
          final showContinue = !(_currentStep == 1 && _adminExists == true && _accessToken == null);
          return Padding(
            padding: const EdgeInsets.only(top: 16.0),
            child: Row(
              children: [
                if (showContinue)
                  ElevatedButton(
                    onPressed: _isLoading ? null : details.onStepContinue,
                    child: _isLoading
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : Text(lastStep
                            ? 'Finish setup'
                            : _currentStep == 0 && _serverReachable != true
                                ? 'Test connection'
                                : _currentStep == 1
                                    ? 'Create account'
                                    : 'Continue'),
                  ),
                const SizedBox(width: 8),
                if (_currentStep > (_accessToken != null ? 2 : 0))
                  TextButton(onPressed: _isLoading ? null : details.onStepCancel, child: const Text('Back')),
              ],
            ),
          );
        },
        steps: [
          Step(
            title: const Text('Server connection'),
            content: _stepContent(
              0,
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextField(
                    controller: _serverUrlController,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    onChanged: (_) => setState(() => _serverReachable = null),
                    decoration: const InputDecoration(labelText: 'Edge server address', hintText: 'http://192.168.1.100:8000'),
                  ),
                  if (_serverReachable == true)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _adminExists == true ? 'Connected. An operator account already exists.' : 'Connected. Ready for first-run setup.',
                        style: TextStyle(color: context.palette.live),
                      ),
                    ),
                ],
              ),
            ),
            isActive: _currentStep >= 0,
          ),
          Step(
            title: const Text('Operator account'),
            content: _stepContent(1, _buildAccountStep()),
            isActive: _currentStep >= 1,
          ),
          Step(
            title: const Text('Hardware detection'),
            content: _stepContent(2, _buildHardwareStep()),
            isActive: _currentStep >= 2,
          ),
          Step(
            title: const Text('Cameras'),
            content: _stepContent(3, _buildCameraStep()),
            isActive: _currentStep >= 3,
          ),
          Step(
            title: const Text('Notifications'),
            content: _stepContent(
              4,
              const Text('Loss-prevention alerts are pushed to this phone once you allow notifications. '
                  'You can change this later in settings.'),
            ),
            isActive: _currentStep >= 4,
          ),
          Step(
            title: const Text('Finish'),
            content: _stepContent(5, const Text('Mark setup complete and open the app.')),
            isActive: _currentStep >= 5,
          ),
        ],
      ),
    );
  }
}
