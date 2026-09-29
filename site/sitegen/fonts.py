"""Web fonts: subset on every build to the characters the site uses.

- Archivo keeps its variable axes, limited to what the design uses: weight 370 to 650
  and width 72 to 100 percent (72 is the condensed label cut).
- Fragment Mono is one static weight.
- The name table is kept whole, so whatever copyright and license records the source
  fonts carry travel with every subset (both have the copyright notice, name ID 0, and the
  OFL URL, name ID 14; the Fontsource files have no license text, name ID 13). The full
  OFL text ships next to the served fonts either way.
- Output is byte-for-byte repeatable: timestamps aren't rewritten on save.
- Static TTF cuts go to .cache for the social images, which resvg renders from TTFs.
"""

from __future__ import annotations

import shutil
import string
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

from sitegen import paths

ARCHIVO_SRC = paths.FONT_SRC / "archivo-latin-wdth-normal.woff2"
MONO_SRC = paths.FONT_SRC / "fragment-mono-latin-400-normal.woff2"
LICENSES = {"OFL-Archivo.txt": paths.FONT_SRC / "OFL-Archivo.txt", "OFL-FragmentMono.txt": paths.FONT_SRC / "OFL-FragmentMono.txt"}

WGHT = (370, 650)
WDTH = (72, 100)
FEATURES = ["kern", "liga", "tnum", "pnum", "ccmp", "locl", "mark", "mkmk", "calt", "rvrn"]

# Always keep these, so small copy edits don't silently fall back to a system font.
BASE_CHARS = set(string.printable) - set("\t\n\r\x0b\x0c") | set("’‘“”…°×±µ·•½")


MONO_TAGS = {"code", "pre", "time"}
# Classes whose text is set in Fragment Mono (see site.css). Extra classes only make the
# subset a little bigger; a missing one fails the coverage check.
MONO_CLASSES = {"mono", "r", "ax", "lbl", "none", "pin", "lvl", "fl", "svcs", "maphd"}
MONO_IDS = {"smax"}  # the service map's axis labels, drawn from a sprite


class _MonoScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.stack: list[bool] = []
        self.text: list[str] = []

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        a = dict(attrs)
        mono = tag in MONO_TAGS or bool(set((a.get("class") or "").split()) & MONO_CLASSES) or a.get("id") in MONO_IDS
        self.stack.append(mono)
        self.depth += mono

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass  # self-closing SVG elements hold no text

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if self.stack:
            self.depth -= self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.depth > 0:
            self.text.append(data)


def mono_text(html: str) -> str:
    """The text of an HTML page that renders in the monospace font."""
    scan = _MonoScan()
    scan.feed(html)
    return "".join(scan.text)


@dataclass
class WebFonts:
    text: Path
    mono: Path
    text_cmap: frozenset[int]
    mono_cmap: frozenset[int]


def _subset(font: TTFont, chars: set[str], out: Path) -> None:
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = FEATURES
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True
    sub = subset.Subsetter(options)
    sub.populate(unicodes=sorted(ord(c) for c in chars))
    sub.subset(font)
    font.recalcTimestamp = False
    font.flavor = "woff2"
    out.parent.mkdir(parents=True, exist_ok=True)
    font.save(out)


def source_cmaps() -> tuple[frozenset[int], frozenset[int]]:
    return (
        frozenset(TTFont(ARCHIVO_SRC, recalcTimestamp=False).getBestCmap()),
        frozenset(TTFont(MONO_SRC, recalcTimestamp=False).getBestCmap()),
    )


# Monospace only sets hex, frames, code, and dates, so its subset is what those contexts
# use plus hex digits and code punctuation.
MONO_BASE = set(string.digits + "ABCDEFabcdefx#.:=/-_()[]<>+*%,;'\"` ")


def build(out_dir: Path, used_text: str, mono_text: str = "") -> WebFonts:
    chars = (set(used_text) | BASE_CHARS) - {"\n", "\r", "\t"}
    text_cmap, mono_cmap = source_cmaps()
    text_chars = {c for c in chars if ord(c) in text_cmap}
    mono_chars = {c for c in (set(mono_text) | MONO_BASE) if ord(c) in mono_cmap}

    archivo = instancer.instantiateVariableFont(TTFont(ARCHIVO_SRC, recalcTimestamp=False), {"wght": WGHT, "wdth": WDTH})
    text_out = out_dir / "archivo.woff2"
    _subset(archivo, text_chars, text_out)

    mono_out = out_dir / "fragment-mono.woff2"
    _subset(TTFont(MONO_SRC, recalcTimestamp=False), mono_chars, mono_out)

    for name, src in LICENSES.items():
        shutil.copyfile(src, out_dir / name)
    return WebFonts(text_out, mono_out, frozenset(ord(c) for c in text_chars), frozenset(ord(c) for c in mono_chars))


OG_CUTS = {
    # family name used in the OG template: (wght, wdth)
    "Lasto Label": (650, 72),
    "Lasto Head": (600, 100),
    "Lasto Text": (400, 100),
}


def _rename(font: TTFont, family: str) -> None:
    name = font["name"]
    ps = family.replace(" ", "")
    for nid, value in ((1, family), (2, "Regular"), (4, family), (6, ps), (16, family), (17, "Regular")):
        name.setName(value, nid, 3, 1, 0x409)
    name.removeNames(nameID=25)


def og_fonts() -> list[Path]:
    """Static TTF cuts for resvg. Cached; rebuilt when the source font changes."""
    out = paths.CACHE / "og-fonts"
    out.mkdir(parents=True, exist_ok=True)
    stamp = out / ".source-mtime"
    mtime = f"{ARCHIVO_SRC.stat().st_mtime_ns}:{MONO_SRC.stat().st_mtime_ns}"
    files = [out / f"{fam.replace(' ', '')}.ttf" for fam in OG_CUTS] + [out / "LastoMono.ttf"]
    if stamp.exists() and stamp.read_text() == mtime and all(f.exists() for f in files):
        return files
    for fam, (wght, wdth) in OG_CUTS.items():
        font = instancer.instantiateVariableFont(TTFont(ARCHIVO_SRC, recalcTimestamp=False), {"wght": wght, "wdth": wdth})
        font["OS/2"].usWeightClass = 400
        font["OS/2"].usWidthClass = 5
        _rename(font, fam)
        font.flavor = None
        font.save(out / f"{fam.replace(' ', '')}.ttf")
    mono = TTFont(MONO_SRC, recalcTimestamp=False)
    _rename(mono, "Lasto Mono")
    mono.flavor = None
    mono.save(out / "LastoMono.ttf")
    stamp.write_text(mtime)
    return files


def advance_widths(path: Path) -> tuple[dict[int, int], int]:
    """Glyph advances by code point, and units per em, for wrapping text in the OG images."""
    font = TTFont(path)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    return {cp: hmtx[g][0] for cp, g in cmap.items()}, font["head"].unitsPerEm
