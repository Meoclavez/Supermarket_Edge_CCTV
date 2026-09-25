"""The dashboard sends no API request before its stored token has been checked.

A stale token used to be sent by ~10 pollers at once on page load; the server
counted each as a failed attempt and locked the operator out. auth.js now
holds every /api/ request until ``GET /api/v1/auth/status`` has answered, and
drops an invalid token without sending it anywhere else.

Runs static/js/auth.js in a small fake browser under Node (no server, no
real browser); skipped when ``node`` is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HARNESS = Path(__file__).resolve().parent / "fixtures" / "auth_gate_harness.js"
PRE_AUTH = {"/api/v1/auth/status", "/api/v1/auth/refresh"}

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


def _run(scenario: str) -> dict:
    out = subprocess.run([node, str(HARNESS), str(STATIC / "js" / "auth.js"), scenario],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_stale_token_is_checked_once_then_dropped_and_never_sent_elsewhere():
    r = _run("stale")
    assert set(r["sent_before_resolve"]) <= PRE_AUTH
    # The stale token went to the status check only; anything sent after the
    # gate opened (setup endpoints) carries no credential at all.
    assert [n["path"] for n in r["network"] if n["auth"]] == ["/api/v1/auth/status"]
    assert r["state"] == "invalid" and r["event_state"] == "invalid"
    assert r["statuses"] == [401] * len(r["statuses"])                        # answered locally
    assert r["token_left"] is None and r["cookie_cleared"] and r["gate_shown"]
    assert r["is_authenticated"] is False and "token=" not in r["auth_url"]


@needs_node
def test_valid_token_releases_the_held_requests_with_the_token():
    r = _run("valid")
    assert set(r["sent_before_resolve"]) <= PRE_AUTH
    paths = [n["path"] for n in r["network"]]
    assert paths[0] == "/api/v1/auth/status" and len(paths) == 1 + len(r["statuses"])
    assert all(n["auth"] == "Bearer stored.jwt.token" for n in r["network"][1:])
    assert r["state"] == "valid" and r["statuses"] == [200] * len(r["statuses"])
    assert r["is_authenticated"] is True
    # The session cookie carries the token, and the server accepts it for
    # <img>/<a> URLs: the token stays out of the URL (history, proxy logs).
    assert r["cookie_jar"].get("edge_cctv_token") == "stored.jwt.token"
    assert r["auth_url"] == "/stream?camera_id=c1"


@needs_node
def test_token_goes_in_the_url_only_when_the_browser_keeps_no_cookie():
    r = _run("valid_nocookie")
    assert r["state"] == "valid" and r["cookie_jar"] == {}
    assert r["auth_url"] == "/stream?camera_id=c1&token=stored.jwt.token"


@needs_node
def test_unreachable_server_still_releases_requests_after_the_check_fails():
    r = _run("offline")
    assert r["network"][0]["path"] == "/api/v1/auth/status"
    first_other = next(i for i, n in enumerate(r["network"]) if n["path"] not in PRE_AUTH)
    assert all(n["path"] in PRE_AUTH for n in r["network"][:first_other])
    assert r["state"] == "error"
    assert "token=stored.jwt.token" in r["auth_url"]      # consistent with fetch in this state


POLLING_MODULES = ("analytics.js", "today.js", "loss.js", "insights.js", "floorplan.js", "devices.js", "entrances.js", "product_reach.js",
                   "pairing.js", "remote_access.js", "studio.js",
                   "camera_roles.js", "heatmap_history.js")


@pytest.mark.parametrize("name", POLLING_MODULES)
def test_polling_modules_start_after_auth_is_ready(name):
    src = (STATIC / "js" / name).read_text(encoding="utf-8")
    assert "edgeAuth.onReady(" in src, f"{name} must start via window.edgeAuth.onReady"


def test_auth_js_is_loaded_before_every_module_and_cache_busted_together():
    for page in ("index.html", "studio.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        scripts = re.findall(r'<script src="/static/js/([a-z_]+)\.js\?v=([0-9.]+)"', html)
        assert scripts and scripts[0][0] == "auth", page
        assert len({v for _n, v in scripts}) == 1, f"{page}: mixed script versions {scripts}"


@needs_node
def test_tiles_ask_for_a_picture_width_matching_their_rendered_size():
    """analytics.js previewWidth(): rendered width (letterbox-aware) x devicePixelRatio, bucketed."""
    src = (STATIC / "js" / "analytics.js").read_text(encoding="utf-8")
    start = src.index("const PREVIEW_WIDTHS")
    end = src.index("\n}\n", src.index("function previewWidth")) + 3
    script = src[start:end] + r"""
const img = (w, h, nw, nh) => ({ getBoundingClientRect: () => ({ width: w, height: h }), naturalWidth: nw || 0, naturalHeight: nh || 0 });
const out = [];
for (const [dpr, args] of [[1, [400, 225]], [2, [400, 225]], [1, [1200, 675]], [2, [1200, 675]],
                           [1, [400, 225, 3072, 2048]], [1, [0, 0]], [1.5, [300, 169, 704, 576]]]) {
  globalThis.window = { devicePixelRatio: dpr };
  out.push(previewWidth(img(...args)));
}
process.stdout.write(JSON.stringify(out));
"""
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    # 400 css px -> 480; x2 -> 960; 1200 -> 1280; x2 -> 1920 (cap); a 3:2 camera
    # letterboxed in 400x225 shows 338 px wide -> 480; unknown size -> 640;
    # 704x576 in a 300x169 box at 1.5x shows 207 px -> 310 -> 320.
    assert json.loads(res.stdout) == [480, 960, 1280, 1920, 480, 640, 320]
    assert "max_width=${previewWidth(t.img)}" in src and "streamUrl(id, previewWidth(img))" in src
