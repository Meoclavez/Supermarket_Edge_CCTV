import 'dart:async';
import 'dart:developer' as developer;
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../services/api_service.dart';

enum ConnectionState { CONNECTED, CONNECTING, RECONNECTING, DISCONNECTED, SERVER_UNREACHABLE }

class ConnectionMonitor extends ChangeNotifier {
  static final ConnectionMonitor _instance = ConnectionMonitor._internal();
  factory ConnectionMonitor() => _instance;
  ConnectionMonitor._internal();

  ConnectionState _state = ConnectionState.DISCONNECTED;
  DateTime? _lastSuccessfulPing;
  int _consecutiveFailures = 0;
  Timer? _pingTimer;
  Timer? _reconnectTimer;
  
  String _baseUrl = 'http://localhost:8000';

  ConnectionState get state => _state;
  bool get isOnline => _state == ConnectionState.CONNECTED;
  DateTime? get lastSuccessfulPing => _lastSuccessfulPing;
  int get consecutiveFailures => _consecutiveFailures;

  void setBaseUrl(String url) {
    _baseUrl = url;
  }

  void startMonitoring() {
    _state = ConnectionState.CONNECTING;
    notifyListeners();
    _pingTimer?.cancel();
    _pingTimer = Timer.periodic(const Duration(seconds: 10), (_) => _ping());
    _ping();
  }

  void stopMonitoring() {
    _pingTimer?.cancel();
    _reconnectTimer?.cancel();
    _state = ConnectionState.DISCONNECTED;
    notifyListeners();
  }

  Future<void> _ping() async {
    if (_state == ConnectionState.RECONNECTING) return;
    try {
      final response = await http.get(Uri.parse('$_baseUrl/api/v1/health')).timeout(const Duration(seconds: 5));
      if (response.statusCode == 200) {
        _handleSuccess();
      } else {
        _handleFailure();
      }
    } catch (e) {
      _handleFailure();
    }
  }

  void _handleSuccess() {
    if (_state != ConnectionState.CONNECTED) {
      developer.log('Connection restored', name: 'ConnectionMonitor');
      _state = ConnectionState.CONNECTED;
      ApiService().notifyConnectionRestored();
    }
    _lastSuccessfulPing = DateTime.now();
    _consecutiveFailures = 0;
    _reconnectTimer?.cancel();
    notifyListeners();
  }

  void _handleFailure() {
    _consecutiveFailures++;
    developer.log('Ping failed. Failures: $_consecutiveFailures', name: 'ConnectionMonitor');
    
    if (_state == ConnectionState.CONNECTED || _state == ConnectionState.CONNECTING) {
      _state = ConnectionState.DISCONNECTED;
      notifyListeners();
      _attemptReconnect();
    }
  }

  void _attemptReconnect() {
    if (_state == ConnectionState.RECONNECTING) return;
    _state = ConnectionState.RECONNECTING;
    notifyListeners();

    int backoffSeconds = min(30, pow(2, _consecutiveFailures - 1).toInt());
    developer.log('Attempting reconnect in $backoffSeconds seconds', name: 'ConnectionMonitor');
    
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(Duration(seconds: backoffSeconds), () async {
      try {
        final response = await http.get(Uri.parse('$_baseUrl/api/v1/health')).timeout(const Duration(seconds: 5));
        if (response.statusCode == 200) {
          _handleSuccess();
        } else {
          _state = ConnectionState.SERVER_UNREACHABLE;
          notifyListeners();
          _attemptReconnect();
        }
      } catch (e) {
        _state = ConnectionState.SERVER_UNREACHABLE;
        notifyListeners();
        _attemptReconnect();
      }
    });
  }
}

class ErrorBoundary extends StatefulWidget {
  final Widget child;
  
  const ErrorBoundary({Key? key, required this.child}) : super(key: key);

  @override
  _ErrorBoundaryState createState() => _ErrorBoundaryState();
}

class _ErrorBoundaryState extends State<ErrorBoundary> {
  Object? _error;

  @override
  void initState() {
    super.initState();
    ErrorWidget.builder = (FlutterErrorDetails details) {
      developer.log('ErrorBoundary caught error', error: details.exception, stackTrace: details.stack, name: 'ErrorBoundary');
      return _buildErrorWidget(details.exceptionAsString());
    };
  }

  Widget _buildErrorWidget(String error) {
    return Material(
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(16.0),
          child: Card(
            color: Colors.red[50],
            child: Padding(
              padding: const EdgeInsets.all(16.0),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.error_outline, color: Colors.red, size: 48),
                  const SizedBox(height: 16),
                  const Text('Something went wrong', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.red)),
                  const SizedBox(height: 8),
                  Text(error, textAlign: TextAlign.center, style: TextStyle(color: Colors.red[900])),
                  const SizedBox(height: 16),
                  ElevatedButton(
                    onPressed: () {
                      setState(() {
                        _error = null;
                      });
                    },
                    child: const Text('Retry'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return _buildErrorWidget(_error.toString());
    }
    return widget.child;
  }
}

class OfflineIndicatorBanner extends StatelessWidget {
  final ConnectionState state;

  const OfflineIndicatorBanner({Key? key, required this.state}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    if (state == ConnectionState.CONNECTED || state == ConnectionState.CONNECTING) {
      return const SizedBox.shrink();
    }

    String message = 'Disconnected from Server';
    Color bgColor = Colors.red;

    if (state == ConnectionState.RECONNECTING) {
      message = 'Reconnecting...';
      bgColor = Colors.amber;
    } else if (state == ConnectionState.SERVER_UNREACHABLE) {
      message = 'Server Unreachable - Retrying';
      bgColor = Colors.red;
    }

    return Container(
      color: bgColor,
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 16),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.wifi_off, color: Colors.white, size: 16),
          const SizedBox(width: 8),
          Text(message, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
        ],
      ),
    );
  }
}
