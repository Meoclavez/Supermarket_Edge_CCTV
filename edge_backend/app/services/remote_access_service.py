"""Remote access: publish the local dashboard on the operator's own domain.

Two providers:

``cloudflare_tunnel`` (primary)
    A remotely managed Cloudflare Tunnel. The operator creates the tunnel in
    the Cloudflare Zero Trust dashboard, routes a public hostname on their own
    domain to ``http://localhost:<PORT>`` and pastes the tunnel token here.
    This service then runs ``cloudflared tunnel run`` as a child process. The
    connection is outbound only, so it needs no port forwarding, works behind
    carrier-grade NAT, and Cloudflare terminates TLS on the operator's domain.

``direct``
    For sites with a public IP and port forwarding. The operator runs their own
    reverse proxy; :func:`caddy_site_block` generates a Caddy site block for the
    hostname. This service does not run Caddy.

Settings live in ``system_setup`` under the key ``remote_access`` (JSON:
``enabled``, ``provider``, ``hostname`` plus the last verification result).
The tunnel token is a named secret (``cloudflare_tunnel_token``) encrypted
with this machine's key. It is passed to cloudflared in the ``TUNNEL_TOKEN``
environment variable, never on the command line (where ``ps`` would show it),
and it is never logged or returned by the API.

The tunnel only runs when remote access is enabled AND a hostname AND a token
are configured AND authentication is on. ``AUTH_DISABLED`` refuses it: an
unauthenticated system must never be reachable from the internet.

The private route, ``tailscale serve`` on the owner's tailnet, is set up by
``deploy/install.sh`` (``EDGE_TAILSCALE_SERVE=1``) because it needs root.
This module only reports it, read-only (:func:`read_tailscale_status`,
``status()["tailscale"]``), so Settings -> Online access can show the
``https://<machine>.<tailnet>.ts.net`` address or say what is missing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings

logger = logging.getLogger("edge.remote_access")

SETTINGS_KEY = "remote_access"
TOKEN_SECRET_NAME = "cloudflare_tunnel_token"
PROVIDERS = ("cloudflare_tunnel", "direct")
REPO_DIR = Path(__file__).resolve().parents[3]

# Process states reported to the dashboard.
STOPPED, STARTING, CONNECTED, ERROR = "stopped", "starting", "connected", "error"

_HOSTNAME_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)
_TOKEN_WORD_RE = re.compile(r"eyJ[A-Za-z0-9_\-+/=]{40,}")
# cloudflared log line: "2026-09-23T10:00:00Z INF Registered tunnel connection ..."
_LEVEL_RE = re.compile(r"^\S+\s+(DBG|INF|WRN|ERR|FTL)\s+(.*)$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

class RemoteAccessError(ValueError):
    """A configuration the operator must correct; the message is shown as is."""


def normalise_hostname(value: Optional[str]) -> str:
    """Accept ``cctv.example.com`` or a pasted ``https://cctv.example.com/``.

    Returns "" for empty input. Raises RemoteAccessError for anything that is
    not a public DNS name (IP addresses, ``localhost``, ports, paths).
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^[a-z]+://", "", raw).rstrip("/")
    if "/" in raw or ":" in raw or "@" in raw:
        raise RemoteAccessError(
            "Enter only the hostname, for example cctv.yourstore.com.au "
            "(no port, path or login)."
        )
    raw = raw.rstrip(".")
    if not _HOSTNAME_RE.match(raw) or raw.endswith(".local") or raw.endswith(".localhost"):
        raise RemoteAccessError(
            f"'{value}' is not a public hostname. Use a name on your own domain, "
            "for example cctv.yourstore.com.au."
        )
    return raw


