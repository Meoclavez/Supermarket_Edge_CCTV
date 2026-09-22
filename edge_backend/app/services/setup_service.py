import os
import uuid
import secrets
import asyncio
import subprocess
import logging
from typing import Dict, Any, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.db_models import SystemSetupModel

logger = logging.getLogger("SetupService")

class SystemSetupService:
    @staticmethod
    async def is_setup_completed(session: AsyncSession) -> bool:
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        return record is not None and record.value == "true"

    @staticmethod
    async def get_setup_step(session: AsyncSession) -> int:
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_step")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record:
            return int(record.value)
        return 1

    @staticmethod
    async def set_setup_step(session: AsyncSession, step: int):
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_step")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record:
            record.value = str(step)
        else:
            session.add(SystemSetupModel(key="setup_step", value=str(step)))
        await session.commit()

    @staticmethod
    async def complete_setup(session: AsyncSession):
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record:
            record.value = "true"
        else:
            session.add(SystemSetupModel(key="setup_completed", value="true"))
        await session.commit()

    @staticmethod
    def detect_hardware() -> Dict[str, Any]:
        hailo_available = os.path.exists("/dev/hailo0")
        vaapi_available = os.path.exists("/dev/dri/renderD128")
        
        hw_decode_supported = False
        try:
            result = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                hw_decode_supported = True
        except Exception:
            pass
            
        cpu_info = "Unknown"
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        cpu_info = line.split(":")[1].strip()
                        break
        except Exception:
            pass

        return {
            "hailo_available": hailo_available,
            "vaapi_available": vaapi_available,
            "hw_decode_supported": hw_decode_supported,
            "cpu_info": cpu_info
        }

    @staticmethod
    async def scan_rtsp_cameras(subnet: str) -> list:
        # Placeholder for simple port scan (e.g., nmap or python async socket connection)
        # Simplified for now
        return []

    @staticmethod
    async def test_rtsp_url(url: str) -> Dict[str, Any]:
        import asyncio
        import os
        from app.services.camera_drivers import redact_url

        def _probe() -> Dict[str, Any]:
            import cv2

            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;3000000"
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                return {
                    "success": False,
                    "error": f"Could not connect to stream {redact_url(url)}. Verify IP, port, and authentication.",
                    "error_code": "CONNECT_FAILED",
                }

            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()
                return {
                    "success": False,
                    "error": f"Stream opened but failed to read video frame. Channel may be inactive or signal lost.",
                    "error_code": "NO_SIGNAL",
                }

            h, w = frame.shape[:2]
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.release()

            return {
                "success": True,
                "resolution": f"{w}x{h}",
                "width": int(w),
                "height": int(h),
                "fps": round(fps, 1),
                "url": redact_url(url),
            }

        return await asyncio.to_thread(_probe)

    @staticmethod
    def generate_secure_secrets():
        env_file = ".env"
        secrets_dict = {
            "JWT_SECRET": secrets.token_hex(32),
            "COTURN_SECRET": secrets.token_hex(32),
            "INTERNAL_SERVICE_KEY": secrets.token_hex(32)
        }
        
        env_lines = []
        if os.path.exists(env_file):
            with open(env_file, "r") as f:
                env_lines = f.readlines()
        
        new_env_lines = []
        for line in env_lines:
            key = line.split("=")[0].strip()
            if key not in secrets_dict:
                new_env_lines.append(line)
                
        for k, v in secrets_dict.items():
            new_env_lines.append(f"{k}={v}\n")
            
        with open(env_file, "w") as f:
            f.writelines(new_env_lines)

setup_service = SystemSetupService()
