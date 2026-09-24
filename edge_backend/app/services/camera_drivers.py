"""Camera driver profiles: how to turn a discovered device into a stream URL.

Different vendors expose the same RTSP transport behind different path
grammars, so a single hardcoded URL template cannot address a real mixed
fleet. Each driver here knows one vendor's grammar and how to enumerate the
channels behind a single IP -- which matters for the NVR-style deployments in
the site plan, where one recorder address fronts dozens of cameras.

Credentials are never baked into this module. They arrive from the operator or
from settings at adoption time and are stored per camera.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

RTSP_PORT = 554
ONVIF_PORT = 80


@dataclass
class StreamProfile:
    """One addressable stream on a device."""

    url: str
    channel: int = 1
    # "main" is the full-resolution stream used for recording; "sub" is the
    # low-bitrate stream the analytics pipeline prefers, because running
    # detection on a 1080p main stream wastes decode budget for no accuracy gain.
    quality: str = "main"
    label: str = ""


@dataclass
class DeviceProfile:
    """A device a scan found, with everything needed to adopt it."""

    id: str
    transport: str          # rtsp | usb | mjpeg
    driver: str             # dahua | hikvision | onvif | v4l2 | generic
    host: Optional[str] = None
    port: Optional[int] = None
    device_path: Optional[str] = None
    model_name: Optional[str] = None
    manufacturer: Optional[str] = None
    channels: int = 1
    requires_credentials: bool = False
    streams: list[StreamProfile] = field(default_factory=list)
    reachable: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transport": self.transport,
            "driver": self.driver,
            "host": self.host,
            "port": self.port,
            "device_path": self.device_path,
            "model_name": self.model_name,
            "manufacturer": self.manufacturer,
            "channels": self.channels,
            "requires_credentials": self.requires_credentials,
            "reachable": self.reachable,
            "stream_urls": [
                {"url": s.url, "channel": s.channel, "quality": s.quality, "label": s.label}
                for s in self.streams
            ],
        }


def _auth_prefix(username: Optional[str], password: Optional[str]) -> str:
    """Build the ``user:pass@`` portion, percent-encoding reserved characters.

    Passwords containing ``@`` or ``/`` would otherwise truncate the URL and
    produce a confusing connection failure rather than an auth failure.
    """
    if not username:
        return ""
    if password:
        return f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{quote(username, safe='')}@"


class CameraDriver:
    """Base driver. Subclasses translate a device into concrete stream URLs."""

    name = "generic"

    def build_urls(
        self,
        host: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        channel: int = 1,
        port: int = RTSP_PORT,
    ) -> list[StreamProfile]:
        raise NotImplementedError


class DahuaDriver(CameraDriver):
    """Dahua / Dahua-OEM RTSP grammar.

    Covers both standalone IP cameras and the NVRs used on site, where
    ``channel=N`` selects the camera behind the recorder. ``subtype=0`` is the
    main stream, ``subtype=1`` the sub stream.
    """

    name = "dahua"

    def build_urls(self, host, *, username=None, password=None, channel=1, port=RTSP_PORT):
        auth = _auth_prefix(username, password)
        base = f"rtsp://{auth}{host}:{port}/cam/realmonitor"
        return [
            StreamProfile(
                url=f"{base}?channel={channel}&subtype=1",
                channel=channel,
                quality="sub",
                label=f"Dahua ch{channel} sub (AI recommended)",
            ),
            StreamProfile(
                url=f"{base}?channel={channel}&subtype=0",
                channel=channel,
                quality="main",
                label=f"Dahua ch{channel} main",
            ),
        ]

    def build_nvr_channels(
        self,
        host: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        channels: int = 16,
        port: int = RTSP_PORT,
    ) -> list[StreamProfile]:
        """Generate stream profiles for all channels on a Dahua NVR/DVR."""
        profiles: list[StreamProfile] = []
        for ch in range(1, channels + 1):
            profiles.extend(self.build_urls(host, username=username, password=password, channel=ch, port=port))
        return profiles


class HikvisionDriver(CameraDriver):
    """Hikvision grammar: ``/Streaming/Channels/<ch><stream>``."""

    name = "hikvision"

    def build_urls(self, host, *, username=None, password=None, channel=1, port=RTSP_PORT):
        auth = _auth_prefix(username, password)
        base = f"rtsp://{auth}{host}:{port}/Streaming/Channels"
        return [
            StreamProfile(f"{base}/{channel}01", channel, "main", f"Hikvision ch{channel} main"),
            StreamProfile(f"{base}/{channel}02", channel, "sub", f"Hikvision ch{channel} sub"),
        ]


class OnvifDriver(CameraDriver):
    """Generic ONVIF device.

    Real stream URIs come from the device's Media service at adoption time via
    ``GetStreamUri``. Until the operator supplies credentials we can only offer
    the most common default paths as candidates.
    """

    name = "onvif"

    def build_urls(self, host, *, username=None, password=None, channel=1, port=RTSP_PORT):
        auth = _auth_prefix(username, password)
        candidates = [
            ("/onvif1", "main"),
            ("/Streaming/Channels/101", "main"),
            ("/cam/realmonitor?channel=1&subtype=0", "main"),
            ("/live", "main"),
            ("/stream1", "main"),
        ]
        return [
            StreamProfile(f"rtsp://{auth}{host}:{port}{path}", channel, q, f"ONVIF candidate {path}")
            for path, q in candidates
        ]


class V4L2Driver(CameraDriver):
    """A USB / CSI camera exposed as ``/dev/videoN``."""

    name = "v4l2"

    def build_urls(self, host, *, username=None, password=None, channel=1, port=0):
        # `host` carries the device path for this transport.
        return [StreamProfile(url=host, channel=channel, quality="main", label=f"V4L2 {host}")]


DRIVERS: dict[str, CameraDriver] = {
    d.name: d
    for d in (DahuaDriver(), HikvisionDriver(), OnvifDriver(), V4L2Driver())
}

# Substrings that identify a vendor from an ONVIF scope, HTTP banner or
# mDNS service name. Checked lowercase.
_VENDOR_HINTS = (
    ("dahua", "dahua"),
    ("amcrest", "dahua"),      # Amcrest is Dahua OEM and shares the URL grammar.
    ("lorex", "dahua"),
    ("imou", "dahua"),
    ("hikvision", "hikvision"),
    ("hiwatch", "hikvision"),
    ("annke", "hikvision"),
)


def driver_for_hint(text: Optional[str]) -> str:
    """Guess the driver from a vendor banner. Falls back to generic ONVIF."""
    if not text:
        return "onvif"
    low = text.lower()
    for needle, driver in _VENDOR_HINTS:
        if needle in low:
            return driver
    return "onvif"


def get_driver(name: str) -> CameraDriver:
    return DRIVERS.get((name or "").lower(), DRIVERS["onvif"])


def build_stream_urls(
    driver_name: str,
    host: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    channel: int = 1,
    port: Optional[int] = None,
) -> list[StreamProfile]:
    driver = get_driver(driver_name)
    return driver.build_urls(
        host,
        username=username,
        password=password,
        channel=channel,
        port=port or RTSP_PORT,
    )


def redact_url(url: str) -> str:
    """Strip credentials from a stream URL before it reaches a log or the UI."""
    # Up to the LAST "@" of the authority: a password containing a raw "@"
    # must not leave its tail visible.
    return re.sub(r"://[^/?#\s]*@", "://***@", url or "")


def alternate_stream_url(url: str) -> Optional[str]:
    """If a Dahua or Hikvision stream fails, return its alternate stream quality.

    For Dahua:
      subtype=1 (substream) -> subtype=0 (mainstream)
      subtype=0 (mainstream) -> subtype=1 (substream)
    For Hikvision:
      channel01 -> channel02, channel02 -> channel01
    """
    if not url:
        return None
    if "/cam/realmonitor" in url:
        if "subtype=1" in url:
            return url.replace("subtype=1", "subtype=0")
        elif "subtype=0" in url:
            return url.replace("subtype=0", "subtype=1")
    elif "/Streaming/Channels/" in url:
        if re.search(r"(\d+)02\b", url):
            return re.sub(r"(\d+)02\b", r"\g<1>01", url)
        elif re.search(r"(\d+)01\b", url):
            return re.sub(r"(\d+)01\b", r"\g<1>02", url)
    return None
