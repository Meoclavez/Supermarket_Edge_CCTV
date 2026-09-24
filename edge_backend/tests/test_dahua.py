"""Unit and integration tests for Dahua NVR protocol, credential persistence, and probing."""

import pytest
import asyncio
from fastapi.testclient import TestClient
from pathlib import Path
import tempfile
import os
from unittest.mock import patch

from app.main import app
from app.services.camera_drivers import (
    DahuaDriver,
    alternate_stream_url,
    redact_url,
)
from app.services.nvr_credential_service import NVRCredentialService
from app.services.dahua_probe_service import (
    DahuaProbeService,
    ChannelProbeResult,
)
from app.database import engine
from app.models.db_models import Base
from app.services.auth_service import auth_service


@pytest.fixture(scope="session", autouse=True)
def init_test_database():
    """Ensure database schema is created and initialized."""
    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_init())


@pytest.fixture
def auth_headers():
    token = auth_service.create_access_token({"sub": "admin_test", "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


class TestDahuaDriverAndUrls:
    def test_dahua_url_generation(self):
        driver = DahuaDriver()
        profiles = driver.build_urls("192.168.1.108", username="admin", password="dummy-test-password", channel=1)
        assert len(profiles) == 2

        sub = next(p for p in profiles if p.quality == "sub")
        main = next(p for p in profiles if p.quality == "main")

        assert "channel=1" in sub.url
        assert "subtype=1" in sub.url
        assert "channel=1" in main.url
        assert "subtype=0" in main.url
        assert "admin:dummy-test-password@" in sub.url

    def test_dahua_multi_channel_expansion(self):
        driver = DahuaDriver()
        profiles = driver.build_nvr_channels("192.168.1.108", username="admin", password="dummy-test-password", channels=4)
        assert len(profiles) == 8  # 4 channels * 2 streams (sub + main)
        ch_nums = {p.channel for p in profiles}
        assert ch_nums == {1, 2, 3, 4}

    def test_alternate_stream_url_fallback(self):
        sub_url = "rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=3&subtype=1"
        main_url = "rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=3&subtype=0"

        # Substream falls back to mainstream
        assert alternate_stream_url(sub_url) == main_url
        # Mainstream falls back to substream
        assert alternate_stream_url(main_url) == sub_url

    def test_redact_url(self):
        url = "rtsp://admin:fake-secret-pw!@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1"
        redacted = redact_url(url)
        assert "fake-secret-pw!" not in redacted
        assert "rtsp://***@192.168.1.108:554" in redacted


class TestNVRCredentialService:
    def test_persistence_and_masking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = NVRCredentialService(storage_dir=Path(tmpdir))

            # Initial empty state
            empty = svc.load_credentials()
            assert empty["default_username"] == "admin"
            assert empty["default_password"] == ""

            # Save credentials
            saved = svc.save_credentials(
                username="security_admin",
                password="not-a-real-password-for-tests",
                host="192.168.1.150",
                port=554,
                default_channels=16,
            )

            assert saved["default_username"] == "security_admin"
            assert saved["has_password"] is True
            assert saved["password_masked"] == "••••••••"
            assert "192.168.1.150" in saved["nvrs"]

            # Reload from disk in a fresh service instance
            svc2 = NVRCredentialService(storage_dir=Path(tmpdir))
            u, p = svc2.get_auth_for_host("192.168.1.150")
            assert u == "security_admin"
            assert p == "not-a-real-password-for-tests"

            # Check file permissions on POSIX
            if os.name == "posix":
                mode = oct(svc2.config_path.stat().st_mode & 0o777)
                assert mode == "0o600"


class TestDahuaProbeService:
    def test_probe_unreachable_host(self):
        probe_svc = DahuaProbeService()
        with patch.object(probe_svc, "check_tcp", return_value=False):
            res = asyncio.run(probe_svc.probe_nvr("192.0.2.1", port=554))
            assert res.reachable is False
            assert res.authenticated is False
            assert "Could not connect" in (res.error or "")

    def test_probe_auth_failed(self):
        probe_svc = DahuaProbeService()
        with patch.object(probe_svc, "check_tcp", return_value=True), \
             patch.object(probe_svc, "probe_rtsp_auth", return_value=(False, "401 Unauthorized: Invalid NVR username or password")):
            res = asyncio.run(probe_svc.probe_nvr("192.168.1.108", username="admin", password="wrong", port=554))
            assert res.reachable is True
            assert res.authenticated is False
            assert "401 Unauthorized" in (res.error or "")

    def test_probe_channels_active(self):
        probe_svc = DahuaProbeService()
        mock_result_ch1 = ChannelProbeResult(
            channel=1,
            active=True,
            status="ACTIVE",
            sub_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1",
            main_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=1&subtype=0",
            preferred_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1",
            preferred_subtype=1,
            resolution="1280x720",
            fps=25.0,
        )
        mock_result_ch2 = ChannelProbeResult(
            channel=2,
            active=False,
            status="NO_SIGNAL",
            sub_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=2&subtype=1",
            main_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=2&subtype=0",
            preferred_url="rtsp://admin:dummy@192.168.1.108:554/cam/realmonitor?channel=2&subtype=1",
            preferred_subtype=1,
            error="No video signal",
        )

        def mock_worker(host, port, ch, u, p):
            return mock_result_ch1 if ch == 1 else mock_result_ch2

        with patch.object(probe_svc, "check_tcp", return_value=True), \
             patch.object(probe_svc, "probe_rtsp_auth", return_value=(True, None)), \
             patch.object(probe_svc, "_test_channel_worker", side_effect=mock_worker):
            res = asyncio.run(probe_svc.probe_nvr("192.168.1.108", max_channels=2))
            assert res.reachable is True
            assert res.authenticated is True
            assert res.active_channels_count == 1
            assert len(res.channels) == 2
            assert res.channels[0].active is True
            assert res.channels[1].active is False


class TestDahuaAPIRoutes:
    def test_get_and_save_credentials(self, auth_headers):
        client = TestClient(app)
        # 1. Get credentials
        res = client.get("/api/v1/dahua/credentials", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert "credentials" in data

        # 2. Save credentials
        save_res = client.post(
            "/api/v1/dahua/credentials",
            headers=auth_headers,
            json={
                "username": "dahua_operator",
                "password": "fake-nvr-password-for-tests",
                "host": "192.168.1.200",
                "port": 554,
                "default_channels": 16,
            },
        )
        assert save_res.status_code == 200
        saved = save_res.json()["credentials"]
        assert saved["default_username"] == "dahua_operator"
        assert saved["has_password"] is True
        assert saved["password_masked"] == "••••••••"

    def test_adopt_dahua_channels(self, auth_headers):
        client = TestClient(app)
        adopt_payload = {
            "host": "192.168.1.200",
            "port": 554,
            "username": "admin",
            "password": "secret_nvr_pass",
            "channels": [
                {"channel": 1, "name": "Entrance Main Dahua", "department": "ENTRANCE", "quality": "sub"},
                {"channel": 2, "name": "Aisle 1 Dahua", "department": "AISLE", "quality": "sub"},
            ],
        }
        res = client.post("/api/v1/dahua/adopt", headers=auth_headers, json=adopt_payload)
        assert res.status_code == 201
        data = res.json()
        assert data["adopted_count"] == 2
        assert len(data["cameras"]) == 2
        assert data["cameras"][0]["channel"] == 1
        assert "subtype=1" in data["cameras"][0]["stream_url"]
        assert "secret_nvr_pass" not in data["cameras"][0]["stream_url"]  # Redacted
