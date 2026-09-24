"""The Dahua probe must answer the NVR's Digest challenge.

It used to send HTTP Basic, which Dahua rejects with 401 whatever the
password: the dashboard said "Invalid username or password" for a correct
password, and every click cost a failed login toward the NVR's lockout.
"""

from __future__ import annotations

import asyncio

from app.services.dahua_probe_service import DahuaProbeService
from tests.test_camera_auth_and_discovery import GOOD_PW, USER, FakeRtspCamera


def _probe(cam, user, pw):
    return asyncio.run(DahuaProbeService.probe_rtsp_auth("127.0.0.1", cam.port, user, pw))


def test_right_password_is_accepted_with_digest():
    cam = FakeRtspCamera(per_connection_nonce=True)
    try:
        assert _probe(cam, USER, GOOD_PW) == (True, None)
        assert cam.good_logins == 1 and cam.failed_logins == 0
    finally:
        cam.close()


def test_wrong_password_costs_exactly_one_failed_login():
    cam = FakeRtspCamera(per_connection_nonce=True)
    try:
        ok, err = _probe(cam, USER, "wrong")
        assert not ok and "rejected the username/password" in err and "wrong" not in err
        assert cam.failed_logins == 1
    finally:
        cam.close()


def test_missing_password_is_not_reported_as_authenticated():
    cam = FakeRtspCamera()
    try:
        ok, err = _probe(cam, USER, "")
        assert not ok and "requires a username and password" in err
        assert cam.failed_logins == 0
    finally:
        cam.close()
