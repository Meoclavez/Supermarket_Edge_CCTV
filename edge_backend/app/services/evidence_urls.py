"""Links to alert evidence (stills and clips) served by this API.

Evidence links are stored and pushed as same-origin paths
(``/api/v1/events/snapshots/<id>.jpg?token=...``): the dashboard opens them on
whatever address it was loaded from, and the phone app prefixes the address
it is paired with. Older builds stored absolute links built from
``EDGE_BASE_URL`` (by default ``http://localhost:8000``), which is wrong for a
phone, another address or online access; :func:`evidence_path` turns those back
into paths when they are read.

Where a consumer needs an absolute link (the phone app plays an alert's
``clip_url`` as given), :func:`absolute_for` builds it from the address the
request itself came in on, so it is right for that caller.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection


def evidence_path(url: Optional[str]) -> Optional[str]:
    """``http://host:port/api/...?q`` -> ``/api/...?q``; paths and other values unchanged."""
    if not url or not isinstance(url, str):
        return url
    v = url.strip()
    if not v.lower().startswith(("http://", "https://")):
        return v
    try:
        parts = urlsplit(v)
    except ValueError:
        return v
    if not parts.path.startswith("/api/"):
        return v                          # not one of ours: leave it as it is
    return parts.path + (f"?{parts.query}" if parts.query else "")


def request_base(request: HTTPConnection) -> str:
    """``scheme://host[:port]`` the caller used to reach this server."""
    from app.services.public_exposure import came_via_https_proxy

    scheme = "https" if came_via_https_proxy(request) else request.url.scheme
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    host = (request.headers.get("host") or request.url.netloc or "").strip()
    return f"{scheme}://{host}"


def absolute_for(request: HTTPConnection, url: Optional[str]) -> Optional[str]:
    """An evidence link as an absolute URL on the address this request came in on."""
    path = evidence_path(url)
    if not path or not path.startswith("/"):
        return path
    return request_base(request) + path
