"""mDNS / Bonjour auto-discovery broadcaster for Edge CCTV AI system.

Periodically re-checks the host LAN IP (every 30 seconds) and re-registers the
mDNS service if the IP changes (e.g., after VPN drop, cable replug, DHCP renewal).
"""

import asyncio
import socket
import logging
from typing import Optional
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf
from app.config import settings

logger = logging.getLogger("mDNSService")

IP_CHECK_INTERVAL_SECONDS = 30


class EdgeMDNSAdvertiser:
    def __init__(self):
        self.aiozc: Optional[AsyncZeroconf] = None
        self.service_info: Optional[ServiceInfo] = None
        self._current_ip: Optional[str] = None
        self._monitor_task: Optional[asyncio.Task] = None

    def _get_local_lan_ip(self) -> str:
        """Detects host primary LAN IP address by opening a UDP socket to a public DNS."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    def _build_service_info(self, lan_ip: str) -> ServiceInfo:
        """Constructs the mDNS ServiceInfo record for the given IP."""
        properties = {
            "version": settings.VERSION,
            "server_name": settings.APP_NAME,
            "api_port": str(settings.PORT),
            "webrtc_port": "8555",
            "tls_enabled": "true",
            "device_id": "edge-n100-cctv-01"
        }
        return ServiceInfo(
            type_="_cctv-edge._tcp.local.",
            name="Edge-CCTV-Core._cctv-edge._tcp.local.",
            addresses=[socket.inet_aton(lan_ip)],
            port=settings.PORT,
            properties=properties,
            server="edge-cctv.local."
        )

    async def start(self):
        """Register and broadcast mDNS service on local LAN, then start IP monitor."""
        try:
            lan_ip = self._get_local_lan_ip()
            self._current_ip = lan_ip
            logger.info(f"Registering mDNS service for Edge Server at IP: {lan_ip}")

            self.aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            self.service_info = self._build_service_info(lan_ip)

            if hasattr(self.aiozc, "async_register_service"):
                await self.aiozc.async_register_service(self.service_info)
            else:
                await self.aiozc.register_service(self.service_info)
            logger.info("mDNS service '_cctv-edge._tcp.local.' broadcast active.")

            # Start background IP monitor
            self._monitor_task = asyncio.create_task(self._ip_change_monitor())

        except Exception as e:
            logger.error(f"Failed to start mDNS service: {e}")

    async def _ip_change_monitor(self):
        """Periodically checks if the LAN IP has changed and re-registers if so."""
        while True:
            try:
                await asyncio.sleep(IP_CHECK_INTERVAL_SECONDS)
                new_ip = self._get_local_lan_ip()

                if new_ip != self._current_ip and new_ip != "127.0.0.1":
                    logger.warning(
                        f"LAN IP changed ({self._current_ip} -> {new_ip})! Re-registering mDNS..."
                    )
                    if self.aiozc and self.service_info:
                        if hasattr(self.aiozc, "async_unregister_service"):
                            await self.aiozc.async_unregister_service(self.service_info)
                        else:
                            await self.aiozc.unregister_service(self.service_info)
                    
                    self._current_ip = new_ip
                    self.service_info = self._build_service_info(new_ip)
                    if hasattr(self.aiozc, "async_register_service"):
                        await self.aiozc.async_register_service(self.service_info)
                    else:
                        await self.aiozc.register_service(self.service_info)
                    logger.info(f"mDNS service re-registered with new IP: {new_ip}")
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"Error during mDNS IP check: {err}")

    async def stop(self):
        """Cancel IP monitor and unregister mDNS service cleanly upon server shutdown."""
        # Stop monitor task first
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self.aiozc and self.service_info:
            try:
                logger.info("Unregistering mDNS service...")
                if hasattr(self.aiozc, "async_unregister_service"):
                    await self.aiozc.async_unregister_service(self.service_info)
                else:
                    await self.aiozc.unregister_service(self.service_info)
                if hasattr(self.aiozc, "async_close"):
                    await self.aiozc.async_close()
                else:
                    await self.aiozc.close()
                logger.info("mDNS service stopped cleanly.")
            except Exception as e:
                logger.warning(f"Error stopping mDNS: {e}")


mdns_advertiser = EdgeMDNSAdvertiser()
