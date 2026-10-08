"""Dashboard side of tiered theft alerts (static checks + a Node harness).

* static/js/loss.js: every incident card shows its alert level (or "No level"
  for rows recorded before levels), the risk score and "Why this level" from
  risk_factors; level chips filter with GET /theft/incidents?tier=<level>;
  "Waiting for review" lists Critical first; a KPI per level from
  /theft/statistics. The banner is raised only when the incident's
  alert_channels.banner is true and the alarm sound plays only when .sound is
  true; incidents without a level (null channels) keep the old behaviour.
* live_overlay.js / live_behaviour.js colour and name a fired incident by its level.
* site_settings.js: a "Theft alert levels" card (group theft_alerts) grouped
  by section, with the policy explainer from GET /theft/alert-policy and an
  inline check that the thresholds rise; phone_alerts.js points there.
* tests/fixtures/theft_tier_harness.js runs the real scripts in a fake browser
  under Node (skipped without ``node``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HARNESS = Path(__file__).resolve().parent / "fixtures" / "theft_tier_harness.js"
TOUCHED_JS = ("loss.js", "live_behaviour.js", "live_overlay.js", "site_settings.js", "phone_alerts.js")
TOUCHED_CSS = ("loss.css", "site_settings.css", "phone_alerts.css", "live_view.css")
TIERS = ("review", "watch", "alert", "critical")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node is not installed")


def _js(name: str) -> str:
    return (STATIC / "js" / name).read_text(encoding="utf-8")


def _css_classes() -> set[str]:
    css = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "css").glob("*.css"))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    defined: set[str] = set()
    for selector in re.findall(r"([^{}]+)\{", css):
        if not selector.strip().startswith("@"):
            defined |= set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", selector))
    return defined


# --------------------------------------------------------------------------- #
# Static checks
# --------------------------------------------------------------------------- #

def test_level_chips_filter_with_the_tier_query():
    src = _js("loss.js")
    assert "const TIER_CHIPS = ['all', 'critical', 'alert', 'watch', 'review'];" in src
    assert "`${API}/incidents?limit=200&tier=${encodeURIComponent(tier)}`" in src
    assert 'data-tier-filter="${t}"' in src
    # Old rows are offered as their own level only when there are any ("unclassified" on the server).
    assert "const UNCLASSIFIED = 'unclassified';" in src
    # Waiting for review: Critical first, then newest.
    assert "(tierRank(b) - tierRank(a)) || (timeOf(b) - timeOf(a))" in src


def test_banner_and_sound_are_gated_by_the_incident_channels():
    src = _js("loss.js")
    banner = re.search(r"function bannerAllowed\(inc\) \{(.*?)\n  \}", src, re.S).group(1)
    sound = re.search(r"function soundAllowed\(inc\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "ch.banner === true" in banner and ": true" in banner        # no level (null channels): as before
    assert "ch.sound === true" in sound and ": true" in sound
    assert "channelsOf(inc)" in banner and "channelsOf(inc)" in sound
    assert "inc.alert_channels" in src
    # The banner pick uses the gate; renderBanner itself never sounds the alarm.
    pick = re.search(r"function bannerItem\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "bannerAllowed(i)" in pick
    render = re.search(r"function renderBanner\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "playSound" not in render
    ring = re.search(r"function ringForNew\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "fresh.filter(soundAllowed)" in ring and "playSound(" in ring
    assert src.count("playSound(") == 2        # the definition and ringForNew only


def test_dashboard_has_the_loss_and_settings_hosts():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    loss = html[html.index("<!-- LOSS:BEGIN"):html.index("<!-- LOSS:END")]
    for i in ("lossTierKpis", "lossTierChips", "lossFilterChips", "theftIncidentsList"):
        assert f'id="{i}"' in loss, i
    assert 'id="settings-theft-alerts"' in html
    assert "jumpTo('settings-theft-alerts')" in html


def test_settings_card_for_theft_alert_levels():
    src = _js("site_settings.js")
    card = re.search(r"theftAlerts: \{(.*?)\n    \},", src, re.S).group(1)
    assert "host: 'settings-theft-alerts'" in card and "group: 'theft_alerts'" in card and "sections: true" in card
    assert "title: 'Theft alert levels'" in card
    assert "fetch('/api/v1/theft/alert-policy'" in src
    assert "const TIER_KEYS = ['THEFT_TIER_WATCH_MIN', 'THEFT_TIER_ALERT_MIN', 'THEFT_TIER_CRITICAL_MIN'];" in src
    assert "if (name === 'theftAlerts' && !checkTierOrder(false)) return;" in src
    assert "The levels must rise" in src
    # Reset to default keeps the existing DELETE pattern.
    assert "fetch(`${API}/${encodeURIComponent(values)}`, { method: 'DELETE' })" in src
    assert "data-ss-reset=" in src
    # Explainer parts from the policy.
    for key in ("p.tiers", "p.examples", "p.roles", "combos.rules", "p.place_weight_cap", "ch.banner", "ch.sound", "ch.push"):
        assert key in src, key


def test_phone_roster_level_points_to_theft_alert_levels():
    src = _js("phone_alerts.js")
    assert 'id="paLevel"' in src
    assert "theft alerts without a level" in src
    assert 'data-pa-jump="settings-theft-alerts"' in src


def test_live_overlay_and_behaviour_use_the_tier():
    overlay = _js("live_overlay.js")
    assert "track.behaviour.tier" in overlay and "TIER_WORD" in overlay
    for t in ("critical", "alert", "watch", "review"):
        assert f"--lo-tier-{t}" in overlay
        assert f"--lo-tier-{t}:" in (STATIC / "css" / "live_view.css").read_text(encoding="utf-8")
    behaviour = _js("live_behaviour.js")
    assert "loss.incidentTier(t.incident_id)" in behaviour and "TIERS.includes(t.tier)" in behaviour


@pytest.mark.parametrize("name", [f"js/{n}" for n in TOUCHED_JS] + [f"css/{n}" for n in TOUCHED_CSS])
def test_no_emoji_and_no_blocking_dialogs(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    assert not EMOJI.findall(text), name
    if name.endswith(".js"):
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        assert not re.search(r"(?<![\w.])(prompt|confirm|alert)\s*\(", code), name
        assert not re.search(r"window\.(prompt|confirm|alert)\s*\(", code), name


def test_every_class_the_tier_ui_uses_is_defined():
    defined = _css_classes()
    used: set[str] = set()
    for name in TOUCHED_JS:
        src = _js(name)
        for m in re.finditer(r'class(?:Name)?\s*=\s*["\'`]([^"\'`]*)["\'`]', src):
            value = re.sub(r"[\w-]*\$\{[^{}]*\}", " ", m.group(1))  # whole ${...} expressions (and "tier-${t}")
            value = value.split("${")[0]                            # an expression cut off by a quote
            used |= {t for t in value.split() if re.fullmatch(r"[A-Za-z_][\w-]*", t)}
        for m in re.finditer(r"classList\.(?:toggle|add|remove)\(\s*'([\w-]+)'", src):
            used.add(m.group(1))
    # Classes built from a level in template strings.
    for prefix in ("tier-", "loss-card-", "loss-dot-", "lb-tier-"):
        used |= {prefix + t for t in TIERS}
    used |= {"theft-banner-critical", "theft-banner-alert", "theft-banner-watch", "lb-chip-critical", "lb-chip-tier",
             "loss-tier-none", "loss-mix", "loss-mix-badge", "loss-mix-n", "loss-why", "loss-why-list", "loss-risk",
             "ss-section", "ss-section-title", "ta-explain", "ta-table", "pa-level-note", "kpi", "kpi-label",
             "kpi-value", "kpi-delta", "tier-badge", "loss-tier-kpis", "loss-chips-label"}
    missing = sorted(c for c in used if c not in defined)
    assert not missing, missing


def test_icons_used_by_the_tier_ui_exist():
    names = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', (STATIC / "icons" / "sprite.svg").read_text(encoding="utf-8")))
    src = _js("loss.js")
    tier_icons = re.search(r"const TIER_ICON = \{([^}]*)\}", src).group(1)
    for name in re.findall(r"'([a-z0-9-]+)'", tier_icons):
        assert name in names, name


# --------------------------------------------------------------------------- #
# The real scripts under Node
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def harness() -> dict:
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, str(HARNESS), str(STATIC)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_gates_per_level(harness):
    assert harness["gates"] == [
        {"tier": "review", "banner": False, "sound": False},
        {"tier": "watch", "banner": True, "sound": False},
        {"tier": "alert", "banner": True, "sound": True},
        {"tier": "critical", "banner": True, "sound": True},
        {"tier": None, "banner": True, "sound": True},
    ]


@needs_node
def test_banner_and_sound_follow_the_level(harness):
    assert harness["review_only"] == {"sounds": 0, "banner": "none"}
    w = harness["watch"]
    assert w["sounds"] == 0 and w["banner"] == "flex" and w["classes"] == ["theft-banner-watch"]
    assert w["headline"] == "Watch · Please check: Possible concealment on Aisle 4"
    assert w["meta"] == "Risk 62% · Confidence 50%"
    c = harness["critical"]
    # One alarm per poll for the new incidents that may sound: a new Critical makes it three beeps.
    assert c["new_beeps"] == 3
    assert c["banner_incident"] == "c1" and c["classes"] == ["theft-banner-critical"]
    assert harness["repeat_poll_beeps"] == 0
    u = harness["untiered"]
    assert u == {"sounds": 1, "banner": "flex", "classes": [], "headline": "Please check: Possible concealment on Aisle 4"}


@needs_node
def test_queue_card_and_kpis(harness):
    assert harness["open_order"] == ["c1", "a1", "w1", "r1"]
    assert harness["tier_url"] == "/api/v1/theft/incidents?limit=200&tier=critical"
    assert harness["card"] == {"badge": True, "risk": True, "why": True, "route": True, "old_badge": True, "old_why": False}
    assert harness["kpis"] == {"order": ["critical", "alert", "watch", "review"], "alert_rate": True,
                               "no_outcomes": True, "unclassified": True}
    assert "Critical <span class=\"loss-mix-n\">1</span>" in harness["mix"]
    assert "No level" in harness["mix"] and "Review" not in harness["mix"]
    assert harness["mix_none"] == ""


@needs_node
def test_live_overlay_and_behaviour_tiers(harness):
    o = harness["overlay"]
    assert o["tiers"] == ["critical", None, None]
    assert o["colors"] == ["tierCritical", "tierWatch", "alert", "watch"]
    assert o["chips"] == ["CRITICAL · Hand at pocket", "WATCH LEVEL · Hand at pocket", "ALERT · Hand at pocket"]
    b = harness["behaviour"]
    assert b["own"] == {"tier": "alert", "label": "Alert", "risk": 0.6}
    assert b["from_queue"] == {"tier": "critical", "label": "Critical", "risk": 0.62}
    assert b["watch"] is None and b["unknown"] is None
    assert 'class="tier-badge tier-critical lb-tier"' in b["row"] and "lb-tier-critical" in b["row"]
