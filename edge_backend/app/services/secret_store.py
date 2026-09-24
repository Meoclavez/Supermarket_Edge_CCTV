"""Per-machine secret store.

Every installation (this development PC, the test box, each store's edge PC)
gets its own random secrets, and none of them ever lives in the repository.

Resolution order for each secret, first match wins:

1. the process environment (``JWT_SECRET=... uvicorn ...``, systemd, docker);
2. ``edge_backend/.env`` (pydantic-settings reads it into the same field);
3. ``<STORAGE_DIR>/secrets/device_secrets.json`` on this machine.

A value that is empty or looks like a shipped placeholder (``CHANGE_ME...``,
the old hardcoded defaults, ``<...>``) is treated as unset, so copying
``.env.example`` verbatim can never produce a known secret.

Missing secrets are generated on first start with ``secrets.token_urlsafe(64)``
and written atomically to the store file (directory 0700, file 0600). Later
starts reuse them, so sessions and encrypted NVR passwords survive restarts.

This module must not import ``app.config`` (config imports it).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional

logger = logging.getLogger("edge.security")

SECRET_NAMES: tuple[str, ...] = (
    "JWT_SECRET",
    "AUTH_SECRET_KEY",
    "INTERNAL_SERVICE_KEY",
    "COTURN_SECRET",
    "NVR_CREDENTIAL_KEY",
)

SECRETS_DIRNAME = "secrets"
SECRETS_FILENAME = "device_secrets.json"

# Values that shipped in earlier versions of this repository. They are public
# (they are in git history), so they are never accepted as a real secret.
_LEGACY_DEFAULTS = frozenset(
    {
        "super_secret_edge_cctv_key_change_in_prod",
        "edge_ai_vision_internal_secret",
        "cctv_turn_super_secret_dynamic_key_change_me_in_prod",
    }
)
_PLACEHOLDER_MARKERS = (
    "change_me",
    "changeme",
    "change-me",
    "change_in_prod",
    "replace_me",
    "your_secret",
    "placeholder",
)

# name -> "env" | "store" | "generated" | "ephemeral"; filled by resolve_secrets().
_sources: dict[str, str] = {}


def is_placeholder(value: Optional[str]) -> bool:
    """True when ``value`` must not be used as a real secret."""
    if value is None:
        return True
    v = str(value).strip().strip('"').strip("'")
    if not v:
        return True
    low = v.lower()
    if low in _LEGACY_DEFAULTS:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def secrets_file(storage_dir: Path | str) -> Path:
    return Path(storage_dir) / SECRETS_DIRNAME / SECRETS_FILENAME


def generate_secret() -> str:
    return secrets.token_urlsafe(64)


@contextlib.contextmanager
def _locked(directory: Path):
    """Serialise first-start generation across processes (uvicorn workers)."""
    try:
        import fcntl  # POSIX only
    except ImportError:  # pragma: no cover - Windows dev boxes
        yield
        return
    lock_path = directory / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)


def _read_store(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Keep the unreadable file for inspection instead of silently losing
        # the NVR encryption key it may hold.
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        with contextlib.suppress(OSError):
            path.replace(backup)
        logger.error(f"Secret store {path} is unreadable ({exc.__class__.__name__}); moved to {backup.name}")
        return {}
    if not isinstance(data, dict):
        return {}
    # Tighten permissions if someone loosened them.
    with contextlib.suppress(OSError):
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _write_store_atomic(path: Path, data: Mapping[str, str]) -> None:
    """Write ``data`` to ``path`` with mode 0600 from the first byte."""
    fd, tmp = tempfile.mkstemp(prefix=".device_secrets.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(data), fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_or_create(storage_dir: Path | str, names: Iterable[str] = SECRET_NAMES) -> tuple[dict[str, str], set[str]]:
    """Return ``(values, generated_names)`` from the machine-local store.

    Missing or placeholder entries are generated and persisted. Raises
    ``OSError`` if the store cannot be created or written.
    """
    path = secrets_file(storage_dir)
    _ensure_private_dir(path.parent)
    with _locked(path.parent):
        stored = _read_store(path)
        generated: set[str] = set()
        for name in names:
            if is_placeholder(stored.get(name)):
                stored[name] = generate_secret()
                generated.add(name)
        if generated or not path.exists():
            _write_store_atomic(path, stored)
    return stored, generated


def resolve_secrets(storage_dir: Path | str, provided: Mapping[str, Optional[str]]) -> dict[str, str]:
    """Resolve every secret in ``SECRET_NAMES``.

    ``provided`` holds what the environment / .env supplied (already merged by
    pydantic-settings). Anything missing or placeholder comes from the store.
    """
    resolved: dict[str, str] = {}
    need_store = []
    for name in SECRET_NAMES:
        value = provided.get(name)
        if not is_placeholder(value):
            resolved[name] = str(value).strip()
            _sources[name] = "env"
        else:
            need_store.append(name)

    if need_store:
        try:
            stored, generated = load_or_create(storage_dir, need_store)
            for name in need_store:
                resolved[name] = stored[name]
                _sources[name] = "generated" if name in generated else "store"
            if generated:
                logger.info(
                    f"Generated machine-local secrets {sorted(generated)} in {secrets_file(storage_dir)} (mode 0600)"
                )
        except OSError as exc:
            # Never fall back to a fixed value. Random per-process secrets keep
            # the system safe; they just do not survive a restart.
            logger.error(
                f"Cannot persist secrets under {Path(storage_dir) / SECRETS_DIRNAME} ({exc}); "
                "using random in-memory secrets for this run only"
            )
            for name in need_store:
                resolved[name] = generate_secret()
                _sources[name] = "ephemeral"
    return resolved


def secret_sources() -> dict[str, str]:
    """Where each secret came from in this process (never the values)."""
    return dict(_sources)


def ensure_device_secrets(storage_dir: Path | str) -> list[str]:
    """Make sure the store exists and holds every secret. Never rotates.

    Returns the names that had to be generated (usually none).
    """
    _, generated = load_or_create(storage_dir)
    return sorted(generated)


def fernet_key_from_secret(secret: str) -> bytes:
    """Derive a Fernet key from any secret string (HKDF-SHA256)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"edge-cctv/nvr-credentials",
        info=b"fernet-v1",
    ).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


