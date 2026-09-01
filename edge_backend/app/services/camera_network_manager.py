"""Camera Network Discovery and Hardware Scanner Service."""

import os
import sys
import socket
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict, Any
import cv2

logger = logging.getLogger("CameraNetworkManager")

class CameraScanner:
    @staticmethod
    def scan_usb_cameras() -> List[Dict[str, str]]:
        found = []
        if sys.platform.startswith("linux"):
            for dev in sorted(Path("/dev").glob("video[0-9]*")):
                try:
                    cap = cv2.VideoCapture(str(dev))
                    if cap.isOpened():
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            found.append({
                                "id": f"usb_{dev.name}",
                                "name": f"USB Camera ({dev.name})",
                                "url": str(dev),
                                "type": "USB"
                            })
                        cap.release()
                except Exception:
                    pass
        else:
            for idx in range(3):
                try:
                    cap = cv2.VideoCapture(idx)
                    if cap.isOpened():
                        found.append({
                            "id": f"usb_{idx}",
                            "name": f"USB Camera (Index {idx})",
                            "url": str(idx),
                            "type": "USB"
                        })
                        cap.release()
                except Exception:
                    pass
        return found

    @staticmethod
    def test_tcp_port(host: str, port: int, timeout: float = 0.1) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                return s.connect_ex((host, port)) == 0
        except Exception:
            return False

    @classmethod
    def _probe_host(cls, ip: str) -> List[Dict[str, str]]:
        results = []
        if cls.test_tcp_port(ip, 81, timeout=0.1):
            results.append({
                "id": f"net_esp_{ip.replace('.', '_')}",
                "name": f"ESP32-S3 Camera ({ip}:81)",
                "url": f"http://{ip}:81/stream",
                "type": "ESP32"
            })
        elif cls.test_tcp_port(ip, 554, timeout=0.1):
            results.append({
                "id": f"rtsp_{ip.replace('.', '_')}",
                "name": f"RTSP IP Camera ({ip}:554)",
                "url": f"rtsp://admin:admin123@{ip}:554/h264Preview_01_sub",
                "type": "RTSP"
            })
        return results

    @classmethod
    def scan_network_cameras(cls) -> List[Dict[str, str]]:
        found = []
        try:
            ip = socket.gethostbyname("esp32-cctv.local")
            found.append({
                "id": "esp32_mdns",
                "name": "ESP32-S3 Camera (esp32-cctv.local)",
                "url": f"http://{ip}:81/stream",
                "type": "ESP32"
            })
        except Exception:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            ip_parts = local_ip.split(".")
            subnet_prefix = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}."

            candidate_ips = [
                local_ip,
                f"{subnet_prefix}86",
                f"{subnet_prefix}50",
                f"{subnet_prefix}51",
                f"{subnet_prefix}52",
                f"{subnet_prefix}10",
                f"{subnet_prefix}1",
                f"{subnet_prefix}2",
                f"{subnet_prefix}100"
            ]

            with ThreadPoolExecutor(max_workers=len(candidate_ips)) as executor:
                probe_results = list(executor.map(cls._probe_host, candidate_ips))
                for res in probe_results:
                    found.extend(res)
        except Exception:
            pass

        return found

    @classmethod
    def discover_all(cls) -> List[Dict[str, str]]:
        sources = [
            {
                "id": "cam_living_room",
                "name": "Living Room (Synthetic Benchmark)",
                "url": "synthetic",
                "type": "SYNTHETIC"
            },
            {
                "id": "cam_front_door",
                "name": "Front Door Entrance",
                "url": "synthetic",
                "type": "SYNTHETIC"
            },
            {
                "id": "cam_backyard",
                "name": "Backyard & Patio",
                "url": "synthetic",
                "type": "SYNTHETIC"
            }
        ]
        sources.extend(cls.scan_usb_cameras())
        sources.extend(cls.scan_network_cameras())
        return sources

camera_network_manager = CameraScanner()
