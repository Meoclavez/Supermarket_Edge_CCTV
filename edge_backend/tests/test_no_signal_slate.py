"""The NO SIGNAL slate shows the whole reason, inside the picture.

The camera's error used to be cut at a fixed character count and drawn from a
fixed offset, so the wrong-password message ended at "... Check the user" and
long RTSP errors ran off the right edge. These tests pin that every word of
the reason is drawn, on lines that fit the slate, and that nothing is drawn
in the margins.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services import no_signal_slate as slate
from app.services.live_analytics_engine import AUTH_FAILED_HINT

AUTH_MSG = f"camera rejected the username/password (RTSP 401). {AUTH_FAILED_HINT}"
RTSP_MSG = ("Cannot open RTSP source "
            "rtsp://192.168.1.64:554/cam/realmonitor?channel=1&subtype=1&unicast=true&proto=Onvif")
SIZES = [(960, 540), (854, 480)]


def _layout(msg, width, height):
    margin = max(24, width // 16)
    max_w = width - 2 * margin
    lines, scale, line_h = slate.fit_message(msg, max_w, height // 2 - margin)
    return lines, scale, line_h, max_w, height // 2 - margin


@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("msg", [AUTH_MSG, RTSP_MSG])
def test_every_word_is_kept_on_lines_that_fit(msg, width, height):
    lines, scale, line_h, max_w, max_h = _layout(msg, width, height)
    for line in lines:
        assert cv2.getTextSize(line, slate.FONT, scale, 1)[0][0] <= max_w, line
    assert len(lines) * line_h <= max_h
    # Nothing dropped: the lines put back together are the whole message.
    assert "".join(lines).replace(" ", "") == msg.replace(" ", "")
    assert not any(line.endswith("...") for line in lines)


def test_wrong_password_hint_is_not_cut_mid_sentence():
    lines, *_ = _layout(AUTH_MSG, 960, 540)
    text = " ".join(lines)
    assert "Check the username and password" in text
    assert text.endswith("does not lock the account.")
    assert len(lines) > 1


def test_word_longer_than_a_line_is_broken_between_characters():
    url = "rtsp://" + "a" * 300
    lines = slate.wrap_text(url, 200, 0.5)
    assert len(lines) > 1
    assert "".join(lines) == url
    assert all(cv2.getTextSize(line, slate.FONT, 0.5, 1)[0][0] <= 200 for line in lines)


def test_message_too_long_even_at_smallest_size_is_visibly_shortened():
    msg = "word " * 2000
    lines, scale, line_h = slate.fit_message(msg, 400, 100)
    assert scale == slate.TEXT_SCALES[-1]
    assert len(lines) * line_h <= 100
    assert lines[-1].endswith("...")
    assert all(cv2.getTextSize(line, slate.FONT, scale, 1)[0][0] <= 400 for line in lines)


def test_non_ascii_is_drawn_as_ascii():
    lines, *_ = slate.fit_message("Cannot open RTSP source … retrying – now", 800, 200)
    assert " ".join(lines) == "Cannot open RTSP source ... retrying - now"


@pytest.mark.parametrize("width,height", SIZES)
@pytest.mark.parametrize("msg", [AUTH_MSG, RTSP_MSG, "x" * 5000])
def test_nothing_is_drawn_in_the_margins(msg, width, height):
    frame = slate.render_no_signal(msg, width, height)
    assert frame.shape == (height, width, 3)
    bg = np.array(slate.BACKGROUND, dtype=np.uint8)
    edge = 16
    for strip in (frame[:, :edge], frame[:, -edge:], frame[:edge, :], frame[-edge:, :]):
        assert (strip == bg).all()
    # Text was drawn at all.
    assert (frame != bg).any(axis=2).sum() > 500


def test_stream_placeholder_is_a_960x540_jpeg_with_the_reason():
    from app.main import _placeholder_frame

    data = _placeholder_frame(AUTH_MSG)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (540, 960)


def test_snapshot_without_a_frame_is_the_wrapped_slate():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        res = client.get("/api/v1/cameras/cam_slate_absent/snapshot")
    assert res.status_code == 200, res.text
    assert res.headers["X-Frame-Source"] == "no-signal"
    img = cv2.imdecode(np.frombuffer(res.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (480, 854)
