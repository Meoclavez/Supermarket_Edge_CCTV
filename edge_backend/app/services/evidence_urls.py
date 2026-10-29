"""Links to alert evidence (stills and clips) served by this API.

Evidence links are stored as same-origin paths without any token
(``/api/v1/events/snapshots/<id>.jpg``): the dashboard opens them on whatever
address it was loaded from with its session cookie, and the phone app
prefixes the address it is paired with. Where a link must work without a
header (the phone's video player, a link in a websocket message),
:func:`signed_path` adds a 15-minute clip token minted for the person the
link is served to and for that one file. Tokens are never stored: rows from
older builds that hold ``?token=`` (24-hour, unscoped) have it removed when
they are read, and such old tokens are refused anyway. Older builds stored absolute links built from
``EDGE_BASE_URL`` (by default ``http://localhost:8000``), which is wrong for a
phone, another address or online access; :func:`evidence_path` turns those back
into paths when they are read.

Where a consumer needs an absolute link (the phone app plays an alert's
``clip_url`` as given), :func:`absolute_for` builds it from the address the
request itself came in on, so it is right for that caller.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.requests import HTTPConnection


def _strip_token(path: str) -> str:
    """Drop any ``token=`` query parameter (kept: everything else)."""
    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() != "token"]
    return base + (f"?{urlencode(kept)}" if kept else "")


def evidence_path(url: Optional[str]) -> Optional[str]:
    """``http://host:port/api/...?token=..`` -> ``/api/...``; other values unchanged.

    Strips the host of absolute links from older builds and any stored token.
    """
    if not url or not isinstance(url, str):
        return url
    v = url.strip()
    if not v.lower().startswith(("http://", "https://")):
        return _strip_token(v) if v.startswith("/api/") else v
    try:
        parts = urlsplit(v)
    except ValueError:
        return v
    if not parts.path.startswith("/api/"):
        return v                          # not one of ours: leave it as it is
    return _strip_token(parts.path + (f"?{parts.query}" if parts.query else ""))


def scoped_file(path: str) -> Optional[str]:
    """The file a clip token for this evidence path must name, or None."""
    p = path.split("?", 1)[0]
    for prefix in ("/api/v1/events/snapshots/", "/api/v1/events/clips/"):
        if p.startswith(prefix):
            rest = p[len(prefix):]
            return rest if rest and "/" not in rest else None
    if p.startswith("/api/v1/dvr/archives/") and p.endswith("/download"):
        rest = p[len("/api/v1/dvr/archives/"):-len("/download")]
        return rest if rest and "/" not in rest else None
    return None


def signed_path(url: Optional[str], owner: Optional[Dict[str, Any]]) -> Optional[str]:
    """An evidence path with a fresh clip token for ``owner`` (a verified
    session payload) and exactly this file; other values as :func:`evidence_path`."""
    path = evidence_path(url)
    if not path or not isinstance(path, str) or not path.startswith("/"):
        return path
    f = scoped_file(path)
    if not f:
        return path
    from app.services.auth_service import auth_service

    token = auth_service.generate_clip_token(f, owner=owner)
    return f"{path}{'&' if '?' in path else '?'}{urlencode({'token': token})}"


def request_base(request: HTTPConnection) -> str:
    """``scheme://host[:port]`` the caller used to reach this server."""
    from app.services.public_exposure import came_via_https_proxy

    scheme = "https" if came_via_https_proxy(request) else request.url.scheme
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    host = (request.headers.get("host") or request.url.netloc or "").strip()
    return f"{scheme}://{host}"


def absolute_for(request: HTTPConnection, url: Optional[str]) -> Optional[str]:
    """An evidence link as an absolute URL on the address this request came in
    on, with a clip token for the caller's session and this one file."""
    owner = getattr(request.state, "user", None) if hasattr(request, "state") else None
    path = signed_path(url, owner if isinstance(owner, dict) else None)
    if not path or not path.startswith("/"):
        return path
    return request_base(request) + path
