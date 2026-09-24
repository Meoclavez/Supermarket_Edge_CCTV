import 'package:flutter_test/flutter_test.dart';
import 'package:edge_ai_cctv/core/operator_policy.dart';

void main() {
  group('username policy (auth_service.USERNAME_RE)', () {
    test('accepts valid names', () {
      for (final u in ['abc', 'store.manager', 'lp_team-1', 'A1b']) {
        expect(usernamePolicyError(u), isNull, reason: u);
      }
    });
    test('rejects invalid names', () {
      for (final u in ['ab', '_admin', '.x1', 'has space', 'a' * 65, 'é-user']) {
        expect(usernamePolicyError(u), isNotNull, reason: u);
      }
    });
  });

  group('password policy (auth_service.password_policy_error)', () {
    test('length bounds', () {
      expect(passwordPolicyError('1234567'), contains('at least 8'));
      expect(passwordPolicyError('12345678'), isNull);
      expect(passwordPolicyError('x' * 257), contains('at most 256'));
    });
    test('only spaces is rejected', () {
      expect(passwordPolicyError('        '), 'Password cannot be only spaces.');
    });
    test('same as username (case-insensitive, trimmed) is rejected', () {
      expect(passwordPolicyError(' Manager01 ', username: 'manager01'), contains('same as the username'));
      expect(passwordPolicyError('manager01!', username: 'manager01'), isNull);
    });
  });

  test('setup code normalisation matches setup_service.normalise_setup_code', () {
    expect(normaliseSetupCode('abcd-1234'), 'ABCD1234');
    expect(normaliseSetupCode(' AbCd 12 34 '), 'ABCD1234');
    expect(normaliseSetupCode('--'), '');
  });

  group('describeHttpError', () {
    test('403 uses the server detail', () {
      expect(describeHttpError(403, '{"detail":"Setup code is missing or incorrect."}'), 'Setup code is missing or incorrect.');
    });
    test('429 without detail gives a wait hint from Retry-After', () {
      expect(describeHttpError(429, '', retryAfter: '900'), contains('about 15 min'));
    });
    test('422 flattens FastAPI validation errors', () {
      const body = '{"detail":[{"loc":["body","username"],"msg":"Field required","type":"missing"}]}';
      expect(describeHttpError(422, body), 'The server rejected the form: username: Field required');
    });
    test('non-JSON body falls back to the status code', () {
      expect(describeHttpError(500, '<html>'), 'Server returned HTTP 500.');
    });
  });
}
