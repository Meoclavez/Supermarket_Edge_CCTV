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
        import cv2
        cap = cv2.VideoCapture(url)
        if not cap.isOpened():
            return {"success": False, "error": "Could not connect to RTSP stream"}
        
        ret, frame = cap.read()
        if not ret:
            cap.release()
            return {"success": False, "error": "Could not read frame"}
            
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        
        return {
            "success": True,
            "resolution": f"{width}x{height}",
            "fps": fps
        }

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
