"""Live video sessions: direct peer-to-peer WebRTC through go2rtc, on demand only.

Contract: docs/REMOTE_VIDEO_CONTRACT.md. One session is one browser
RTCPeerConnection for one camera tile/focus/frame grab.

* **One go2rtc stream per session** (named after the session id). go2rtc
  opens the camera's RTSP stream when the session's WebRTC consumer attaches
  and closes it, synchronously, when that consumer leaves. So the camera is
  pulled only while someone watches, and "consumer gone" identifies exactly
  which session ended. Two viewers of one camera pull it twice; the limit
  ``max_video_sessions`` bounds that.
* **Ending a session** removes it from the registry at once (its heartbeat
  then answers 404, which tells the page to close the connection) and deletes
  its go2rtc stream as soon as the consumer is gone. go2rtc has no API that
  closes one WebRTC consumer, so a server-side end (heartbeat missed, absolute
  cap, camera turned off/deleted/credentials changed, admin) relies on the
  page closing within one heartbeat; if a consumer is still attached
  ``FORCE_GRACE_S`` after its session ended, go2rtc is restarted, which ends
  every connection (the other sessions are ended too, and their pages reopen).
* **The reaper** runs every ``WEBRTC_REAP_INTERVAL_S`` (about 3 s) and only
  while a session or a leftover stream exists: it checks heartbeats, the
  absolute cap, go2rtc's consumer list, the camera's on/off state and source
  (credentials included, by fingerprint), and deletes orphan go2rtc streams.
  With nobody watching, nothing polls.
* **Codec**: the camera's stream is passed through when the browser's offer
  supports its codec. When go2rtc reports "codecs not matched" (typically
  H.265 to a browser without H.265), the session's stream is re-pointed to an
  ffmpeg H.264 transcode (go2rtc ``#hardware``: NVENC, VA-API, else libx264;
  see go2rtc_manager.transcode_source) and negotiated again. The camera's
  codec is remembered per source, so later sessions go straight to the right
  path.
* **Diagnostics**: a ring buffer of the last 50 session outcomes and browser
  reports, and an on-demand STUN NAT check (services/stun_probe.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.services.go2rtc_manager import Go2RtcError, Go2RtcUnavailable, go2rtc_manager, redact, transcode_source

logger = logging.getLogger("edge.webrtc")

RING_SIZE = 50
STREAM_PREFIX = "ws_"
# Seconds a consumer may stay attached after its session ended before go2rtc
# is restarted to cut it (one heartbeat for the page to notice, plus slack).
FORCE_GRACE_EXTRA_S = 10.0
# A consumer that never connected (the page closed while its offer was still
# being answered: rotation, focus or a view switch during negotiation) cannot
# receive anything; go2rtc drops it when its ICE checks give up. Restarting
# go2rtc for it would cut every other viewer, so it is only waited for, up to
# this bound (integration run 2026-09-29: a restart 25 s after every such close).
UNCONNECTED_CONSUMER_MAX_S = 90.0
_CODEC_RE = re.compile(r"a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", re.I)
_CODEC_ALIASES = {"HEVC": "H265"}


class SessionError(Exception):
    """An error with an HTTP status and a contract ``code``."""

    def __init__(self, status: int, code: str, reason: str, **extra: Any):
        super().__init__(reason)
        self.status = status
        self.code = code
        self.reason = reason
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason, "detail": self.reason, **self.extra}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def offer_video_codecs(sdp: str) -> set[str]:
    """Video codecs the browser offers (``H264``, ``H265``, ``VP8``, ``VP9``, ``AV1``)."""
    codecs: set[str] = set()
    in_video = False
    for line in (sdp or "").splitlines():
        line = line.strip()
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            continue
        if in_video:
            m = _CODEC_RE.match(line)
            if m:
                name = m.group(1).upper()
                codecs.add(_CODEC_ALIASES.get(name, name))
    return codecs


def _codec_from_text(text: str) -> Optional[str]:
    up = (text or "").upper()
    for name in ("H265", "HEVC", "H264", "AV1", "VP9", "VP8", "MJPEG", "JPEG"):
        if re.search(rf"\b{name}\b", up):
            return _CODEC_ALIASES.get(name, name)
    return None


def producer_codec(stream_info: Optional[dict[str, Any]]) -> Optional[str]:
    """Video codec of a go2rtc stream's first connected producer."""
    for prod in (stream_info or {}).get("producers") or []:
        for media in prod.get("medias") or []:
            if isinstance(media, str) and media.lower().startswith("video"):
                codec = _codec_from_text(media.split(",")[-1])
                if codec:
                    return codec
        for rx in prod.get("receivers") or []:
            codec = _codec_from_text(str((rx.get("codec") or {}).get("codec_name") or ""))
            if codec:
                return codec
    return None


