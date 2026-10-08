"""Rebuild the dashboard icon sprite (app/static/icons/sprite.svg) from Lucide.

The dashboard ships only the icons it uses, as one SVG sprite of
``<symbol id="i-NAME">`` elements, and app/static/js/icons.js keeps the same
list of names (between ``NAMES:BEGIN`` / ``NAMES:END``) so an unknown name can
fall back to a neutral circle. Both are rewritten here, together.

Usage (offline once the package is downloaded; pin the version and record it
in app/static/icons/SOURCE.txt and THIRD_PARTY_NOTICES.md):

    curl -fsSLO https://registry.npmjs.org/lucide-static/-/lucide-static-1.53.0.tgz
    mkdir lucide && tar xzf lucide-static-1.53.0.tgz -C lucide
    python scripts/build_icon_sprite.py lucide/package            # rebuild the current set
    python scripts/build_icon_sprite.py lucide/package bell-ring   # add icons

Symbols carry fill/stroke/linecap/linejoin but no stroke-width, so the
``.icon`` rule in css/style.css sets the stroke width for every icon.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SPRITE = STATIC / "icons" / "sprite.svg"
ICONS_JS = STATIC / "js" / "icons.js"
SYMBOL_ATTRS = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"'


def current_names() -> list[str]:
    if not SPRITE.exists():
        return []
    return re.findall(r'<symbol id="i-([a-z0-9-]+)"', SPRITE.read_text(encoding="utf-8"))


def symbol(pkg: Path, name: str) -> str:
    src = (pkg / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    body = re.search(r"<svg[^>]*>(.*)</svg>", src, re.S).group(1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
    return f'<symbol id="i-{name}" viewBox="0 0 24 24" {SYMBOL_ATTRS}>{body}</symbol>'


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    pkg = Path(argv[0])
    version = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["version"]
    names = sorted(set(current_names()) | set(argv[1:]))
    missing = [n for n in names if not (pkg / "icons" / f"{n}.svg").exists()]
    if missing:
        print(f"Not in lucide-static {version}: {', '.join(missing)}", file=sys.stderr)
        return 1
    SPRITE.write_text(
        f"<!-- Lucide icons v{version} (lucide-static, ISC; some icons MIT via Feather). "
        "See SOURCE.txt and LICENSE-lucide.txt. -->\n"
        '<svg xmlns="http://www.w3.org/2000/svg">\n'
        + "\n".join(symbol(pkg, n) for n in names) + "\n</svg>\n",
        encoding="utf-8",
    )
    js = ICONS_JS.read_text(encoding="utf-8")
    listed = ",\n".join(f"    '{n}'" for n in names)
    js, count = re.subn(r"(/\* NAMES:BEGIN \*/\n).*?(\n\s*/\* NAMES:END \*/)",
                        lambda m: m.group(1) + listed + m.group(2), js, flags=re.S)
    if count != 1:
        print("icons.js: NAMES:BEGIN / NAMES:END markers not found", file=sys.stderr)
        return 1
    ICONS_JS.write_text(js, encoding="utf-8")
    print(f"{len(names)} icons from lucide-static {version} -> {SPRITE.relative_to(STATIC.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
