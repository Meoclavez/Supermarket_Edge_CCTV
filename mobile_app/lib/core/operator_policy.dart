import 'dart:convert';

/// Client-side copies of the edge server's operator account rules
/// (edge_backend/app/services/auth_service.py: USERNAME_RE,
/// PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, password_policy_error,
/// username_policy_error) and setup-code normalisation
/// (setup_service.normalise_setup_code). The server remains the authority;
/// these only give earlier, identical feedback.

const int kPasswordMinLength = 8;
const int kPasswordMaxLength = 256;
final RegExp kUsernamePattern = RegExp(r'^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$');

String? usernamePolicyError(String username) {
  if (!kUsernamePattern.hasMatch(username)) {
    return 'Username must be 3-64 characters: letters, digits, dot, dash or '
        'underscore, starting with a letter or digit.';
  }
  return null;
}

String? passwordPolicyError(String password, {String? username}) {
  if (password.length < kPasswordMinLength) {
    return 'Password must be at least $kPasswordMinLength characters.';
  }
  if (password.length > kPasswordMaxLength) {
    return 'Password must be at most $kPasswordMaxLength characters.';
  }
  if (password.trim().isEmpty) return 'Password cannot be only spaces.';
  if (username != null &&
      username.isNotEmpty &&
      password.trim().toLowerCase() == username.trim().toLowerCase()) {
    return 'Password must not be the same as the username.';
  }
  return null;
}

/// Upper-case letters and digits only, as the server compares them
/// (so `abcd-1234`, `ABCD 1234` and `ABCD1234` are the same code).
String normaliseSetupCode(String code) =>
    code.toUpperCase().split('').where((c) => RegExp(r'[A-Z0-9]').hasMatch(c)).join();

/// Turns an error response from the setup/auth endpoints into a sentence for
/// an inline error line. Understands FastAPI's `{"detail": "..."}` and the
/// 422 validation form `{"detail": [{"loc": [...], "msg": "..."}]}`.
String describeHttpError(int statusCode, String body, {String? retryAfter}) {
  String? detail;
  try {
    final decoded = jsonDecode(body);
    final d = decoded is Map ? decoded['detail'] : null;
    if (d is String) {
      detail = d;
    } else if (d is List) {
      detail = d.map((e) {
        if (e is Map) {
          final loc = e['loc'] is List ? (e['loc'] as List).where((p) => p != 'body').join('.') : '';
          final msg = e['msg']?.toString() ?? '';
          return loc.isEmpty ? msg : '$loc: $msg';
        }
        return e.toString();
      }).join('; ');
    }
  } catch (_) {
    // Not JSON: fall through to the generic messages below.
  }

  switch (statusCode) {
    case 401:
      return detail ?? 'Your session is missing or expired. Sign in again.';
    case 403:
      return detail ??
          'Setup code is missing or incorrect. It is printed in the server log at startup '
              'and stored in storage/setup_code.txt on the server.';
    case 422:
      return 'The server rejected the form: ${detail ?? 'invalid fields'}';
    case 429:
      final wait = int.tryParse(retryAfter ?? '');
      final hint = wait != null ? ' Try again in about ${(wait / 60).ceil()} min.' : '';
      return detail ?? 'Too many failed attempts from this device.$hint';
    default:
      return detail ?? 'Server returned HTTP $statusCode.';
  }
}
