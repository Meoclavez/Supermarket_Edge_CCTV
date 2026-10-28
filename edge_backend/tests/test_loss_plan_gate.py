"""Loss prevention tab and the iKorex plan: no theft requests when theft alerts are not in the plan.

static/js/loss.js waits for fleet.js's 'edge:fleet' event (the plan status) before its first
load, and every theft request (incidents, statistics, level filter, patterns) checks the plan,
so a box whose plan leaves theft alerts out never gets polled into 403 feature_disabled.
tests/fixtures/loss_plan_gate_harness.js runs the real script under Node (skipped without node).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HARNESS = Path(__file__).resolve().parent / "fixtures" / "loss_plan_gate_harness.js"
node = shutil.which("node")


def test_fleet_announces_the_plan_status():
    src = (STATIC / "js" / "fleet.js").read_text(encoding="utf-8")
    assert "new CustomEvent('edge:fleet'" in src


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_loss_tab_sends_no_theft_requests_outside_the_plan():
    res = subprocess.run([node, str(HARNESS), str(STATIC)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["off_known"] == 0            # plan without theft alerts: nothing, even on refresh
    assert out["off_then_on"] > 0           # plan adds them back: loads at once
    assert out["unknown_before"] == 0       # waits for the plan status
    assert out["unknown_then_off"] == 0
    assert out["unknown_then_on"] > 0
    assert out["no_fleet"] > 0              # pages without fleet.js keep the old behaviour
