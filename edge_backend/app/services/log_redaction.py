"""Scrub credentials out of every log record before it is written.

The dashboard authenticates MJPEG/snapshot URLs with ``?token=<jwt>`` because
``<img>`` tags cannot send headers, so without this uvicorn's access log would
write a live session token for every stream request. The same applies to
``Authorization`` headers, API keys and ``rtsp://user:password@`` URLs that end
up in error messages.

``install_log_redaction()`` wraps the log-record factory, so every record from
every logger (including ``uvicorn.access`` and handlers configured later) is
redacted at creation, and also attaches ``RedactingFilter`` to the known
loggers and their handlers. Redaction is idempotent.

Only string content is rewritten. Tuple ``args`` keep their shape because
uvicorn's access formatter unpacks them positionally.
"""

from __future__ import annotations

import logging
import re
from typing import Any

JWT_PLACEHOLDER = "<redacted-jwt>"
REDACTED = "<redacted>"

# header.payload.signature where header and payload are base64url JSON ("eyJ").
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&;](?:token|access_token|refresh_token|id_token|stream_token|api_key|apikey|"
    r"password|passwd|pwd|secret|auth)=)[^&\s\"'#<>]+"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[^\s\"',;]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)(?!<redacted)[A-Za-z0-9._~+/=-]{8,}")
_API_KEY_HEADER_RE = re.compile(r"(?i)(x-edge-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+")
_URL_CREDS_RE = re.compile(r"(?i)\b((?:rtsps?|https?|rtmp|onvif)://[^:/@\s\"']+:)[^@/\s\"']+(@)")
# password=..., "password": "...", JWT_SECRET=..., "nvr_api_key": ... (booleans kept).
_KV_PASSWORD_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z0-9]+_)*(?:password|passwd|pwd|secret|api_key)[\"']?\s*[:=]\s*[\"']?)"
    r"(?!<redacted|true\b|false\b|null\b|none\b)[^\s\"',}&]+"
)


def redact(text: str) -> str:
    """Return ``text`` with tokens, keys and passwords replaced."""
    if not text or not isinstance(text, str):
        return text
    out = _JWT_RE.sub(JWT_PLACEHOLDER, text)
    # "<" is not in the value class, so an already-redacted JWT is left alone.
    out = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _API_KEY_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _URL_CREDS_RE.sub(lambda m: m.group(1) + REDACTED + m.group(2), out)
    out = _KV_PASSWORD_RE.sub(lambda m: m.group(1) + REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes):
        try:
            return redact(value.decode("utf-8", "replace"))
        except Exception:
            return value
    return value


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    if getattr(record, "_edge_redacted", False):
        return record
    try:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_value(v) for k, v in record.args.items()}
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        # A secret split across the format string and its args ("Bearer %s")
        # only shows once formatted. Collapse such records to the redacted
        # text, except uvicorn.access whose formatter needs the args tuple.
        if record.args and not record.name.startswith("uvicorn.access"):
            formatted = record.getMessage()
            cleaned = redact(formatted)
            if cleaned != formatted:
                record.msg, record.args = cleaned, None
    except Exception:
        pass  # logging must never fail because of redaction
    record._edge_redacted = True
    return record


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets in place and never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_FILTER = RedactingFilter()
_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error", "edge", "app", "fastapi")


def install_log_redaction() -> None:
    """Install redaction for every log record in this process. Idempotent."""
    current = logging.getLogRecordFactory()
    if not getattr(current, "_edge_redacting", False):
        def factory(*args, **kwargs):
            return redact_record(current(*args, **kwargs))

        factory._edge_redacting = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)

    for name in _LOGGERS:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        for handler in lg.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
