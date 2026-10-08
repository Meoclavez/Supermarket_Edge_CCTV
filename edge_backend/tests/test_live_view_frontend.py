"""Dashboard live view: AI overlay (skeleton + behaviour), zoom, frame-rate settings.

* static/js/live_overlay.js and static/js/view_zoom.js run in a small fake
  browser under Node (tests/fixtures/live_view_harness.js; skipped without
  ``node``): letterbox geometry, velocity extrapolation (minus 0.2 s picture delay, capped at 0.3 s),
  colour level, the skeleton drawn only between joints with visibility >= 0.3,
  unconfirmed tracks skipped, and the honest "live AI data unavailable" state
  on a 404 (nothing drawn, the server picture keeps its own overlay).
* The pages carry the elements the scripts use, load them before their
  users, and every CSS class the new code uses is defined.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HARNESS = Path(__file__).resolve().parent / "fixtures" / "live_view_harness.js"

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.fixture(scope="module")
def harness() -> dict:
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(HARNESS), str(STATIC)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_overlay_geometry_and_extrapolation(harness):
    assert harness["content"] == {"x": 0, "y": 60, "w": 640, "h": 360}       # 16:9 in 4:3, letterboxed
    assert harness["content_unknown"] == {"x": 0, "y": 0, "w": 640, "h": 480}
    assert abs(harness["extrap_capped"] - 0.3) < 1e-6
    # 0.2 s after the analysed frame is the picture's own delay: no move.
    assert abs(harness["extrap_small"]) < 1e-6
    assert harness["extrap_coasting"] == 0 and harness["extrap_novel"] == 0
    assert [round(v, 6) for v in harness["moved"]] == [0.2, 0.15, 0.4, 0.35]
    assert harness["levels"] == ["static", "alert", "watch", "normal", "normal"]
    assert harness["chips"][0] == "ALERT · Reaching: Spirits · Hand at chest"
    assert harness["chips"][1:] == ["", "static"]
    assert harness["delays"] == [200, 1000, 500, 333]                         # <= 5 Hz, >= 1 Hz
    assert harness["limbs"] == 19


@needs_node
def test_zoom_keeps_the_point_under_the_cursor(harness):
    z = harness["zoom"]
    assert z["z2"] == {"s": 2, "tx": -100, "ty": -50}
    assert z["fixed"] == {"x": 100, "y": 50}
    assert z["back"] == {"s": 1, "tx": 0, "ty": 0}
    assert z["max"] == 6
    assert z["clamp"] == {"tx": 0, "ty": -360}


@needs_node
def test_live_tracks_draw_box_skeleton_and_behaviour(harness):
    live = harness["live"]
    assert live["fetched"] == ["/api/v1/live/tracks?cameras=cam1"]
    assert live["state"] == "ok" and live["clientDraws"] and live["showing"] and not live["canvas_hidden"]
    assert live["canvas_style"] == {"left": "0px", "top": "60px", "width": "640px", "height": "360px"}
    assert live["strokeRects"] == 1                     # the unconfirmed track is not drawn
    assert live["limbs"] == 17                          # 19 limbs minus the two to the hidden wrists
    assert live["joints"] == 15                         # 17 joints minus the two below visibility 0.3
    assert live["texts"] == ["ALERT · Reaching: Spirits · Hand at chest"]
    assert live["status_text"] == "AI 4.8 fps"
    assert harness["off"] == {"clientDraws": False, "canvas_hidden": True, "stored": '{"cameras":false}'}


@needs_node
def test_unavailable_or_unknown_is_said_and_nothing_is_drawn(harness):
    u = harness["unavailable"]
    assert u["state"] == "unavailable" and u["clientDraws"] is False
    assert u["status_text"] == "AI: live AI data unavailable" and u["status_hidden"] is False
    assert u["canvas_hidden"] is True and u["drew_boxes"] == 0
    assert harness["missing"] == {"status_text": "AI: no live data for this camera", "canvas_hidden": True}


def _ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def test_dashboard_has_the_live_view_controls_and_loads_the_scripts_first():
    html = (STATIC / "index.html").read_text()
    ids = _ids(html)
    for i in ("aiOverlayToggle", "liveBehaviourStrip", "liveBehaviourList", "liveBehaviourNote", "settings-analysis",
              "configAnalysisPriority", "configMaxAnalysisFps", "configAnalysisRateNow"):
        assert i in ids, i
    prio = re.search(r'<select[^>]*id="configAnalysisPriority".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="([^"]*)"', prio) == ["", "low", "normal", "high"]
    fps = re.search(r'<input[^>]*id="configMaxAnalysisFps"[^>]*>', html).group(0)
    assert 'min="0.5"' in fps and 'max="30"' in fps
    assert 'data-banner="live"' in html and 'data-fps="25"' in html
    scripts = re.findall(r'<script src="/static/js/([^"?]+)', html)
    assert scripts.index("view_zoom.js") < scripts.index("analytics.js")
    assert scripts.index("live_overlay.js") < scripts.index("analytics.js")
    assert scripts.index("loss.js") < scripts.index("live_behaviour.js")
    assert "/static/css/live_view.css" in html


def test_studio_zooms_picture_overlay_and_drawing_canvas_together():
    html = (STATIC / "studio.html").read_text()
    stage = re.search(r'<div class="studio-stage" id="viewportStage">(.*?)</div>', html, re.S).group(1)
    for i in ("streamImg", "streamVideo", "interactiveCanvas"):
        assert f'id="{i}"' in stage, i
    assert {"studioAiStatus", "studioViewTools", "studioOverlayToggle"} <= _ids(html)
    scripts = re.findall(r'<script src="/static/js/([^"?]+)', html)
    assert scripts.index("view_zoom.js") < scripts.index("studio.js")
    assert scripts.index("live_overlay.js") < scripts.index("studio.js")
    assert "/static/css/live_view.css" in html


def test_server_overlay_is_requested_only_when_the_page_does_not_draw_it():
    a = (STATIC / "js" / "analytics.js").read_text()
    s = (STATIC / "js" / "studio.js").read_text()
    # No URL is built with a fixed overlay=1 any more (comments may still mention it).
    templates = re.findall(r"`[^`]*`", a) + re.findall(r"`[^`]*`", s) + re.findall(r"'[^'\n]*'", a + s)
    assert not [t for t in templates if "overlay=1" in t]
    assert "overlay=${serverOverlayParam(true)}" in a and "overlay=${serverOverlayParam(false)}" in a
    assert "overlay=${studioServerOverlay()}" in s
    # The camera editor saves the analysis speed with the features.
    assert "analysis_priority: analysisPriority" in a and "max_analysis_fps: maxAnalysisFps" in a


def test_every_class_the_live_view_uses_is_defined():
    css = "".join(p.read_text() for p in (STATIC / "css").glob("*.css"))
    defined = set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", css))
    used: set[str] = set()
    for name in ("live_overlay.js", "view_zoom.js", "live_behaviour.js"):
        src = (STATIC / "js" / name).read_text()
        for m in re.finditer(r'class(?:Name)?\s*=\s*["\'`]([^"\'`]*)["\'`]', src):
            used |= {t for t in m.group(1).split() if "$" not in t and "}" not in t}
        for m in re.finditer(r"classList\.(?:toggle|add|remove)\(\s*'([\w-]+)'", src):
            used.add(m.group(1))
        for m in re.finditer(r'([\w-]+)-\$\{level\}', src):
            used |= {f"{m.group(1)}-watch", f"{m.group(1)}-alert"}
    for c in ("cam-stage", "cam-view-tools", "lo-status", "studio-stage", "studio-view-tools", "studio-ai-status",
              "lb-strip", "lb-list", "lb-note", "lo-toggle", "lo-link", "cam-ai-toggle", "theft-banner-live"):
        used.add(c)
    missing = sorted(c for c in used if c not in defined)
    assert not missing, missing
