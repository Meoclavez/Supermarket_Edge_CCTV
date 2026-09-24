"""Per-machine secret store, log redaction and NVR credential encryption."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from app.services import secret_store
from app.services.log_redaction import RedactingFilter, install_log_redaction, redact
from app.services.nvr_credential_service import ENCRYPTED_PREFIX, NVRCredentialService

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.dGVzdC1zaWduYXR1cmUtbm90LXJlYWw"  # gitleaks:allow (synthetic, unsigned test token)
TEST_KEY = "unit-test-only-nvr-key-" + "x" * 40


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _restore_sources():
    """resolve_secrets() records per-process sources; keep the app's view intact."""
    saved = dict(secret_store._sources)
    yield
    secret_store._sources.clear()
    secret_store._sources.update(saved)


# --------------------------------------------------------------- secret store
def test_generates_all_secrets_in_empty_dir_with_private_permissions(tmp_path):
    values = secret_store.resolve_secrets(tmp_path, {})
    path = secret_store.secrets_file(tmp_path)

    assert path.exists()
    assert set(values) == set(secret_store.SECRET_NAMES)
    for v in values.values():
        assert len(v) >= 64 and not secret_store.is_placeholder(v)
    assert len(set(values.values())) == len(values), "each secret must be independent"
    if os.name == "posix":
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700
    assert json.loads(path.read_text()) == values
    # No temp files left behind by the atomic write.
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_env_value_wins_over_store(tmp_path):
    first = secret_store.resolve_secrets(tmp_path, {})
    pinned = "pinned-by-environment-" + "a" * 50
    values = secret_store.resolve_secrets(tmp_path, {"JWT_SECRET": pinned})
    assert values["JWT_SECRET"] == pinned
    assert secret_store.secret_sources()["JWT_SECRET"] == "env"
    # The rest still come from the store, unchanged.
    assert values["INTERNAL_SERVICE_KEY"] == first["INTERNAL_SERVICE_KEY"]
    assert secret_store.secret_sources()["INTERNAL_SERVICE_KEY"] == "store"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        None,
        "CHANGE_ME",
        "CHANGE_ME_BEFORE_DEPLOYMENT",
        "changeme",
        "<generate-me>",
        "super_secret_edge_cctv_key_change_in_prod",
        "edge_ai_vision_internal_secret",
        "cctv_turn_super_secret_dynamic_key_change_me_in_prod",
    ],
)
def test_placeholder_is_treated_as_unset(tmp_path, value):
    assert secret_store.is_placeholder(value)
    values = secret_store.resolve_secrets(tmp_path, {"JWT_SECRET": value, "COTURN_SECRET": value})
    assert values["JWT_SECRET"] != (value or "")
    assert not secret_store.is_placeholder(values["JWT_SECRET"])
    assert secret_store.secret_sources()["JWT_SECRET"] in ("store", "generated")


def test_values_are_stable_across_restarts(tmp_path):
    first = secret_store.resolve_secrets(tmp_path, {})
    mtime = secret_store.secrets_file(tmp_path).stat().st_mtime_ns
    second = secret_store.resolve_secrets(tmp_path, {})
    assert first == second
    assert secret_store.secret_sources() == {n: "store" for n in secret_store.SECRET_NAMES}
    # A restart with everything present must not rewrite the file.
    assert secret_store.secrets_file(tmp_path).stat().st_mtime_ns == mtime


def test_store_fills_only_missing_names_and_tightens_mode(tmp_path):
    first = secret_store.resolve_secrets(tmp_path, {})
    path = secret_store.secrets_file(tmp_path)
    data = json.loads(path.read_text())
    del data["COTURN_SECRET"]
    path.write_text(json.dumps(data))
    os.chmod(path, 0o644)

    again = secret_store.resolve_secrets(tmp_path, {})
    assert again["JWT_SECRET"] == first["JWT_SECRET"]
    assert again["COTURN_SECRET"] != first["COTURN_SECRET"]
    if os.name == "posix":
        assert _mode(path) == 0o600


