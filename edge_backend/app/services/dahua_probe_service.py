"""Dahua NVR Multi-Channel Probe & Health Service.

Specialised for Dahua XVR/DVR/NVRs where a single network IP fronts multiple
CCTV camera channels. Handles port verification (554 / 37777), fast RTSP auth
checking, bounded channel sweeps, and stream quality fallbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.services.camera_drivers import redact_url
from app.services.nvr_credential_service import nvr_credential_service

logger = logging.getLogger(__name__)

DAHUA_PRIVATE_PORT = 37777
RTSP_PORT = 554


@dataclass
class ChannelProbeResult:
    channel: int
    active: bool
    status: str             # "ACTIVE" | "NO_SIGNAL" | "AUTH_FAILED" | "TIMEOUT" | "ERROR"
    sub_url: str
    main_url: str
    preferred_url: str
    preferred_subtype: int  # 1 for sub, 0 for main
    resolution: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sub_url_redacted"] = redact_url(self.sub_url)
        d["main_url_redacted"] = redact_url(self.main_url)
        d["preferred_url_redacted"] = redact_url(self.preferred_url)
        return d


@dataclass
class NVRProbeSummary:
    host: str
    port: int
    reachable: bool
    is_dahua: bool
    authenticated: bool
    channel_count_scanned: int
    active_channels_count: int
    channels: List[ChannelProbeResult]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "reachable": self.reachable,
            "is_dahua": self.is_dahua,
            "authenticated": self.authenticated,
            "channel_count_scanned": self.channel_count_scanned,
            "active_channels_count": self.active_channels_count,
            "error": self.error,
            "channels": [c.to_dict() for c in self.channels],
        }


def _auth_prefix(username: Optional[str], password: Optional[str]) -> str:
    if not username:
        return ""
    if password:
        return f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{quote(username, safe='')}@"


def build_dahua_url(host: str, port: int, channel: int, subtype: int, username: str = "", password: str = "") -> str:
    auth = _auth_prefix(username, password)
    return f"rtsp://{auth}{host}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"


class DahuaProbeService:
    """Probes Dahua NVR devices, tests authentication, and enumerates channels."""

    @staticmethod
    async def check_tcp(host: str, port: int, timeout: float = 1.2) -> bool:
        """Test if a TCP port is open."""
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    @staticmethod
    async def probe_rtsp_auth(host: str, port: int, username: str, password: str, timeout: float = 4.0) -> Tuple[bool, Optional[str]]:
        """Verify the NVR accepts these credentials with one RTSP DESCRIBE.

        Returns (authenticated, error_message). Uses the shared RTSP handshake
        (rtsp_probe), which answers Dahua's Digest challenge on the same
        connection. The previous inline version sent HTTP Basic, which Dahua
        rejects with 401 whatever the password, so a correct password was
        reported as wrong and every click cost a failed login on the NVR.
        """
        from app.services import rtsp_probe

        creds = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username and password else ""
        url = f"rtsp://{creds}{host}:{port}/cam/realmonitor?channel=1&subtype=1"
        res = await asyncio.to_thread(rtsp_probe.probe_rtsp, url, max(1.0, float(timeout)))
        if res.outcome == rtsp_probe.OK:
            return True, None
        if res.outcome == rtsp_probe.AUTH_FAILED:
            return False, f"NVR rejected the username/password (RTSP {res.status_code})"
        if res.outcome == rtsp_probe.AUTH_REQUIRED:
            return False, "NVR requires a username and password"
        if res.outcome in (rtsp_probe.NOT_FOUND, rtsp_probe.RTSP_ERROR) and res.status_code not in (None, 401, 403):
            # Some firmware answers 404/400/454 when channel 1 is empty or
            # disabled; the login itself was accepted.
            return True, None
        if res.outcome == rtsp_probe.NOT_RTSP:
            return False, "Device did not respond with RTSP protocol"
        return False, f"RTSP probe failed: {res.detail}"

    @staticmethod
    def _test_channel_worker(
        host: str,
        port: int,
        channel: int,
        username: str,
        password: str,
    ) -> ChannelProbeResult:
        """Synchronous probe for a single Dahua NVR channel using OpenCV FFMPEG TCP."""
        import cv2

        sub_url = build_dahua_url(host, port, channel, subtype=1, username=username, password=password)
        main_url = build_dahua_url(host, port, channel, subtype=0, username=username, password=password)

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;2000000"

        # Try substream first (ideal for AI)
        cap = cv2.VideoCapture(sub_url, cv2.CAP_FFMPEG)
        used_subtype = 1
        active = False
        frame = None
        w, h, fps = 0, 0, 0.0

        try:
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    active = True
                    h, w = frame.shape[:2]
                    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.release()

            # If substream failed, fallback to test mainstream
            if not active:
                cap_main = cv2.VideoCapture(main_url, cv2.CAP_FFMPEG)
                if cap_main.isOpened():
                    ok, frame = cap_main.read()
                    if ok and frame is not None:
                        active = True
                        used_subtype = 0
                        h, w = frame.shape[:2]
                        fps = cap_main.get(cv2.CAP_PROP_FPS) or 25.0
                cap_main.release()

        except Exception as e:
            logger.debug(f"Channel {channel} probe exception: {e}")

        pref_url = sub_url if used_subtype == 1 else main_url
        if active:
            return ChannelProbeResult(
                channel=channel,
                active=True,
                status="ACTIVE",
                sub_url=sub_url,
                main_url=main_url,
                preferred_url=pref_url,
                preferred_subtype=used_subtype,
                resolution=f"{w}x{h}",
                width=w,
                height=h,
                fps=round(fps, 1),
                error=None,
            )
        else:
            return ChannelProbeResult(
                channel=channel,
                active=False,
                status="NO_SIGNAL",
                sub_url=sub_url,
                main_url=main_url,
                preferred_url=sub_url,
                preferred_subtype=1,
                error="No video signal on this channel",
            )

    async def probe_nvr(
        self,
        host: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        port: int = RTSP_PORT,
        max_channels: int = 16,
        concurrency: int = 4,
    ) -> NVRProbeSummary:
        """Full probe of a Dahua NVR: check connectivity, auth, and scan channels."""
        host = host.strip()
        # Fall back to saved credentials if not provided
        if not username or password is None:
            saved_u, saved_p = nvr_credential_service.get_auth_for_host(host)
            username = username or saved_u or "admin"
            password = password if password is not None else saved_p

        # 1. Connectivity check on RTSP port (554) and Dahua port (37777)
        rtsp_open = await self.check_tcp(host, port, timeout=1.5)
        dahua_port_open = await self.check_tcp(host, DAHUA_PRIVATE_PORT, timeout=1.2)

        if not rtsp_open:
            return NVRProbeSummary(
                host=host,
                port=port,
                reachable=False,
                is_dahua=dahua_port_open,
                authenticated=False,
                channel_count_scanned=0,
                active_channels_count=0,
                channels=[],
                error=f"Could not connect to {host}:{port}. Check that the Dahua NVR is powered on and IP is correct.",
            )

        # 2. Authenticate RTSP
        auth_ok, auth_err = await self.probe_rtsp_auth(host, port, username, password)
        if not auth_ok:
            return NVRProbeSummary(
                host=host,
                port=port,
                reachable=True,
                is_dahua=dahua_port_open or True,
                authenticated=False,
                channel_count_scanned=0,
                active_channels_count=0,
                channels=[],
                error=auth_err or "RTSP authentication failed (401 Unauthorized)",
            )

        # 3. Channel scan with bounded concurrency
        max_ch = max(1, min(int(max_channels or 16), 64))
        sem = asyncio.Semaphore(concurrency)

        async def probe_single(ch: int) -> ChannelProbeResult:
            async with sem:
                return await asyncio.to_thread(
                    self._test_channel_worker,
                    host,
                    port,
                    ch,
                    username,
                    password,
                )

        tasks = [probe_single(ch) for ch in range(1, max_ch + 1)]
        results = await asyncio.gather(*tasks)

        # Sort by channel number
        sorted_results = sorted(results, key=lambda x: x.channel)
        active_count = sum(1 for c in sorted_results if c.active)

        return NVRProbeSummary(
            host=host,
            port=port,
            reachable=True,
            is_dahua=dahua_port_open or True,
            authenticated=True,
            channel_count_scanned=max_ch,
            active_channels_count=active_count,
            channels=sorted_results,
            error=None,
        )


dahua_probe_service = DahuaProbeService()
