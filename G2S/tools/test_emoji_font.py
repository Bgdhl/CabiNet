#!/usr/bin/env python3
"""
Gate for the hub's own emoji font (v0.3.5).

The pages use emoji as icons. A Pi has no emoji font, so on the hub's own
touchscreen every one of them was an empty box (2026-11-16). The hub now
ships G2S/webui/emoji.woff2 — a subset of Noto Color Emoji holding the
emoji this codebase can put on a page — and the pages fall back to it.

A subset goes stale the moment someone adds an emoji it does not carry, and
that failure is silent everywhere except on a device with no emoji font. So
this gate fails until the font is rebuilt:

    python3 G2S/tools/build_emoji_font.py

Stdlib only (the hub has no font tools): it compares the sources against
the list the builder wrote next to the font.

Run from G2S/:
    python3 tools/test_emoji_font.py
Exits 0 if all checks pass.
"""

import os
import sys
from pathlib import Path

G2S = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(G2S))
sys.path.insert(0, str(G2S / "tools"))
import build_emoji_font as bef  # noqa: E402
import g2s_host  # noqa: E402
import web_auth  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail!r}")


def main():
    webui = G2S / "webui"
    font = webui / "emoji.woff2"

    print("— the font is there and is what it says")
    check("emoji.woff2 is shipped", font.is_file())
    blob = font.read_bytes() if font.is_file() else b""
    check("...and is a WOFF2 file", blob[:4] == b"wOF2", blob[:4])
    check("...small enough to be a subset, big enough to hold glyphs",
          20_000 < len(blob) < 1_500_000, len(blob))
    lic = webui / "emoji-LICENSE.txt"
    check("its licence ships beside it (SIL OFL 1.1)", lic.is_file()
          and "SIL OPEN FONT LICENSE Version 1.1" in lic.read_text("utf-8"))

    print("— every emoji the code can put on a page is in it")
    have, text = bef.load_list()
    used = bef.scan()
    check("the sources were found", len(bef.source_files()) > 20
          and len(used) > 30, (len(bef.source_files()), len(used)))
    missing = sorted(cp for cp in used if cp not in have and cp not in text)
    check("no emoji outside the font — otherwise rebuild it: "
          "python3 G2S/tools/build_emoji_font.py",
          not missing,
          ["U+%04X %s in %s" % (cp, chr(cp), used[cp]) for cp in missing])
    check("the list and the font agree on size (a font rebuilt without its "
          "list, or the other way round)",
          0 < len(have) and len(blob) / max(len(have), 1) > 500,
          (len(have), len(blob)))
    icons = "🎰🪙💳🎟✨🛠👤"            # the tab bar and the sign-in chip
    check("the tab-bar icons are in the font",
          all(ord(c) in have for c in icons),
          [c for c in icons if ord(c) not in have])
    check("nothing is listed as both drawn and left to text", not have & text)

    print("— the scan looks where emoji live, and only there")
    check("a pictograph counts", bef.is_emoji(ord("🎰")))
    check("a dingbat counts", bef.is_emoji(ord("✅")))
    check("an arrow, a dash and a letter do not",
          not any(bef.is_emoji(ord(c)) for c in "→—é⇧"))
    skipped = [f for f in bef.source_files()
               if "/tests/" in f or "/tools/" in f]
    check("tests and dev tools are not scanned (their ticks go to a "
          "terminal)", not skipped, skipped[:3])

    print("— the pages use it: first in line, only where it is needed")
    import re
    for name in ("home", "login", "index", "console", "board"):
        html = (webui / f"{name}.html").read_text("utf-8")
        check(f"{name}.html declares the font from the hub's own file",
              '@font-face{font-family:"CabiNet Emoji"' in html
              and 'url("/webui/emoji.woff2")' in html)
        own_at = html.find(":root.emoji-own{")
        own_block = html[own_at:html.find("}", own_at)] if own_at >= 0 else ""
        own = dict(re.findall(r'(--(?:display|sans|mono))\s*:([^;]*);',
                              own_block))
        rest = html[:own_at] + html[own_at + len(own_block):]
        base = dict(re.findall(
            r'(--(?:display|sans|mono))\s*:([^;}]*)[;}]', rest))
        check(f"{name}.html: the plain font stacks do NOT name the hub's "
              "font (a device with its own emoji keeps them)",
              base and all("CabiNet" not in v for v in base.values()), base)
        # First, not last: on the hub's kiosk a web font listed AFTER system
        # fonts was not reliably reached — the tab bar stayed empty boxes.
        check(f"{name}.html: html.emoji-own puts it FIRST in every stack, "
              "and changes nothing else about them",
              own and set(own) == set(base) and all(
                  own[k].strip() == '"CabiNet Emoji",' + base[k].strip()
                  for k in base), (own, base))
        check(f"{name}.html: the class is set only on a device with no "
              "emoji of its own, and only once the font has loaded",
              "getImageData" in html and "if(own()) return;" in html
              and """document.fonts.load('16px "CabiNet Emoji"'""" in html
              and 'classList.add("emoji-own")' in html)
        check(f"{name}.html: the body text uses a stack the class can "
              "change", "var(--sans)" in html)

    print("— the hub can hand it to any browser")
    check("served as a font", g2s_host.WEBUI_CONTENT_TYPES.get(".woff2")
          == "font/woff2")
    check("...without a sign-in (the sign-in page needs it too)",
          web_auth.get_level("/webui/emoji.woff2")[0] == web_auth.PUBLIC)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
