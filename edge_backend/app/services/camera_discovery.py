"""Real camera discovery: USB, ONVIF WS-Discovery, mDNS and an RTSP subnet sweep.

This replaces ``camera_network_manager``, which injected three hardcoded
"SYNTHETIC" devices into every scan result regardless of what hardware was
present, and probed only nine fixed host suffixes rather than the subnet.

Everything here reports only what actually answered. A scan on a network with
no cameras returns an empty list -- it never invents a device so the UI has
something to show.

A device is offered as a camera only with evidence that it streams video:

* it answered an RTSP request with an ``RTSP/1.0`` status line, or
* it answered ONVIF WS-Discovery with a ProbeMatch whose Types include
  ``NetworkVideoTransmitter``.

An mDNS advertisement alone is not evidence: printers advertise ``_http._tcp``
too (a Brother printer, ``BRWxxxxxxxxxxxx.local``, was once offered and
adopted as ``rtsp://<printer>:80/onvif1``). mDNS hosts are therefore verified
with an RTSP handshake, and printers are excluded outright. No stream URL is
ever guessed on a port that did not answer RTSP.
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
# that let us identify the vendor; 37777 is Dahua private.
RTSP_PORTS = (554, 8554)
HTTP_PORTS = (80, 8000)
DAHUA_PRIVATE_PORT = 37777

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


def _vendor_from_realm(realm: str) -> Optional[str]:
    """Vendor signature in an RTSP/HTTP auth realm (e.g. Dahua's "Login to <serial>")."""
    low = (realm or "").lower()
    if low.startswith("login to "):
        return "Dahua"
    if "hikvision" in low or low.startswith("ip camera("):
        return "Hikvision"
    return None


async def rtsp_options(host: str, port: int, timeout: float = 1.5) -> Optional[str]:
    """Speak RTSP OPTIONS and return the Server banner, or None if it is not RTSP.

    A genuine handshake distinguishes a camera from any other service that
    happens to hold port 554 open, and the banner usually names the vendor,
    which is what selects the right URL grammar. A reply must start with an
    RTSP status line; an HTTP server that echoes the request is not RTSP.
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
        if not re.match(r"RTSP/\d\.\d \d{3}", text):
            return None
        m = re.search(r"Server:\s*([^\r\n]+)", text, re.IGNORECASE)
        # An RTSP-speaking device with no Server header is still a camera.
        banner = m.group(1).strip() if m else "RTSP"
        realm = re.search(r'WWW-Authenticate:[^\r\n]*realm="([^"]*)"', text, re.IGNORECASE)
        vendor = _vendor_from_realm(realm.group(1)) if realm else None
        if vendor and vendor.lower() not in banner.lower():
            banner = f"{banner} ({vendor})"
        return banner
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
        dev = parse_ws_discovery_reply(host, xml)
        if dev is not None:
            devices[host] = dev
    return list(devices.values())


def parse_ws_discovery_reply(host: str, xml: str) -> Optional[DeviceProfile]:
    """A camera profile from one WS-Discovery reply, or None if it is not a video transmitter.

    Other WS-Discovery devices (printers announce ``wprt:PrintDeviceType``,
    Windows hosts ``pub:Computer``) can answer on the same multicast group;
    only a ProbeMatch whose Types list ``NetworkVideoTransmitter`` is a camera.
    """
    if not re.search(r"ProbeMatch\b", xml or ""):
        return None
    types = " ".join(re.findall(r"<(?:\w+:)?Types[^>]*>([^<]*)</", xml))
    if "networkvideotransmitter" not in types.lower():
        return None
    # Scopes carry vendor/model, e.g. onvif://www.onvif.org/name/Dahua
    scopes = " ".join(re.findall(r"onvif://[^\s<]+", xml))
    driver = driver_for_hint(scopes)
    name_m = re.search(r"onvif://www\.onvif\.org/name/([^\s<]+)", xml)
    hw_m = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<]+)", xml)
    return DeviceProfile(
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


# mDNS services browsed. Camera-ish ones yield candidates (verified by an RTSP
# handshake before being offered); printer ones only mark a host as a printer.
MDNS_CAMERA_SERVICES = ("_rtsp._tcp.local.", "_onvif._tcp.local.", "_http._tcp.local.")
MDNS_PRINTER_SERVICES = (
    "_ipp._tcp.local.", "_ipps._tcp.local.", "_printer._tcp.local.",
    "_pdl-datastream._tcp.local.", "_scanner._tcp.local.", "_uscan._tcp.local.",
)
# Brother's default hostnames: BRW (wireless) / BRN (wired) + MAC address.
_PRINTER_NAME_RE = re.compile(r"(^|[\s.@])BR[WN][0-9A-F]{12}\b|\b(printer|laserjet|officejet|deskjet|"
                              r"pixma|imageclass|ecotank|workforce|mfc-|dcp-|hl-l)", re.IGNORECASE)
PRINTER_ONLY_PORTS = {515, 631, 9100}


def is_printer_mdns(service_types: Iterable[str], names: Iterable[str], ports: Iterable[int] = ()) -> bool:
    """True when mDNS says this host is a printer/scanner, not a camera."""
    types = {t.lower() for t in service_types}
    if types & set(MDNS_PRINTER_SERVICES):
        return True
    if any(_PRINTER_NAME_RE.search(n or "") for n in names):
        return True
    ports = set(ports)
    return bool(ports) and ports <= PRINTER_ONLY_PORTS


async def classify_mdns_records(records: list[dict], rtsp_check=None) -> list[DeviceProfile]:
    """Turn raw mDNS records into camera profiles, keeping only verified RTSP devices.

    ``records`` are ``{"host", "type", "name", "server", "port"}`` dicts. For
    each host that is not a printer, RTSP is tried on the port it advertises
    for ``_rtsp._tcp`` (if any) and then on the standard RTSP ports. Only a
    host that answers RTSP is returned, with stream URLs on the port that
    answered.
    """
    rtsp_check = rtsp_check or rtsp_options
    hosts: dict[str, dict] = {}
    for r in records:
        h = hosts.setdefault(r["host"], {"types": set(), "names": [], "ports": set(), "rtsp_port": None})
        h["types"].add(str(r.get("type") or "").lower())
        h["names"] += [str(r.get("name") or ""), str(r.get("server") or "")]
        if r.get("port"):
            h["ports"].add(int(r["port"]))
        if str(r.get("type") or "").lower() == "_rtsp._tcp.local." and r.get("port"):
            h["rtsp_port"] = int(r["port"])

    async def verify(host: str, h: dict) -> Optional[DeviceProfile]:
        if is_printer_mdns(h["types"], h["names"], h["ports"]):
            logger.info(f"mDNS: {host} ({', '.join(n for n in h['names'] if n)[:80]}) is a printer; not a camera")
            return None
        if not h["types"] & set(MDNS_CAMERA_SERVICES):
            return None
        candidates = [p for p in (h["rtsp_port"], *RTSP_PORTS) if p]
        for port in dict.fromkeys(candidates):
            banner = await rtsp_check(host, port)
            if banner is None:
                continue
            label = " ".join([banner, *h["names"]])
            driver = driver_for_hint(label)
            server = next((n for n in h["names"][1::2] if n), "") or next((n for n in h["names"] if n), host)
            return DeviceProfile(
                id=f"mdns:{host}:{port}",
                transport="rtsp",
                driver=driver,
                host=host,
                port=port,
                model_name=server.rstrip(".")[:120],
                manufacturer=driver.upper() if driver != "onvif" else "mDNS",
                requires_credentials=True,
                streams=build_stream_urls(driver, host, port=port),
            )
        logger.debug(f"mDNS: {host} advertises {sorted(h['types'])} but does not answer RTSP; not offered")
        return None

    results = await asyncio.gather(*(verify(host, h) for host, h in hosts.items()))
    return [d for d in results if d is not None]


async def scan_mdns(timeout: float = 2.5) -> list[DeviceProfile]:
    """Browse mDNS, then offer only hosts that answer RTSP and are not printers."""

    def _browse() -> list[dict]:
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        except ImportError:
            return []

        found: list[dict] = []

        class _Listener(ServiceListener):
            def add_service(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name, timeout=1500)
                except Exception:
                    return
                if not info or not info.addresses:
                    return
                found.append({
                    "host": socket.inet_ntoa(info.addresses[0]),
                    "type": type_,
                    "name": name,
                    "server": info.server or "",
                    "port": info.port,
                })

            def update_service(self, zc, type_, name):
                pass

            def remove_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        try:
            listener = _Listener()
            for svc in (*MDNS_CAMERA_SERVICES, *MDNS_PRINTER_SERVICES):
                ServiceBrowser(zc, svc, listener)
            time.sleep(timeout)
        except Exception as e:
            logger.debug(f"mDNS browse failed: {e}")
        finally:
            with contextlib.suppress(Exception):
                zc.close()
        return list(found)

    records = await asyncio.to_thread(_browse)
    return await classify_mdns_records(records)


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
                # Port 554 answered RTSP. Check for Dahua private port 37777 or HTTP for vendor hint.
                is_dahua = await _tcp_open(host, DAHUA_PRIVATE_PORT, timeout * 0.5)
                hint = banner
                if is_dahua:
                    driver = "dahua"
                    model_name = f"Dahua NVR/Camera ({banner[:80]})"
                else:
                    for hp in HTTP_PORTS:
                        if await _tcp_open(host, hp, timeout * 0.5):
                            if page := await http_banner(host, hp):
                                hint = f"{banner} {page[:512]}"
                            break
                    driver = driver_for_hint(hint)
                    model_name = banner[:120]

                # Candidate URLs are kept credential-free: they are persisted in
                # discovered_devices.stream_urls. Saved NVR credentials are
                # applied at adoption and stored encrypted per camera.
                streams = build_stream_urls(driver, host, port=port)

                found.append(
                    DeviceProfile(
                        id=f"rtsp:{host}:{port}",
                        transport="rtsp",
                        driver=driver,
                        host=host,
                        port=port,
                        model_name=model_name,
                        manufacturer=driver.upper(),
                        channels=16 if driver == "dahua" else 1,
                        requires_credentials=True,
                        streams=streams,
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
