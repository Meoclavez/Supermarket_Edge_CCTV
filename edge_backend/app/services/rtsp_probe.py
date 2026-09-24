"""A tiny, bounded RTSP handshake: does this URL speak RTSP, and does it accept our login?

OpenCV/FFmpeg only report "can't open" when a camera answers ``401
Unauthorized``: the status code goes to FFmpeg's stderr, never to Python. A
capture worker therefore could not tell a wrong password from an unplugged
cable and retried both every 30 s. Dahua (and most NVR/IP camera firmware)
locks an account after about five failed logins, so that retry loop kept the
account locked indefinitely, for the operator as well.

This module sends at most two RTSP requests (each on its own short TCP connection):

1. ``DESCRIBE <url>`` without credentials, which a camera that needs a login
   answers with ``401`` and a ``WWW-Authenticate`` challenge (Digest or Basic).
2. The same ``DESCRIBE`` with an ``Authorization`` header built from the
   username/password in the URL. A second ``401`` means the camera rejected
   them.

So one probe costs the camera at most one failed login. The body of the
``DESCRIBE`` reply (the SDP) is not read and no media session is set up.

Limits: only plain ``rtsp://`` is spoken (``rtsps://`` and HTTP sources are
reported ``SKIPPED``); Digest with MD5, MD5-sess and SHA-256 and Basic are
supported. A camera that answers DESCRIBE with something other than 200/401
(e.g. ``404`` for a wrong path) is reported as such and callers fall back to
their normal open path. Credentials are never logged or returned.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlsplit, urlunsplit

# Outcomes.
OK = "OK"                          # 200 to DESCRIBE (with or without our login)
AUTH_FAILED = "AUTH_FAILED"        # 401/403 after sending credentials
AUTH_REQUIRED = "AUTH_REQUIRED"    # 401 and the URL carries no credentials
NOT_FOUND = "NOT_FOUND"            # RTSP answered 404: wrong stream path
RTSP_ERROR = "RTSP_ERROR"          # RTSP answered some other status
NOT_RTSP = "NOT_RTSP"              # something answered, but not RTSP (e.g. a web server)
UNREACHABLE = "UNREACHABLE"        # refused / no route / DNS failure / connect timed out
TIMEOUT = "TIMEOUT"                # connected, but no reply in time
SKIPPED = "SKIPPED"                # not an rtsp:// URL; nothing was sent

AUTH_OUTCOMES = (AUTH_FAILED, AUTH_REQUIRED)

MAX_HEADER_BYTES = 16 * 1024
USER_AGENT = "EdgeCCTV-Probe"


@dataclass
class RtspProbeResult:
    outcome: str
    status_code: Optional[int] = None
    server: Optional[str] = None
    realm: Optional[str] = None
    detail: str = ""

    @property
    def speaks_rtsp(self) -> bool:
        return self.status_code is not None

    @property
    def auth_failed(self) -> bool:
        return self.outcome in AUTH_OUTCOMES

    @property
    def vendor_hint(self) -> str:
        """Server banner and auth realm, for vendor detection (no secrets)."""
        return " ".join(x for x in (self.server, self.realm) if x)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "status_code": self.status_code,
                "server": self.server, "realm": self.realm, "detail": self.detail}


def _parse_headers(raw: bytes) -> tuple[Optional[int], dict[str, list[str]], str]:
    text = raw.decode("utf-8", "ignore")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    first = lines[0].strip() if lines else ""
    m = re.match(r"^RTSP/\d\.\d\s+(\d{3})", first)
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line.strip():
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    return (int(m.group(1)) if m else None), headers, first


def _parse_challenge(value: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = value.strip().partition(" ")
    params = {k.lower(): (v1 if v1 else v2) for k, v1, v2 in
              re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', rest)}
    return scheme.lower(), params


def _pick_challenge(values: list[str]) -> tuple[Optional[str], dict[str, str]]:
    parsed = [_parse_challenge(v) for v in values if v.strip()]
    for scheme, params in parsed:
        if scheme == "digest":
            return scheme, params
    for scheme, params in parsed:
        if scheme == "basic":
            return scheme, params
    return (parsed[0] if parsed else (None, {}))


def _hash(algorithm: str, data: str) -> str:
    alg = (algorithm or "MD5").upper().replace("-SESS", "")
    fn = hashlib.sha256 if alg in ("SHA-256", "SHA256") else hashlib.md5
    return fn(data.encode("utf-8")).hexdigest()


def digest_authorization(method: str, uri: str, username: str, password: str,
                         params: dict[str, str], cnonce: Optional[str] = None, nc: str = "00000001") -> str:
    """RFC 7616 / 2617 Digest ``Authorization`` value for one request."""
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    algorithm = params.get("algorithm", "MD5")
    qop_offered = [q.strip() for q in params.get("qop", "").split(",") if q.strip()]
    ha1 = _hash(algorithm, f"{username}:{realm}:{password}")
    cnonce = cnonce or os.urandom(8).hex()
    if algorithm.upper().endswith("-SESS"):
        ha1 = _hash(algorithm, f"{ha1}:{nonce}:{cnonce}")
    ha2 = _hash(algorithm, f"{method}:{uri}")
    fields = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in qop_offered:
        response = _hash(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        fields += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = _hash(algorithm, f"{ha1}:{nonce}:{ha2}")
    fields.append(f'response="{response}"')
    if params.get("opaque"):
        fields.append(f'opaque="{params["opaque"]}"')
    if params.get("algorithm"):
        fields.append(f"algorithm={params['algorithm']}")
    return "Digest " + ", ".join(fields)


def _split(url: str) -> tuple[str, str, int, Optional[str], Optional[str]]:
    """(credential-free request URI, host, port, username, password)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or 554
    user = unquote(parts.username) if parts.username else None
    pw = unquote(parts.password) if parts.password else None
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc += f":{parts.port}"
    clean = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    return clean, host, port, user, pw