# --------------------------------------------------------------- named secrets
#
# Operator-supplied credentials (remote-access tunnel token, push provider
# service account, ...) are stored one file per name under
# ``<STORAGE_DIR>/secrets/named/<name>.enc``, Fernet-encrypted with a key
# derived from this machine's NVR_CREDENTIAL_KEY. They are write-only through
# the API: callers may check presence, but only server code reads values.

NAMED_DIRNAME = "named"
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-.")


def _named_path(storage_dir: Path | str, name: str) -> Path:
    if not name or len(name) > 64 or set(name) - _NAME_CHARS or name.startswith("."):
        raise ValueError(f"invalid secret name {name!r}")
    return Path(storage_dir) / SECRETS_DIRNAME / NAMED_DIRNAME / f"{name}.enc"


def _named_fernet(machine_key: str):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"edge-cctv/named-secrets",
        info=b"fernet-v1",
    ).derive(machine_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(raw))


def set_named_secret(storage_dir: Path | str, name: str, value: bytes | str, machine_key: str) -> None:
    """Encrypt and store ``value`` under ``name`` (atomic, mode 0600)."""
    path = _named_path(storage_dir, name)
    _ensure_private_dir(path.parent.parent)
    _ensure_private_dir(path.parent)
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    token = _named_fernet(machine_key).encrypt(data)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def get_named_secret(storage_dir: Path | str, name: str, machine_key: str) -> Optional[bytes]:
    """Return the decrypted value, or None if absent or undecryptable."""
    path = _named_path(storage_dir, name)
    try:
        token = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return _named_fernet(machine_key).decrypt(token)
    except Exception:  # wrong machine key or corrupted file
        logger.error(f"Named secret {name!r} could not be decrypted with this machine's key")
        return None


def has_named_secret(storage_dir: Path | str, name: str) -> bool:
    return _named_path(storage_dir, name).is_file()


def delete_named_secret(storage_dir: Path | str, name: str) -> bool:
    path = _named_path(storage_dir, name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
