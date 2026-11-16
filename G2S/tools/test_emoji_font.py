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

    print("— the pages use it, last in line")
    for name in ("home", "login", "index", "console", "board"):
        html = (webui / f"{name}.html").read_text("utf-8")
        check(f"{name}.html declares the font from the hub's own file",
              '@font-face{font-family:"CabiNet Emoji"' in html
              and 'url("/webui/emoji.woff2")' in html)
        stacks = [l for l in html.splitlines()
                  if l.strip().startswith(("--sans", "--display", "--mono"))]
        if name == "console":
            stacks = [l for l in html.splitlines() if "sans-serif" in l]
        check(f"{name}.html: every font stack ends with it, after the "
              "device's own emoji fonts",
              stacks and all(
                  l.rstrip().endswith('"Noto Color Emoji","CabiNet Emoji";')
                  for l in stacks), stacks)

    print("— the hub can hand it to any browser")
    check("served as a font", g2s_host.WEBUI_CONTENT_TYPES.get(".woff2")
          == "font/woff2")
    check("...without a sign-in (the sign-in page needs it too)",
          web_auth.get_level("/webui/emoji.woff2")[0] == web_auth.PUBLIC)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