def producer_encoder(stream_info: Optional[dict[str, Any]]) -> Optional[str]:
    """ffmpeg encoder of a transcoding producer (``h264_nvenc``, ``h264_vaapi``, ``libx264``)."""
    for prod in (stream_info or {}).get("producers") or []:
        text = " ".join(str(prod.get(k) or "") for k in ("source", "url"))
        m = re.search(r"-c:v\s+(\S+)", text)
        if m:
            return m.group(1)
    return None


def mismatch_source_codec(message: str) -> Optional[str]:
    """Producer codec from go2rtc's "codecs not matched: video:H265 => video:H264 ..."."""
    m = re.search(r"codecs not matched:\s*(.*?)=>", message or "")
    if not m:
        return None
    for part in m.group(1).split(","):
        part = part.strip()
        if part.startswith("video:"):
            return _codec_from_text(part[6:])
    return None


def _consumer_connected(consumer: dict[str, Any]) -> bool:
    """A go2rtc WebRTC consumer whose ICE connected (it then has a ``remote_addr``, protocol ``http+udp``).

    Not the senders' byte counts: go2rtc counts what it hands to the track, which
    grows for a consumer still checking ICE too (seen with go2rtc 1.9.14).
    """
    if not isinstance(consumer, dict):
        return True
    return bool(consumer.get("remote_addr")) or int(consumer.get("bytes_send") or 0) > 0


def _fingerprint(source: str) -> str:
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


@dataclass
class Session:
    id: str
    camera_id: str
    purpose: str
    viewer_ip: str
    remote: bool
    user: Optional[str]
    heartbeat_required: bool = True
    started_at: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.time)
    last_heartbeat_mono: float = field(default_factory=time.monotonic)
    fingerprint: str = ""
    codec: Optional[str] = None
    source_codec: Optional[str] = None
    transcoded: bool = False
    encoder: Optional[str] = None
    ready: bool = False              # negotiation finished
    consumer_seen: bool = False
    consumers: int = 0
    bytes_sent: int = 0
    remote_addr: Optional[str] = None
    pair: Optional[dict[str, Any]] = None
    report_state: Optional[str] = None

    @property
    def stream(self) -> str:
        return STREAM_PREFIX + self.id

    def expires_mono(self, idle_timeout: float, max_session: float) -> float:
        cap = self.started_mono + max_session
        if not self.heartbeat_required:
            return cap
        return min(cap, self.last_heartbeat_mono + idle_timeout)

    def expires_at(self, idle_timeout: float, max_session: float) -> str:
        delta = self.expires_mono(idle_timeout, max_session) - time.monotonic()
        return _iso(time.time() + max(0.0, delta)) or ""

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "camera_id": self.camera_id,
            "purpose": self.purpose,
            "viewer_ip": self.viewer_ip,
            "remote": self.remote,
            "started_at": _iso(self.started_at),
            "last_heartbeat": _iso(self.last_heartbeat),
            "heartbeat_required": self.heartbeat_required,
            "pair": self.pair,
            "bytes_sent": self.bytes_sent,
            "codec": self.codec,
            "transcoded": self.transcoded,
            "encoder": self.encoder,
            "connected_to": self.remote_addr,
        }


