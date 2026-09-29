"""Social images (Open Graph, 1200x630), the GitHub social preview (1280x640), and PNG/ICO icons.

Rendered with resvg from SVG templates, using static cuts of the site's own fonts and no
system fonts, so Windows and ubuntu produce the same pixels. Images are cached by a hash
of their SVG, so rebuilds only render what changed.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import resvg_py

from sitegen import fonts, paths

MARK_PATH = "M2 6.5H30V14L27 25.5H5L2 14Z"
MARK_DOTS = ((21, 11.75), (21, 19.75))


class TextFit:
    """Wraps text to a width using the real glyph advances of a font cut."""

    def __init__(self, ttf: Path) -> None:
        self.adv, self.upm = fonts.advance_widths(ttf)

    def width(self, text: str, size: float) -> float:
        fallback = self.adv.get(ord("n"), self.upm // 2)
        return sum(self.adv.get(ord(c), fallback) for c in text) * size / self.upm

    def wrap(self, text: str, size: float, max_width: float, max_lines: int) -> list[str]:
        words, lines, line = text.split(), [], ""
        for w in words:
            trial = f"{line} {w}".strip()
            if self.width(trial, size) <= max_width or not line:
                line = trial
            else:
                lines.append(line)
                line = w
        if line:
            lines.append(line)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and self.width(lines[-1] + "…", size) > max_width:
                lines[-1] = lines[-1].rsplit(" ", 1)[0]
            lines[-1] += "…"
        return lines


def _render(svg: str, out: Path, font_files: list[Path]) -> None:
    key = hashlib.sha256(svg.encode()).hexdigest()[:16]
    cached = paths.CACHE / "og" / f"{key}.png"
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        png = resvg_py.svg_to_bytes(
            svg_string=svg,
            skip_system_fonts=True,
            font_files=[str(f) for f in font_files],
            text_rendering="geometric_precision",
        )
        cached.write_bytes(bytes(png))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cached.read_bytes())


def render_card(env, out_dir: Path, name: str, *, width: int, height: int, stamp: str, title: str, lede: str, status: str,
                allowed: frozenset[int], never: frozenset[int], polled_label: str = "POLLED MODE", hashed: bool = True) -> str:
    """Render a card into out_dir. Returns the file name, which carries a hash of the image's
    SVG when hashed=True, so any change to the card gets a new URL and social caches refresh."""
    font_files = fonts.og_fonts()
    head = TextFit(font_files[1])
    text = TextFit(font_files[2])
    scale = width / 1200
    text_w = 700 * scale
    title_size = 58 * scale
    title_lines = head.wrap(title, title_size, text_w, 3)
    lede_size = 27 * scale
    lede_lines = text.wrap(lede, lede_size, text_w, 3)
    svg = env.get_template("og.svg.j2").render(
        width=width, height=height, s=scale, stamp=stamp, title_lines=title_lines, title_size=title_size,
        lede_lines=lede_lines, lede_size=lede_size, status=status, allowed=sorted(allowed), never=sorted(never),
        mark_path=MARK_PATH, mark_dots=MARK_DOTS, polled_label=polled_label,
    )
    filename = f"{name}-{hashlib.sha256(svg.encode()).hexdigest()[:10]}.png" if hashed else f"{name}.png"
    _render(svg, out_dir / filename, font_files)
    return filename


def mark_svg(size: int, background: str | None, ink: str = "#141414", pad: float = 0.16) -> str:
    """The logo mark: the DLC3 face with only pins 6 and 14, the CAN pair."""
    inner = size * (1 - 2 * pad)
    scale = inner / 30.5
    ox = (size - 30.5 * scale) / 2 - 0.75 * scale
    oy = (size - 21.5 * scale) / 2 - 5.25 * scale
    bg = f'<rect width="{size}" height="{size}" fill="{background}"/>' if background else ""
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="2.75" fill="{ink}"/>' for x, y in MARK_DOTS)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">{bg}'
        f'<g transform="translate({ox:.3f} {oy:.3f}) scale({scale:.4f})">'
        f'<path d="{MARK_PATH}" fill="none" stroke="{ink}" stroke-width="2.5"/>{dots}</g></svg>'
    )


def png(svg: str) -> bytes:
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=True))


def ico(images: dict[int, bytes]) -> bytes:
    """An ICO file holding PNG images (supported by every current browser)."""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = b"", b""
    offset = 6 + 16 * len(images)
    for size, data in sorted(images.items()):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return header + entries + blobs


def write_icons(out_dir: Path) -> None:
    ico_sizes = {s: png(mark_svg(s, "#FFFFFF", pad=0.08)) for s in (16, 32, 48)}
    (out_dir / "favicon.ico").write_bytes(ico(ico_sizes))
    (out_dir / "icon-192.png").write_bytes(png(mark_svg(192, "#FFFFFF")))
    (out_dir / "apple-touch-icon.png").write_bytes(png(mark_svg(180, "#FFFFFF")))
