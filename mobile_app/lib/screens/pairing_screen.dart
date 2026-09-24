import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../core/theme/app_theme.dart';
import '../models/server_entry.dart';
import '../services/pairing_payload.dart';
import '../services/pairing_service.dart';
import '../services/push_service.dart';
import '../services/server_registry.dart';
import 'app_shell.dart';
import 'setup_wizard_screen.dart';

enum PairingMethod { scan, code, signIn }

/// Connects this phone to one store's edge server.
///
/// Three ways in, all ending in the same [ServerEntry] keyed by the server's
/// device id:
///  * scan the QR code from the dashboard (`edgecctv://pair?...`),
///  * type the server address, device ID and pairing code,
///  * sign in with the server address, username and password.
///
/// Every error is shown inline next to the form; nothing blocks in a dialog.
class PairingScreen extends StatefulWidget {
  /// Called after a successful pairing. Defaults to opening the app shell.
  final void Function(ServerEntry entry)? onPaired;

  /// When set, the screen re-signs in to this known server and refuses an
  /// address that belongs to any other server.
  final String? reSignInDeviceId;

  final PairingMethod initialMethod;

  const PairingScreen({Key? key, this.onPaired, this.reSignInDeviceId, this.initialMethod = PairingMethod.scan})
      : super(key: key);

  @override
  State<PairingScreen> createState() => _PairingScreenState();
}

class _PairingScreenState extends State<PairingScreen> {
  final _service = PairingService();
  final _phoneName = TextEditingController();
  final _address = TextEditingController();
  final _deviceId = TextEditingController();
  final _code = TextEditingController();
  final _username = TextEditingController();
  final _password = TextEditingController();
  final _pastedLink = TextEditingController();

  late PairingMethod _method = widget.initialMethod;
  bool _busy = false;
  bool _showPassword = false;
  bool _scannerOpen = false;
  String? _lastRejectedScan;
  String? _error;
  String? _status;
  final Map<String, String> _fieldErrors = {};

  @override
  void initState() {
    super.initState();
    _prefill();
  }

  Future<void> _prefill() async {
    final registry = ServerRegistry();
    final name = await registry.phoneName();
    final re = registry.byId(widget.reSignInDeviceId);
    if (!mounted) return;
    setState(() {
      _phoneName.text = name ?? PhoneInfo.defaultName();
      if (re != null) {
        _method = PairingMethod.signIn;
        _address.text = registry.urlFor(re.deviceId) ?? '';
        _deviceId.text = re.deviceId;
      }
    });
  }

  @override
  void dispose() {
    for (final c in [_phoneName, _address, _deviceId, _code, _username, _password, _pastedLink]) {
      c.dispose();
    }
    super.dispose();
  }

  void _clearMessages() {
    _error = null;
    _status = null;
    _fieldErrors.clear();
  }

