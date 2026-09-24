"""Move camera credentials out of ``cameras.rtsp_url``.

Earlier builds stored stream URLs as ``rtsp://user:password@host/...`` in
plaintext (Dahua NVR adoption, scan adoption, first-run add-cameras). For
every camera row whose URL carries a ``user:pw@`` part this migration:

1. writes the username/password to the per-machine encrypted named-secret
   store (``camera_source.store_credentials``: Fernet, keyed from
   ``NVR_CREDENTIAL_KEY``, one secret per camera), then
2. rewrites ``rtsp_url`` without them. The capture pipeline injects them
   again only when it opens the stream (``camera_source.stream_source_for``).

It also strips userinfo from the candidate URLs cached in
``discovered_devices.stream_urls`` (copies of saved NVR credentials that are
applied again at adoption time, so nothing needs storing for them).

Idempotent: rows without userinfo are skipped, and re-storing a secret with
the same value is harmless. Only camera ids and counts are logged. A row whose
credentials cannot be stored (no machine key, unwritable store) is left as it
was -- losing the password would take the camera offline -- and API responses
still mask it.
"""

from __future__ import annotations

import json
import logging

from .sqlite_utils import columns, has_table

NAME = "camera_credentials_out_of_url"

logger = logging.getLogger("edge.migrations")


def upgrade(conn, *, storage_dir=None, machine_key=None) -> None:
    from app.services import camera_source

    if has_table(conn, "cameras") and "rtsp_url" in columns(conn, "cameras"):
        rows = conn.execute(
            "SELECT id, rtsp_url FROM cameras WHERE rtsp_url LIKE '%://%@%'"
        ).fetchall()
        moved, kept = [], []
        for cam_id, url in rows:
            clean, user, pw = camera_source.split_url_credentials(url)
            if clean == url or not (user or pw):
                continue  # '@' in a path/query, or unparsable: nothing to move
            try:
                camera_source.store_credentials(
                    cam_id, user, pw, storage_dir=storage_dir, machine_key=machine_key,
                )
            except Exception as exc:  # noqa: BLE001
                kept.append(cam_id)
                logger.warning(f"m0009: camera {cam_id}: credentials not moved ({type(exc).__name__}); "
                               "URL left unchanged")
                continue
            conn.execute("UPDATE cameras SET rtsp_url = ? WHERE id = ?", (clean, cam_id))
            moved.append(cam_id)
        if moved:
            logger.info(f"m0009: moved stream credentials of {len(moved)} camera(s) to the encrypted "
                        f"store: {', '.join(moved)}")
        if kept:
            logger.warning(f"m0009: {len(kept)} camera(s) still carry credentials in rtsp_url: {', '.join(kept)}")

    if has_table(conn, "discovered_devices") and "stream_urls" in columns(conn, "discovered_devices"):
        scrubbed = 0
        for dev_id, raw in conn.execute(
            "SELECT id, stream_urls FROM discovered_devices WHERE stream_urls LIKE '%@%'"
        ).fetchall():
            try:
                streams = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                continue
            if not isinstance(streams, list):
                continue
            changed = False
            for s in streams:
                if isinstance(s, dict) and isinstance(s.get("url"), str):
                    clean, user, pw = camera_source.split_url_credentials(s["url"])
                    if user or pw:
                        s["url"] = clean
                        changed = True
            if changed:
                conn.execute("UPDATE discovered_devices SET stream_urls = ? WHERE id = ?",
                             (json.dumps(streams), dev_id))
                scrubbed += 1
        if scrubbed:
            logger.info(f"m0009: removed cached credentials from {scrubbed} discovered device(s)")