class WebRtcSessions:
    def __init__(self, manager=go2rtc_manager) -> None:
        self.go2rtc = manager
        self.sessions: dict[str, Session] = {}
        # Streams of ended sessions whose consumer may still be attached:
        # stream name -> monotonic time the session ended.
        self._ending: dict[str, float] = {}
        self._outcomes: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
        self._codec_hint: dict[str, tuple[str, str]] = {}  # camera_id -> (fingerprint, codec)
        self._reaper: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[tuple[object, asyncio.Lock]] = None
        self._restart_when_idle = False
        self.violations = 0
        manager.on_exit = self._on_go2rtc_exit

    # -- settings -------------------------------------------------------------------

    @staticmethod
    def video_settings() -> dict[str, Any]:
        from app.services.remote_access_service import remote_access_service

        return remote_access_service.webrtc_settings()

    @staticmethod
    def timings() -> dict[str, float]:
        return {"heartbeat_s": int(settings.WEBRTC_HEARTBEAT_S),
                "idle_timeout_s": int(settings.WEBRTC_IDLE_TIMEOUT_S),
                "max_session_s": int(settings.WEBRTC_MAX_SESSION_S)}

    def force_grace_s(self) -> float:
        return float(settings.WEBRTC_HEARTBEAT_S) + FORCE_GRACE_EXTRA_S

    def settings_changed(self) -> None:
        """Mode/port/STUN changed: go2rtc picks them up on its next start."""
        if not self.go2rtc.running():
            return
        self._restart_when_idle = True
        if not self.sessions and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._apply_idle_restart(), self._loop)

    async def _apply_idle_restart(self) -> None:
        if self._restart_when_idle and not self.sessions and not self._ending:
            self._restart_when_idle = False
            logger.info("Remote video settings changed: stopping go2rtc; the next session starts it "
                        "with the new settings")
            await self.go2rtc.stop()

    # -- helpers ----------------------------------------------------------------------

    def _async_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock[0] is not loop:
            self._lock = (loop, asyncio.Lock())
        return self._lock[1]

    def active_count(self) -> int:
        return len(self.sessions)

    def list(self) -> list[dict[str, Any]]:
        return [s.public() for s in self.sessions.values()]

    def outcomes(self) -> list[dict[str, Any]]:
        return list(self._outcomes)

    def _record(self, entry: dict[str, Any]) -> None:
        entry.setdefault("at", _iso(time.time()))
        self._outcomes.append(entry)

    async def _camera(self, camera_id: str):
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.db_models import CameraModel

        async with async_session_factory() as db:
            res = await db.execute(select(CameraModel).where(CameraModel.id == camera_id))
            return res.scalar_one_or_none()

    @staticmethod
    def _source_for(cam) -> str:
        """The URL analysis opens: stream_selection + stored credentials (pipeline_supervisor)."""
        from app.services.pipeline_supervisor import _camera_source

        return _camera_source(cam) or ""

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._loop = asyncio.get_running_loop()
            self._reaper = asyncio.create_task(self._reap_loop(), name="webrtc-session-reaper")

    # -- create -----------------------------------------------------------------------

    async def create(self, *, camera_id: str, offer_sdp: str, purpose: str, viewer_ip: str, remote: bool,
                     user: Optional[str], heartbeat_required: bool = True) -> tuple[Session, str]:
        """Open a session and return it with go2rtc's SDP answer. Raises SessionError."""
        limit = int(self.video_settings()["max_video_sessions"])
        async with self._async_lock():
            if len(self.sessions) >= limit:
                raise SessionError(429, "video_session_limit",
                                   f"{limit} live video sessions are already open (Settings -> Online access).",
                                   max_sessions=limit)
            sess = Session(id=secrets.token_hex(16), camera_id=camera_id, purpose=purpose, viewer_ip=viewer_ip,
                           remote=remote, user=user, heartbeat_required=heartbeat_required)
            self.sessions[sess.id] = sess  # reserves the slot while negotiating
        try:
            answer = await self._negotiate(sess, offer_sdp)
        except SessionError as exc:
            self.sessions.pop(sess.id, None)
            await self._drop_stream(sess.stream)
            self._record({"session_id": sess.id, "camera_id": camera_id, "purpose": purpose, "remote": remote,
                          "viewer_ip": viewer_ip, "state": "failed", "end_reason": exc.code,
                          "error": exc.reason})
            raise
        except BaseException:
            self.sessions.pop(sess.id, None)
            await self._drop_stream(sess.stream)
            raise
        sess.ready = True
        sess.last_heartbeat, sess.last_heartbeat_mono = time.time(), time.monotonic()
        self._ensure_reaper()
        logger.info(f"Live video session {sess.id[:8]} opened: camera={camera_id} purpose={purpose} "
                    f"remote={remote} viewer={viewer_ip} codec={sess.codec}"
                    + (f" (transcoded from {sess.source_codec} with {sess.encoder or 'ffmpeg'})"
                       if sess.transcoded else ""))
        return sess, answer

    async def _negotiate(self, sess: Session, offer_sdp: str) -> str:
        cam = await self._camera(sess.camera_id)
        if cam is None:
            raise SessionError(404, "unknown_camera", f"Unknown camera {sess.camera_id}.")
        if not cam.is_ai_enabled:
            raise SessionError(409, "camera_off", "This camera is turned off.")
        try:
            source = self._source_for(cam)
        except Exception as exc:  # noqa: BLE001
            raise SessionError(503, "webrtc_unavailable",
                               f"no stream source ({exc.__class__.__name__})") from None
        if not source:
            raise SessionError(503, "webrtc_unavailable", "no stream source")
        if not source.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
            raise SessionError(503, "webrtc_unavailable",
                               "no stream source (only network cameras can be viewed directly)")
        sess.fingerprint = _fingerprint(source)
        try:
            await self.go2rtc.ensure_running()
        except Go2RtcUnavailable as exc:
            raise SessionError(503, "webrtc_unavailable", f"go2rtc not running: {exc}") from None

        offered = offer_video_codecs(offer_sdp)
        if not offered:
            raise SessionError(502, "negotiation_failed", "the offer has no video section (recvonly video expected)")
        hint = self._codec_hint.get(sess.camera_id)
        known = hint[1] if hint and hint[0] == sess.fingerprint else None
        transcode = bool(known) and known not in offered
        if transcode:
            sess.source_codec = known   # (the log and diagnostics said "transcoded from None")
        if transcode and "H264" not in offered:
            raise SessionError(502, "negotiation_failed",
                               f"this browser supports neither the camera's {known} nor H.264")
        try:
            answer = await self._exchange(sess, source, offer_sdp, transcode)
        except Go2RtcError as exc:
            msg = str(exc)
            if not transcode and "codecs not matched" in msg:
                sess.source_codec = mismatch_source_codec(msg)
                if sess.source_codec:
                    self._codec_hint[sess.camera_id] = (sess.fingerprint, sess.source_codec)
                if "H264" not in offered:
                    raise SessionError(502, "negotiation_failed",
                                       f"this browser supports neither the camera's "
                                       f"{sess.source_codec or 'codec'} nor H.264") from None
                logger.info(f"Camera {sess.camera_id}: the browser cannot play "
                            f"{sess.source_codec or 'the stream codec'}; transcoding to H.264")
                try:
                    answer = await self._exchange(sess, source, offer_sdp, True)
                except Go2RtcError as exc2:
                    raise self._negotiation_error(exc2) from None
            else:
                raise self._negotiation_error(exc) from None
        await self._learn_codec(sess)
        return answer

    async def _exchange(self, sess: Session, source: str, offer_sdp: str, transcode: bool) -> str:
        # The first transcode probes the hardware once (about a second): off the event loop.
        src = await asyncio.to_thread(transcode_source, source) if transcode else source
        sess.transcoded = transcode
        await self.go2rtc.add_stream(sess.stream, src)
        return await self.go2rtc.webrtc_answer(sess.stream, offer_sdp)

    @staticmethod
    def _negotiation_error(exc: Go2RtcError) -> SessionError:
        msg = redact(str(exc))
        if exc.status == 0:
            return SessionError(503, "webrtc_unavailable", f"go2rtc not running ({msg})")
        low = msg.lower()
        if "401" in low or "unauthorized" in low:
            reason = "the camera refused the login (check its credentials)"
        elif any(w in low for w in ("timeout", "refused", "no route", "unreachable", "i/o")):
            reason = f"the camera stream could not be opened ({msg[:160]})"
        else:
            reason = msg[:240]
        return SessionError(502, "negotiation_failed", reason)

    async def _learn_codec(self, sess: Session) -> None:
        try:
            info = await self.go2rtc.stream(sess.stream)
        except Go2RtcError:
            info = None
        codec = producer_codec(info)
        if sess.transcoded:
            sess.codec = "H264"
            sess.encoder = producer_encoder(info)
        else:
            sess.codec = codec
            sess.source_codec = codec
            if codec:
                self._codec_hint[sess.camera_id] = (sess.fingerprint, codec)
        cons = (info or {}).get("consumers") or []
        if cons:
            sess.consumer_seen = True
            sess.consumers = len(cons)

    # -- heartbeat / close / report -------------------------------------------------

    def heartbeat(self, session_id: str) -> Optional[str]:
        sess = self.sessions.get(session_id)
        if sess is None or not sess.ready:
            return None
        sess.last_heartbeat, sess.last_heartbeat_mono = time.time(), time.monotonic()
        t = self.timings()
        return sess.expires_at(t["idle_timeout_s"], t["max_session_s"])

    async def close(self, session_id: str, reason: str = "closed_by_viewer") -> bool:
        sess = self.sessions.get(session_id)
        if sess is None or not sess.ready:
            return False
        await self._end(sess, reason)
        # The page closes its RTCPeerConnection with the DELETE: check soon
        # instead of waiting a full reaper tick.
        if self._loop is not None:
            self._loop.call_later(1.0, lambda: asyncio.ensure_future(self._settle_ending()))
        return True

    def report(self, session_id: str, data: dict[str, Any]) -> bool:
        sess = self.sessions.get(session_id)
        pair = data.get("pair") or None
        entry = {"session_id": session_id, "report": data.get("state"), "pair": pair,
                 "bytes_received": data.get("bytes_received"), "frames_decoded": data.get("frames_decoded"),
                 "error": redact(str(data.get("error")))[:300] if data.get("error") else None}
        if sess is not None:
            sess.report_state = data.get("state")
            if pair:
                sess.pair = pair
            entry.update(camera_id=sess.camera_id, purpose=sess.purpose, remote=sess.remote,
                         viewer_ip=sess.viewer_ip, codec=sess.codec, transcoded=sess.transcoded)
        else:
            # The page sends its closing report and DELETE together, so the report
            # often lands after the session ended: take the camera etc. from its
            # outcome (the diagnostics table showed such rows without a camera).
            for prev in reversed(self._outcomes):
                if prev.get("session_id") == session_id and prev.get("camera_id"):
                    entry.update({k: prev.get(k) for k in ("camera_id", "purpose", "remote", "viewer_ip",
                                                           "codec", "transcoded")})
                    break
        if pair and "relay" in (pair.get("local"), pair.get("remote")):
            self.violations += 1
            entry["violation"] = "relay candidate pair: remote video must be direct, a relay was used"
            logger.error(f"Live video session {session_id[:8]}: a RELAY candidate pair was selected. "
                         "Remote video must be direct; no TURN server may be configured.")
        self._record(entry)
        return sess is not None

    async def _end(self, sess: Session, reason: str) -> None:
        if self.sessions.pop(sess.id, None) is None:
            return
        t = time.time()
        self._record({"session_id": sess.id, "camera_id": sess.camera_id, "purpose": sess.purpose,
                      "remote": sess.remote, "viewer_ip": sess.viewer_ip, "state": "ended", "end_reason": reason,
                      "started_at": _iso(sess.started_at), "duration_s": round(t - sess.started_at, 1),
                      "pair": sess.pair, "bytes_sent": sess.bytes_sent, "codec": sess.codec,
                      "transcoded": sess.transcoded, "encoder": sess.encoder})
        logger.info(f"Live video session {sess.id[:8]} ended ({reason}): camera={sess.camera_id} "
                    f"{t - sess.started_at:.0f}s, {sess.bytes_sent} bytes sent")
        self._ending[sess.stream] = time.monotonic()

    async def end_camera(self, camera_id: str, reason: str) -> int:
        """End every session of one camera (turned off, deleted, credentials changed)."""
        n = 0
        for sess in list(self.sessions.values()):
            if sess.camera_id == camera_id and sess.ready:
                await self._end(sess, reason)
                n += 1
        return n

    async def _drop_stream(self, name: str) -> None:
        self._ending.pop(name, None)
        if not self.go2rtc.running():
            return
        try:
            await self.go2rtc.delete_stream(name)
        except Go2RtcError as exc:
            logger.debug(f"go2rtc stream {name} not deleted: {exc}")

    # -- reaper -----------------------------------------------------------------------

    async def _reap_loop(self) -> None:
        interval = max(0.5, float(settings.WEBRTC_REAP_INTERVAL_S))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.reap_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Live video reaper pass failed")
                if not self.sessions and not self._ending:
                    await self._apply_idle_restart()
                    break  # nothing to watch: stop polling until the next session
        finally:
            self._reaper = None

    async def reap_once(self) -> None:
        t = self.timings()
        now = time.monotonic()
        for sess in list(self.sessions.values()):
            if not sess.ready:
                continue
            if now - sess.started_mono > t["max_session_s"]:
                await self._end(sess, "max_session")
            elif sess.heartbeat_required and now - sess.last_heartbeat_mono > t["idle_timeout_s"]:
                await self._end(sess, "heartbeat_missed")
        await self._check_cameras()
        await self._check_go2rtc()

    async def _check_cameras(self) -> None:
        ids = {s.camera_id for s in self.sessions.values() if s.ready}
        if not ids:
            return
        from sqlalchemy import select

        from app.database import async_session_factory
        from app.models.db_models import CameraModel

        async with async_session_factory() as db:
            rows = (await db.execute(select(CameraModel).where(CameraModel.id.in_(ids)))).scalars().all()
        cams = {c.id: c for c in rows}
        fingerprints: dict[str, str] = {}
        for sess in list(self.sessions.values()):
            if not sess.ready:
                continue
            cam = cams.get(sess.camera_id)
            if cam is None:
                await self._end(sess, "camera_deleted")
                continue
            if not cam.is_ai_enabled:
                await self._end(sess, "camera_off")
                continue
            if sess.camera_id not in fingerprints:
                try:
                    fingerprints[sess.camera_id] = _fingerprint(self._source_for(cam))
                except Exception:  # noqa: BLE001
                    fingerprints[sess.camera_id] = ""
            if fingerprints[sess.camera_id] != sess.fingerprint:
                await self._end(sess, "camera_source_changed")

    async def _check_go2rtc(self) -> None:
        if not self.go2rtc.running():
            for sess in list(self.sessions.values()):
                if sess.ready:
                    await self._end(sess, "gateway_stopped")
            self._ending.clear()
            return
        try:
            streams = await self.go2rtc.streams()
        except Go2RtcError as exc:
            logger.warning(f"Live video: go2rtc did not answer ({exc})")
            return
        now = time.monotonic()
        for sess in list(self.sessions.values()):
            if not sess.ready:
                continue
            info = streams.get(sess.stream)
            if info is None:
                await self._end(sess, "gateway_restarted")
                continue
            consumers = info.get("consumers") or []
            if consumers:
                sess.consumer_seen = True
                sess.consumers = len(consumers)
                c = consumers[0]
                sent = sum(int(x.get("bytes") or 0) for x in (c.get("senders") or []) if isinstance(x, dict))
                sess.bytes_sent = max(int(c.get("bytes_send") or 0), sent)
                sess.remote_addr = c.get("remote_addr") or sess.remote_addr
            elif sess.consumer_seen:
                sess.consumers = 0
                await self._end(sess, "connection_closed")
            else:
                await self._end(sess, "never_connected")
        force = False
        for name, ended in list(self._ending.items()):
            info = streams.get(name)
            consumers = (info or {}).get("consumers") or []
            if not consumers:
                await self._drop_stream(name)
            elif now - ended > self.force_grace_s() and (
                    any(_consumer_connected(c) for c in consumers) or now - ended > UNCONNECTED_CONSUMER_MAX_S):
                force = True
        # Orphans: streams no session owns (e.g. from before an app restart).
        live = {s.stream for s in self.sessions.values()} | set(self._ending)
        for name in streams:
            if name not in live:
                if (streams[name] or {}).get("consumers"):
                    self._ending[name] = now
                else:
                    await self._drop_stream(name)
        if force:
            await self._force_restart()

    async def _settle_ending(self) -> None:
        if not self._ending or not self.go2rtc.running():
            return
        try:
            streams = await self.go2rtc.streams()
        except Go2RtcError:
            return
        for name in list(self._ending):
            info = streams.get(name)
            if info is None or not (info.get("consumers") or []):
                await self._drop_stream(name)

    async def _force_restart(self) -> None:
        logger.warning("Live video: a viewer kept its connection open after its session ended; restarting "
                       "go2rtc to cut it (every other live video connection is ended too and reopens)")
        for sess in list(self.sessions.values()):
            if sess.ready:
                await self._end(sess, "gateway_restart")
        self._ending.clear()
        await self.go2rtc.stop()

    def _on_go2rtc_exit(self, reason: str) -> None:
        """Called from the supervisor thread when go2rtc dies: its sessions are gone."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def end_all():
            for sess in list(self.sessions.values()):
                if sess.ready:
                    await self._end(sess, "gateway_crashed")
            self._ending.clear()

        asyncio.run_coroutine_threadsafe(end_all(), loop)

    # -- lifespan -----------------------------------------------------------------------

    async def stop(self) -> None:
        for sess in list(self.sessions.values()):
            await self._end(sess, "shutdown")
        self._ending.clear()
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        await self.go2rtc.stop()

    # -- diagnostics -----------------------------------------------------------------------

    async def diagnostics(self, probe_nat: bool = True) -> dict[str, Any]:
        from app.services import stun_probe

        s = self.video_settings()
        g = self.go2rtc.status()
        api_ok = False
        if g["running"]:
            try:
                await self.go2rtc.api_info()
                api_ok = True
            except Go2RtcError:
                api_ok = False
        nat: Optional[dict[str, Any]] = None
        if probe_nat:
            nat = await asyncio.to_thread(stun_probe.probe, list(s["stun_servers"]))
        out = {
            "go2rtc": {**g, "api_reachable": api_ok},
            "mode": s["webrtc_mode"],
            "port": s["webrtc_port"] if s["webrtc_mode"] == "fixed_port" else None,
            "stun_servers": s["stun_servers"],
            "max_sessions": s["max_video_sessions"],
            "active_sessions": self.active_count(),
            "relay_violations": self.violations,
            "nat": nat,
            "public_address": (nat or {}).get("public_address"),
            "nat_mapping": (nat or {}).get("mapping", "unknown"),
            "sessions": self.outcomes(),
            "transcode_encoder": settings.WEBRTC_TRANSCODE_ENCODER or "auto",
        }
        out["advice"] = advice(out)
        return out


def advice(d: dict[str, Any]) -> list[str]:
    tips: list[str] = []
    g = d.get("go2rtc") or {}
    if not g.get("installed"):
        tips.append("go2rtc is not installed, so remote live video cannot work. Re-run the installer "
                    "(it fetches the pinned go2rtc) or set GO2RTC_PATH.")
    elif g.get("state") == "error" and g.get("last_error"):
        tips.append(f"go2rtc failed: {g['last_error']}")
    if not d.get("stun_servers"):
        tips.append("No STUN server is configured: viewers outside the store cannot discover this device's "
                    "public address. Add one (default stun:stun.ikorex.com.au:3478).")
    nat = d.get("nat") or {}
    configured = [r for r in nat.get("servers") or [] if r.get("configured")]
    if configured and not any(r.get("mapped") for r in configured):
        tips.append("None of the configured STUN servers answered. Check that outgoing UDP to them is allowed "
                    "and that the STUN server is running.")
    mapping = d.get("nat_mapping")
    if mapping == "endpoint_independent":
        tips.append("The store router keeps the same public port for every destination: direct video works "
                    "without any router change (mode 'auto').")
    elif mapping == "endpoint_dependent":
        if d.get("mode") == "fixed_port":
            tips.append("The store router changes the public port per destination (symmetric NAT). Fixed-port "
                        "mode needs UDP port %s forwarded to this device on the router." % d.get("port"))
        else:
            tips.append("The store router changes the public port per destination (symmetric NAT): direct "
                        "video may fail from some networks. Forward a UDP port (e.g. 8555) to this device "
                        "on the router and switch the video mode to 'fixed_port'.")
    elif nat:
        tips.append("The NAT behaviour could not be determined (fewer than two STUN servers answered).")
    if d.get("mode") == "fixed_port":
        tips.append(f"Fixed-port mode: forward UDP {d.get('port')} on the store router to this device, and allow "
                    f"it in this device's firewall (for example: sudo ufw allow {d.get('port')}/udp).")
    if d.get("relay_violations"):
        tips.append("A browser reported a RELAY connection. Remote video must be direct: remove any TURN "
                    "server from the browser or network configuration.")
    failed = [o for o in d.get("sessions") or [] if o.get("report") == "failed"]
    if failed:
        tips.append(f"{len(failed)} recent session(s) could not connect directly. Typical causes: the viewer's "
                    "network blocks UDP (some corporate or guest Wi-Fi), or a symmetric NAT on both sides.")
    return tips


webrtc_sessions = WebRtcSessions()