def extract_tunnel_token(value: Optional[str]) -> str:
    """Pull the tunnel token out of what the operator pasted.

    Cloudflare shows the token inside an install command such as
    ``sudo cloudflared service install eyJhIjoi...``; accept the whole command
    as well as the bare token. A remotely managed tunnel token is base64 JSON
    carrying the account tag ``a``, tunnel id ``t`` and secret ``s``.
    """
    text = (value or "").strip()
    if not text:
        raise RemoteAccessError("Paste the tunnel token from the Cloudflare dashboard.")
    match = _TOKEN_WORD_RE.search(text)
    token = match.group(0) if match else text
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = json.loads(base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True))
        ok = isinstance(decoded, dict) and all(decoded.get(k) for k in ("a", "t", "s"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        ok = False
    if not ok:
        raise RemoteAccessError(
            "That is not a Cloudflare tunnel token. In Zero Trust -> Networks -> Tunnels, open "
            "your tunnel, choose any install command and copy the long value that starts with 'eyJ'."
        )
    return token


def caddy_site_block(hostname: str, port: Optional[int] = None) -> str:
    """Caddy v2 site block for the ``direct`` provider (Caddy obtains the certificate)."""
    port = port or settings.PORT
    host = hostname or "cctv.example.com"
    return (
        f"{host} {{\n"
        f"    encode gzip\n"
        f"    # Streams (MJPEG /stream, websockets) must not be buffered.\n"
        f"    reverse_proxy 127.0.0.1:{port} {{\n"
        f"        flush_interval -1\n"
        f"    }}\n"
        f"}}\n"
    )


# --------------------------------------------------------------------------- #
# Settings persistence (sync sqlite so any thread, and middleware, can read it)
# --------------------------------------------------------------------------- #

def _default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": "cloudflare_tunnel",
        "hostname": "",
        "verified": False,
        "verified_at": None,
        "verified_hostname": None,
        "verify_error": None,
    }


def _db_path() -> Path:
    return Path(settings.DATABASE_PATH)


def load_settings() -> dict[str, Any]:
    data = _default_settings()
    path = _db_path()
    if not path.exists():
        return data
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = ?", (SETTINGS_KEY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"Could not read remote access settings: {exc}")
        return data
    if row:
        try:
            stored = json.loads(row[0])
            if isinstance(stored, dict):
                data.update({k: stored[k] for k in data if k in stored})
        except ValueError:
            logger.warning("Stored remote access settings are not valid JSON; using defaults")
    if data.get("provider") not in PROVIDERS:
        data["provider"] = "cloudflare_tunnel"
    return data


def save_settings(data: dict[str, Any]) -> None:
    clean = {k: data.get(k) for k in _default_settings()}
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    try:
        conn.execute(
            "INSERT INTO system_setup (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(clean), datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")),
        )
        conn.commit()
    finally:
        conn.close()


def _secret_api():
    from app.services import secret_store

    return secret_store


def token_configured() -> bool:
    try:
        return _secret_api().has_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME)
    except Exception:
        return False


def _read_token() -> Optional[str]:
    raw = _secret_api().get_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME, settings.NVR_CREDENTIAL_KEY)
    return raw.decode("utf-8") if raw else None


