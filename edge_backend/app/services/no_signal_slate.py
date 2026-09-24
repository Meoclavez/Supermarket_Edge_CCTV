"""The "NO SIGNAL" slate served in place of a camera picture.

The reason drawn under the title is the camera's own error, which is often
longer than one line ("Cannot open RTSP source rtsp://...", or the rejected
login with its hint). It used to be cut at a fixed character count and drawn
from a fixed offset, so it stopped mid-sentence ("... Check the user") or ran
off the edge. It is now word-wrapped to the slate's width, with the font made
smaller until every line fits, and only a message too long even at the
smallest size is shortened, visibly, with "...".
"""

from __future__ import annotations

import cv2
import numpy as np

FONT = cv2.FONT_HERSHEY_SIMPLEX
BACKGROUND = (18, 20, 26)
TITLE_COLOUR = (90, 96, 112)
TEXT_COLOUR = (70, 76, 92)
# Largest first; the first scale at which the wrapped message fits is used.
TEXT_SCALES = (0.6, 0.55, 0.5, 0.45, 0.4, 0.35)
LINE_GAP = 10


def _ascii(text: str) -> str:
    """Hershey fonts draw only ASCII; anything else would print as '?'."""
    text = str(text).replace("…", "...").replace("–", "-").replace("—", "-")
    return " ".join(text.encode("ascii", "replace").decode("ascii").split())


def _width(text: str, scale: float, thickness: int = 1) -> int:
    return cv2.getTextSize(text, FONT, scale, thickness)[0][0]


def wrap_text(text: str, max_width: int, scale: float, thickness: int = 1) -> list[str]:
    """Split ``text`` into lines no wider than ``max_width`` pixels.

    Breaks between words; a single word wider than a line (a long URL) is
    broken between characters.
    """
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if _width(candidate, scale, thickness) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        while _width(word, scale, thickness) > max_width:
            cut = len(word) - 1
            while cut > 1 and _width(word[:cut], scale, thickness) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def fit_message(text: str, max_width: int, max_height: int, thickness: int = 1) -> tuple[list[str], float, int]:
    """(lines, font scale, line height) for ``text`` inside a box of that size."""
    text = _ascii(text)
    for scale in TEXT_SCALES:
        line_h = cv2.getTextSize("Ag", FONT, scale, thickness)[0][1] + LINE_GAP
        lines = wrap_text(text, max_width, scale, thickness)
        if len(lines) * line_h <= max_height:
            return lines, scale, line_h
    # Still too long at the smallest size: keep what fits and say it was cut.
    max_lines = max(1, max_height // line_h)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _width(last + "...", scale, thickness) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return lines, scale, line_h


def render_no_signal(reason: str, width: int = 960, height: int = 540) -> np.ndarray:
    """A plain slate: "NO SIGNAL" and why, centred, with nothing that could
    pass for a live analysed picture."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:] = BACKGROUND

    title_scale = 1.1 * width / 960
    (tw, th), _ = cv2.getTextSize("NO SIGNAL", FONT, title_scale, 2)
    margin = max(24, width // 16)
    lines, scale, line_h = fit_message(reason or "", width - 2 * margin, height // 2 - margin)

    block_h = th + 24 + len(lines) * line_h
    y = max(margin, (height - block_h) // 2) + th
    cv2.putText(frame, "NO SIGNAL", ((width - tw) // 2, y), FONT, title_scale, TITLE_COLOUR, 2)
    y += 24
    for line in lines:
        y += line_h
        lw = _width(line, scale)
        cv2.putText(frame, line, ((width - lw) // 2, y - LINE_GAP), FONT, scale, TEXT_COLOUR, 1)
    return frame
