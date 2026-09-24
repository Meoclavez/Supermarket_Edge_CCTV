import os
import uuid
import secrets
import asyncio
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.db_models import SystemSetupModel

logger = logging.getLogger("SetupService")
# Under the "edge" logger tree so the handler main.py installs prints it.
_code_log = logging.getLogger("edge.setup")

# Unambiguous alphabet (no 0/O, 1/I/L) so the code survives being read off a
# terminal and typed on a phone.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUP = 4
_CODE_GROUPS = 2


def setup_code_path() -> Path:
    from app.config import settings
    return Path(settings.STORAGE_DIR) / "setup_code.txt"


def normalise_setup_code(code: Optional[str]) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class SetupCodeManager:
    """One-time code that must accompany first-run account creation.

    Without it, the first-run form is first-come-first-served: whoever on the
    store LAN reaches the dashboard before the owner becomes the owner. The
    code is printed in the server log and written to
    ``<STORAGE_DIR>/setup_code.txt`` (mode 0600), so only someone with access
    to the machine's console, journal or disk can read it.

    The file is the source of truth, which lets the out-of-process recovery CLI
    (scripts/manage_operator.py reset-setup) issue a code the running server
    will accept without a restart.
    """

    def generate(self, reason: str = "") -> str:
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP * _CODE_GROUPS))
        code = "-".join(raw[i:i + _CODE_GROUP] for i in range(0, len(raw), _CODE_GROUP))
        path = setup_code_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(code + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        self.announce(code, reason)
        return code

    @staticmethod
    def announce(code: str, reason: str = "") -> None:
        bar = "=" * 64
        _code_log.warning(
            "\n%s\n  FIRST-RUN SETUP CODE:  %s\n"
            "  No operator account exists yet. Enter this code in the dashboard's\n"
            "  'Create the operator account' form. It is single use and is also\n"
            "  stored in %s (mode 0600).%s\n%s",
            bar, code, setup_code_path(), f"\n  ({reason})" if reason else "", bar,
        )

    def read(self) -> Optional[str]:
        try:
            text = setup_code_path().read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return None
        return text if len(normalise_setup_code(text)) == _CODE_GROUP * _CODE_GROUPS else None

    def ensure(self, reason: str = "", announce_existing: bool = False) -> str:
        code = self.read()
        if code is None:
            return self.generate(reason)
        if announce_existing:
            self.announce(code, reason)
        return code

    def verify(self, candidate: Optional[str]) -> bool:
        expected = self.read()
        if not expected:
            return False
        a = normalise_setup_code(candidate).encode()
        b = normalise_setup_code(expected).encode()
        # compare_digest is constant-time for equal lengths; the length itself
        # is public (fixed format), so padding is not needed.
        return secrets.compare_digest(a, b)

    def invalidate(self) -> None:
        try:
            setup_code_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _code_log.error("Could not remove used setup code file: %s", exc)


setup_code_manager = SetupCodeManager()


async def active_admin_exists(session: AsyncSession) -> bool:
    from sqlalchemy import func, or_
    from app.models.db_models import AdminUserModel

    count = await session.scalar(
        select(func.count(AdminUserModel.id)).where(
            or_(AdminUserModel.is_active.is_(True), AdminUserModel.is_active.is_(None))
        )
    )
    return bool(count)


async def ensure_setup_code_if_needed(session: Optional[AsyncSession] = None,
                                      reason: str = "server start",
                                      announce_existing: bool = True) -> Optional[str]:
    """Issue (or re-announce) the setup code when no operator exists; drop it otherwise."""
    if session is None:
        from app.database import async_session_factory
        async with async_session_factory() as s:
            return await ensure_setup_code_if_needed(s, reason, announce_existing)
    if await active_admin_exists(session):
        setup_code_manager.invalidate()
        return None
    return setup_code_manager.ensure(reason, announce_existing=announce_existing)


class SystemSetupService:
    @staticmethod
    async def is_setup_completed(session: AsyncSession) -> bool:
        """Completed means an operator account exists *and* setup was finished.

        The stored flag alone is not trusted: a database has been seen with
        setup_completed=true written weeks before any account existed, which
        would have let the dashboard skip the protected first-run step.
        """
        if not await active_admin_exists(session):
            return False
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_completed")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        return record is not None and record.value == "true"

    @staticmethod
    async def get_setup_step(session: AsyncSession) -> int:
        if not await active_admin_exists(session):
            return 1
        stmt = select(SystemSetupModel).where(SystemSetupModel.key == "setup_step")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if record and str(record.value).isdigit():
            return int(record.value)
        return 1

    @staticmethod
    async def reset_setup_state(session: AsyncSession) -> None:
        """Forget setup progress so the next visitor sees first-run again."""
        from sqlalchemy import delete
        await session.execute(
            delete(SystemSetupModel).where(SystemSetupModel.key.in_(("setup_completed", "setup_step")))
        )
        await session.commit()

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
        """Open the stream, grab one frame and report it (shared with the
        dashboard's Add camera form, see app/services/camera_source.py).

        ``fps`` is null when it is not known; it used to default to 25.
        """
        from app.services import camera_source

        try:
            src = camera_source.normalize_source(None, url)
        except camera_source.SourceError as exc:
            return {"success": False, "error": str(exc), "error_code": "INVALID_SOURCE"}
        return await camera_source.test_connection(src)

    @staticmethod
    def generate_secure_secrets() -> list[str]:
        """Ensure the per-machine secret store holds every secret. Never rotates.

        This used to rewrite JWT_SECRET/COTURN_SECRET/INTERNAL_SERVICE_KEY in
        .env when setup completed, which invalidated the operator's fresh
        session and put secrets in a file people copy around. Secrets are now
        generated once per machine at startup by app/services/secret_store.py;
        this only re-creates the store if it was deleted, and never touches
        .env or a secret that is already in use.
        """
        from app.config import settings
        from app.services.secret_store import ensure_device_secrets

        try:
            return ensure_device_secrets(settings.STORAGE_DIR)
        except OSError as exc:
            logger.error(f"Could not verify the machine-local secret store: {exc}")
            return []

setup_service = SystemSetupService()