def test_unwritable_store_falls_back_to_random_not_fixed(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")  # STORAGE_DIR is a file, so secrets/ cannot be created
    a = secret_store.resolve_secrets(blocker, {})
    b = secret_store.resolve_secrets(blocker, {})
    assert secret_store.secret_sources()["JWT_SECRET"] == "ephemeral"
    assert a["JWT_SECRET"] != b["JWT_SECRET"]


def test_settings_have_no_placeholder_secrets():
    from app.config import settings

    for name in secret_store.SECRET_NAMES:
        assert not secret_store.is_placeholder(getattr(settings, name)), name


def test_complete_setup_helper_never_rotates_or_writes_env(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.setup_service import SystemSetupService

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "STORAGE_DIR", tmp_path)
    before = secret_store.resolve_secrets(tmp_path, {})
    assert SystemSetupService.generate_secure_secrets() == []
    after = json.loads(secret_store.secrets_file(tmp_path).read_text())
    assert after == before
    assert not (tmp_path / ".env").exists()


# -------------------------------------------------------------- log redaction
def test_redact_covers_tokens_keys_and_url_passwords():
    line = (
        f'127.0.0.1:5 - "GET /api/v1/cameras/c1/mjpeg?token={FAKE_JWT}&fps=5 HTTP/1.1" 200 '
        "Authorization: Bearer opaque-token-value-123 X-Edge-API-Key: fake-api-key "
        "rtsp://admin:fake-pw@10.0.0.9:554/cam?access_token=abcdef123 "
        f"loose {FAKE_JWT}"
    )
    out = redact(line)
    for secret in (FAKE_JWT, "opaque-token-value-123", "fake-api-key", "fake-pw", "abcdef123"):
        assert secret not in out
    assert "token=<redacted-jwt>" in out
    assert "<redacted-jwt>" in out
    assert "GET /api/v1/cameras/c1/mjpeg" in out and "fps=5" in out
    assert redact(out) == out  # idempotent
    assert redact('{"has_password": true}') == '{"has_password": true}'


def test_log_filter_redacts_uvicorn_access_records_and_keeps_args_shape(tmp_path):
    """uvicorn's AccessFormatter unpacks record.args positionally.

    Order-independent: other tests import the app (which installs redaction)
    and may reconfigure or disable loggers, so the logger state this test
    relies on is set explicitly and restored afterwards.
    """
    from uvicorn.logging import AccessFormatter

    install_log_redaction()
    install_log_redaction()  # idempotent
    fmt = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    access_args = ("127.0.0.1:5000", "GET", f"/api/v1/cameras/c1/snapshot?token={FAKE_JWT}", "1.1", 200)

    # 1. Through the installed record factory, independent of logger config.
    record = logging.getLogRecordFactory()(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', access_args, None
    )
    assert isinstance(record.args, tuple) and len(record.args) == 5
    line = fmt.format(record)
    assert FAKE_JWT not in line
    assert "/api/v1/cameras/c1/snapshot?token=<redacted-jwt>" in line and "200" in line

    # 2. Through the real uvicorn.access logger into a file.
    log_file = tmp_path / "access.log"
    handler = logging.FileHandler(log_file)
    handler.setFormatter(fmt)
    access = logging.getLogger("uvicorn.access")
    saved = (access.level, access.propagate, access.disabled, logging.root.manager.disable)
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    access.propagate = False
    access.disabled = False
    logging.disable(logging.NOTSET)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', *access_args)
    finally:
        access.removeHandler(handler)
        handler.close()
        access.setLevel(saved[0])
        access.propagate = saved[1]
        access.disabled = saved[2]
        logging.disable(saved[3])
    text = log_file.read_text()
    assert FAKE_JWT not in text
    assert "/api/v1/cameras/c1/snapshot?token=<redacted-jwt>" in text

    # 3. The filter alone, with a secret split across format string and args.
    split = logging.LogRecord("edge.x", logging.INFO, __file__, 1, "Bearer %s", ("abcdefgh12345678",), None)
    assert RedactingFilter().filter(split) is True
    assert "abcdefgh12345678" not in split.getMessage()


# ------------------------------------------------------ NVR credentials at rest
def test_nvr_password_round_trip_is_encrypted_on_disk(tmp_path):
    svc = NVRCredentialService(storage_dir=tmp_path, encryption_secret=TEST_KEY)
    saved = svc.save_credentials("operator", "fake-nvr-pw-1", host="10.0.0.5", port=554, default_channels=8)
    assert saved["has_password"] is True
    assert saved["password_masked"] == "••••••••"
    assert "password" not in json.dumps(saved["nvrs"]).replace("has_password", "")

    raw_text = svc.config_path.read_text()
    assert "fake-nvr-pw-1" not in raw_text
    raw = json.loads(raw_text)
    assert raw["default_password"].startswith(ENCRYPTED_PREFIX)
    assert raw["nvrs"]["10.0.0.5"]["password"].startswith(ENCRYPTED_PREFIX)
    if os.name == "posix":
        assert _mode(svc.config_path) == 0o600

    fresh = NVRCredentialService(storage_dir=tmp_path, encryption_secret=TEST_KEY)
    assert fresh.get_auth_for_host("10.0.0.5") == ("operator", "fake-nvr-pw-1")
    assert fresh.get_auth_for_host() == ("operator", "fake-nvr-pw-1")

    # Empty password on save keeps the stored one (unchanged API behaviour).
    fresh.save_credentials("operator", "", host="10.0.0.5")
    assert fresh.get_auth_for_host("10.0.0.5")[1] == "fake-nvr-pw-1"

    # Another machine's key cannot read it and does not crash.
    other = NVRCredentialService(storage_dir=tmp_path, encryption_secret="another-machine-key-" + "y" * 40)
    assert other.get_auth_for_host("10.0.0.5") == ("operator", "")


def test_plaintext_nvr_file_is_migrated_on_first_read(tmp_path):
    path = tmp_path / "nvr_credentials.json"
    path.write_text(
        json.dumps(
            {
                "default_username": "admin",
                "default_password": "fake-legacy-pw",
                "default_port": 554,
                "default_channels": 16,
                "nvrs": {"10.0.0.7": {"host": "10.0.0.7", "username": "admin", "password": "fake-legacy-pw",
                                      "port": 554, "channels": 16}},
            }
        )
    )
    os.chmod(path, 0o644)

    svc = NVRCredentialService(storage_dir=tmp_path, encryption_secret=TEST_KEY)
    assert svc.get_auth_for_host("10.0.0.7") == ("admin", "fake-legacy-pw")

    migrated = path.read_text()
    assert "fake-legacy-pw" not in migrated
    data = json.loads(migrated)
    assert data["default_password"].startswith(ENCRYPTED_PREFIX)
    assert data["nvrs"]["10.0.0.7"]["password"].startswith(ENCRYPTED_PREFIX)
    if os.name == "posix":
        assert _mode(path) == 0o600
    assert svc.get_safe_credentials()["nvrs"]["10.0.0.7"]["has_password"] is True