class _Conn:
    def __init__(self, host: str, port: int, deadline: float):
        self.host, self.port, self.deadline = host, port, deadline
        self.sock: Optional[socket.socket] = None

    def _remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("deadline")
        return left

    def open(self) -> None:
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self._remaining())

    def request(self, raw: bytes) -> bytes:
        if self.sock is None:
            self.open()
        self.sock.settimeout(self._remaining())
        self.sock.sendall(raw)
        data = b""
        while b"\r\n\r\n" not in data and b"\n\n" not in data:
            self.sock.settimeout(self._remaining())
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_HEADER_BYTES:
                break
        return data

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


def _request(method: str, uri: str, cseq: int, auth: Optional[str]) -> bytes:
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {USER_AGENT}"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if auth:
        lines.append(f"Authorization: {auth}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def probe_rtsp(url: str, timeout_s: float = 5.0, method: str = "DESCRIBE") -> RtspProbeResult:
    """Handshake with an RTSP URL. Blocking, bounded by ``timeout_s`` overall. Never raises."""
    if not url or not url.lower().startswith("rtsp://"):
        return RtspProbeResult(SKIPPED, detail="not an rtsp:// URL")
    try:
        uri, host, port, user, pw = _split(url)
    except ValueError as exc:
        return RtspProbeResult(RTSP_ERROR, detail=f"invalid URL: {type(exc).__name__}")
    if not host:
        return RtspProbeResult(RTSP_ERROR, detail="URL has no host")

    conn = _Conn(host, port, time.monotonic() + max(0.2, float(timeout_s)))
    server = realm = None
    try:
        try:
            conn.open()
        except socket.timeout:
            return RtspProbeResult(UNREACHABLE, detail=f"{host}:{port} did not accept a connection within {timeout_s:.0f} s")
        except OSError as exc:
            return RtspProbeResult(UNREACHABLE, detail=f"cannot connect to {host}:{port}: {exc.strerror or type(exc).__name__}")

        try:
            raw = conn.request(_request(method, uri, 1, None))
        except socket.timeout:
            return RtspProbeResult(TIMEOUT, detail=f"{host}:{port} accepted the connection but sent no RTSP reply")
        except OSError as exc:
            return RtspProbeResult(NOT_RTSP, detail=f"{host}:{port} dropped the connection: {type(exc).__name__}")
        code, headers, first = _parse_headers(raw)
        if code is None:
            what = first[:60] if first else "nothing"
            return RtspProbeResult(NOT_RTSP, detail=f"{host}:{port} does not speak RTSP (replied {what!r})")
        server = (headers.get("server") or [None])[0]
        challenges = headers.get("www-authenticate") or []
        scheme, params = _pick_challenge(challenges)
        realm = params.get("realm")

        if code == 200:
            return RtspProbeResult(OK, code, server, realm, "stream answered")
        if code not in (401, 403):
            return _other(code, first, server, realm)
        if code == 403 or not user:
            if code == 403:
                return RtspProbeResult(AUTH_FAILED, code, server, realm, "camera refused access (403 Forbidden)")
            return RtspProbeResult(AUTH_REQUIRED, code, server, realm,
                                   "camera requires a username and password, but none are saved for it")
        if scheme == "digest":
            auth = digest_authorization(method, uri, user, pw or "", params)
        elif scheme == "basic":
            auth = "Basic " + base64.b64encode(f"{user}:{pw or ''}".encode("utf-8")).decode("ascii")
        else:
            return RtspProbeResult(RTSP_ERROR, code, server, realm,
                                   f"camera asked for an unsupported login scheme ({scheme or 'none'})")

        try:
            # A fresh connection: the 401 may carry a body we did not read,
            # and some servers close after a challenge anyway.
            conn.open()
            raw = conn.request(_request(method, uri, 2, auth))
        except socket.timeout:
            return RtspProbeResult(TIMEOUT, 401, server, realm, "no reply to the authenticated request")
        except OSError as exc:
            return RtspProbeResult(RTSP_ERROR, 401, server, realm, f"connection dropped after login: {type(exc).__name__}")
        code2, headers2, first2 = _parse_headers(raw)
        server = (headers2.get("server") or [server])[0]
        if code2 == 200:
            return RtspProbeResult(OK, code2, server, realm, "login accepted, stream answered")
        if code2 in (401, 403):
            return RtspProbeResult(AUTH_FAILED, code2, server, realm, "camera rejected the username/password")
        if code2 is None:
            return RtspProbeResult(RTSP_ERROR, None, server, realm, "no RTSP reply to the authenticated request")
        return _other(code2, first2, server, realm)
    finally:
        conn.close()


def _other(code: int, first: str, server, realm) -> RtspProbeResult:
    if code == 404:
        return RtspProbeResult(NOT_FOUND, code, server, realm, "camera has no stream at this path (404)")
    return RtspProbeResult(RTSP_ERROR, code, server, realm, f"camera answered {first[:60]!r}")
