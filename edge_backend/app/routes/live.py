"""Live tracks and behaviour for the dashboard's client-side overlay.

``GET /api/v1/live/tracks?cameras=a,b`` -- per camera: the latest analysed
frame's tracks (boxes, velocity and 17 COCO keypoints normalised 0..1 to the
analysed frame) with each track's live behaviour from pose analytics.
``GET /api/v1/live/behaviour`` -- every live track at "watch" or "alert"
across all cameras, newest first.

Both read what the camera workers already published (no inference runs, no
database access) and are plain JSON, so they pass the online-access tunnel
like the rest of the API (services/public_exposure.py blocks only live video
and snapshot routes). Same authentication as /api/v1/layout/live. Sync
handlers: the brief per-camera lock copies run in the threadpool, never on
the event loop.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services.auth_service import auth_service
from app.services.live_analytics_engine import live_engine

router = APIRouter(prefix="/api/v1/live", tags=["Live"])

# Most cameras one request may ask for (a dashboard shows at most a 4x4 grid).
MAX_CAMERAS = 16


def parse_camera_ids(raw: Optional[str]) -> list[str]:
    """Comma-separated camera ids, trimmed and de-duplicated in order; 422 over ``MAX_CAMERAS``."""
    ids: list[str] = []
    for part in (raw or "").split(","):
        cid = part.strip()
        if cid and cid not in ids:
            ids.append(cid)
    if len(ids) > MAX_CAMERAS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_CAMERAS} cameras per request ({len(ids)} given)")
    return ids


@router.get("/tracks")
def live_tracks(
    cameras: str = Query(..., description="Comma-separated camera ids (at most 16)"),
    _: bool = Depends(auth_service.verify_api_access),
):
    """Live tracks of the given cameras; unknown camera ids are omitted.

    Per camera: ``seq`` (analysed-frame counter), ``analysed_at`` (unix
    time of that frame), ``frame_width``/``frame_height`` (its pixels),
    ``analysis_fps`` (measured rate, null when not analysing) and ``tracks``,
    each with a normalised ``box`` (the tracker's predicted box while
    coasting), ``velocity`` (normalised units per second, null until two
    matches), ``fresh``/``age_sec``, normalised ``keypoints`` (last matched
    pose) and ``behaviour`` (null when shelf interaction and theft detection
    are off for the camera, or not available). A behaviour at level "alert"
    carries ``tier`` (review | watch | alert | critical) and ``risk_score``
    of the incident that fired (services/theft_alert_policy.py); both are
    null otherwise.
    """
    return live_engine.live_tracks(parse_camera_ids(cameras))


@router.get("/behaviour")
def live_behaviour(_: bool = Depends(auth_service.verify_api_access)):
    """Live tracks at "watch" or "alert" on every camera, newest ``since`` first."""
    return live_engine.live_behaviour()
