"""Real camera discovery: USB, ONVIF WS-Discovery, mDNS and an RTSP subnet sweep.

This replaces ``camera_network_manager``, which injected three hardcoded
"SYNTHETIC" devices into every scan result regardless of what hardware was
present, and probed only nine fixed host suffixes rather than the subnet.

Everything here reports only what actually answered. A scan on a network with
no cameras returns an empty list -- it never invents a device so the UI has
something to show.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import time
import uuid
from dataclasses import replace
from typing import Iterable, Optional

from app.services.camera_drivers import (
    DeviceProfile,
    build_stream_urls,
    driver_for_hint,
)

logger = logging.getLogger(__name__)

# Ports worth probing. 554 is RTSP; 80/8000 carry the ONVIF and web services
# that let us identify the vendor; 81 is the ESP32-CAM stream port.
RTSP_PORTS = (554, 8554)
HTTP_PORTS = (80, 8000, 81)

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)

# ONVIF probe for any NetworkVideoTransmitter on the segment.
_WS_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{msg_id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body>
  <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
 </e:Body>
</e:Envelope>"""


def _local_ipv4() -> Optional[str]:
    """Best-effort primary IPv4 of this host, without sending traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket sets the source address without any packets.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_subnets() -> list[ipaddress.IPv4Network]:
    """Every /24 this host has an address on, so a scan covers all NICs."""
    nets: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    try:
        import ifaddr  # bundled with zeroconf, already a dependency

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if not ip.is_IPv4:
                    continue
                addr = ip.ip
                if addr.startswith("127."):
                    continue
                net = ipaddress.ip_network(f"{addr}/24", strict=False)
                if str(net) not in seen:
                    seen.add(str(net))
                    nets.append(net)
    except Exception as e:  # pragma: no cover - depends on host NIC layout
        logger.debug(f"ifaddr enumeration failed, falling back to primary IP: {e}")

    if not nets and (ip := _local_ipv4()):
        nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    return nets


async def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def rtsp_options(host: str, port: int, timeout: float = 1.5) -> Optional[str]:
    """Speak RTSP OPTIONS and return the Server banner.

    A genuine handshake distinguishes a camera from any other service that
    happens to hold port 554 open, and the banner usually names the vendor,
    which is what selects the right URL grammar.
    """
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        req = (
            f"OPTIONS rtsp://{host}:{port} RTSP/1.0\r\n"
            "CSeq: 1\r\n"
            "User-Agent: EdgeCCTV-Discovery\r\n\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        text = data.decode("utf-8", "ignore")
        if "RTSP/1.0" not in text:
            return None
        m = re.search(r"Server:\s*([^\r\n]+)", text, re.IGNORECASE)
        # An RTSP-speaking device with no Server header is still a camera.
        return (m.group(1).strip() if m else "RTSP")
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def http_banner(host: str, port: int, timeout: float = 1.2) -> Optional[str]:
    """Fetch enough of the device's HTTP root to identify the vendor."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(2048), timeout=timeout)
        return data.decode("utf-8", "ignore")
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def scan_usb_cameras() -> list[DeviceProfile]:
    """Enumerate ``/dev/video*`` nodes that actually yield a frame.

    A capture device can enumerate but fail to produce video (many laptops
    expose metadata nodes alongside the real one), so each candidate must
    deliver a frame before it is reported.
    """
    import glob

    def _probe() -> list[DeviceProfile]:
        import cv2

        found: list[DeviceProfile] = []
        for path in sorted(glob.glob("/dev/video[0-9]*")):
            cap = None
            try:
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                h, w = frame.shape[:2]
                found.append(
                    DeviceProfile(
                        id=f"usb:{path}",
                        transport="usb",
                        driver="v4l2",
                        device_path=path,
                        model_name=f"USB capture {w}x{h}",
                        manufacturer="V4L2",
                        streams=build_stream_urls("v4l2", path),
                    )
                )
            except Exception as e:
                logger.debug(f"USB probe failed for {path}: {e}")
            finally:
                if cap is not None:
                    with contextlib.suppress(Exception):
                        cap.release()
        return found

    return await asyncio.to_thread(_probe)


