"""Starts the live pipeline at boot and drains its output into the database.

Previously the application's entire startup sequence was ``await init_db()``.
Nothing ran afterwards, which is the root reason every analytics table stayed
empty and every dashboard number had to be a hardcoded fallback.

The supervisor closes that gap. It owns two periodic jobs:

* **flush** -- moves the facts the camera workers have buffered (zone visits,
  finished tracks) into ``zone_visits`` and ``customer_tracks``. Workers are
  plain threads and must not touch the async session themselves, so they hand
  over through an in-memory buffer that this loop empties.
* **reconcile** -- keeps the running worker set equal to the enabled cameras in
  the database, and republishes the blueprint whenever it changes, so adding a
  camera or redrawing a zone takes effect without a restart.

A camera's on/off switch is ``cameras.is_ai_enabled`` (PUT
/api/v1/cameras/{id}/enabled). An enabled camera with a source gets a worker;
a camera turned off gets a DISABLED runtime with no worker, so it is reported
as off rather than offline and costs no connection, decode or inference.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models.db_models import CameraModel, CustomerTrackModel, ZoneVisitModel
from app.services.camera_drivers import redact_url
from app.services.live_analytics_engine import live_engine
from app.services.store_layout_service import store_layout_service
from app.services.timeutil import utc_from_ts
from app.services.tracking_service import floor_projector

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 5.0
RECONCILE_INTERVAL_SECONDS = 15.0
WORKER_STOP_TIMEOUT_SECONDS = 3.0


def _camera_source(cam: CameraModel) -> str:
    """The URL or device path a worker should capture from.

    Analytics prefers the vendor sub-stream when one is configured, because
    running detection on a 1080p main stream costs several times the decode
    budget for no measurable gain in person detection accuracy.

    Cameras added by URL store a credential-free URL; their username and
    password live in the encrypted secret store and are injected here, at
    open time only (app/services/camera_source.py).
    """
    try:
        from app.services.camera_source import stream_source_for

        return stream_source_for(cam.id, cam.rtsp_url)
    except Exception as exc:  # noqa: BLE001 - never block a worker on the store
        logger.warning(f"Could not load stored credentials for {cam.id}: {type(exc).__name__}")
        return cam.rtsp_url


class PipelineSupervisor:
    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._layout_stamp: str = ""
        self._reconcile_lock: tuple[object, asyncio.Lock] | None = None

    def _lock(self) -> asyncio.Lock:
        """One reconcile at a time on the running loop.

        The periodic reconcile and one triggered by an on/off switch would
        otherwise interleave at the database read, and the one that read
        first could restart a worker for a camera that was just turned off.
        """
        loop = asyncio.get_running_loop()
        if self._reconcile_lock is None or self._reconcile_lock[0] is not loop:
            self._reconcile_lock = (loop, asyncio.Lock())
        return self._reconcile_lock[1]

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Tripwire / restricted-area alerts are scheduled onto this loop.
        from app.services.night_watch import night_watch
        from app.services.tripwire_engine import tripwire_engine

        tripwire_engine.bind_loop()
        night_watch.bind_loop()
        await self._close_orphaned_visits()
        await self.reload_layout()
        await self.reconcile_cameras()
        live_engine.mark_started()
        self._tasks = [
            asyncio.create_task(self._flush_loop(), name="pipeline-flush"),
            asyncio.create_task(self._reconcile_loop(), name="pipeline-reconcile"),
        ]
        logger.info("Live analytics pipeline supervisor started")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        # Bounded join off the event loop (a worker may sit in an RTSP read).
        await asyncio.to_thread(live_engine.stop_all, WORKER_STOP_TIMEOUT_SECONDS)
        # Persist whatever the workers produced before shutdown so a restart
        # does not silently discard the last few seconds of real observations.
        await self.flush_once()
        logger.info("Live analytics pipeline supervisor stopped")

    async def _close_orphaned_visits(self) -> None:
        """Close visits left open by a previous unclean shutdown.

        An open visit means "this person is in the zone right now". After a
        crash those rows are stale, and leaving them would make the store look
        permanently occupied by people who left hours ago.
        """
        from sqlalchemy import update

        try:
            async with async_session_factory() as db:
                res = await db.execute(
                    select(ZoneVisitModel).where(ZoneVisitModel.exited_at.is_(None))
                )
                orphans = list(res.scalars().all())
                for row in orphans:
                    row.exited_at = row.entered_at
                    row.dwell_seconds = 0.0
                if orphans:
                    await db.commit()
                    logger.warning(
                        f"Closed {len(orphans)} zone visit(s) orphaned by a previous shutdown"
                    )
        except Exception as e:
            logger.error(f"Could not close orphaned visits: {e}")

    # -------------------------------------------------------------------- jobs

    @staticmethod
    def _layout_stamp_for(layout, zones) -> str:
        """Change marker for the analytics-relevant part of the blueprint.

        Only zones feed the workers; structures (walls, rooms, fixtures) are
        drawing-only and deliberately excluded so redrawing a wall does not
        republish anything. Zone edits that do not touch the layout row are
        caught by folding the newest zone timestamp into the stamp.
        """
        newest = max((z.updated_at for z in zones if z.updated_at), default=None)
        return f"{layout.updated_at}:{len(zones)}:{newest}"

    async def reload_layout(self) -> None:
        """Push the current blueprint into the running workers."""
        async with async_session_factory() as db:
            layout = await store_layout_service.get_active_layout(db)
            if layout is None:
                return
            zones = await store_layout_service.list_zones(db, layout.id)
            live_engine.set_zones(
                [store_layout_service.serialize_zone(z) for z in zones]
            )
            self._layout_stamp = self._layout_stamp_for(layout, zones)

    async def reconcile_cameras(self) -> None:
        """Make the worker set match the enabled cameras in the database."""
        async with self._lock():
            async with async_session_factory() as db:
                res = await db.execute(select(CameraModel))
                cameras = list(res.scalars().all())

            wanted: dict[str, CameraModel] = {}
            off: dict[str, CameraModel] = {}
            for cam in cameras:
                floor_projector.set_homography(cam.id, cam.homography_matrix)
                if not cam.is_ai_enabled:
                    off[cam.id] = cam
                elif cam.rtsp_url:
                    wanted[cam.id] = cam

            running = set(live_engine.runtimes.keys())

            for cam_id in running - set(wanted.keys()) - set(off.keys()):
                logger.info(f"Stopping worker for camera {cam_id} (removed, or no source)")
                live_engine.stop_camera(cam_id)

            for cam_id, cam in off.items():
                rt = live_engine.runtimes.get(cam_id)
                if rt is not None and not rt.enabled:
                    rt.name = cam.name
                    continue
                if rt is not None:
                    logger.info(f"Camera {cam_id} turned off: stopping its worker")
                live_engine.start_camera(cam_id, cam.name, "", enabled=False)

            for cam_id, cam in wanted.items():
                rt = live_engine.runtimes.get(cam_id)
                source = _camera_source(cam)
                if rt is not None and rt.enabled and rt.source == source:
                    continue
                logger.info(f"Starting worker for {cam_id} -> {redact_url(source)}")
                live_engine.start_camera(cam_id, cam.name, source, enabled=True)

    async def flush_once(self) -> int:
        """Write buffered facts to the database. Returns rows written."""
        # Tripwire crossings go to tripwire_events (their own session, so a
        # failure there cannot lose zone visits or tracks, and vice versa).
        from app.services.tripwire_engine import flush_tripwire_events

        crossings = await flush_tripwire_events(async_session_factory)
        visits, tracks = live_engine.drain()
        if not visits and not tracks:
            return crossings

        written = crossings
        try:
            async with async_session_factory() as db:
                # Rows added in this drain, so a later phase of the same visit
                # finds them without depending on autoflush.
                added: dict[str, ZoneVisitModel] = {}
                for v in visits:
                    if v.phase == "open":
                        # The person is in the zone now; the row exists with no
                        # exit time so live occupancy is a plain SQL question.
                        row = ZoneVisitModel(
                            id=v.visit_id,
                            zone_id=v.zone_id,
                            track_id=v.track_id,
                            camera_id=v.camera_id,
                            entered_at=v.entered_at,
                            exited_at=None,
                            dwell_seconds=0.0,
                            interacted=bool(v.interacted),
                        )
                        db.add(row)
                        added[v.visit_id] = row
                        written += 1
                    elif v.phase == "interact":
                        # A shelf interaction seen by pose analytics while the
                        # visit is still open.
                        row = added.get(v.visit_id) or await db.get(ZoneVisitModel, v.visit_id)
                        if row is not None:
                            row.interacted = True
                            written += 1
                    else:
                        row = added.get(v.visit_id) or await db.get(ZoneVisitModel, v.visit_id)
                        if row is None:
                            # The open phase and close phase can land in the
                            # same drain; insert the completed visit directly.
                            db.add(
                                ZoneVisitModel(
                                    id=v.visit_id,
                                    zone_id=v.zone_id,
                                    track_id=v.track_id,
                                    camera_id=v.camera_id,
                                    entered_at=v.entered_at,
                                    exited_at=v.exited_at,
                                    dwell_seconds=v.dwell_seconds,
                                    interacted=v.interacted,
                                )
                            )
                        else:
                            row.exited_at = v.exited_at
                            row.dwell_seconds = v.dwell_seconds
                            row.interacted = v.interacted
                        written += 1

                for t in tracks:
                    db.add(
                        CustomerTrackModel(
                            id=f"ct_{uuid.uuid4().hex[:14]}",
                            track_id=t.track_id,
                            camera_id=t.camera_id,
                            # Naive UTC, like created_at (services/timeutil.py).
                            start_time=utc_from_ts(t.first_seen),
                            end_time=utc_from_ts(t.last_seen),
                            hits=int(t.hits),
                            # Sampled path (image + floor when calibrated,
                            # heatmap_history); floor-only for older Track objects.
                            trajectory_points=(
                                list(getattr(t, "path_points", None) or [])[:settings.TRAJECTORY_MAX_POINTS]
                                or t.floor_points[:500]
                            ),
                        )
                    )
                    written += 1

                await db.commit()
        except Exception as e:
            logger.error(f"Failed to flush pipeline facts: {e}")
            return 0

        if written:
            logger.debug(f"Flushed {written} pipeline fact(s) to the database")
        return written

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Flush loop error: {e}")

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
                await self.reconcile_cameras()
                async with async_session_factory() as db:
                    layout = await store_layout_service.get_active_layout(db)
                    if layout is not None:
                        zones = await store_layout_service.list_zones(db, layout.id)
                        stamp = self._layout_stamp_for(layout, zones)
                        if stamp != self._layout_stamp:
                            live_engine.set_zones(
                                [store_layout_service.serialize_zone(z) for z in zones]
                            )
                            self._layout_stamp = stamp
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Reconcile loop error: {e}")


pipeline_supervisor = PipelineSupervisor()
