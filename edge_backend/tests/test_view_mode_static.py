"""Simple / All settings (static/js/view_mode.js, data-advanced in index.html and the modules).

Simple is the default for everyone; owners and admins switch to All settings in the header.
Deep links (help guides, checklist steps, Features "Full settings") reveal what they point to.
The browser behaviour is checked in headless Chrome; these tests pin the page contract.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = STATIC / "js"


def _open_tag(elem_id: str) -> str:
    m = re.search(r'<[a-z]+\b[^>]*\bid="%s"[^>]*>' % re.escape(elem_id), HTML)
    assert m, elem_id
    return m.group(0)


def test_mode_is_set_before_first_paint_and_switch_is_in_the_header():
    assert '<script src="/static/js/view_mode.js?v=' in HTML
    head = HTML.split("</head>")[0]
    assert "view_mode.js" in head and 'view_mode.js?v=2.6.0" defer' not in head
    toggle = _open_tag("viewModeToggle")
    assert "view-mode-toggle" in toggle and "aria-pressed" in toggle
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    assert 'html[data-mode="simple"] [data-advanced]:not([data-revealed])' in css
    assert 'html[data-mode="all"] [data-simple-only]' in css


def test_view_mode_api_and_reveal_hooks():
    src = (JS / "view_mode.js").read_text(encoding="utf-8")
    for name in ("mode", "set", "reveal", "isHidden", "canSwitch"):
        assert re.search(r"\b%s[,:]" % name, src), name
    assert "edge_view_mode:" in src and "'simple'" in src           # per account, Simple by default
    assert "Element.prototype.scrollIntoView" in src                # safety net for every deep link
    assert "edge:tab" in src                                        # revealed parts hide again on another tab
    help_js = (JS / "help.js").read_text(encoding="utf-8")
    assert help_js.count("reveal(") >= 4                            # settings card, view element, camera part
    analytics = (JS / "analytics.js").read_text(encoding="utf-8")
    assert "window.edgeMode.reveal(node)" in analytics and "window.edgeMode.reveal(card)" in analytics


def test_settings_simple_view_shows_the_everyday_cards_only():
    simple = {"settings-fleet", "settings-support", "settings-phone-alerts", "settings-account",
              "settings-accounts", "settings-store", "settings-store-hours", "settings-theft-alerts",
              "settings-appearance", "settings-health"}
    advanced = {"settings-device", "settings-evidence", "settings-alerts", "settings-restricted-areas",
                "settings-analysis", "settings-remote", "settings-remote-video", "settings-pairing",
                "settings-push", "settings-devices", "settings-recorder-streams", "settings-backups",
                "settings-pos"}   # sales data: shown again by analytics.js while the tills are not connected
    for card in simple:
        assert "data-advanced" not in _open_tag(card), card
    for card in advanced:
        assert "data-advanced" in _open_tag(card), card


def test_automation_mounts_and_assets_are_in_place():
    for mount in ("autoSuggestions", "settings-store-hours", "autoTuneStatus"):
        assert "data-advanced" not in _open_tag(mount), mount
    assert '<link rel="stylesheet" href="/static/css/automation.css?v=' in HTML
    tag = lambda name: HTML.index('<script src="/static/js/%s?v=' % name)
    assert tag("automation.js") > tag("features.js") > tag("poll.js")


def test_renames_and_removed_cards():
    assert '<span class="tab-label" data-short="Security">Security</span>' in HTML
    assert "Shopper profile" not in HTML and "demographicsChart" not in HTML
    assert ">Area alerts</span>" in HTML and ">Wrong alerts</span>" in HTML and ">Live activity</span>" in HTML
    for f in JS.glob("*.js"):
        assert "Open Loss prevention" not in f.read_text(encoding="utf-8"), f.name
    # The colour theme card stays for phones, where the header switch is hidden.
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    assert "#settings-appearance" in css and "selftest-results" not in css


def test_theft_levels_preset_writes_only_the_thresholds():
    src = (JS / "site_settings.js").read_text(encoding="utf-8")
    assert "Fewer alerts" in src and "Balanced" in src and "More alerts" in src
    block = src[src.index("const PRESETS"):src.index("function tierValues")]
    for tiers in re.findall(r"tiers: \[([^\]]+)\]", block):
        w, a, c = (float(x) for x in tiers.split(","))
        assert 0.05 <= w < a < c <= 1.0                             # the levels must rise, within range
    assert "values[k] = p.tiers ? p.tiers[i] : null" in src          # Balanced = back to the box defaults


def test_restricted_dialog_has_presets_and_fine_tune():
    src = (JS / "restricted.js").read_text(encoding="utf-8")
    assert "When it is guarded" in src and "Outside store hours" in src and "Custom times" in src
    assert "How strict" in src and "Fine-tune triggers" in src
    assert "source: 'store_hours'" in src and "schedule_mode: 'armed_outside'" in src
    assert "Store hours not learnt yet" in src
    for level in ("'standard'", "'high'", "'maximum'"):
        assert level in src