async def scan_onvif_wsdiscovery(timeout: float = 3.0) -> list[DeviceProfile]:
    """ONVIF WS-Discovery multicast probe.

    This is how ONVIF devices are meant to be found, and it works without
    credentials or guessing addresses.
    """

    def _probe() -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        msg = _WS_PROBE.format(msg_id=uuid.uuid4()).encode()
        for net_ip in {ip for ip in (_local_ipv4(),) if ip} or {""}:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                if net_ip:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(net_ip),
                    )
                    sock.bind((net_ip, 0))
                sock.settimeout(timeout)
                sock.sendto(msg, WS_DISCOVERY_ADDR)
                # This runs in a worker thread, which has no event loop of its
                # own, so the deadline must come from a plain monotonic clock.
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        data, addr = sock.recvfrom(8192)
                    except socket.timeout:
                        break
                    except OSError:
                        break
                    results.append((addr[0], data.decode("utf-8", "ignore")))
            except OSError as e:
                logger.debug(f"WS-Discovery on {net_ip or 'default'} failed: {e}")
            finally:
                sock.close()
        return results

    raw = await asyncio.to_thread(_probe)

    devices: dict[str, DeviceProfile] = {}
    for host, xml in raw:
        # Scopes carry vendor/model, e.g. onvif://www.onvif.org/name/Dahua
        scopes = " ".join(re.findall(r"onvif://[^\s<]+", xml))
        driver = driver_for_hint(scopes)
        name_m = re.search(r"onvif://www\.onvif\.org/name/([^\s<]+)", xml)
        hw_m = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<]+)", xml)
        devices[host] = DeviceProfile(
            id=f"onvif:{host}",
            transport="rtsp",
            driver=driver,
            host=host,
            port=554,
            model_name=(hw_m.group(1) if hw_m else None),
            manufacturer=(name_m.group(1) if name_m else "ONVIF"),
            requires_credentials=True,
            streams=build_stream_urls(driver, host),
        )
    return list(devices.values())


async def scan_mdns(timeout: float = 2.5) -> list[DeviceProfile]:
    """Browse mDNS for camera-ish services (ESP32-CAM and ONVIF advertisers)."""

    def _browse() -> list[DeviceProfile]:
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        except ImportError:
            return []

        found: list[DeviceProfile] = []

        class _Listener(ServiceListener):
            def add_service(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name, timeout=1500)
                except Exception:
                    return
                if not info or not info.addresses:
                    return
                host = socket.inet_ntoa(info.addresses[0])
                label = f"{name} {info.server or ''}"
                driver = driver_for_hint(label)
                port = info.port or (81 if driver == "esp32" else 554)
                found.append(
                    DeviceProfile(
                        id=f"mdns:{host}:{port}",
                        transport="mjpeg" if driver == "esp32" else "rtsp",
                        driver=driver,
                        host=host,
                        port=port,
                        model_name=(info.server or name).rstrip("."),
                        manufacturer="mDNS",
                        streams=build_stream_urls(driver, host, port=port),
                    )
                )

            def update_service(self, zc, type_, name):
                pass

            def remove_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        try:
            listener = _Listener()
            for svc in ("_rtsp._tcp.local.", "_onvif._tcp.local.", "_http._tcp.local."):
                ServiceBrowser(zc, svc, listener)
            import time

            time.sleep(timeout)
        except Exception as e:
            logger.debug(f"mDNS browse failed: {e}")
        finally:
            with contextlib.suppress(Exception):
                zc.close()
        return found

    return await asyncio.to_thread(_browse)


