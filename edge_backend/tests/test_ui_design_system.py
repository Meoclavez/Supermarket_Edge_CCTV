"""Dashboard design system: icons, fonts, component classes (static checks).

The dashboard uses one vendored Lucide sprite (static/icons/sprite.svg) through
js/icons.js (window.EdgeIcon), the browser's system fonts, and the component
classes defined in css/style.css. These tests keep it that way:

* no emoji or pictograph anywhere in static/ (pages, styles, scripts);
* every icon referenced (``#i-NAME`` in markup, ``EdgeIcon.svg('NAME'`` and the
  modules' ``ico('NAME'`` / ``uiIcon('NAME'`` helpers) exists in the sprite, and
  js/icons.js lists exactly the sprite's names;
* no web font or CDN URL in static/ (the dashboard must work offline on the store LAN);
* every class used in index.html / studio.html is defined in css/*.css;
* icons.js loads right after auth.js, before every other dashboard script;
* the sprite is served from /static with an SVG content type.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SPRITE = STATIC / "icons" / "sprite.svg"
ICONS_JS = STATIC / "js" / "icons.js"
PAGES = ("index.html", "studio.html")

# Emoji and pictographs: Misc Symbols and Pictographs .. Symbols and Pictographs
# Extended-A, Misc Symbols + Dingbats, Misc Symbols and Arrows, and the emoji
# variation selector.
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️]")
# The pages and the design-system files also avoid typographic arrows,
# geometric-shape glyphs (triangles, bullets used as icons) and the full-width
# plus: those are icons too, and render differently on every platform.
GLYPHS = re.compile("[←-⇿■-◿＋]")
DESIGN_SYSTEM_FILES = (
    [STATIC / p for p in PAGES]
    + sorted((STATIC / "css").glob("*.css"))
    + [ICONS_JS]
)
CDN = re.compile(
    r"fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|unpkg\.com"
    r"|use\.typekit\.net|use\.fontawesome\.com|kit\.fontawesome\.com|fonts\.bunny\.net",
    re.I,
)
COMPONENT_CLASSES = (
    "btn btn-primary btn-secondary btn-ghost btn-danger btn-success btn-sm btn-lg btn-icon btn-group "
    "icon icon-sm icon-md icon-lg badge badge-neutral badge-info badge-success badge-warning badge-danger "
    "badge-dot status-dot is-online is-offline is-warning card card-header card-title card-actions card-body "
    "toolbar field field-label field-hint input select table table-compact num empty-state kpi kpi-label "
    "kpi-value kpi-delta tier-badge tier-review tier-watch tier-alert tier-critical "
    "btn-xs btn-warning badge-green"
).split()


def _text_files():
    for path in sorted(STATIC.rglob("*")):
        if path.is_file() and path.suffix in (".html", ".css", ".js", ".svg", ".webmanifest", ".json"):
            yield path


def _sprite_names() -> list[str]:
    return re.findall(r'<symbol id="i-([a-z0-9-]+)"', SPRITE.read_text(encoding="utf-8"))


def _css_classes() -> set[str]:
    css = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "css").glob("*.css"))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    defined: set[str] = set()
    for selector in re.findall(r"([^{}]+)\{", css):
        if selector.strip().startswith("@"):
            continue
        defined |= set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", selector))
    return defined


def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script\b[^>]*\bsrc="([^"?]+)', html)


# --------------------------------------------------------------------------- #
# No emoji / pictographs
# --------------------------------------------------------------------------- #

def test_no_emoji_or_pictographs_in_static():
    found = []
    for path in _text_files():
        if "vendor" in path.relative_to(STATIC).parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for ch in EMOJI.findall(line):
                found.append(f"{path.relative_to(STATIC)}:{n}: U+{ord(ch):04X}")
    assert not found, "emoji/pictographs left (use icons from the sprite):\n" + "\n".join(found[:80])


@pytest.mark.parametrize("path", DESIGN_SYSTEM_FILES, ids=lambda p: str(p.relative_to(STATIC)))
def test_design_system_files_use_no_arrow_or_shape_glyphs(path):
    bad = [f"{n}: U+{ord(ch):04X}" for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
           for ch in GLYPHS.findall(line)]
    assert not bad, f"{path.name}: {bad}"


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #

def test_sprite_is_valid_svg_with_24px_stroke_symbols():
    names = _sprite_names()
    assert len(names) == len(set(names)) and len(names) >= 60
    root = ET.fromstring(SPRITE.read_text(encoding="utf-8"))
    ns = "{http://www.w3.org/2000/svg}"
    symbols = root.findall(f"{ns}symbol")
    assert len(symbols) == len(names)
    for sym in symbols:
        assert sym.get("viewBox") == "0 0 24 24", sym.get("id")
        assert sym.get("fill") == "none" and sym.get("stroke") == "currentColor", sym.get("id")
        # Stroke width comes from the .icon rule, so one CSS value styles every icon.
        assert sym.get("stroke-width") is None, sym.get("id")
        assert len(sym), f"{sym.get('id')} is empty"
    # Licence travels with the vendored icons.
    assert "ISC License" in (STATIC / "icons" / "LICENSE-lucide.txt").read_text(encoding="utf-8")
    assert "lucide-static 1.53.0" in (STATIC / "icons" / "SOURCE.txt").read_text(encoding="utf-8")


def test_icons_js_lists_exactly_the_sprite_icons():
    js = ICONS_JS.read_text(encoding="utf-8")
    block = re.search(r"/\* NAMES:BEGIN \*/(.*?)/\* NAMES:END \*/", js, re.S).group(1)
    assert re.findall(r"'([a-z0-9-]+)'", block) == _sprite_names()
    assert "window.EdgeIcon" in js and "'/static/icons/sprite.svg'" in js
    assert "FALLBACK = 'circle'" in js and "circle" in _sprite_names()


def test_every_referenced_icon_exists_in_the_sprite():
    names = set(_sprite_names())
    refs: dict[str, set[str]] = {}
    for path in _text_files():
        if path == SPRITE or "vendor" in path.relative_to(STATIC).parts:
            continue
        text = path.read_text(encoding="utf-8")
        found = set(re.findall(r"#i-([a-z0-9-]+)", text))
        found |= set(re.findall(r"EdgeIcon\.(?:svg|el)\(\s*['\"]([a-z0-9-]+)['\"]", text))
        found |= set(re.findall(r"\b(?:ico|uiIcon|icon|iconSvg)\(\s*['\"]([a-z0-9-]+)['\"]", text))
        for name in found:
            refs.setdefault(name, set()).add(str(path.relative_to(STATIC)))
    missing = {n: sorted(files) for n, files in refs.items() if n not in names}
    assert not missing, f"icons not in sprite.svg (add them with scripts/build_icon_sprite.py): {missing}"
    assert refs, "no icon references found"


@pytest.mark.parametrize("page", PAGES)
def test_page_icons_are_sprite_references_with_accessible_names(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    svgs = re.findall(r"<svg\b[^>]*>.*?</svg>", html, re.S)
    assert len(svgs) >= 5, page
    for svg in svgs:
        assert 'class="icon' in svg, svg
        assert 'aria-hidden="true"' in svg or ('role="img"' in svg and "aria-label=" in svg), svg
        assert re.search(r'<use href="/static/icons/sprite\.svg#i-[a-z0-9-]+"', svg), svg
    # Icon-only buttons and links must be named for screen readers.
    for m in re.finditer(r"<(button|a)\b([^>]*)>(.*?)</\1>", html, re.S):
        attrs, body = m.group(2), m.group(3)
        text = re.sub(r"<svg\b.*?</svg>", "", body, flags=re.S)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if "<svg" in body and not text:
            assert "aria-label=" in attrs, m.group(0)[:160]


# --------------------------------------------------------------------------- #
# Fonts and third-party URLs
# --------------------------------------------------------------------------- #

def test_no_web_fonts_or_cdn_urls_in_static():
    hits = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8")
        for m in CDN.finditer(text):
            hits.append(f"{path.relative_to(STATIC)}: {m.group(0)}")
        if path.suffix == ".css":
            if re.search(r"@import\s+url\(\s*['\"]?https?:", text) or re.search(r"url\(\s*['\"]?https?:", text):
                hits.append(f"{path.relative_to(STATIC)}: remote url()")
            if re.search(r"@font-face", text):
                hits.append(f"{path.relative_to(STATIC)}: @font-face")
    assert not hits, hits
    css = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "css").glob("*.css"))
    assert "Plus Jakarta Sans" not in css and "JetBrains Mono" not in css
    assert "--font-sans: ui-sans-serif, system-ui" in css
    assert "--font-mono: ui-monospace, SFMono-Regular" in css


# --------------------------------------------------------------------------- #
# Classes and components
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("page", PAGES)
def test_every_class_used_in_the_page_is_defined(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    used: set[str] = set()
    for value in re.findall(r'\bclass="([^"]*)"', html):
        used |= set(value.split())
    missing = sorted(used - _css_classes())
    assert not missing, f"{page}: classes with no CSS rule: {missing}"


def test_component_classes_are_defined_in_style_css():
    style = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", re.sub(r"/\*.*?\*/", "", style, flags=re.S)))
    missing = [c for c in COMPONENT_CLASSES if c not in defined]
    assert not missing, missing


def test_both_themes_define_the_base_tokens():
    style = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    dark = re.search(r":root \{(.*?)\n\}", style, re.S).group(1)
    light = re.search(r':root\[data-theme="light"\] \{(.*?)\n\}', style, re.S).group(1)
    system = re.search(r'@media \(prefers-color-scheme: light\) \{\s*:root:not\(\[data-theme="dark"\]\) \{(.*?)\n  \}', style, re.S).group(1)
    tokens = lambda block: dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))  # noqa: E731
    for name in ("--bg", "--surface", "--border", "--text", "--text-secondary", "--accent", "--accent-solid",
                 "--success", "--warning", "--danger", "--info", "--focus-ring"):
        assert name in tokens(dark) and name in tokens(light), name
    # The explicit light theme and the system-light block must stay identical.
    assert tokens(light) == tokens(system)
    # Flat surfaces: no glassmorphism or glow left in the design system.
    css = "".join(p.read_text(encoding="utf-8") for p in (STATIC / "css").glob("*.css"))
    assert "backdrop-filter" not in css and "text-shadow" not in css and "drop-shadow" not in css


# --------------------------------------------------------------------------- #
# Loading order and delivery
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("page", PAGES)
def test_icons_js_loads_before_every_other_dashboard_script(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    srcs = _script_srcs(html)
    assert srcs[0] == "/static/js/auth.js", srcs
    assert srcs[1] == "/static/js/icons.js", srcs
    assert re.search(r'<script src="/static/js/icons\.js\?v=[0-9.]+" defer></script>', html)
    assert "fonts.googleapis.com" not in html


def test_sprite_and_icons_js_are_served_from_static():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.get("/static/icons/sprite.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert '<symbol id="i-circle"' in r.text
    js = client.get("/static/js/icons.js")
    assert js.status_code == 200 and "window.EdgeIcon" in js.text
    page = client.get("/dashboard")
    assert re.search(r'/static/js/icons\.js\?v=[0-9a-f]{12}"', page.text)