def this_device_id() -> Optional[str]:
    """The device id served at /api/v1/device/identity (device_identity.py)."""
    try:
        from app.services.device_identity import get_identity

        return (get_identity() or {}).get("device_id")
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(f"device identity unavailable: {exc}")
        return None
    # device_identity.py not installed yet: read the same system_setup key it owns.
    try:
        conn = sqlite3.connect(str(_db_path()), timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM system_setup WHERE key = 'device_id'").fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- #
# Tailscale (private HTTPS for the owner's tailnet): read-only status
# --------------------------------------------------------------------------- #

TAILSCALE_ADMIN_DNS_URL = "https://login.tailscale.com/admin/dns"
TAILSCALE_CACHE_SECONDS = 30.0


def find_tailscale() -> Optional[str]:
    explicit = os.environ.get("TAILSCALE_PATH")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    return shutil.which("tailscale")


def _run_tailscale(binary: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _serve_targets(serve: dict[str, Any], dns_name: str) -> tuple[list[str], bool]:
    """(proxy targets published on https://<dns_name>:443, whether Funnel is on for it)."""
    targets: list[str] = []
    web = serve.get("Web") or {}
    for hostport, cfg in web.items():
        host, _, port = str(hostport).rpartition(":")
        if port != "443" or (dns_name and host.rstrip(".").lower() != dns_name):
            continue
        for handler in ((cfg or {}).get("Handlers") or {}).values():
            proxy = (handler or {}).get("Proxy")
            if proxy:
                targets.append(str(proxy))
    funnel = any(bool(v) and str(k).rpartition(":")[0].rstrip(".").lower() == dns_name
                 for k, v in (serve.get("AllowFunnel") or {}).items())
    return targets, funnel


def _targets_this_port(target: str, port: int) -> bool:
    from urllib.parse import urlsplit

    raw = target if "://" in target else f"http://{target}"
    try:
        parts = urlsplit(raw)
        return parts.hostname in ("127.0.0.1", "localhost", "::1") and parts.port == port
    except ValueError:
        return False


def read_tailscale_status(port: Optional[int] = None,
                          finder: Callable[[], Optional[str]] = find_tailscale,
                          runner: Callable[..., subprocess.CompletedProcess] = _run_tailscale) -> dict[str, Any]:
    """What ``tailscale status``/``serve status`` say about private HTTPS for this dashboard.

    Read-only and never raises: a machine without Tailscale, a daemon that is
    down, or a CLI this user may not run all come back as a state + message.
    """
    port = port or settings.PORT
    serve_cmd = f"sudo tailscale serve --bg --https=443 http://127.0.0.1:{port}"
    out: dict[str, Any] = {
        "installed": False, "state": "not_installed", "message": None, "dns_name": None,
        "tailnet": None, "https_enabled": False, "serving": None, "funnel": False, "url": None,
        "serve_command": serve_cmd, "admin_url": TAILSCALE_ADMIN_DNS_URL,
    }
    binary = finder()
    if not binary:
        out["message"] = "Tailscale is not installed on this machine."
        return out
    out["installed"] = True
    try:
        res = runner(binary, "status", "--json")
    except (OSError, subprocess.SubprocessError) as exc:
        out.update(state="unavailable", message=f"tailscale status could not run: {exc.__class__.__name__}")
        return out
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        text = re.sub(r"\s+", " ", (res.stderr or res.stdout or "")).strip()[:200]
        out.update(state="unavailable", message=text or f"tailscale status failed (exit {res.returncode}).")
        return out
    backend = data.get("BackendState") or ""
    this = data.get("Self") or {}
    dns_name = str(this.get("DNSName") or "").rstrip(".").lower() or None
    cert_domains = [str(d).rstrip(".").lower() for d in (data.get("CertDomains") or [])]
    out.update(dns_name=dns_name, tailnet=(data.get("CurrentTailnet") or {}).get("Name"),
               https_enabled=bool(dns_name and dns_name in cert_domains))
    if backend != "Running":
        out.update(state="stopped", message=f"Tailscale is not connected (state: {backend or 'unknown'}).")
        return out
    out["state"] = "running"
    if not dns_name:
        out["message"] = "MagicDNS is off, so this machine has no tailnet name. Turn on MagicDNS."
        return out
    if not out["https_enabled"]:
        out["message"] = ("HTTPS certificates are not enabled for this tailnet. In the Tailscale admin "
                          "console open DNS, turn on MagicDNS and HTTPS Certificates.")
    try:
        sres = runner(binary, "serve", "status", "--json")
        serve = json.loads(sres.stdout or "{}") if sres.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        serve = None
    if isinstance(serve, dict):
        targets, funnel = _serve_targets(serve, dns_name)
        out["serving"] = any(_targets_this_port(t, port) for t in targets)
        out["funnel"] = funnel
        if out["serving"]:
            out["url"] = f"https://{dns_name}/dashboard"
        elif targets:
            out["message"] = (f"tailscale serve publishes https://{dns_name} but not to this dashboard "
                              f"({', '.join(targets)[:120]}). Run: {serve_cmd}")
        elif out["https_enabled"]:
            out["message"] = f"Not published on the tailnet yet. On this machine run: {serve_cmd}"
    return out


def find_cloudflared() -> Optional[str]:
    """``CLOUDFLARED_PATH``, then PATH, then ``<repo>/bin/cloudflared`` (bootstrap.py)."""
    explicit = os.environ.get("CLOUDFLARED_PATH")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("cloudflared")
    if found:
        return found
    for name in ("cloudflared", "cloudflared.exe"):
        local = REPO_DIR / "bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return None


# --------------------------------------------------------------------------- #
# Process manager
# --------------------------------------------------------------------------- #

class RemoteAccessService:
    """Supervises one cloudflared child process according to the settings."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: dict[str, Any] = _default_settings()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.state = STOPPED
        self.last_error: Optional[str] = None
        self.restarts = 0
        self.connections = 0
        self.started_at: Optional[float] = None
        self.backoff_base = 2.0
        self.backoff_max = 60.0
        self.stable_after = 60.0  # seconds up before the backoff resets
        self.verify_interval = 600.0
        self._last_verify = 0.0
        self._secret_redactions: tuple[str, ...] = ()
        # Injectable for tests.
        self.binary_finder: Callable[[], Optional[str]] = find_cloudflared
        self.tailscale_reader: Callable[[], dict[str, Any]] = read_tailscale_status
        self._tailscale_cache: Optional[tuple[float, dict[str, Any]]] = None

    # -- settings -----------------------------------------------------------

    def reload_settings(self) -> dict[str, Any]:
        data = load_settings()
        with self._lock:
            self._settings = data
        return dict(data)

    @property
    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def hostname(self) -> str:
        """Configured hostname (even when disabled): requests using it are remote."""
        with self._lock:
            return self._settings.get("hostname") or ""

    def public_url(self) -> Optional[str]:
        """``https://<hostname>`` while remote access is enabled, else None."""
        s = self.settings
        if s.get("enabled") and s.get("hostname"):
            return f"https://{s['hostname']}"
        return None

    def update(self, *, enabled: Optional[bool] = None, provider: Optional[str] = None,
               hostname: Optional[str] = None, token: Optional[str] = None,
               clear_token: bool = False) -> dict[str, Any]:
        """Validate and persist a change, then start/stop the tunnel to match."""
        with self._lock:
            new = dict(self._settings)
            if provider is not None:
                if provider not in PROVIDERS:
                    raise RemoteAccessError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
                new["provider"] = provider
            if hostname is not None:
                new["hostname"] = normalise_hostname(hostname)
            parsed_token = extract_tunnel_token(token) if token else None
            if enabled is not None:
                new["enabled"] = bool(enabled)

            will_have_token = bool(parsed_token) or (token_configured() and not clear_token)
            if new["enabled"]:
                if settings.AUTH_DISABLED:
                    raise RemoteAccessError(
                        "Remote access cannot be enabled while AUTH_DISABLED=true: the dashboard would be "
                        "open to anyone on the internet. Remove AUTH_DISABLED and restart first."
                    )
                if not new["hostname"]:
                    raise RemoteAccessError("Enter the public hostname before enabling remote access.")
                if new["provider"] == "cloudflare_tunnel" and not will_have_token:
                    raise RemoteAccessError("Paste the Cloudflare tunnel token before enabling remote access.")

            if new["hostname"] != self._settings.get("hostname"):
                new.update(verified=False, verified_at=None, verified_hostname=None, verify_error=None)

            if parsed_token:
                _secret_api().set_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME, parsed_token,
                                               settings.NVR_CREDENTIAL_KEY)
                logger.info("Cloudflare tunnel token stored (encrypted)")
            elif clear_token:
                _secret_api().delete_named_secret(settings.STORAGE_DIR, TOKEN_SECRET_NAME)
                logger.info("Cloudflare tunnel token removed")
            save_settings(new)
            self._settings = new
            token_changed = bool(parsed_token) or clear_token
        logger.info(
            f"Remote access settings saved: enabled={new['enabled']} provider={new['provider']} "
            f"hostname={new['hostname'] or '-'}"
        )
        self.apply(restart=token_changed)
        return self.status()

    # -- desired state --------------------------------------------------------

    def should_run(self) -> tuple[bool, Optional[str]]:
        s = self.settings
        if not s.get("enabled"):
            return False, None
        if settings.AUTH_DISABLED:
            return False, "AUTH_DISABLED=true: the tunnel is not started while authentication is off."
        if s.get("provider") != "cloudflare_tunnel":
            return False, None  # direct: an external reverse proxy serves the hostname
        if not s.get("hostname"):
            return False, "No public hostname configured."
        if not token_configured():
            return False, "No Cloudflare tunnel token configured."
        return True, None

    def apply(self, restart: bool = False) -> None:
        """Start or stop the tunnel so it matches the settings."""
        run, reason = self.should_run()
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
        if running and (not run or restart):
            self._stop_supervisor()
            running = False
        if run and not running:
            self._start_supervisor()
        elif not run:
            with self._lock:
                self.state = ERROR if reason else STOPPED
                self.last_error = reason

    def _start_supervisor(self) -> None:
        with self._lock:
            self._stop = threading.Event()
            self.state = STARTING
            self.last_error = None
            self.restarts = 0
            self._thread = threading.Thread(target=self._supervise, args=(self._stop,),
                                            name="remote-access-tunnel", daemon=True)
            self._thread.start()

    def _stop_supervisor(self, timeout: float = 15.0) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
        stop.set()
        self._terminate_process()
        if thread is not None and thread is not threading.current_thread():
            # The supervisor may be between "should I start?" and Popen when
            # stop is requested; keep terminating until the thread is gone so
            # a child started in that window never outlives the service.
            deadline = time.monotonic() + timeout
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(0.5)
                if thread.is_alive():
                    self._terminate_process()
        with self._lock:
            self._thread = None
            self.state = STOPPED
            self.connections = 0
        if thread is not None:
            logger.info("Remote access tunnel stopped")

    def _terminate_process(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def _redact(self, line: str) -> str:
        for secret in self._secret_redactions:
            if secret:
                line = line.replace(secret, "[REDACTED]")
        return line

    def _supervise(self, stop: threading.Event) -> None:
        backoff = self.backoff_base
        while not stop.is_set():
            binary = self.binary_finder()
            token = None
            try:
                token = _read_token()
            except Exception as exc:
                logger.error(f"Tunnel token could not be read: {exc.__class__.__name__}")
            if not binary or not token:
                with self._lock:
                    self.state = ERROR
                    self.last_error = (
                        "cloudflared is not installed. Run ./run.sh --with-tunnel (downloads it into bin/) "
                        "or install cloudflared from your package manager."
                        if not binary else
                        "The stored tunnel token could not be read on this machine. Paste it again."
                    )
                logger.error(f"Remote access: {self.last_error}")
                if stop.wait(min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue

            self._secret_redactions = (token,)
            env = {k: v for k, v in os.environ.items() if not k.startswith("TUNNEL_")}
            env["TUNNEL_TOKEN"] = token
            # Never pass the token on argv: `ps` would show it to every local user.
            cmd = [binary, "tunnel", "--no-autoupdate", "run"]
            if stop.is_set():
                break
            with self._lock:
                self.state = STARTING
                self.connections = 0
            logger.info(f"Starting cloudflared ({binary}) for {self.hostname() or '-'}")
            try:
                proc = subprocess.Popen(
                    cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    start_new_session=(os.name == "posix"),
                )
            except OSError as exc:
                with self._lock:
                    self.state = ERROR
                    self.last_error = f"cloudflared could not be started: {exc.strerror or exc}"
                logger.error(f"Remote access: {self.last_error}")
                if stop.wait(min(backoff, self.backoff_max)):
                    break
                backoff = min(backoff * 2, self.backoff_max)
                continue

            started = time.monotonic()
            with self._lock:
                self._proc = proc
                self.started_at = time.time()
            if stop.is_set():  # stop requested while we were starting
                self._terminate_process()
            last_err_line: Optional[str] = None
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = self._redact(raw.rstrip())
                if not line:
                    continue
                last_err_line = self._handle_log_line(line) or last_err_line
                if stop.is_set():
                    break
            code = proc.wait()
            with self._lock:
                self._proc = None
                self.connections = 0
            if stop.is_set():
                break

            uptime = time.monotonic() - started
            if uptime >= self.stable_after:
                backoff = self.backoff_base
            delay = min(backoff, self.backoff_max)
            with self._lock:
                self.state = ERROR
                self.restarts += 1
                self.last_error = (
                    f"cloudflared exited with code {code}"
                    + (f": {last_err_line}" if last_err_line else "")
                    + f". Restarting in {delay:.0f}s."
                )
            logger.warning(f"Remote access: {self.last_error}")
            if stop.wait(delay):
                break
            backoff = min(backoff * 2, self.backoff_max)

        with self._lock:
            self.state = STOPPED
            self.connections = 0

    def _handle_log_line(self, line: str) -> Optional[str]:
        """Log a cloudflared line and update the connection state. Returns an error text."""
        line = self._redact(line)
        m = _LEVEL_RE.match(line)
        level, msg = (m.group(1), m.group(2)) if m else ("INF", line)
        log_level = {"DBG": logging.DEBUG, "INF": logging.INFO, "WRN": logging.WARNING,
                     "ERR": logging.ERROR, "FTL": logging.ERROR}[level]
        logger.log(log_level, f"cloudflared: {msg}")
        if "Registered tunnel connection" in msg:
            with self._lock:
                self.connections += 1
                self.state = CONNECTED
                self.last_error = None
            self._maybe_verify_soon()
        elif "Unregistered tunnel connection" in msg or "Connection terminated" in msg:
            with self._lock:
                self.connections = max(0, self.connections - 1)
                if self.connections == 0 and self.state == CONNECTED:
                    self.state = STARTING
        if level in ("ERR", "FTL"):
            text = re.sub(r"\s+", " ", msg).strip()[:300]
            with self._lock:
                if self.state != CONNECTED:
                    self.last_error = text
            return text
        return None

    def _maybe_verify_soon(self) -> None:
        if time.monotonic() - self._last_verify < 30:
            return
        self._last_verify = time.monotonic()

        def later():
            time.sleep(5)  # let Cloudflare propagate the new connection
            if self.state == CONNECTED:
                self.verify()

        threading.Thread(target=later, name="remote-access-verify", daemon=True).start()

    # -- verification ---------------------------------------------------------

    def verify(self, timeout: float = 10.0, client_factory: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
        """Fetch https://<hostname>/api/v1/device/identity and compare device_id.

        Proves the public name reaches THIS device, not another store's box
        that happens to use the same hostname or a stale tunnel route.
        """
        import httpx

        s = self.settings
        hostname = s.get("hostname")
        result: dict[str, Any] = {"verified": False, "verified_at": None, "verify_error": None}
        if not hostname:
            result["verify_error"] = "No hostname configured."
        else:
            mine = this_device_id()
            url = f"https://{hostname}/api/v1/device/identity"
            try:
                factory = client_factory or httpx.Client
                with factory(timeout=timeout, follow_redirects=False) as client:
                    res = client.get(url, headers={"Accept": "application/json"})
                if res.status_code != 200:
                    result["verify_error"] = f"{url} answered HTTP {res.status_code}."
                else:
                    body = res.json()
                    theirs = body.get("device_id") if isinstance(body, dict) else None
                    if not mine:
                        result["verify_error"] = "This device has no device id yet (device identity service missing)."
                    elif theirs == mine:
                        result.update(verified=True, verified_at=_utcnow_iso())
                    elif theirs:
                        result["verify_error"] = (
                            f"{hostname} reaches a different device (device id {str(theirs)[:8]}...). "
                            "Check the tunnel's public hostname route."
                        )
                    else:
                        result["verify_error"] = f"{url} did not return a device identity."
            except Exception as exc:  # DNS, TLS, timeout, non-JSON
                result["verify_error"] = f"{hostname} is not reachable: {exc.__class__.__name__}: {str(exc)[:160]}"
        with self._lock:
            new = dict(self._settings)
            new.update(result, verified_hostname=hostname if result["verified"] else None)
            self._settings = new
        try:
            save_settings(new)
        except sqlite3.Error as exc:
            logger.warning(f"Could not persist verification result: {exc}")
        if result["verified"]:
            logger.info(f"Remote access verified: https://{hostname} reaches this device")
        else:
            logger.warning(f"Remote access verification failed: {result['verify_error']}")
        return result

    def _periodic_verify(self) -> None:
        if self.state == CONNECTED and time.monotonic() - self._last_verify > self.verify_interval:
            self._last_verify = time.monotonic()
            threading.Thread(target=self.verify, name="remote-access-verify", daemon=True).start()

    # -- status -----------------------------------------------------------------

    def tailscale_status(self, max_age: float = TAILSCALE_CACHE_SECONDS) -> dict[str, Any]:
        """Private tailnet HTTPS status, cached (the Settings page polls every 3 s)."""
        with self._lock:
            cached = self._tailscale_cache
        if cached and time.monotonic() - cached[0] < max_age:
            return dict(cached[1])
        try:
            data = self.tailscale_reader()
        except Exception as exc:  # never break the remote-access page over it
            data = {"installed": None, "state": "unavailable", "message": f"{exc.__class__.__name__}"}
        with self._lock:
            self._tailscale_cache = (time.monotonic(), data)
        return dict(data)

    def status(self) -> dict[str, Any]:
        self._periodic_verify()
        s = self.settings
        with self._lock:
            state, last_error = self.state, self.last_error
            restarts, connections = self.restarts, self.connections
        binary = self.binary_finder() if s.get("provider") == "cloudflare_tunnel" else None
        return {
            "enabled": bool(s.get("enabled")),
            "provider": s.get("provider"),
            "hostname": s.get("hostname") or "",
            "public_url": self.public_url(),
            "token_configured": token_configured(),
            "process": state if s.get("provider") == "cloudflare_tunnel" else ("stopped" if not s.get("enabled") else "external"),
            "last_error": last_error,
            "restarts": restarts,
            "connections": connections,
            "verified": bool(s.get("verified")) and s.get("verified_hostname") == s.get("hostname"),
            "verified_at": s.get("verified_at"),
            "verify_error": s.get("verify_error"),
            "cloudflared_found": bool(binary) if s.get("provider") == "cloudflare_tunnel" else None,
            "auth_disabled": bool(settings.AUTH_DISABLED),
            # 127.0.0.1 rather than localhost: with HOST=127.0.0.1 uvicorn
            # listens on IPv4 only, and "localhost" may resolve to ::1 first.
            "local_origin": f"http://127.0.0.1:{settings.PORT}",
            "caddy_site_block": caddy_site_block(s.get("hostname") or "", settings.PORT)
            if s.get("provider") == "direct" else None,
            "tailscale": self.tailscale_status(),
        }

    # -- lifespan ---------------------------------------------------------------

    async def start(self) -> None:
        import asyncio

        await asyncio.to_thread(self.reload_settings)
        run, reason = self.should_run()
        if reason:
            logger.warning(f"Remote access enabled but not started: {reason}")
        self.apply()

    async def stop(self) -> None:
        import asyncio

        await asyncio.to_thread(self._stop_supervisor)


remote_access_service = RemoteAccessService()