  Future<void> _run(String working, Future<ServerEntry> Function(PhoneInfo phone) action) async {
    setState(() {
      _clearMessages();
      _busy = true;
      _status = working;
    });
    try {
      final phone = await _service.phoneInfo(name: _phoneName.text, pushToken: PushService().token);
      final entry = await action(phone);
      if (!mounted) return;
      setState(() => _status = 'Paired with ${entry.name}.');
      _password.clear();
      _code.clear();
      final done = widget.onPaired;
      if (done != null) {
        done(entry);
      } else {
        Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const AppShell()), (_) => false);
      }
    } on PairingException catch (e) {
      if (!mounted) return;
      setState(() {
        _status = null;
        if (e.field != null && _fieldVisible(e.field!)) {
          _fieldErrors[e.field!] = e.message;
        } else {
          _error = e.message;
        }
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _status = null;
        _error = 'Pairing failed: $e';
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  bool _fieldVisible(String field) {
    switch (_method) {
      case PairingMethod.scan:
        return false;
      case PairingMethod.code:
        return field == 'address' || field == 'device_id' || field == 'code';
      case PairingMethod.signIn:
        return field == 'address';
    }
  }

  // ------------------------------------------------------------------ actions

  void _usePayloadText(String raw) {
    final PairingPayload payload;
    try {
      payload = PairingPayload.parseQr(raw);
    } on FormatException catch (e) {
      setState(() {
        _clearMessages();
        _error = e.message;
        _lastRejectedScan = raw;
      });
      return;
    }
    if (widget.reSignInDeviceId != null && payload.deviceId != normaliseDeviceId(widget.reSignInDeviceId!)) {
      setState(() {
        _clearMessages();
        _error = 'This QR code is for a different Edge CCTV server.';
      });
      return;
    }
    setState(() => _scannerOpen = false);
    _run('Pairing with ${payload.name ?? 'the store server'}...', (phone) => _service.claimPayload(payload, phone));
  }

  void _onDetect(BarcodeCapture capture) {
    if (_busy) return;
    for (final code in capture.barcodes) {
      final raw = code.rawValue;
      if (raw == null || raw == _lastRejectedScan) continue;
      _usePayloadText(raw);
      return;
    }
  }

  void _pairWithCode() {
    final address = normaliseBaseUrl(_address.text);
    _run(
      'Checking the server...',
      (phone) => _service.claim(
        deviceId: _deviceId.text,
        code: _code.text,
        urls: address == null ? const [] : [address],
        phone: phone,
      ),
    );
  }

  void _signIn() {
    _run(
      'Signing in...',
      (phone) => _service.signIn(
        address: _address.text,
        username: _username.text,
        password: _password.text,
        phone: phone,
        expectedDeviceId: widget.reSignInDeviceId,
      ),
    );
  }

  // ---------------------------------------------------------------------- UI

  @override
  Widget build(BuildContext context) {
    final hasServers = ServerRegistry().servers.isNotEmpty;
    return Scaffold(
      backgroundColor: context.palette.background,
      appBar: AppBar(
        title: Text(widget.reSignInDeviceId != null
            ? 'Sign in again'
            : hasServers
                ? 'Add a store'
                : 'Connect to your store'),
      ),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              Text(
                'Each store\'s edge server has its own device ID, so this phone always talks to the right store '
                'even when two stores use the same network address.',
                style: TextStyle(color: context.palette.muted, fontSize: 13),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _phoneName,
                decoration: const InputDecoration(labelText: 'This phone\'s name', hintText: 'e.g. Duty manager phone'),
              ),
              const SizedBox(height: 16),
              Wrap(
                spacing: 8,
                children: [
                  _methodChip(PairingMethod.scan, 'Scan QR', Icons.qr_code_scanner),
                  _methodChip(PairingMethod.code, 'Pairing code', Icons.pin_outlined),
                  _methodChip(PairingMethod.signIn, 'Sign in', Icons.person_outline),
                ],
              ),
              const SizedBox(height: 16),
              if (_method == PairingMethod.scan) ..._scanForm(),
              if (_method == PairingMethod.code) ..._codeForm(),
              if (_method == PairingMethod.signIn) ..._signInForm(),
              if (_busy) ...[
                const SizedBox(height: 12),
                LinearProgressIndicator(color: context.palette.accent),
              ],
              if (_status != null) ...[
                const SizedBox(height: 12),
                Text(_status!, key: const ValueKey('pairing-status'), style: TextStyle(color: context.palette.live)),
              ],
              if (_error != null) ...[
                const SizedBox(height: 12),
                Container(
                  key: const ValueKey('pairing-error'),
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: context.palette.alert.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: context.palette.alert.withValues(alpha: 0.5)),
                  ),
                  child: Text(_error!, style: TextStyle(color: context.palette.alert)),
                ),
              ],
              if (widget.reSignInDeviceId == null) ...[
                const SizedBox(height: 24),
                Divider(color: context.palette.border),
                TextButton.icon(
                  onPressed: _busy
                      ? null
                      : () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const SetupWizardScreen())),
                  icon: const Icon(Icons.add_business_outlined),
                  label: const Text('Set up a new edge server'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _methodChip(PairingMethod m, String label, IconData icon) => ChoiceChip(
        avatar: Icon(icon, size: 18),
        label: Text(label),
        selected: _method == m,
        onSelected: _busy
            ? null
            : (_) => setState(() {
                  _method = m;
                  _scannerOpen = false;
                  _clearMessages();
                }),
      );

  List<Widget> _scanForm() => [
        Text('On the store dashboard open Settings, Pair a phone, and scan the code shown there.',
            style: TextStyle(color: context.palette.dim(0.70))),
        const SizedBox(height: 12),
        if (_scannerOpen)
          ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: SizedBox(height: 300, child: MobileScanner(onDetect: _onDetect)),
          ),
        const SizedBox(height: 8),
        ElevatedButton.icon(
          onPressed: _busy ? null : () => setState(() => _scannerOpen = !_scannerOpen),
          icon: Icon(_scannerOpen ? Icons.close : Icons.qr_code_scanner),
          label: Text(_scannerOpen ? 'Close camera' : 'Open camera to scan'),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: _pastedLink,
          decoration: const InputDecoration(labelText: 'Or paste the pairing link', hintText: 'edgecctv://pair?...'),
        ),
        const SizedBox(height: 8),
        OutlinedButton(
          onPressed: _busy ? null : () => _usePayloadText(_pastedLink.text),
          child: const Text('Use pairing link'),
        ),
      ];

  List<Widget> _codeForm() => [
        TextField(
          controller: _address,
          keyboardType: TextInputType.url,
          decoration: InputDecoration(
            labelText: 'Server address',
            hintText: '192.168.1.20:8000',
            errorText: _fieldErrors['address'],
            errorMaxLines: 4,
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _deviceId,
          decoration: InputDecoration(
            labelText: 'Device ID',
            hintText: 'shown on the dashboard next to the code',
            errorText: _fieldErrors['device_id'],
            errorMaxLines: 3,
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _code,
          textCapitalization: TextCapitalization.characters,
          decoration: InputDecoration(
            labelText: 'Pairing code',
            hintText: 'ABCD-1234',
            errorText: _fieldErrors['code'],
            errorMaxLines: 3,
          ),
        ),
        const SizedBox(height: 16),
        ElevatedButton(onPressed: _busy ? null : _pairWithCode, child: const Text('Pair this phone')),
      ];

  List<Widget> _signInForm() => [
        TextField(
          controller: _address,
          keyboardType: TextInputType.url,
          decoration: InputDecoration(
            labelText: 'Server address',
            hintText: '192.168.1.20:8000 or https://store.example.com',
            errorText: _fieldErrors['address'],
            errorMaxLines: 4,
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _username,
          autofillHints: const [AutofillHints.username],
          decoration: const InputDecoration(labelText: 'Username'),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _password,
          obscureText: !_showPassword,
          autofillHints: const [AutofillHints.password],
          decoration: InputDecoration(
            labelText: 'Password',
            suffixIcon: IconButton(
              tooltip: _showPassword ? 'Hide password' : 'Show password',
              icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
              onPressed: () => setState(() => _showPassword = !_showPassword),
            ),
          ),
        ),
        const SizedBox(height: 16),
        ElevatedButton(onPressed: _busy ? null : _signIn, child: const Text('Sign in')),
      ];
}