async def scan_subnet(
    subnet: Optional[str] = None,
    *,
    concurrency: int = 256,
    timeout: float = 0.6,
) -> list[DeviceProfile]:
    """Sweep every host on the given /24 (or all local /24s) for RTSP.

    The previous implementation checked nine hardcoded host suffixes, which
    would miss almost any real installation. This walks the whole range with a
    bounded number of concurrent connections so it finishes in a few seconds.
    """
    nets: list[ipaddress.IPv4Network]
    if subnet:
        try:
            nets = [ipaddress.ip_network(subnet, strict=False)]
        except ValueError as e:
            raise ValueError(f"invalid subnet '{subnet}': {e}") from e
    else:
        nets = local_subnets()

    hosts: list[str] = []
    for net in nets:
        # Guard against someone passing a /8 and hanging the scan for hours.
        if net.num_addresses > 4096:
            logger.warning(f"Subnet {net} too large to sweep; limiting to first 4096 hosts")
        for i, h in enumerate(net.hosts()):
            if i >= 4096:
                break
            hosts.append(str(h))

    sem = asyncio.Semaphore(concurrency)
    found: list[DeviceProfile] = []

    async def probe(host: str):
        async with sem:
            for port in RTSP_PORTS:
                if not await _tcp_open(host, port, timeout):
                    continue
                banner = await rtsp_options(host, port)
                if banner is None:
                    continue
                # Port 554 answered RTSP. Try HTTP for a better vendor hint.
                hint = banner
                for hp in HTTP_PORTS:
                    if await _tcp_open(host, hp, timeout * 0.5):
                        if page := await http_banner(host, hp):
                            hint = f"{banner} {page[:512]}"
                        break
                driver = driver_for_hint(hint)
                found.append(
                    DeviceProfile(
                        id=f"rtsp:{host}:{port}",
                        transport="rtsp",
                        driver=driver,
                        host=host,
                        port=port,
                        model_name=banner[:120],
                        manufacturer=driver.upper(),
                        requires_credentials=True,
                        streams=build_stream_urls(driver, host, port=port),
                    )
                )
                return

    await asyncio.gather(*(probe(h) for h in hosts))
    return found


def _merge(devices: Iterable[DeviceProfile]) -> list[DeviceProfile]:
    """Collapse the same physical device found by more than one method.

    A Dahua camera typically answers WS-Discovery *and* the RTSP sweep; the
    ONVIF result is richer, so it wins on vendor metadata.
    """
    by_host: dict[str, DeviceProfile] = {}
    standalone: list[DeviceProfile] = []
    for d in devices:
        if not d.host:
            standalone.append(d)
            continue
        key = d.host
        if key not in by_host:
            by_host[key] = d
            continue
        existing = by_host[key]
        # Prefer the entry that identified a specific vendor.
        if existing.driver in ("onvif", "generic") and d.driver not in ("onvif", "generic"):
            by_host[key] = replace(d, requires_credentials=True)
        elif not existing.model_name and d.model_name:
            by_host[key] = replace(existing, model_name=d.model_name)
    return standalone + list(by_host.values())


class CameraDiscoveryService:
    """Runs every discovery method concurrently and merges the results."""

    async def discover(
        self,
        *,
        subnet: Optional[str] = None,
        include_usb: bool = True,
        include_network: bool = True,
        timeout: float = 45.0,
    ) -> list[DeviceProfile]:
        """Run every enabled method concurrently and merge what answered.

        Each method gets its own budget and is awaited independently, so one
        slow probe cannot discard the results of the others. A method that
        times out contributes nothing; it never fails the whole scan.
        """
        jobs: list[tuple[str, "asyncio.Future", float]] = []
        if include_usb:
            jobs.append(("usb", asyncio.ensure_future(scan_usb_cameras()), 25.0))
        if include_network:
            jobs.append(("onvif", asyncio.ensure_future(scan_onvif_wsdiscovery()), 12.0))
            jobs.append(("mdns", asyncio.ensure_future(scan_mdns()), 12.0))
            jobs.append(("subnet", asyncio.ensure_future(scan_subnet(subnet)), timeout))

        if not jobs:
            return []

        devices: list[DeviceProfile] = []
        for label, task, budget in jobs:
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=budget)
                devices.extend(result)
                logger.info(
                    f"Discovery[{label}] found {len(result)} in {time.monotonic() - started:.1f}s"
                )
            except asyncio.TimeoutError:
                task.cancel()
                logger.warning(f"Discovery[{label}] exceeded its {budget:.0f}s budget; skipped")
            except Exception as e:
                logger.warning(f"Discovery[{label}] failed: {e}")

        merged = _merge(devices)
        logger.info(f"Discovery finished: {len(merged)} device(s) responded")
        return merged


camera_discovery_service = CameraDiscoveryService()
