#!/usr/bin/env python3
"""
Build G2S/webui/emoji.woff2 — the emoji the hub's pages use, as a web font.

WHY THE HUB SHIPS A FONT
------------------------
The pages use emoji as icons (the tab bar, buttons, log lines). A phone or a
desktop draws them from its own emoji font. A Pi does not have one: on the
hub's own touchscreen every icon was an empty box (2026-11-16). Installing a
system font is not something an update can do — the updater may restart the
hub service and nothing else — and the slot network has no internet. So the
hub carries the font itself and the pages load it as a LAST-RESORT fallback
("CabiNet Emoji", after the device's own emoji fonts). It arrives with a
normal update and needs nothing installed.

It is a SUBSET of Noto Color Emoji (SIL Open Font License 1.1 — see
G2S/webui/emoji-LICENSE.txt): only the emoji this codebase can put on a page,
a few hundred kilobytes instead of eleven megabytes.

WHEN TO RUN IT
--------------
When the gate test_emoji_font.py fails with "not in the emoji font": someone
added an emoji the font does not carry yet. On a machine with fontTools,
brotli and Noto Color Emoji installed (any desktop Linux):

    python3 G2S/tools/build_emoji_font.py            # rebuild font + list
    python3 G2S/tools/build_emoji_font.py --check    # report only

and commit G2S/webui/emoji.woff2 and G2S/webui/emoji.json together.

The hub never runs this. scan() and the list file are stdlib-only, so the
gate works on a Pi with no font tools.
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEBUI = os.path.join(ROOT, "G2S", "webui")
OUT_FONT = os.path.join(WEBUI, "emoji.woff2")
OUT_LIST = os.path.join(WEBUI, "emoji.json")
DEFAULT_SOURCE = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"

#: Everything whose text can end up on a page: the pages themselves, and the
#: code that writes log lines, event labels and updater output the pages show.
SOURCE_GLOBS = (
    "G2S/*.py", "G2S/webui/*.html", "G2S/webui/*.js",
    "SAS/**/*.py", "Companion/**/*.py", "deploy/*.py",
)
#: ...but not tests and dev tools: their ticks and crosses go to a terminal.
SKIP_DIRS = ("tests", "tools", "__pycache__")

#: Where an emoji lives. A character in one of these blocks is either drawn
#: by the emoji font or it is an empty box on a device with no emoji font —
#: plain text fonts cover them patchily or not at all. Arrows, punctuation
#: and box drawing are left alone: text fonts have those.
EMOJI_BLOCKS = (
    (0x2300, 0x23FF),      # Miscellaneous Technical      ⌚ ⏱ ⏳
    (0x2600, 0x27BF),      # Misc Symbols + Dingbats      ⚙ ✨ ✅ ➕
    (0x2B00, 0x2BFF),      # Misc Symbols and Arrows      ⬆ ⭐
    (0x1F000, 0x1FFFF),    # every pictograph plane-1 block
)
#: Joiners that turn several emoji into one glyph. Kept when the font has
#: them, so "gear + variation selector" and the like still resolve.
JOINERS = (0x200D, 0x20E3, 0xFE0F)


def is_emoji(cp):
    return any(lo <= cp <= hi for lo, hi in EMOJI_BLOCKS)


def source_files():
    out = []
    for pat in SOURCE_GLOBS:
        for path in sorted(glob.glob(os.path.join(ROOT, pat), recursive=True)):
            parts = os.path.relpath(path, ROOT).split(os.sep)
            if not any(d in SKIP_DIRS for d in parts[:-1]):
                out.append(path)
    return out


def scan():
    """-> {codepoint: first file it was seen in} for every emoji-block
    character in the sources."""
    seen = {}
    for path in source_files():
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, ROOT)
        for ch in set(text):
            cp = ord(ch)
            if is_emoji(cp) and cp not in seen:
                seen[cp] = rel
    return seen


def load_list():
    """-> (have, text): codepoints the shipped font draws, and codepoints
    that were checked and are NOT emoji in Noto (a text font draws them)."""
    with open(OUT_LIST, encoding="utf-8") as f:
        d = json.load(f)
    return ({int(x, 16) for x in d.get("have", [])},
            {int(x, 16) for x in d.get("text", [])})


def build(source, check_only=False):
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        sys.exit("fontTools is needed to build the font: pip install "
                 "fonttools brotli")
    if not os.path.isfile(source):
        sys.exit("Noto Color Emoji not found at %s — install it (Debian/"
                 "Ubuntu: fonts-noto-color-emoji) or pass --font PATH." % source)
    font = TTFont(source)
    cmap = font.getBestCmap()
    name = font["name"]
    version = (name.getDebugName(5) or "").split(";")[0]
    wanted = scan()
    have = sorted(cp for cp in wanted if cp in cmap)
    text = sorted(cp for cp in wanted if cp not in cmap)
    print("sources carry %d emoji-block characters: %d in the font, %d left "
          "to text fonts" % (len(wanted), len(have), len(text)))
    for cp in text:
        print("   text font: U+%04X %s  (%s)" % (cp, chr(cp), wanted[cp]))
    if check_only:
        try:
            old_have, old_text = load_list()
        except (OSError, ValueError):
            old_have, old_text = set(), set()
        new = [cp for cp in have if cp not in old_have]
        gone = [cp for cp in old_have if cp not in have]
        print("vs the shipped list: %d to add%s, %d no longer used"
              % (len(new), (" (" + " ".join(chr(c) for c in new) + ")")
                 if new else "", len(gone)))
        return 1 if new or set(text) - old_text else 0

    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]        # keep the sequences (GSUB ligatures)
    opts.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]   # incl. copyright + licence
    opts.notdef_outline = True
    opts.glyph_names = False
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=have + [j for j in JOINERS if j in cmap])
    sub.subset(font)
    font.flavor = "woff2"      # Options.flavor alone does not reach save()
    font.save(OUT_FONT)
    with open(OUT_FONT, "rb") as f:
        if f.read(4) != b"wOF2":
            sys.exit("the font did not come out as WOFF2 — is brotli "
                     "installed? (pip install brotli)")
    with open(OUT_LIST, "w", encoding="utf-8") as f:
        json.dump({
            "_": "Written by G2S/tools/build_emoji_font.py — do not edit. "
                 "'have' = drawn by emoji.woff2; 'text' = checked, not an "
                 "emoji in the source font, left to the page's text fonts.",
            "source": "Noto Color Emoji, %s" % version,
            "have": ["%04X" % cp for cp in have],
            "text": ["%04X" % cp for cp in text],
        }, f, indent=1)
        f.write("\n")
    print("wrote %s (%d KB) and %s"
          % (os.path.relpath(OUT_FONT, ROOT), os.path.getsize(OUT_FONT) // 1024,
             os.path.relpath(OUT_LIST, ROOT)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--font", default=os.environ.get("NOTO_COLOR_EMOJI",
                                                     DEFAULT_SOURCE),
                    help="path to NotoColorEmoji.ttf")
    ap.add_argument("--check", action="store_true",
                    help="report what a rebuild would change; write nothing")
    a = ap.parse_args(argv)
    return build(a.font, check_only=a.check)


if __name__ == "__main__":
    sys.exit(main())
